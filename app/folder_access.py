"""Write access to a protected plugin folder, asked for one folder at a time.

Windows keeps folders such as Program Files\\Common Files\\VST3 read-only for
programs that aren't running as Administrator: moving, organizing or deleting a
plugin there fails with "Access is denied". Before, the only way past it was to
relaunch the whole app as Administrator (lib.rs relaunch_elevated). The owner's
call (2026-10-19, superseding "don't open these folders up", AGENTS.md): ask per
folder. When an action is refused there, the page offers "Allow VEXT to manage
<folder>"; accepted, Windows asks for permission once and icacls gives the
signed-in user Modify on that folder and everything in it. VEXT never does this
by itself, and only for a scan root (or a folder inside one).

The folder offered is the scan root holding the plugin (the most specific one),
so one grant covers that whole format folder, not just one vendor's subfolder.
Trade-off the owner accepted: any program running as that user can then change
what's in that folder too.
"""

import logging
import os
import secrets
import subprocess
from pathlib import Path

from app import db
from app.config import DB_PATH

log = logging.getLogger(__name__)

_NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW
_DENIED = {5, 1314}  # ERROR_ACCESS_DENIED, ERROR_PRIVILEGE_NOT_HELD


class NeedsAccess(Exception):
    """An action was refused by the folder's permissions: `folder` is the one to offer."""

    def __init__(self, message: str, folder: str):
        super().__init__(message)
        self.message, self.folder = message, folder


def _roots() -> list[Path]:
    with db.connect(DB_PATH) as conn:
        rows = db.list_scan_roots(conn)
    return [Path(r["host_path"]) for r in rows if r["host_path"]]


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def access_root(path: str) -> str:
    """The folder to offer for `path`: the most specific scan root holding it, else its own folder."""
    p = Path(path)
    holding = [r for r in _roots() if _inside(p, r)]
    if holding:
        return str(max(holding, key=lambda r: len(str(r))))
    return str(p.parent if p.suffix else p)


def _denied(exc: BaseException | None) -> bool:
    """Whether `exc` (or what caused it) is Windows refusing access."""
    seen = 0
    while exc is not None and seen < 5:
        if isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in _DENIED:
            return True
        exc, seen = exc.__cause__ or exc.__context__, seen + 1
    return False


def raise_if_denied(exc: BaseException, path: str) -> None:
    """Turns a refused action on `path` into NeedsAccess (the page then offers the grant); else returns."""
    if _denied(exc):
        folder = access_root(path)
        raise NeedsAccess(f"Windows refused the change in {folder} (Access is denied): VEXT isn't allowed to "
                          "change files there yet. Nothing was changed.", folder)


def writable(folder: str) -> bool:
    """Whether this process can create (and delete) a file in `folder` now."""
    probe = Path(folder) / f".vext-access-{secrets.token_hex(4)}"
    try:
        probe.write_bytes(b"")
        probe.unlink()
        return True
    except OSError:
        return False


def _user_sid() -> str:
    """The signed-in user's SID (S-1-5-21-...): icacls takes it whatever the language of Windows."""
    out = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True,
                         creationflags=_NO_WINDOW, timeout=15, check=True).stdout.strip()
    sid = out.rsplit(",", 1)[-1].strip().strip('"')
    if not sid.startswith("S-1-"):
        raise OSError(f"couldn't read the user's SID ({out!r})")
    return sid


def grant(folder: str) -> dict:
    """Gives the signed-in user Modify on `folder` (and what's in it), elevated: Windows asks first.
    {ok, folder, message}. Only scan roots and folders inside them."""
    target = Path(folder)
    if not target.is_dir():
        raise ValueError(f"Not a folder: {folder}")
    if not any(_inside(target, r) for r in _roots()):
        raise ValueError("VEXT only asks for access to its plugin folders (Configuration › Scan paths).")
    # Asked for after a refusal, so it runs even when the folder itself takes new files: the refusal may
    # have come from a file inside whose installer set its own permissions (/T reaches it).
    sid = _user_sid()
    # (OI)(CI)M: Modify, inherited by the files and subfolders; /T also reaches the ones an installer
    # cut off from inheriting, /C goes on past a file it can't change.
    args = f'"{target}" /grant *{sid}:(OI)(CI)M /T /C /Q'.replace("'", "''")
    command = ("try { $p = Start-Process icacls.exe -Verb RunAs -Wait -PassThru -WindowStyle Hidden "
               f"-ArgumentList '{args}'; exit $p.ExitCode }} catch {{ exit 1223 }}")
    try:
        done = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
                              creationflags=_NO_WINDOW, capture_output=True, timeout=600)
        code = done.returncode
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "folder": str(target), "message": f"Couldn't ask Windows: {e}"}
    if code == 1223:
        return {"ok": False, "folder": str(target), "message": "Windows didn't give permission, so nothing was changed."}
    if not writable(str(target)):
        log.warning("icacls grant on %s ended with exit %s but the folder is still not writable", target, code)
        return {"ok": False, "folder": str(target),
                "message": f"Windows didn't let VEXT change {target} (icacls exit {code})."}
    log.info("Write access to %s given to the signed-in user (icacls exit %s)", target, code)
    return {"ok": True, "folder": str(target),
            "message": f"VEXT can now move and delete plugins in {target} without running as Administrator."}
