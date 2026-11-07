"""Watches the enabled plugin folders and rescans when an install, update or uninstall
changes them (Configuration > General, "Watch plugin folders": watch_plugin_folders).

Windows change notifications (FindFirstChangeNotificationW over the whole subtree) only
say that *something* changed under a folder. Installers write in bursts and plugins
sometimes write into their own folder, so a change only starts a timer. Once the folders
have been quiet for SETTLE_S, the plugin entries are listed (names and modification
times, no metadata; scanner.plugin_entries) and compared with the last listing. A scan
runs only when the difference is something the catalog doesn't already know:

- a new entry that isn't a cataloged or ignored plugin (an install),
- a cataloged plugin that disappeared and isn't in quarantine (an uninstall),
- an entry whose binary's modification time changed (an update).

VEXT's own moves (organize, quarantine, restore) change the catalog first, so they don't
cause a scan. The scan is the Scan button's pipeline (scan_service.run_scan): read-only,
and proposals stay proposals. A folder that can't be watched (a network drive, more than
64 folders) is still covered by a listing every POLL_S.
"""

import ctypes
import logging
import os
import threading
import time
from ctypes import wintypes
from datetime import datetime, timezone

from app import db, metadata, scan_service, scanner
from app.config import DB_PATH

log = logging.getLogger("vext.plugin_watch")

SETTING_KEY = "watch_plugin_folders"
SETTLE_S = 20.0         # quiet time after the last change before listing (installers write in bursts)
ROOTS_REFRESH_S = 60.0  # re-read the scan folders (one created by an installer, a setting changed)
POLL_S = 300.0          # a listing even without notifications: folders Windows couldn't watch
_WAIT_MS = 1000
_MAX_HANDLES = 64       # MAXIMUM_WAIT_OBJECTS

_FILE_NOTIFY = 0x1 | 0x2 | 0x10  # FILE_NAME | DIR_NAME | LAST_WRITE
_INVALID_HANDLE = wintypes.HANDLE(-1).value
_WAIT_TIMEOUT = 0x102

_state_lock = threading.Lock()
_state = {"seq": 0, "last": None, "scanning": False}
_wake = threading.Event()
_stop = threading.Event()
_thread: threading.Thread | None = None


def _kernel32():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.FindFirstChangeNotificationW.argtypes = [wintypes.LPCWSTR, wintypes.BOOL, wintypes.DWORD]
    k.FindFirstChangeNotificationW.restype = wintypes.HANDLE
    k.FindNextChangeNotification.argtypes = [wintypes.HANDLE]
    k.FindNextChangeNotification.restype = wintypes.BOOL
    k.FindCloseChangeNotification.argtypes = [wintypes.HANDLE]
    k.FindCloseChangeNotification.restype = wintypes.BOOL
    k.WaitForMultipleObjects.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE), wintypes.BOOL,
                                         wintypes.DWORD]
    k.WaitForMultipleObjects.restype = wintypes.DWORD
    return k


def status() -> dict:
    """{enabled, seq, last, scanning}: seq grows with each scan the watcher ran, last is
    that scan's {at, new, removed, found}. The frontend polls this to refresh its lists."""
    with db.connect(DB_PATH) as conn:
        enabled = _enabled(conn)
    with _state_lock:
        return {"enabled": enabled, **_state}


def reload() -> None:
    """Settings or scan folders changed: re-read them now instead of within a minute."""
    _wake.set()


def start() -> None:
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_run, name="vext-plugin-watch", daemon=True)
    _thread.start()


def stop() -> None:
    _stop.set()
    _wake.set()


def _enabled(conn) -> bool:
    return db.get_setting(conn, SETTING_KEY, "1") == "1"


def _stamp(entry) -> int:
    """What an update changes: a bundle's binary (a plugin writing its own files into its
    bundle doesn't count; 0 until an install has written the binary), else the entry."""
    if entry.is_dir() and entry.suffix.lower() in (".vst3", ".aaxplugin"):
        binary = metadata.find_bundle_binary(entry, entry.name)
        return binary.stat().st_mtime_ns if binary else 0
    return entry.stat().st_mtime_ns


def _listing(roots) -> dict[str, dict[str, int]]:
    """{root folder: {lowercased host_path: _stamp}} for every plugin entry."""
    out = {}
    for root in roots:
        entries = {}
        try:
            for host_path, entry in scanner.plugin_entries(root):
                try:
                    entries[host_path.lower()] = _stamp(entry)
                except OSError:
                    continue
        except OSError:
            pass  # a folder that can't be read now is compared again on the next listing
        out[root.host_path.lower()] = entries
    return out


