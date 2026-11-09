"""Executes approved cleanup actions against real files.

Two-step by design (see AGENTS.md safety rules): a "propose" call only writes a
row to action_log (dry_run=1, applied=0) — nothing on disk changes. A separate
explicit "apply" call is required to actually touch a file.

Three tiers, from safest to most dangerous:

1. **quarantine** — moves the file into data/quarantine/, mirroring its
   original path. Always recoverable: see the Quarantine tab (restore or
   permanently delete each item individually).
2. **reorganize** — moves a plugin into a `<Vendor>/` subfolder next to where
   it already lives. Not destructive, just relocates.
3. **permanent delete** — only ever available on something ALREADY in
   quarantine (never directly on a live plugin — that's the whole point of
   the two-tier design). Irreversible.

Note on "move to the Windows Recycle Bin": running natively now (see AGENTS.md's
Docker-to-native migration), so this actually *could* use a real Recycle-Bin API
(e.g. `SHFileOperation`/`IFileOperation` via pywin32) — not done, kept as
quarantine-only by choice (recoverable, browsable via the Quarantine tab, and
consistent regardless of whether a given path is one the Windows shell would
even let go to the Recycle Bin, e.g. some Program Files locations). Worth
revisiting now that it's technically possible, see AGENTS.md.
"""

import os
import shutil
import stat
from pathlib import Path

from app.config import QUARANTINE_DIR


class ActionError(Exception):
    pass


def host_to_container(host_path: str) -> Path | None:
    """Named for history (this used to translate a Windows host path into its
    Docker-container-mount equivalent) — now just validates and wraps a real
    path, since there's no container to translate into anymore. Kept as its
    own function/name rather than inlined `Path(host_path)` everywhere, so a
    future re-introduction of sandboxing has one place to change."""
    if not host_path or len(host_path) < 2 or host_path[1] != ":":
        return None
    return Path(host_path)


def container_to_host(container_path: str) -> str:
    """Inverse of host_to_container — same "no real translation anymore, kept
    for the call sites and the name's historical clarity" situation."""
    return container_path


def quarantine_target(host_path: str, quarantine_dir: str = QUARANTINE_DIR) -> Path:
    # Flatten the drive letter / leading colon out of the path so it's a valid
    # relative path under quarantine, e.g. C:\Program Files\... -> C/Program Files/...
    # quarantine_dir defaults to the hardcoded constant but callers normally
    # pass it explicitly, resolved from the "quarantine_dir" setting (see the
    # Configuration tab) — app/web.py only allows changing that setting while
    # quarantine is empty, so this is always consistent with wherever
    # existing quarantined files actually live.
    safe = host_path.replace(":", "").replace("\\", "/")
    return Path(quarantine_dir) / safe


def _host_path_from_quarantine_rel(rel: str) -> str:
    """Reverse of quarantine_target's flattening, e.g. 'C/Program Files/x' -> 'C:\\Program Files\\x'."""
    parts = rel.split("/", 1)
    drive = parts[0] + ":"
    tail = parts[1].replace("/", "\\") if len(parts) > 1 else ""
    return f"{drive}\\{tail}" if tail else f"{drive}\\"


def _clear_readonly(path: Path) -> None:
    """Vendor installers often ship binaries with the Windows read-only attribute
    set, which blocks deletion through Docker's bind-mount layer even with
    directory write access (a real bug this fixes — see AGENTS.md). Best-effort:
    walk everything under path and add the write bit."""
    targets = [path] if path.is_file() else [path, *path.rglob("*")]
    for p in targets:
        try:
            p.chmod(p.stat().st_mode | stat.S_IWRITE)
        except OSError:
            pass  # if this fails too, the move/delete below will raise a clear error


def _safe_move(src: Path, dest: Path, quarantine_dir: str = QUARANTINE_DIR) -> None:
    """Move src to dest, safely across the different filesystem mounts (a plugin
    directory vs ./data) — shutil.move's automatic copy+rmtree fallback can fail
    PARTWAY (copy succeeds, delete of the original fails), leaving a duplicate on
    both sides with no error surfaced. This makes that failure atomic instead:
    either the move fully completes, or the destination copy is rolled back and
    a clear ActionError is raised, so a retry is always safe."""
    try:
        os.rename(src, dest)
        return
    except OSError:
        pass  # cross-device (or dest exists) — fall through to copy+delete

    if src.is_dir():
        shutil.copytree(str(src), str(dest))
    else:
        shutil.copy2(str(src), str(dest))

    _clear_readonly(src)
    try:
        if src.is_dir():
            shutil.rmtree(str(src))
        else:
            src.unlink()
    except OSError as exc:
        # Roll back the copy so a retry doesn't hit "destination already exists"
        # and so we never leave two copies of the same file lying around. Also
        # prune the now-empty parent directories mkdir(parents=True) created.
        if dest.is_dir():
            shutil.rmtree(str(dest), ignore_errors=True)
        else:
            dest.unlink(missing_ok=True)
        parent = dest.parent
        while parent != Path(quarantine_dir) and parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent
        raise ActionError(
            f"Windows denied deleting the original file ({exc.strerror or exc}). This usually means "
            f"the file has the read-only attribute or a restrictive ACL set by its installer. Nothing "
            f"was changed — try clearing 'Read-only' on it in Windows Explorer (Properties → General), "
            f"or allow VEXT to manage the folder when it offers to, then retry."
        ) from exc


