"""Opt-in full C:\\ drive scan for plugins living outside the known ScanRoot
directories (app/config.py:ROOTS) — for when an installer drops a plugin
somewhere unexpected and the owner didn't check the install path. Deliberately
separate from the normal fast targeted rescan (app/scanner.py): a real
full-drive walk through Docker's Windows bind-mount is far slower than the
already-slow targeted scan (see AGENTS.md), so this only ever runs when
explicitly requested via the "Include full C: scan" checkbox, never
automatically as part of a normal rescan.

Findings go into their own `out_of_scope_plugins` table, never the main
`plugins` catalog — these aren't enriched (no vendor/category resolution
runs on them), aren't part of dedup/cleanup, and the only actions available
are "open in Explorer to go fix it yourself" or "ignore" (see the
/api/out-of-scope endpoints in app/web.py). Promoting one into the real
catalog means moving the actual file into a known ScanRoot directory and
rescanning normally — this module doesn't do that itself.
"""

import os
from pathlib import Path

from app.config import FULL_C_ROOT

# Recognized plugin bundle/flat-file extensions. .dll is deliberately NOT
# unconditionally included — a blanket .dll match across an entire Windows C:
# drive would be both catastrophically slow (hundreds of thousands of
# unrelated system/app DLLs) and overwhelmingly noisy. A VST2 .dll is only
# picked up when it sits in a folder whose name suggests one (see
# _looks_like_vst_folder) — a deliberate precision/recall tradeoff, documented
# rather than silently guessed at.
_PLUGIN_EXTENSIONS = {".vst3", ".clap", ".aaxplugin"}

# Large/irrelevant subtrees skipped outright — without this, a real Windows C:
# drive (hundreds of thousands of files under Windows/, AppData/, node_modules/,
# browser profiles, etc.) would make this scan impractically slow for close to
# zero benefit. Matched against the directory's own name, case-insensitive.
_SKIP_DIR_NAMES = {
    "windows", "$recycle.bin", "system volume information", "$windows.~bt",
    "$windows.~ws", "recovery", "programdata", "node_modules", ".git",
    "appdata", "windowsapps", "$sysreset",
}


def _looks_like_vst_folder(dir_name: str) -> bool:
    # Confirmed real false positive from a live run: "VSTO" (Microsoft's Visual
    # Studio Tools for Office runtime, C:\Program Files\Common Files\microsoft
    # shared\VSTO\vstoee.dll) contains "vst" as a bare substring. Excluding it
    # by name rather than trying to build a general word-boundary regex — folder
    # names like "VstPlugins" have no separator between "Vst" and "Plugins"
    # either, so a strict \bvst\b boundary would break real matches too.
    name = dir_name.lower()
    return "vst" in name and "vsto" not in name


def _dir_size(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def scan_full_c(known_root_paths: set[str] = frozenset(), ignored_folder_paths: set[str] = frozenset()) -> list[dict]:
    """Returns [{"host_path", "name", "format_guess", "size_bytes"}, ...] for
    everything found outside the known scan roots. Does not touch the
    database — callers (app/scan_service.py) decide how to persist/reconcile
    it.

    known_root_paths: every currently-enabled scan root (built-in + custom —
    see app/db.py:scan_roots_as_objects), lowercased, so this never descends
    into (and re-flags) a folder that's already scanned normally.
    ignored_folder_paths: folder-level ignore rules from the Configuration
    tab's "Consolidate" feature (app/db.py:ignored_scan_folders), lowercased
    — a whole subtree the owner has decided isn't worth seeing again."""
    root = Path(FULL_C_ROOT)
    if not root.is_dir():
        return []

    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        # os.walk on a native Windows root already yields real Windows paths —
        # no container/host translation needed anymore (see AGENTS.md's
        # Docker-to-native migration).
        host_dir = str(dirpath)

        keep = []
        for d in dirnames:
            child_host = host_dir + "\\" + d
            child_lower = child_host.lower()
            if d.lower() in _SKIP_DIR_NAMES or child_lower in known_root_paths:
                continue
            if any(child_lower == f or child_lower.startswith(f + "\\") for f in ignored_folder_paths):
                continue
            if Path(d).suffix.lower() in _PLUGIN_EXTENSIONS:
                found.append({
                    "host_path": child_host,
                    "name": Path(d).stem,
                    "format_guess": Path(d).suffix.lstrip(".").upper(),
                    "size_bytes": _dir_size(Path(dirpath) / d),
                })
                continue  # a bundle's insides aren't separately interesting
            keep.append(d)
        dirnames[:] = keep

        for f in filenames:
            ext = Path(f).suffix.lower()
            host_path = host_dir + "\\" + f
            if ext in _PLUGIN_EXTENSIONS:
                try:
                    size = (Path(dirpath) / f).stat().st_size
                except OSError:
                    size = 0
                found.append({"host_path": host_path, "name": Path(f).stem, "format_guess": ext.lstrip(".").upper(), "size_bytes": size})
            elif ext == ".dll" and _looks_like_vst_folder(Path(dirpath).name):
                try:
                    size = (Path(dirpath) / f).stat().st_size
                except OSError:
                    size = 0
                found.append({"host_path": host_path, "name": Path(f).stem, "format_guess": "VST2?", "size_bytes": size})

    return found