def needs_scan(old: dict, new: dict, known: set, ignored: set, quarantined: set) -> bool:
    """Whether the change between two listings is one the catalog doesn't know yet
    (see the module docstring). Folders only in one listing (a scan path just added or
    removed in Configuration) aren't compared: that isn't an install."""
    for folder in old.keys() & new.keys():
        before, after = old[folder], new[folder]
        for path in after.keys() - before.keys():
            if path not in known and path not in ignored:
                return True
        for path in before.keys() - after.keys():
            if path in known and path not in quarantined:
                return True
        for path in before.keys() & after.keys():
            if before[path] != after[path]:
                return True
    return False


def _catalog_paths(conn) -> tuple[set, set, set]:
    return (db.known_plugin_host_paths(conn), db.ignored_plugin_host_paths(conn),
            {p.lower() for p in db.quarantined_host_paths(conn)})


def _auto_scan() -> None:
    with _state_lock:
        _state["scanning"] = True
    try:
        summary = scan_service.run_scan(log=lambda msg: None)
    except Exception:  # a failed scan must not stop the watcher
        log.exception("Plugin folders changed, but the automatic scan failed")
        return
    finally:
        with _state_lock:
            _state["scanning"] = False
    with _state_lock:
        _state["seq"] += 1
        _state["last"] = {"at": datetime.now(timezone.utc).isoformat(), "new": summary.get("new", 0),
                          "removed": summary.get("removed", 0), "found": summary.get("found", 0)}
    log.info("Plugin folders changed: scanned automatically, %s new, %s removed",
             summary.get("new", 0), summary.get("removed", 0))


class _Watches:
    """One change-notification handle per existing scan folder (up to 64)."""

    def __init__(self, k32):
        self._k = k32
        self.handles: list[int] = []
        self.paths: list[str] = []

    def set(self, folders: list[str]) -> bool:
        """Watches exactly these folders. False when they were already the ones watched."""
        if folders == self.paths:
            return False
        self.close()
        for folder in folders[:_MAX_HANDLES]:
            h = self._k.FindFirstChangeNotificationW(folder, True, _FILE_NOTIFY)
            if h and h != _INVALID_HANDLE:
                self.handles.append(h)
        self.paths = folders
        return True

    def wait(self) -> bool:
        """True when a folder changed within _WAIT_MS (and re-arms its notification)."""
        if not self.handles:
            _wake.wait(_WAIT_MS / 1000)
            return False
        arr = (wintypes.HANDLE * len(self.handles))(*self.handles)
        rc = self._k.WaitForMultipleObjects(len(self.handles), arr, False, _WAIT_MS)
        if rc < len(self.handles):
            self._k.FindNextChangeNotification(self.handles[rc])
            return True
        if rc != _WAIT_TIMEOUT:
            _wake.wait(_WAIT_MS / 1000)  # WAIT_FAILED (a handle went bad): the next refresh reopens them
        return False

    def close(self) -> None:
        for h in self.handles:
            self._k.FindCloseChangeNotification(h)
        self.handles, self.paths = [], []


def _run() -> None:
    try:
        k32 = _kernel32()
    except (OSError, AttributeError):
        log.warning("Plugin folder watching isn't available on this system")
        return
    watches = _Watches(k32)
    roots, listing = [], None
    enabled = False
    refreshed = polled = 0.0
    changed_at = None
    try:
        while not _stop.is_set():
            now = time.monotonic()
            if _wake.is_set() or now - refreshed >= ROOTS_REFRESH_S:
                _wake.clear()
                refreshed = now
                with db.connect(DB_PATH) as conn:
                    enabled = _enabled(conn)
                    roots = db.scan_roots_as_objects(conn) if enabled else []
                if not enabled:
                    watches.close()
                    listing, changed_at = None, None
                else:
                    folders_changed = watches.set(sorted({r.host_path for r in roots if os.path.isdir(r.host_path)}))
                    if listing is None:
                        listing = _listing(roots)
                        polled = now
                    else:
                        # A scan path added in Configuration starts from what's in it now.
                        listing.update(_listing([r for r in roots if r.host_path.lower() not in listing]))
                        if folders_changed:
                            changed_at = changed_at or now  # a plugin folder appeared (first install of a format) or went
            if not enabled:
                _wake.wait(_WAIT_MS / 1000)
                continue

            if watches.wait():
                changed_at = time.monotonic()
            now = time.monotonic()
            if changed_at is None and now - polled >= POLL_S:
                changed_at = now - SETTLE_S
            if changed_at is None or now - changed_at < SETTLE_S:
                continue
            if scan_service.scan_running():
                continue  # it reads the folders itself; compare once it's done

            new_listing = _listing(roots)
            polled = time.monotonic()
            scanned_since = scan_service.last_scan_finished >= changed_at
            changed_at = None
            with db.connect(DB_PATH) as conn:
                known, ignored, quarantined = _catalog_paths(conn)
            if not scanned_since and needs_scan(listing, new_listing, known, ignored, quarantined):
                _auto_scan()
                new_listing = _listing(roots)
            listing = new_listing
    finally:
        watches.close()