def _sibling_data_dir(src: Path) -> Path | None:
    """Some vendors (confirmed real: Blue Cat Audio) ship a companion
    "<Plugin> data" folder next to the plugin file itself (About/Legal/Model
    text, sometimes an uninstaller .exe/.pdf) — not a plugin, but not junk
    either, and needed for the plugin to work correctly. A real bug found via
    the stray-files audit (see AGENTS.md/UNRECOGNIZED_FILES_REPORT.md):
    quarantine/reorganize only moved the plugin file itself, silently
    orphaning this folder at the old location every time. Only handles this
    one confirmed naming convention (exact sibling "<stem> data" folder) —
    Acustica Audio's differently-named *DATA folders (SIENNADATA, BLACKDATA,
    ...) don't follow a derivable-from-the-plugin-name pattern and aren't
    handled here."""
    candidate = src.parent / f"{src.stem} data"
    return candidate if candidate.is_dir() else None


def apply_quarantine(host_path: str, quarantine_dir: str = QUARANTINE_DIR) -> str:
    src = host_to_container(host_path)
    if src is None or not src.exists():
        raise ActionError(f"Source not found (or outside known roots): {host_path}")

    dest = quarantine_target(host_path, quarantine_dir)
    if dest.exists():
        raise ActionError(f"Quarantine destination already exists: {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)

    _safe_move(src, dest, quarantine_dir)

    data_dir = _sibling_data_dir(src)
    if data_dir is not None:
        data_dest = dest.parent / data_dir.name
        _safe_move(data_dir, data_dest, quarantine_dir)

    return str(dest)


def restore_from_quarantine(host_path: str, quarantine_dir: str = QUARANTINE_DIR) -> str:
    """Moves a quarantined item back to its original location."""
    src = quarantine_target(host_path, quarantine_dir)
    if not src.exists():
        raise ActionError(f"Not found in quarantine: {host_path}")

    dest = host_to_container(host_path)
    if dest is None:
        raise ActionError(f"Original location is outside known roots: {host_path}")
    if dest.exists():
        raise ActionError(f"Original location is occupied again (something else is there now): {host_path}")
    dest.parent.mkdir(parents=True, exist_ok=True)

    _safe_move(src, dest, quarantine_dir)
    return str(dest)


def permanent_delete_from_quarantine(host_path: str, quarantine_dir: str = QUARANTINE_DIR) -> None:
    """Irreversible. Only ever called on something already sitting in quarantine —
    see app/web.py's endpoint, which enforces this by only offering the button on
    quarantined items."""
    target = quarantine_target(host_path, quarantine_dir)
    if not target.exists():
        raise ActionError(f"Not found in quarantine: {host_path}")

    _clear_readonly(target)
    if target.is_dir():
        shutil.rmtree(str(target))
    else:
        target.unlink()


def permanent_delete_out_of_scope(host_path: str) -> None:
    """Deletes a file/bundle found outside the known scan directories (see
    app/full_scan.py) directly — no quarantine step, since these were never
    part of the tracked catalog to begin with (no vendor/category resolution,
    no dedup, nothing to "restore" to). Requested specifically for AAX/Pro
    Tools files (not part of this owner's workflow, same stance as the
    existing Flagged-tab AAX handling) but works for any out-of-scope entry —
    the UI's plain "Are you sure?" confirm is the only gate, matching every
    other delete in this app (see AGENTS.md)."""
    target = host_to_container(host_path)
    if target is None or not target.exists():
        raise ActionError(f"Not found on disk: {host_path}")

    _clear_readonly(target)
    if target.is_dir():
        shutil.rmtree(str(target))
    else:
        target.unlink()


def _replace_existing(dest: Path) -> None:
    """Reorganizing can find a file already sitting at the destination — the
    owner's real-world case: a plugin reinstall drops a fresh copy back at
    the plain (un-organized) scan-root path, while an earlier organized copy
    already sits in `<Vendor>\\`. Same vendor folder, same filename, so this
    is always the same plugin superseding its own older organized copy, not
    an unrelated collision — always replaced, unconditionally.

    Real bug found and fixed: the first version of this compared mtimes and
    only replaced when the destination was no newer than the incoming file —
    but VST3/CLAP bundles are directories, and a directory's own mtime only
    updates when an entry is directly added/removed/renamed in it, not when
    a file deep inside (e.g. `Contents\\x86_64-win\\Pumper3.vst3`) is
    overwritten by a reinstall. Confirmed real: a genuinely-stale organized
    copy's top-level folder mtime read as *newer* than the freshly
    reinstalled one, so the mtime guard blocked exactly the case it was
    built for. Dropped entirely — the destination path only collides when
    vendor+filename already match, so there's no unrelated-file case to
    guard against here."""
    _clear_readonly(dest)
    if dest.is_dir():
        shutil.rmtree(str(dest))
    else:
        dest.unlink()


def apply_reorganize(host_path: str, vendor: str) -> str:
    if not vendor or not vendor.strip():
        raise ActionError("Cannot reorganize without a known vendor name")

    src = host_to_container(host_path)
    if src is None or not src.exists():
        raise ActionError(f"Source not found (or outside known roots): {host_path}")

    safe_vendor = "".join(c for c in vendor if c not in '\\/:*?"<>|').strip()
    dest_dir = src.parent / safe_vendor
    dest = dest_dir / src.name
    if dest.exists():
        _replace_existing(dest)
    dest_dir.mkdir(parents=True, exist_ok=True)

    _safe_move(src, dest)

    data_dir = _sibling_data_dir(src)
    if data_dir is not None:
        data_dest = dest_dir / data_dir.name
        if data_dest.exists():
            _replace_existing(data_dest)
        _safe_move(data_dir, data_dest)

    return container_to_host(str(dest))
