"""Reorganizes FL Studio's own plugin *browse* tree — "Plugin database/Effects"
and "Plugin database/Generators" (siblings of "Installed", and what FL's main
Browser panel actually shows when picking a plugin to insert) — directly via
file operations, using VEXT's own (corrected) vendor/category data as
the source of truth.

(Illustrative paths below are written with forward slashes purely to keep
this docstring free of backslash-escape issues in a normal, non-raw Python
string — every real path on disk still uses Windows backslashes as usual.)

WHY NOT FL STUDIO'S OWN PLUGIN MANAGER UI
    An earlier approach drove FL's Plugin Manager UI (Category field + Apply
    changes) to do this. Confirmed real and fatal: that only ever *copies* a
    plugin's .fst into the browse tree — it never clears the entry back out
    of Installed/Effects/New or Installed/Generators/New. The next time the
    owner runs FL's own "Find installed plugins", those stale New entries are
    still sitting there and get re-processed, undoing the categorization.
    Confirmed by hand: plugins already filed into Effects/Acon Digital by
    that approach were STILL present, unchanged, in Installed/Effects/New
    afterward.

    This module sidesteps FL's UI entirely — plain file operations driven by
    VEXT's own database, so there's no FL-internal state to go stale.

THE "New" FOLDERS
    Installed/Effects/New and Installed/Generators/New are flat: one .fst +
    one .nfo per plugin, same filename stem, created fresh by FL's own "Find
    installed plugins" scan. The .nfo is plain key=value text (see
    app/fl_database.py's module docstring for the full field reference —
    ps_name, ps_file_filename_N, ps_file_type_N, the %FLPath% template for
    FL-NATIVE, WaveShell wrapper detection for Waves). Only the .fst ever
    needs to persist in the browse tree — confirmed by hand: the files the
    old UI-driven approach actually placed into Effects/<Vendor> were .fst
    only, never .nfo.

PIPELINE (see propose() / apply())
    1. Parse every .nfo in Installed/Effects/New and Installed/Generators/New.
    2. Resolve each entry's real file path the same way app/fl_database.py's
       build_index()/lookup() already do (bundle-root truncation, %FLPath%
       resolution for FL-NATIVE, WaveShell detection for Waves) and match it
       against VEXT's own plugins.host_path.
    3. Matched, non-Waves: sync plugins.name from the .nfo's ps_name if it
       disagrees — same correction the normal scan's FL cross-reference
       already does (app/scan_service.py), and respects the same
       name_source='manual' protection (never overwrites a manual rename).
    4. Waves is special-cased: WaveShell-hosted .nfo entries have no 1:1
       VEXT row (see AGENTS.md's VEXT-vs-FL-count section — FL
       enumerates per-channel-variant names, VEXT catalogs one row per
       physical .bundle file), so they're never "matched" to a specific
       plugin row. Vendor-mode still files them under "Waves Audio" (every
       WaveShell entry's vendor is unambiguous); category-mode has no
       reliable per-variant source and skips them (they fall through to the
       section root as "unmatched").
    5. Mode is exclusive, "vendor" or "category" — never both at once (the
       owner's call: filing the same plugin under its vendor *and* every one
       of its categories simultaneously made the browse tree confusing to
       reason about). vendor -> Section/Vendor (one copy); category ->
       Section/Category for *every* tag the plugin has (one copy each —
       deliberate: the owner wants the same plugin reachable from each of its
       categories). A matched plugin with nothing to file it under in the
       chosen mode (e.g. category mode, but the plugin has zero tags) is
       root-bound, same as a genuinely unmatched entry.
    6. Anything root-bound — unmatched, or matched with nothing to file it
       under — goes to the plain section root instead of any subfolder,
       UNLESS the owner picked a manual destination for it from the UI's
       combobox (an `overrides` mapping, keyed by the .nfo's own filename
       stem). An override on a *matched* plugin is also persisted back to
       VEXT's own vendor/category data (so the next sync already
       knows it); an override on something with no VEXT row at all
       only ever affects where this one .fst gets filed.
    7. New's .nfo (FL's scan record) is never copied — once read, it's
       deleted from New regardless of match outcome. The browse tree's own
       .nfo is a different file: one "Bitmap=<stem>.png" line that gives the
       .fst its thumbnail (see "thumbnails" below), written by this module
       when it places a VEXT picture.
    8. Once New is empty, the folder itself is deleted.
    9. Reconcile pass (a complete run, scope="full", or a dangerous wipe):
       for every plugin already filed somewhere in the browse tree whose
       VEXT vendor/category has changed since, move it to where it belongs
       now — see _reconcile_section() and the "not wiped by default" note
       below. scope="new" files New and leaves everything else alone.
    10. Pictures: VEXT's own picture of each filed plugin becomes its FL
       thumbnail (hidden <stem>.png, one global size from THUMB_SIZES: 256 /
       128 / 64 px tall, plus the hidden .nfo pointing at it) — see _Pictures.
    11. Manual links (table fl_links, see db.fl_links()): an FL name that VEXT
       can't match is linked to a VEXT plugin by the owner. A link wins over a
       name match and is the fallback when the path and filename find nothing.

inventory() is the read-only browse-tree check (every .fst with its .nfo and
.png, orphans, and VEXT's plugins vs FL's own database); repair() writes the
missing .nfo / hidden attributes it can without choosing a picture.
fix_pictures() gives picked .fst files VEXT's picture at a chosen size;
link_candidates() / set_link() find and store the links.

NOT WIPED BY DEFAULT
    apply()'s `dangerous_wipe` (the UI's "dangerous" checkbox, unchecked by
    default) controls whether Effects\\/Generators\\ get wiped and rebuilt
    from scratch, or left alone and only reconciled in place. Real bug found
    and fixed: the original version of this *always* wiped first — but only
    ever rebuilt from whatever happened to be sitting in New\\ that run,
    typically a small subset (FL doesn't re-list plugins it already knows
    about on every "Find installed plugins" pass). Owner's real hit: an
    earlier run correctly organized ~816/114 plugins; a second run's New\\
    only had ~5 entries, so the wipe destroyed the other ~925 plugins'
    organization with nothing left to rebuild it from. The safe default now
    never wipes at all — it just processes New\\ and reconciles drift (step
    9 above). `dangerous_wipe=True` still snapshots each section's existing
    .fst files *before* wiping, so even a full wipe can restore everything
    through the same reconcile pass afterward — a wipe should never lose
    track of a plugin VEXT still knows about, requested or not.

SAFETY
    Same propose-then-apply model as every other action in this app (see
    AGENTS.md's Safety rules). propose() is fully read-only. apply() never
    touches the existing Effects/Generators contents unless `dangerous_wipe`
    is explicitly requested — see "NOT WIPED BY DEFAULT" above. When it is,
    `save_backup=True` (the UI's "Save backup" checkbox, default on when
    dangerous mode is on) backs up each section's existing contents (renamed
    with a timestamp, not deleted outright — this app's established "always
    leave a way back" principle, e.g. quarantine instead of permanent delete
    elsewhere) before rebuilding it; `save_backup=False` clears the existing
    contents in place instead. Either way, a rename/delete that Windows
    transiently denies (some other handle briefly open on the folder —
    Explorer, an antivirus scan, a search indexer, FL Studio's own folder
    watch for its Browser panel) is retried with backoff before giving up
    with a clear error (see _retry_on_lock()) — real WinError 5 seen on this
    exact operation once, cause never pinned down, hence a generic retry
    rather than guessing which one it was. apply() re-derives its own plan
    fresh rather than trusting one computed earlier (the filesystem could
    have changed in between).

NEVER TOUCHES Installed/Effects/<format> / Installed/Generators/<format>
(the VST/VST3/CLAP/Fruity subfolders — FL's own verified-plugin bookkeeping)
or anything else under Installed besides New.
"""

import ctypes
import difflib
import os
import re
import shutil
import sqlite3
import stat
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from app import db as dbmod
from app import fl_database
from app.config import FL_PLUGIN_DB_ROOT, IMAGES_DIR
from app.fl_database import _FL_PATH, _WAVESHELL_RE, _bundle_root, _parent_folder
from app.scanner import normalize_name

_INVALID_FOLDER_CHARS = '\\/:*?"<>|'
_WAVES_VENDOR = "Waves Audio"
_SECTIONS = ("Effects", "Generators")
_SCOPES = ("new", "full")
# Installed/<section>/<format> folders, in the order a duplicate stem is taken
# from for a complete reorganization: CLAP > VST3 > VST2 (this app's format
# priority), then FL's own; New last (its entries mirror these folders).
_FORMAT_FOLDER_PRIORITY = ("clap", "vst3", "vst", "fruity")


def _safe_folder_name(name: str) -> str:
    cleaned = "".join(c for c in name if c not in _INVALID_FOLDER_CHARS).strip()
    return cleaned or "Unknown"


# ---------- thumbnails: <stem>.png + <stem>.nfo ("Bitmap=<stem>.png"), both hidden ----------
# FL's browse tree shows a picture for a plugin when a hidden <stem>.nfo next
# to its .fst holds one line, "Bitmap=<stem>.png", and that hidden .png is
# there too (confirmed on this machine: Effects/accusonus/ERA 6 De-Breath.nfo
# is 26 bytes, exactly "Bitmap=ERA 6 De-Breath.png", no newline). VEXT writes
# its own pictures the same way, 256 px tall (the owner's size), and marks
# them in a PNG text chunk so a later run can tell its own thumbnails (safe to
# refresh) from any other (kept unless the owner asks to replace them).

# The three sizes FL's browser is standardized on (height in px -> name). The
# width follows each picture's aspect ratio. One size is chosen globally in the UI.
THUMB_SIZES = {256: "Large", 128: "Medium", 64: "Compact"}
THUMB_HEIGHT = 256  # default height
_VEXT_MARK_KEY = "VEXT-Picture"   # value: "<source mtime>@<height>" (a legacy mark has no "@")
_HIDDEN = stat.FILE_ATTRIBUTE_HIDDEN if hasattr(stat, "FILE_ATTRIBUTE_HIDDEN") else 0x2
_READONLY = 0x1
_NORMAL = 0x80


def _attrs(path: Path) -> int | None:
    try:
        return os.stat(path).st_file_attributes  # Windows only
    except (OSError, AttributeError):
        return None


def _is_hidden(path: Path) -> bool:
    a = _attrs(path)
    return bool(a and a & _HIDDEN)


def _set_hidden(path: Path, hidden: bool) -> None:
    a = _attrs(path)
    if a is None:
        return
    new = (a | _HIDDEN) if hidden else (a & ~(_HIDDEN | _READONLY))
    ctypes.windll.kernel32.SetFileAttributesW(str(path), new or _NORMAL)


def _write_hidden(path: Path, data: bytes) -> None:
    """Windows refuses to overwrite a hidden file (CreateFile with CREATE_ALWAYS
    fails with access denied unless the caller asks for the same attribute), so
    the attribute comes off first and goes back on after."""
    if path.exists():
        _set_hidden(path, False)
    path.write_bytes(data)
    _set_hidden(path, True)


def _read_nfo_fields(nfo_path: Path) -> list[tuple[str, str]]:
    try:
        text = nfo_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            out.append((key.strip(), value.strip()))
    return out


def _bitmap_of(nfo_path: Path) -> str | None:
    return next((v for k, v in _read_nfo_fields(nfo_path) if k.lower() == "bitmap" and v), None)


def _write_bitmap_nfo(nfo_path: Path, png_name: str) -> None:
    """Points the .nfo at png_name, keeping any other line it already has
    (a plugin .nfo in the browse tree is normally only the Bitmap line)."""
    fields = [(k, v) for k, v in _read_nfo_fields(nfo_path) if k.lower() != "bitmap"] if nfo_path.exists() else []
    fields.append(("Bitmap", png_name))
    _write_hidden(nfo_path, "\r\n".join(f"{k}={v}" for k, v in fields).encode("utf-8"))


def _png_info(png_path: Path) -> tuple[tuple[int, int] | None, str | None]:
    """(size, mark): mark is the source stamp VEXT wrote into one of its own
    thumbnails, None for any other picture (FL's, the owner's). (None, None)
    for an unreadable file."""
    try:
        from PIL import Image

        with Image.open(png_path) as im:
            return im.size, (getattr(im, "text", None) or im.info).get(_VEXT_MARK_KEY)
    except (OSError, ValueError):  # unreadable or not an image
        return None, None


def _mark_for(stamp: str, height: int) -> str:
    """The VEXT mark a thumbnail carries: the source's mtime and the height it was made at."""
    return f"{stamp}@{height}"


def _thumbnail_bytes(src: str, stamp: str, height: int) -> bytes:
    """src resized to `height` px tall (one of THUMB_SIZES), width following the aspect ratio."""
    import io

    from PIL import Image, PngImagePlugin

    with Image.open(src) as im:
        im.load()
        w, h = im.size
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGBA")
        width = max(1, round(w * height / h))
        small = im.resize((width, height), Image.Resampling.LANCZOS)
    info = PngImagePlugin.PngInfo()
    info.add_text(_VEXT_MARK_KEY, _mark_for(stamp, height))
    buf = io.BytesIO()
    small.save(buf, "PNG", pnginfo=info, optimize=True)
    return buf.getvalue()


def picture_for_host_path(host_path: str) -> str | None:
    """VEXT's own saved picture for a plugin (same key as
    plugin_host.image_path(), not imported from there to keep this module free
    of the plugin host), or None when it has none."""
    import hashlib

    key = hashlib.sha1(host_path.lower().encode("utf-8")).hexdigest()[:16]
    path = IMAGES_DIR / f"{key}.png"
    return str(path) if path.is_file() else None


_WAVES_VARIANT_RE = re.compile(
    r"\s+(?:mono|stereo|mono/stereo|m/s|ms|m2s|s2m|lcr|5\.0|5\.1|6\.0|6\.1|7\.0|7\.1|7\.0\.2|7\.1\.2|"
    r"surround|quad|ambisonics)$", re.IGNORECASE)


def _waves_base_name(fl_name: str) -> str:
    """"C4 Stereo" -> "C4": FL lists one entry per Waves channel variant; VEXT
    has one row per .bundle."""
    name = fl_name.strip()
    while True:
        stripped = _WAVES_VARIANT_RE.sub("", name)
        if stripped == name:
            return name
        name = stripped


def _waves_index(conn) -> list[tuple[str, int]]:
    """(lowercased name, plugin_id) of every WAVES row, longest name first, so
    "L3-LL Ultra" is tried before "L3". Names shared by two rows (the same
    plugin in two Plug-Ins V<N> folders) keep the first; for a picture that's
    the same plugin either way."""
    conn.row_factory = sqlite3.Row
    seen: dict[str, int] = {}
    for r in conn.execute("SELECT id, name FROM plugins WHERE format='WAVES' ORDER BY host_path DESC"):
        seen.setdefault(r["name"].lower(), r["id"])
    return sorted(seen.items(), key=lambda kv: -len(kv[0]))


def _match_waves(fl_name: str, waves: list[tuple[str, int]]) -> int | None:
    """A VEXT WAVES row for an FL Waves entry: the base name equals a row's
    name, or starts with it followed by a space or a digit ("Doubler2 Stereo"
    -> "Doubler", "PAZ Analyzer Stereo" -> "PAZ"). Only ever used to pick a
    picture or to name the plugin in the check list, never to move a file."""
    base = _waves_base_name(fl_name).lower()
    for name, plugin_id in waves:
        if base == name:
            return plugin_id
    for name, plugin_id in waves:
        if base.startswith(name) and len(base) > len(name) and (base[len(name)] == " " or base[len(name)].isdigit()):
            return plugin_id
    return None


class _Pictures:
    """One run's thumbnail writing. `place(fst, picture)` puts VEXT's picture
    beside a filed .fst. A thumbnail VEXT made earlier is refreshed when the
    picture changed; any other one is kept unless `replace`, in which case it's
    first copied to Plugin database/Thumbnails.vext-backup-<UTC date>/ (same
    relative path), outside the browse tree so FL never shows it."""

    def __init__(self, root: Path, enabled: bool, replace: bool, height: int = THUMB_HEIGHT):
        self.root, self.enabled, self.replace, self.height = root, enabled, replace, height
        self.counts = {"added": 0, "updated": 0, "kept": 0, "current": 0, "missing": 0}
        self.backup_dir: Path | None = None
        self._bytes: dict[str, bytes] = {}
        self._done: set[str] = set()

    def place(self, fst: Path, picture: str | None) -> None:
        if not self.enabled or str(fst).lower() in self._done:
            return
        self._done.add(str(fst).lower())
        png, nfo = fst.with_suffix(".png"), fst.with_suffix(".nfo")
        if not picture:
            self.counts["missing"] += 1
            return
        stamp = str(int(os.path.getmtime(picture)))
        updating = False
        if png.exists():
            size, mark = _png_info(png)
            # A thumbnail made at the wanted height from this picture is current. A legacy
            # mark (mtime only, from before the sizes) counts too, when its height matches.
            if mark == _mark_for(stamp, self.height) or (mark == stamp and size and size[1] == self.height):
                if _bitmap_of(nfo) != png.name:
                    _write_bitmap_nfo(nfo, png.name)
                self.counts["current"] += 1
                return
            if mark is None:
                if not self.replace:
                    self.counts["kept"] += 1
                    return
                self._back_up(png)
            updating = True
        data = self._bytes.get(picture)
        if data is None:
            data = self._bytes[picture] = _thumbnail_bytes(picture, stamp, self.height)
        _write_hidden(png, data)
        _write_bitmap_nfo(nfo, png.name)
        self.counts["updated" if updating else "added"] += 1

    def _back_up(self, png: Path) -> None:
        # One folder per day (not per run: fixing row by row made one folder per click). A thumbnail
        # already backed up today keeps its first copy, the one FL or the owner made.
        if self.backup_dir is None:
            day = datetime.now(timezone.utc).strftime("%Y%m%d")
            self.backup_dir = self.root / f"Thumbnails.vext-backup-{day}"
        dest = self.backup_dir / png.relative_to(self.root)
        if dest.exists():
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(png), str(dest))


def _parse_nfo(nfo_path: Path) -> dict | None:
    """Mirrors app/fl_database.py:build_index()'s per-.nfo parsing (not
    imported from there directly — that module iterates the *whole* tree for
    a different purpose; this reads one file at a time for this module's own
    New-folder-only walk). Returns None for an unreadable/nameless .nfo."""
    try:
        text = nfo_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None

    fields = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            fields[key.strip()] = value.strip()

    name = fields.get("ps_name")
    if not name:
        return None

    num_files_raw = fields.get("ps_files")
    num_files = int(num_files_raw) if num_files_raw and num_files_raw.isdigit() else 1

    is_waves = False
    lookup_key = None
    type_code = None
    for i in range(num_files):
        filename = fields.get(f"ps_file_filename_{i}")
        if not filename:
            continue
        if _WAVESHELL_RE.search(filename):
            is_waves = True
            continue
        type_code_raw = fields.get(f"ps_file_type_{i}")
        if type_code_raw and type_code_raw.isdigit():
            type_code = int(type_code_raw)
        if "%FLPath%" in filename:
            if _FL_PATH is not None:
                resolved = filename.replace("%FLPath%", _FL_PATH)
                lookup_key = _parent_folder(resolved).lower()
        else:
            lookup_key = _bundle_root(filename).lower()
        if lookup_key:
            break  # first non-WaveShell file index is enough

    return {"name": name, "lookup_key": lookup_key, "type_code": type_code, "is_waves": is_waves}


def _installed_root(conn) -> Path:
    return Path(dbmod.get_setting(conn, "fl_plugin_db_root", FL_PLUGIN_DB_ROOT))


def _plugin_db_root(conn) -> Path:
    """The browsable tree's root ("Plugin database", parent of "Installed") —
    Effects/<Vendor-or-Category> destinations are built under here."""
    return _installed_root(conn).parent


def _new_folder(conn, section: str) -> Path:
    """New entries live under Installed/<section>/New, NOT under the plain
    browse-tree root - a real bug once here: this used _plugin_db_root()
    (Installed's *parent*) instead of _installed_root(), so every propose()
    call silently found 0 entries (looking in Plugin database/Effects/New,
    which doesn't exist, instead of Plugin database/Installed/Effects/New).
    Caught by hand before ever touching a real file: propose() returned
    empty for both sections on a database known to have hundreds of real
    New\\ entries."""
    return _installed_root(conn) / section / "New"


def _build_path_index(conn) -> tuple[dict[str, int], dict[str, list[int]]]:
    """by_path: exact-path match, the primary and most specific signal.
    by_filename: fallback for when VEXT's own "Reorganize" action has
    since moved a plugin into a vendor subfolder — FL's .nfo still has the
    pre-move path until FL itself rescans, but the file's own name doesn't
    change. Same real-world gap app/fl_database.py's lookup() already
    handles for the name/type cross-reference; found again here by hand
    ("Waves StudioRack for OBS" — reorganized into "VstPlugins/Waves
    Audio/" after FL's last scan, so an exact-path match alone reported it
    as unmatched even though it's still a real, catalogued plugin)."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT id, host_path FROM plugins").fetchall()
    by_path = {r["host_path"].lower(): r["id"] for r in rows}
    by_filename: dict[str, list[int]] = {}
    for r in rows:
        base = Path(r["host_path"]).name.lower()
        by_filename.setdefault(base, []).append(r["id"])
    return by_path, by_filename


def _compute_destinations(row, plugin_id: int, mode: str, section: str, cat_map: dict) -> list[str]:
    """Shared by New\\-entry planning and the existing-tree reconcile pass —
    both need the exact same answer to "where should this plugin's .fst live
    right now, given today's VEXT data," so this is the one place
    that answer is computed."""
    destinations = []
    if mode == "vendor" and row["vendor"]:
        destinations.append(f"{section}/{_safe_folder_name(row['vendor'])}")
    elif mode == "category":
        for cat_name, _slug, _source in cat_map.get(plugin_id, []):
            destinations.append(f"{section}/{_safe_folder_name(cat_name)}")
    return destinations


def _source_nfos(conn, section: str, scope: str) -> tuple[list[tuple[Path, bool]], list[Path]]:
    """The .nfo files a run files from: (nfo_path, from_new) pairs, plus the New
    entries a complete run only consumes (a format folder already has the same
    stem, see _FORMAT_FOLDER_PRIORITY).

    scope "new": Installed/<section>/New only, what FL's last "Find installed
    plugins" added. scope "full": every plugin FL has detected, Installed/
    <section>/<every subfolder>, one entry per stem. The format folders are
    only ever read, never changed."""
    new_dir = _new_folder(conn, section)
    new_nfos = sorted(new_dir.glob("*.nfo")) if new_dir.is_dir() else []
    if scope != "full":
        return [(p, True) for p in new_nfos], []
    installed = _installed_root(conn) / section
    folders = [d for d in installed.iterdir() if d.is_dir() and d.name.lower() != "new"] if installed.is_dir() else []
    rank = {n: i for i, n in enumerate(_FORMAT_FOLDER_PRIORITY)}
    folders.sort(key=lambda d: (rank.get(d.name.lower(), len(rank)), d.name.lower()))
    chosen: dict[str, tuple[Path, bool]] = {}
    for folder in folders:
        for nfo in sorted(folder.rglob("*.nfo")):
            chosen.setdefault(nfo.stem.lower(), (nfo, False))
    consume_only = []
    for nfo in new_nfos:
        if nfo.stem.lower() in chosen:
            consume_only.append(nfo)
        else:
            chosen[nfo.stem.lower()] = (nfo, True)
    return sorted(chosen.values(), key=lambda t: t[0].stem.lower()), consume_only


def _plan_section(conn, section: str, mode: str, by_path: dict[str, int], by_filename: dict[str, list[int]],
                   cat_map: dict, overrides: dict[str, str], scope: str = "new",
                   waves: list[tuple[str, int]] | None = None) -> tuple[list[dict], list[Path]]:
    """Read-only. Returns (entries, consume_only): consume_only are New .nfo
    files a complete run deletes without filing (see _source_nfos()). One entry
    per source .nfo (New only, or every detected plugin, per `scope`), each with:
    key (str, stable within this section — the .nfo's own filename stem, used
    to address this entry from an `overrides` mapping), nfo_path, fst_path,
    ps_name, plugin_id (None if unmatched/Waves), matched (bool), destinations
    (list[str], relative to the Plugin database root — empty means "section
    root"), from_override (bool — destinations came from the owner's manual
    choice, not natural vendor/category data), renamed_from (str|None),
    reason (str|None, why it's root-bound).

    `mode` is "vendor" or "category" (mutually exclusive — the owner picks
    one per run, never both: filing the same plugin under its vendor *and*
    every one of its categories at once made the browse tree confusing to
    reason about). `overrides` maps a root-bound entry's `key` to an owner-
    picked vendor name (vendor mode) or category name (category mode), from
    the combobox the UI offers for anything that would otherwise land at the
    plain section root.

    Every entry also has from_new (bool — it sits in New and is consumed once
    filed) and picture (VEXT's own picture for it, or None)."""
    sources, consume_only = _source_nfos(conn, section, scope)
    entries = []
    waves = waves if waves is not None else _waves_index(conn)
    links = dbmod.fl_links(conn)

    def _root_bound(key, fst_path, nfo_path, ps_name, plugin_id, matched, reason):
        override_value = overrides.get(key)
        if override_value:
            return {
                "key": key, "nfo_path": nfo_path, "fst_path": fst_path, "ps_name": ps_name,
                "plugin_id": plugin_id, "matched": matched,
                "destinations": [f"{section}/{_safe_folder_name(override_value)}"],
                "from_override": True, "override_value": override_value,
                "renamed_from": None, "reason": None,
            }
        return {
            "key": key, "nfo_path": nfo_path, "fst_path": fst_path, "ps_name": ps_name,
            "plugin_id": plugin_id, "matched": matched, "destinations": [],
            "from_override": False, "override_value": None,
            "renamed_from": None, "reason": reason,
        }

    def _picture(plugin_id):
        row = dbmod.get_plugin(conn, plugin_id) if plugin_id else None
        return picture_for_host_path(row["host_path"]) if row else None

    for nfo_path, from_new in sources:
        entry = _plan_entry(conn, section, mode, nfo_path, by_path, by_filename, cat_map, waves, _root_bound,
                            links)
        entry["from_new"] = from_new
        entry["picture"] = _picture(entry.pop("picture_plugin_id", None) or entry["plugin_id"])
        entries.append(entry)
    return entries, consume_only


def _plan_entry(conn, section, mode, nfo_path, by_path, by_filename, cat_map, waves, _root_bound,
                links: dict[str, int]) -> dict:
    """One source .nfo's plan entry (see _plan_section())."""
    fst_path = nfo_path.with_suffix(".fst")
    key = nfo_path.stem
    parsed = _parse_nfo(nfo_path)

    if parsed is None:
        return _root_bound(key, fst_path, nfo_path, nfo_path.stem, None, False,
                           "unreadable .nfo (no ps_name)")

    # A manual link (see db.fl_links) names the VEXT plugin itself. It's the fallback
    # when the path and filename find nothing, and it's what matches a WaveShell entry.
    link_id = links.get(parsed["name"].lower()) or links.get(key.lower())

    if parsed["is_waves"] and link_id is None:
        if mode == "vendor":
            entry = {
                "key": key, "nfo_path": nfo_path, "fst_path": fst_path, "ps_name": parsed["name"],
                "plugin_id": None, "matched": False,
                "destinations": [f"{section}/{_safe_folder_name(_WAVES_VENDOR)}"],
                "from_override": False, "override_value": None,
                "renamed_from": None, "reason": None,
            }
        else:
            entry = _root_bound(key, fst_path, nfo_path, parsed["name"], None, False,
                                "Waves plugin — no per-variant category source; pick one manually "
                                "or switch to vendor mode")
        # Never "matched" (see the module docstring), but the bundle's
        # picture is still the right one for every channel variant.
        entry["picture_plugin_id"] = _match_waves(parsed["name"], waves)
        return entry

    plugin_id = by_path.get(parsed["lookup_key"]) if parsed["lookup_key"] else None
    if plugin_id is None and parsed["lookup_key"]:
        # Exact path didn't match — VEXT's own "Reorganize" may
        # have moved this file into a vendor subfolder since FL's .nfo
        # was last written. Fall back to filename alone; only trust it
        # when there's exactly one candidate (a shared filename across
        # two genuinely different catalogued plugins is possible, if
        # rare, and guessing wrong here would mis-file a plugin).
        base = parsed["lookup_key"].rsplit("\\", 1)[-1] if "\\" in parsed["lookup_key"] else parsed["lookup_key"]
        candidates = by_filename.get(base, [])
        if len(candidates) == 1:
            plugin_id = candidates[0]
    if plugin_id is None:
        plugin_id = link_id
    if plugin_id is None:
        return _root_bound(key, fst_path, nfo_path, parsed["name"], None, False, "no matching VEXT plugin")

    row = dbmod.get_plugin(conn, plugin_id)
    destinations = _compute_destinations(row, plugin_id, mode, section, cat_map)

    renamed_from = None
    if parsed["name"] and parsed["name"] != row["name"] and row["name_source"] != "manual":
        renamed_from = row["name"]

    if not destinations:
        entry = _root_bound(key, fst_path, nfo_path, parsed["name"], plugin_id, True,
                             f"matched, but no {mode} to file it under")
        entry["renamed_from"] = renamed_from
        return entry

    return {
        "key": key, "nfo_path": nfo_path, "fst_path": fst_path, "ps_name": parsed["name"],
        "plugin_id": plugin_id, "matched": True, "destinations": destinations,
        "from_override": False, "override_value": None,
        "renamed_from": renamed_from, "reason": None,
    }


def propose(conn, mode: str, overrides: dict[str, dict[str, str]] | None = None, scope: str = "new") -> dict:
    """Read-only plan. `scope` is "new" (only what FL's last scan put in New)
    or "full" (every plugin FL has detected, see _source_nfos()). Returns
    {section: {"entries": [...], "consume_only": [...]}}.

    `mode` is "vendor" or "category" (exclusive — see
    _plan_section()'s docstring for why). `overrides` is keyed by section
    first (`{"Effects": {key: value, ...}, "Generators": {...}}`) — entry
    keys are only unique *within* a section (the .nfo's own filename stem),
    not globally, so a flat cross-section dict could misapply an override
    meant for one section's plugin to a same-named entry in the other.
    See _plan_section()'s docstring for the entry shape. Paths are Path
    objects; callers that need JSON (web.py) convert them to strings."""
    if mode not in ("vendor", "category"):
        raise ValueError(f"mode must be 'vendor' or 'category', got {mode!r}")
    if scope not in _SCOPES:
        raise ValueError(f"scope must be 'new' or 'full', got {scope!r}")
    overrides = overrides or {}
    by_path, by_filename = _build_path_index(conn)
    cat_map = dbmod.category_tags_by_plugin(conn)
    waves = _waves_index(conn)
    out = {}
    for section in _SECTIONS:
        entries, consume_only = _plan_section(conn, section, mode, by_path, by_filename, cat_map,
                                              overrides.get(section, {}), scope, waves)
        out[section] = {"entries": entries, "consume_only": consume_only}
    return out


def list_options(conn) -> dict:
    """Vendor/category names for the UI's root-bound combobox — the same
    reference lists the Reference data tab itself manages, so a name picked
    here is guaranteed to already exist as a real vendor/category elsewhere
    in the app."""
    return {
        "vendor": [r["name"] for r in dbmod.list_vendors_reference(conn)],
        "category": [r["name"] for r in dbmod.list_categories_reference(conn)],
    }


def _build_name_index(conn) -> dict[str, int]:
    """Lowercased plugin name -> plugin_id, for matching an *already-filed*
    .fst purely by its filename (there's no .nfo sitting next to it anymore
    to resolve a real path from — only New\\ entries have that). A name used
    by more than one VEXT plugin is excluded entirely rather than
    guessed at, same principle as export_vendor_data.py's ambiguous-name
    skip from the earlier (now-superseded) pywinauto approach — a wrong
    guess here would move a file to the wrong plugin's folder. A manual
    link (db.fl_links) is the owner's own answer, so it overrides even that."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT id, name FROM plugins").fetchall()
    counts: dict[str, int] = {}
    for r in rows:
        key = r["name"].lower()
        counts[key] = counts.get(key, 0) + 1
    index = {r["name"].lower(): r["id"] for r in rows if counts[r["name"].lower()] == 1}
    index.update(dbmod.fl_links(conn))
    return index


def _scan_existing_fst(section_dir: Path) -> dict[str, list[Path]]:
    """Lowercased .fst stem -> every path it's currently found at, relative
    to section_dir (there can legitimately be more than one in category
    mode — the same plugin filed under each of its categories). Covers the
    whole section — the plain root and every subfolder — via rglob."""
    result: dict[str, list[Path]] = {}
    if not section_dir.is_dir():
        return result
    for fst_path in sorted(section_dir.rglob("*.fst")):
        rel = fst_path.relative_to(section_dir)
        result.setdefault(fst_path.stem.lower(), []).append(rel)
    return result


_THUMBNAIL_DIR_NAME = "Thumbnail"


def _thumbnail_staging_dir(root: Path) -> Path:
    """Plugin database/Thumbnail — sibling of Effects/Generators/Installed,
    exactly as the owner asked for: a visible, inspectable backup location
    (not just an in-memory mapping) that genuinely exists on disk for as
    long as a sync is in progress, in case anything goes wrong partway."""
    return root / _THUMBNAIL_DIR_NAME


def _stage_thumbnails(section_dir: Path, staging_dir: Path) -> dict[str, str]:
    """Copies every .png found anywhere under section_dir into staging_dir
    (flat, by filename — originals are left in place, this is a safety
    backup, not a move) before anything else touches the section this run.
    Owner's own words: these are "very difficult to create" — confirmed by
    hand, FL's own "Find installed plugins" never regenerates them
    (Installed\\...\\New\\ has zero .png files, ever, unlike .fst/.nfo which
    it recreates on every scan) — so a plugin thumbnail lost or orphaned by
    a move is gone for good, not just inconvenient to redo. Returns
    stem.lower() -> staged filename (read back from staging_dir)."""
    staging_dir.mkdir(parents=True, exist_ok=True)
    staged: dict[str, str] = {}
    if not section_dir.is_dir():
        return staged
    for png_path in sorted(section_dir.rglob("*.png")):
        dest = staging_dir / png_path.name
        if not dest.exists():
            shutil.copy2(str(png_path), str(dest))
        staged[png_path.stem.lower()] = png_path.name
    return staged


def _reconcile_section(conn, section: str, mode: str, section_dir: Path, root: Path,
                        name_index: dict[str, int], cat_map: dict,
                        existing_by_name: dict[str, list[Path]], source_base: Path | None,
                        force_copy: bool, staged_thumbnails: dict[str, str], staging_dir: Path,
                        dest_by_stem: dict[str, list[str]] | None = None) -> int:
    """Fixes drift between what's actually sitting in the browse tree and
    what VEXT's *current* vendor/category data says it should be —
    the owner's explicit ask: a plugin reassigned to a different vendor (or
    whose categories changed) after it was already filed should move, not
    sit stale in its old folder forever. Also the only thing standing
    between a wipe (dangerous mode) and real data loss: `source_base` is
    where the *pre-wipe* files actually are now (the backup folder, if one
    was made) — `force_copy=True` in that case means "everything needs
    recopying, the destination was just wiped," skipping the normal
    already-correct-leave-it-alone check, which has nothing left to compare
    against post-wipe anyway. Only ever acts on a plugin whose (unique)
    name is in `name_index` — anything else (a manually-placed file, a
    leftover from the original installer-default categorization, an
    ambiguous name) is left exactly where it is, not this pass's business.

    `dest_by_stem` (a complete run): stem -> destinations already decided
    from FL's own .nfo for that plugin (path-matched, so it also covers a
    plugin whose VEXT name differs from its .fst name); it wins over the
    name match. A thumbnail travels with its .fst together with the .nfo that
    points FL at it. Returns how many plugins were actually moved/restored."""
    if source_base is None:
        return 0  # dangerous wipe, no backup — nothing left to reconcile from, by design
    moved = 0
    dest_by_stem = dest_by_stem or {}
    for name_lower, rel_paths in existing_by_name.items():
        dest_rels = dest_by_stem.get(name_lower)
        if dest_rels is None:
            plugin_id = name_index.get(name_lower)
            if plugin_id is None:
                continue
            row = dbmod.get_plugin(conn, plugin_id)
            dest_rels = _compute_destinations(row, plugin_id, mode, section, cat_map)
        dest_dirs = {root / d for d in dest_rels} if dest_rels else {section_dir}

        if not force_copy:
            current_dirs = {(section_dir / rel).parent for rel in rel_paths}
            if current_dirs == dest_dirs:
                continue

        source = source_base / rel_paths[0]
        if not source.exists():
            continue
        thumb_name = staged_thumbnails.get(name_lower)
        did_copy = False
        for dest_dir in dest_dirs:
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest_file = dest_dir / source.name
            if not dest_file.exists():
                shutil.copy2(str(source), str(dest_file))
                did_copy = True
            if thumb_name:
                dest_thumb = dest_dir / thumb_name
                if not dest_thumb.exists():
                    shutil.copy2(str(staging_dir / thumb_name), str(dest_thumb))
                    _set_hidden(dest_thumb, True)
                    _write_bitmap_nfo(dest_file.with_suffix(".nfo"), thumb_name)

        if not force_copy:
            for rel in rel_paths:
                p = section_dir / rel
                if p.parent not in dest_dirs and p.exists():
                    p.unlink()
                    p.with_suffix(".nfo").unlink(missing_ok=True)
                    if thumb_name:
                        old_thumb = p.parent / thumb_name
                        if old_thumb.exists():
                            old_thumb.unlink()

        if did_copy or not force_copy:
            moved += 1
    return moved


_LOCK_RETRY_DELAYS = (0.3, 0.6, 1.2, 2.5)  # seconds — total ~4.6s before giving up


def _retry_on_lock(fn, what: str):
    """Windows transiently denies a rename/delete while something else
    (Explorer showing that exact folder, an antivirus scan, a search
    indexer, FL Studio's own folder watch for its Browser panel — any of
    these, never confirmed which on this machine) briefly holds a handle
    open on it. Real error seen: WinError 5 "Access denied" renaming
    Effects\\ to its backup name, moments after the owner had deleted an
    older backup folder by hand in Explorer — plausible trigger, never
    proven, hence a generic retry rather than special-casing one cause.
    Retries with backoff; re-raises the last error with a clearer message
    if every attempt fails."""
    last_exc = None
    for delay in (0,) + _LOCK_RETRY_DELAYS:
        if delay:
            time.sleep(delay)
        try:
            return fn()
        except PermissionError as exc:
            last_exc = exc
    raise OSError(
        f"{what} — Windows kept denying access after {len(_LOCK_RETRY_DELAYS) + 1} tries over "
        f"~{sum(_LOCK_RETRY_DELAYS):.0f}s. Likely something else has a handle on this folder open right "
        f"now (an Explorer window showing it, an antivirus scan, a search indexer, FL Studio itself). "
        f"Close anything that might be browsing it and try again — or turn off \"Save backup\" to skip "
        f"this step entirely."
    ) from last_exc


def _backup_and_recreate(section_dir: Path, save_backup: bool) -> Path | None:
    if not section_dir.exists():
        section_dir.mkdir(parents=True, exist_ok=True)
        return None

    if not save_backup:
        # Clear contents in place rather than renaming/recreating the
        # directory itself — if a watcher/handle is specifically on this
        # exact directory (not a rename target that doesn't exist yet),
        # leaving the directory itself alone and only removing its children
        # sidesteps that case too, not just the "no backup wanted" request.
        for child in section_dir.iterdir():
            def _remove(c=child):
                if c.is_dir():
                    shutil.rmtree(str(c))
                else:
                    c.unlink()
            try:
                _retry_on_lock(_remove, f"Couldn't remove old {child.name}")
            except OSError:
                pass  # best-effort clear — one stuck leftover isn't worth failing the whole sync over
        return None

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = section_dir.parent / f"{section_dir.name}.vext-backup-{ts}"
    _retry_on_lock(lambda: section_dir.rename(backup_dir), f"Couldn't back up {section_dir.name}\\")
    section_dir.mkdir(parents=True, exist_ok=True)
    return backup_dir


def apply(conn, mode: str, overrides: dict[str, dict[str, str]] | None = None,
          sections: list[str] | None = None, dangerous_wipe: bool = False,
          save_backup: bool = True, scope: str = "new", pictures: bool = True,
          replace_thumbnails: bool = False, thumb_height: int = THUMB_HEIGHT) -> dict:
    """Executes a freshly-recomputed plan (never trusts one built earlier —
    the filesystem could have changed since). `overrides` is per-section,
    same shape as propose()'s. `sections` restricts the run to just
    "Effects", just "Generators", or both (default) — the owner's explicit
    ask: Effects and Generators are separate runs, not one inseparable
    operation.

    SAFE BY DEFAULT (dangerous_wipe=False): never wipes anything. Processes
    New\\ as usual (files/removes those entries), then *reconciles* — for
    every plugin already filed somewhere in the current browse tree whose
    VEXT vendor/category has since changed, moves it to where it
    belongs now instead of leaving it stale (see _reconcile_section()).
    Nothing that's already correctly filed is touched, and anything this
    pass doesn't recognize (not a VEXT plugin, or an ambiguous name)
    is left exactly where it is.

    Real bug found and fixed: the original version of this *always* wiped
    the whole section before rebuilding, but only ever rebuilt from what
    happened to be sitting in New\\ that run — which is typically a small
    subset (plugins FL just (re)detected), not everything previously filed.
    Owner's exact real-world hit: ~816/114 plugins correctly organized by an
    earlier run, then a second run's New\\ only had ~5 entries (most were
    already filed and FL's "Find installed plugins" doesn't re-list
    everything it already knows), so the wipe step destroyed the other
    ~925 plugins' organization with nothing to rebuild it from. Fixed by
    making the wipe itself opt-in (`dangerous_wipe`, the UI's "dangerous"
    checkbox, unchecked by default) and, even when it *is* requested,
    snapshotting the section's existing .fst files before wiping so the
    reconcile pass can restore everything afterward — a wipe, requested or
    not, should never lose track of a plugin VEXT still knows about.

    `save_backup` only matters when `dangerous_wipe` is True (nothing is
    ever wiped otherwise) — see _backup_and_recreate()'s docstring.

    Every plugin .png thumbnail found anywhere in a section being processed
    is staged into a shared "Thumbnail" folder (sibling of Effects/
    Generators/Installed) before anything else touches that section, and
    carried along wherever its plugin's .fst ends up — whether that's a
    reconcile move (safe mode) or a full restore from a pre-wipe snapshot
    (dangerous mode). Owner's own words: these are "very difficult to
    create" — confirmed by hand, unlike .fst/.nfo, FL's own "Find installed
    plugins" never regenerates them, so one orphaned or lost by a move is
    gone for good. See _stage_thumbnails()/_reconcile_section(). The staging
    folder is removed again once every requested section is done.

    SCOPE (the owner's choice per run): "new" files only what FL's last "Find
    installed plugins" put in New and leaves everything already filed alone
    (no reconcile pass, unless a dangerous wipe needs one to restore).
    "full" is a complete reorganization from every plugin FL has detected
    (Installed/<section>/<every format folder> + New, read-only except New,
    which is consumed): each is filed where today's data says, an .fst
    already filed elsewhere moves there, and one already in place isn't
    copied over (it may hold a state the owner saved). A detected plugin with
    nowhere to go is only added at the root when the tree doesn't have it
    anywhere yet, so a hand-filed one stays where the owner put it.

    PICTURES (`pictures`, default on): every .fst this run files gets VEXT's
    own picture next to it as FL's thumbnail (see _Pictures; a complete run
    covers every recognized .fst in the section, filed this run or not).
    `replace_thumbnails` also replaces thumbnails VEXT didn't make, after
    backing them up. `thumb_height` is the one global size (a key of
    THUMB_SIZES: 256 / 128 / 64).

    See the module docstring for the full pipeline. Returns a per-section
    summary."""
    if scope not in _SCOPES:
        raise ValueError(f"scope must be 'new' or 'full', got {scope!r}")
    if thumb_height not in THUMB_SIZES:
        raise ValueError(f"thumb_height must be one of {sorted(THUMB_SIZES)}, got {thumb_height!r}")
    overrides = overrides or {}
    sections = [s for s in (sections or list(_SECTIONS)) if s in _SECTIONS] or list(_SECTIONS)
    by_path, by_filename = _build_path_index(conn)
    cat_map = dbmod.category_tags_by_plugin(conn)
    waves = _waves_index(conn)
    plan = {s: _plan_section(conn, s, mode, by_path, by_filename, cat_map, overrides.get(s, {}), scope, waves)
            for s in sections}
    name_index = _build_name_index(conn)
    root = _plugin_db_root(conn)
    staging_dir = _thumbnail_staging_dir(root)
    staged_thumbnails: dict[str, str] = {}
    pics = _Pictures(root, pictures, replace_thumbnails, thumb_height)
    summary = {}

    for section in sections:
        entries, consume_only = plan[section]
        section_dir = root / section
        pics_before = dict(pics.counts)

        # Snapshot BEFORE any wipe — the only thing that lets a dangerous
        # wipe restore plugins that weren't sitting in New\ this run (see
        # the docstring above). Harmless, cheap work when dangerous_wipe is
        # False too (its result is reused directly as the reconcile pass's
        # "what's here right now" input, so it's never wasted). Thumbnails
        # are staged the same way, for the same reason — before anything in
        # this section can possibly move or get wiped.
        existing_by_name = _scan_existing_fst(section_dir)
        staged_thumbnails.update(_stage_thumbnails(section_dir, staging_dir))

        if dangerous_wipe:
            backup = _backup_and_recreate(section_dir, save_backup)
            # Post-wipe, "where things are now" is nowhere — the reconcile
            # pass below must copy from the backup (if one was made) rather
            # than compare current-vs-desired locations, since "current"
            # no longer exists.
            source_base = backup if backup else None
            force_copy = True
        else:
            section_dir.mkdir(parents=True, exist_ok=True)
            backup = None
            source_base = section_dir
            force_copy = False
        present = {} if dangerous_wipe else existing_by_name

        applied = 0
        unmatched = 0
        renamed = 0
        assigned = 0
        skipped = 0
        dest_by_stem: dict[str, list[str]] = {}
        for entry in entries:
            if not entry["fst_path"].exists():
                if entry["from_new"]:
                    entry["nfo_path"].unlink(missing_ok=True)
                continue

            stem = entry["fst_path"].stem.lower()
            if not entry["from_new"] and not entry["destinations"] and stem in present:
                skipped += 1  # nowhere to file it, and the tree already has it somewhere: leave it
                continue
            if entry["destinations"]:
                dest_by_stem[stem] = entry["destinations"]

            dest_rel_list = entry["destinations"] or [section]
            for dest_rel in dest_rel_list:
                dest_dir = root / dest_rel
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest_file = dest_dir / entry["fst_path"].name
                # New is FL's fresh scan: its .fst replaces an older copy. A
                # format folder's is only copied where there's none yet.
                if entry["from_new"] or not dest_file.exists():
                    shutil.copy2(str(entry["fst_path"]), str(dest_file))
                pics.place(dest_file, entry["picture"])

            # "filed" (did this land in a real vendor/category subfolder, as
            # opposed to the plain section root) is deliberately NOT the same
            # thing as "matched" (linked to a specific VEXT plugin_id)
            # — a Waves entry is never matched to a plugin_id (see the module
            # docstring) but is still properly filed under "Waves Audio" in
            # vendor mode. Counting by "matched" alone would wrongly report
            # every correctly-filed Waves plugin as "unmatched".
            if entry["destinations"]:
                applied += 1
            else:
                unmatched += 1

            # An owner-picked override for a *matched* plugin (one with a
            # real plugin_id) is persisted back to VEXT's own data,
            # not just used as a one-off filing destination — so the next
            # sync already knows this plugin's vendor/category and doesn't
            # need the same manual pick again. An override on an unmatched
            # entry (no plugin_id — not in VEXT's catalog at all) has
            # nothing to persist to; it only ever affects where this one
            # .fst gets filed.
            if entry["from_override"] and entry["matched"] and entry["plugin_id"]:
                if mode == "vendor":
                    dbmod.update_plugin_manual(conn, entry["plugin_id"], {"vendor": entry["override_value"]})
                else:
                    # set_plugin_categories_manual takes slugs, but the
                    # combobox (and list_options()) deals in human-readable
                    # category names — look up the matching slug.
                    slug_row = conn.execute(
                        "SELECT slug FROM categories WHERE name=?", (entry["override_value"],)
                    ).fetchone()
                    if slug_row:
                        dbmod.set_plugin_categories_manual(conn, entry["plugin_id"], [slug_row[0]])
                assigned += 1

            if entry["matched"] and entry["renamed_from"]:
                conn.execute(
                    "UPDATE plugins SET name=?, normalized_name=? WHERE id=? AND name_source != 'manual'",
                    (entry["ps_name"], normalize_name(entry["ps_name"]), entry["plugin_id"]),
                )
                renamed += 1

            if entry["from_new"]:
                entry["nfo_path"].unlink(missing_ok=True)
                entry["fst_path"].unlink(missing_ok=True)

        for nfo in consume_only:  # a complete run already filed these from a format folder
            nfo.unlink(missing_ok=True)
            nfo.with_suffix(".fst").unlink(missing_ok=True)

        moved = 0
        if scope == "full" or dangerous_wipe:
            moved = _reconcile_section(conn, section, mode, section_dir, root, name_index, cat_map,
                                        existing_by_name, source_base, force_copy,
                                        staged_thumbnails, staging_dir,
                                        dest_by_stem if scope == "full" else None)
        if scope == "full":
            _picture_pass(conn, section_dir, name_index, waves, pics)

        new_dir = _new_folder(conn, section)
        new_removed = False
        if new_dir.is_dir() and not any(new_dir.iterdir()):
            new_dir.rmdir()
            new_removed = True

        summary[section] = {
            "applied": applied, "unmatched": unmatched, "renamed": renamed, "assigned": assigned,
            "moved": moved, "skipped": skipped, "consumed": len(consume_only),
            "backup": str(backup) if backup else None, "new_folder_removed": new_removed,
            "pictures": {k: pics.counts[k] - pics_before[k] for k in pics.counts} if pictures else None,
            "thumbnail_backup": str(pics.backup_dir) if pics.backup_dir else None,
        }

    # The staging folder's job is done once every requested section has been
    # fully processed — every thumbnail that could be restored/carried along
    # already was, as each section ran. Best-effort cleanup (via the same
    # retry-on-lock helper everything else in this module uses); leaving a
    # stray Thumbnail\ folder behind on a rare failure is harmless (it's
    # never read by anything else), so this never raises.
    if staging_dir.is_dir():
        try:
            _retry_on_lock(lambda: shutil.rmtree(str(staging_dir)), "Couldn't remove the Thumbnail staging folder")
        except OSError:
            pass

    return summary


def _plugin_for_stem(conn, stem: str, name_index: dict[str, int], waves: list[tuple[str, int]]):
    """The VEXT row an .fst in the browse tree stands for: a manual link first,
    then its unique name, or a Waves channel variant's bundle. None if none."""
    plugin_id = (dbmod.fl_links(conn).get(stem.lower()) or name_index.get(stem.lower())
                 or _match_waves(stem, waves))
    return dbmod.get_plugin(conn, plugin_id) if plugin_id else None


def _picture_pass(conn, section_dir: Path, name_index: dict[str, int], waves: list[tuple[str, int]],
                  pics: _Pictures) -> None:
    """A complete run's last step: every recognized .fst in the section gets
    VEXT's picture, filed this run or not."""
    if not pics.enabled or not section_dir.is_dir():
        return
    for fst in sorted(section_dir.rglob("*.fst")):
        row = _plugin_for_stem(conn, fst.stem, name_index, waves)
        if row is not None:
            pics.place(fst, picture_for_host_path(row["host_path"]))


# ---------- the browse-tree check: .fst / .nfo / .png, and VEXT vs FL ----------

_FL_FORMATS = ("VST2", "VST3", "CLAP", "FL-NATIVE", "WAVES")  # what FL Studio can load (not AAX)


def _entry_check(conn, section: str, section_dir: Path, fst: Path, name_index, waves) -> dict:
    """One browse-tree .fst with its .nfo and picture, and what's wrong with them."""
    nfo, png = fst.with_suffix(".nfo"), fst.with_suffix(".png")
    issues = []
    bitmap = _bitmap_of(nfo) if nfo.exists() else None
    picture = png if png.exists() else None
    if not nfo.exists():
        issues.append("no .nfo")
    else:
        if not _is_hidden(nfo):
            issues.append(".nfo isn't hidden")
        if not bitmap:
            if png.exists():
                issues.append(".nfo has no Bitmap line")
        elif not (fst.parent / bitmap).is_file():
            issues.append(f".nfo points to {bitmap}, which isn't there")
        else:
            picture = fst.parent / bitmap
    size = None
    source = None
    if picture is None:
        issues.append("no picture")
    else:
        if not _is_hidden(picture):
            issues.append(".png isn't hidden")
        size, mark = _png_info(picture)
        if size is None:
            issues.append("picture can't be read")
        source = "vext" if mark else "other"
    row = _plugin_for_stem(conn, fst.stem, name_index, waves)
    vext_picture = picture_for_host_path(row["host_path"]) if row else None
    # Any of the three standard heights is fine, whatever the global choice. Only a
    # height that is none of them is a problem: Resize (the same image) or Fix
    # picture (VEXT's) makes it standard.
    can_standardize = bool(size and size[1] not in THUMB_SIZES)
    if can_standardize:
        issues.append(f"picture is {size[1]} px tall: not a standard size (Resize it)")
    linked = row is not None and fst.stem.lower() in dbmod.fl_links(conn)
    # An unrecognized entry the owner chose to ignore isn't flagged any more (Unignore brings it back).
    ignored = row is None and fst.stem.lower() in dbmod.fl_ignored(conn)
    if ignored:
        issues = []
    folder = fst.parent.relative_to(section_dir).as_posix()
    return {
        "section": section, "folder": "" if folder == "." else folder, "name": fst.stem,
        "key": fst.stem.lower(), "fst": str(fst), "nfo": nfo.exists(),
        "picture": str(picture) if picture else None,
        "size": list(size) if size else None, "size_name": THUMB_SIZES.get(size[1]) if size else None,
        "source": source, "can_standardize": can_standardize, "linked": linked, "ignored": ignored,
        "plugin_id": row["id"] if row else None, "plugin_name": row["name"] if row else None,
        "vendor": row["vendor"] if row else None, "vext_picture": bool(vext_picture),
        "issues": issues,
    }


def inventory(conn) -> dict:
    """Read-only. Every .fst in the browse tree (Effects/Generators, any depth)
    with its .nfo/.png check, the .png/.nfo files with no .fst beside them,
    and VEXT's catalog compared with FL's own database (Installed):
    `not_in_fl` (a plugin FL Studio's Plugin Manager most likely hasn't
    scanned yet) and `not_filed` (FL knows it, the browse tree doesn't show it)."""
    root = _plugin_db_root(conn)
    name_index = _build_name_index(conn)
    waves = _waves_index(conn)
    entries, orphans = [], []
    for section in _SECTIONS:
        section_dir = root / section
        if not section_dir.is_dir():
            continue
        for fst in sorted(section_dir.rglob("*.fst")):
            entries.append(_entry_check(conn, section, section_dir, fst, name_index, waves))
        for extra in sorted([*section_dir.rglob("*.png"), *section_dir.rglob("*.nfo")]):
            if extra.with_suffix(".fst").exists():
                continue
            if extra.suffix.lower() == ".nfo" and extra.with_suffix("").is_dir():
                continue  # a folder's own .nfo (its tip/icon in FL's browser), not a plugin's
            if extra.suffix.lower() == ".png" and any(
                    _bitmap_of(n) == extra.name for n in extra.parent.glob("*.nfo")):
                continue  # another plugin's .nfo uses it
            folder = extra.parent.relative_to(section_dir).as_posix()
            orphans.append({"section": section, "path": str(extra), "folder": "" if folder == "." else folder})

    stems = {e["name"].lower() for e in entries}
    # A plugin filed under a linked name (its .fst stem is the link's name) is filed too.
    linked_ids = {pid for name, pid in dbmod.fl_links(conn).items() if name in stems}
    fl_index = fl_database.build_index(str(_installed_root(conn)))
    quarantined = {p.lower() for p in dbmod.quarantined_host_paths(conn)}
    not_in_fl, not_filed = [], []
    for r in dbmod.all_plugins(conn):
        if r["format"] not in _FL_FORMATS or r["host_path"].lower() in quarantined:
            continue
        item = {"id": r["id"], "name": r["name"], "vendor": r["vendor"], "format": r["format"],
                "host_path": r["host_path"]}
        hit = fl_database.lookup(r["host_path"], r["vendor"], fl_index, r["format"])
        if hit is None:
            item["reason"] = ("FL has no WaveShell for this Waves version" if r["format"] == "WAVES"
                              else "not in FL Studio's plugin database")
            not_in_fl.append(item)
        elif (r["format"] != "WAVES" and r["id"] not in linked_ids
              and (hit["name"] or "").lower() not in stems and r["name"].lower() not in stems):
            item["fl_name"] = hit["name"]
            fl_nfo = Path(hit["nfo"]) if hit.get("nfo") else None
            item["fl_nfo"] = str(fl_nfo) if fl_nfo else None
            # FL's own .fst beside its .nfo: "Add to FL's browser" copies it (else: FL's Plugin Manager).
            item["can_add"] = bool(fl_nfo and fl_nfo.with_suffix(".fst").is_file())
            not_filed.append(item)

    with_issues = sum(1 for e in entries if e["issues"])
    return {
        "root": str(root),
        "entries": entries,
        "orphans": orphans,
        "not_in_fl": not_in_fl,
        "not_filed": not_filed,
        "counts": {
            "fst": len(entries), "with_issues": with_issues,
            "no_picture": sum(1 for e in entries if e["picture"] is None),
            "vext_pictures": sum(1 for e in entries if e["source"] == "vext"),
            "can_add_picture": sum(1 for e in entries if e["vext_picture"] and e["source"] != "vext"),
            "can_standardize": sum(1 for e in entries if e["can_standardize"]),
            "linked": sum(1 for e in entries if e["linked"]),
            "unknown": sum(1 for e in entries if e["plugin_id"] is None and not e["ignored"]),
            "ignored": sum(1 for e in entries if e["ignored"]),
            "orphans": len(orphans), "not_in_fl": len(not_in_fl), "not_filed": len(not_filed),
        },
    }


def repair(conn) -> dict:
    """Fixes what the check can fix without choosing a picture: a .png that
    sits beside its .fst but has no (or a wrong) .nfo gets one pointing at it,
    and a plugin's .nfo/.png get the hidden attribute FL gives them."""
    root = _plugin_db_root(conn)
    nfo_written = hidden = 0
    for section in _SECTIONS:
        section_dir = root / section
        if not section_dir.is_dir():
            continue
        for fst in section_dir.rglob("*.fst"):
            nfo, png = fst.with_suffix(".nfo"), fst.with_suffix(".png")
            bitmap = _bitmap_of(nfo) if nfo.exists() else None
            if png.exists() and (not bitmap or not (fst.parent / bitmap).is_file()):
                _write_bitmap_nfo(nfo, png.name)
                nfo_written += 1
            for f in (nfo, png, fst.parent / bitmap if bitmap else None):
                if f is not None and f.exists() and not _is_hidden(f):
                    _set_hidden(f, True)
                    hidden += 1
    return {"nfo_written": nfo_written, "hidden": hidden}


# ---------- fix pictures, link an .fst to a VEXT plugin ----------

def _browse_fst(root: Path, raw: str) -> tuple[Path, str] | str:
    """(resolved path, section) when `raw` is an existing .fst in Effects or
    Generators of the browse tree under `root` (resolved), else the reason it
    isn't. A path outside the Plugin database folder is refused, so a request can
    never write outside it. Installed\\ is refused too: that's FL's own bookkeeping."""
    try:
        fst = Path(raw).resolve()
    except (OSError, ValueError):
        return "not a valid path"
    if not fst.is_relative_to(root):
        return "outside FL Studio's plugin database"
    if fst.suffix.lower() != ".fst":
        return "not an .fst file"
    parts = fst.relative_to(root).parts
    section = next((s for s in _SECTIONS if len(parts) >= 2 and s.lower() == parts[0].lower()), None)
    if section is None:
        return "not in Effects or Generators"
    if not fst.is_file():
        return "file not found"
    return fst, section


def fix_pictures(conn, fst_paths: list[str], height: int) -> dict:
    """Gives each browse-tree .fst VEXT's own picture at `height` (a key of
    THUMB_SIZES) as its FL thumbnail. This is an explicit request, so a thumbnail
    VEXT didn't make is backed up first, then replaced. One bad path is skipped
    with its reason and never raises. A plugin is found by its name or its link
    (_plugin_for_stem), so an unrecognized .fst needs a link first.

    Answers {"fixed", "skipped": [{"fst", "name", "reason"}], "backup", "entries"}.
    "entries" is _entry_check() of every browse-tree .fst asked for that exists.
    "fixed" counts thumbnails that now show VEXT's picture at `height`: written,
    or already current."""
    if height not in THUMB_SIZES:
        raise ValueError(f"height must be one of {sorted(THUMB_SIZES)}, got {height!r}")
    root = _plugin_db_root(conn).resolve()
    name_index = _build_name_index(conn)
    waves = _waves_index(conn)
    pics = _Pictures(root, True, True, height)
    fixed, skipped, found, seen = 0, [], [], set()

    def _skip(raw: str, reason: str) -> None:
        skipped.append({"fst": raw, "name": Path(raw).stem, "reason": reason})

    for raw in fst_paths:
        checked = _browse_fst(root, raw)
        if isinstance(checked, str):
            _skip(raw, checked)
            continue
        fst, section = checked
        if str(fst).lower() in seen:
            continue
        seen.add(str(fst).lower())
        found.append((fst, section))
        row = _plugin_for_stem(conn, fst.stem, name_index, waves)
        if row is None:
            _skip(raw, "not recognized by VEXT: link it first")
            continue
        picture = picture_for_host_path(row["host_path"])
        if picture is None:
            _skip(raw, f"VEXT has no picture for {row['name']}")
            continue
        try:
            pics.place(fst, picture)
        except OSError as exc:
            _skip(raw, f"couldn't write the thumbnail: {exc}")
            continue
        fixed += 1

    entries = [_entry_check(conn, section, root / section, fst, name_index, waves) for fst, section in found]
    return {
        "fixed": fixed, "skipped": skipped,
        "backup": str(pics.backup_dir) if pics.backup_dir else None, "entries": entries,
    }


def fix_all_pictures(conn, height: int) -> dict:
    """Fix picture for the whole browse tree: every .fst VEXT recognizes (by name
    or a link) and has a picture for gets it at `height`; one already current is
    left alone. An explicit request, like fix_pictures(), so a thumbnail VEXT
    didn't make is backed up first, then replaced.

    Answers {"written", "current", "unknown", "no_picture", "failed", "backup"}."""
    if height not in THUMB_SIZES:
        raise ValueError(f"height must be one of {sorted(THUMB_SIZES)}, got {height!r}")
    root = _plugin_db_root(conn).resolve()
    name_index = _build_name_index(conn)
    waves = _waves_index(conn)
    pics = _Pictures(root, True, True, height)
    unknown = no_picture = failed = 0
    for section in _SECTIONS:
        section_dir = root / section
        if not section_dir.is_dir():
            continue
        for fst in sorted(section_dir.rglob("*.fst")):
            row = _plugin_for_stem(conn, fst.stem, name_index, waves)
            if row is None:
                unknown += 1
                continue
            picture = picture_for_host_path(row["host_path"])
            if picture is None:
                no_picture += 1
                continue
            try:
                pics.place(fst, picture)
            except OSError:
                failed += 1
    c = pics.counts
    return {
        "written": c["added"] + c["updated"], "current": c["current"], "unknown": unknown,
        "no_picture": no_picture, "failed": failed,
        "backup": str(pics.backup_dir) if pics.backup_dir else None,
    }


def _resized_bytes(png: Path, height: int) -> bytes:
    """The thumbnail as it is, `height` px tall (width follows). A VEXT mark
    keeps its picture time with the new height, so later runs count it current."""
    import io

    from PIL import Image, PngImagePlugin

    with Image.open(png) as im:
        im.load()
        mark = (getattr(im, "text", None) or im.info).get(_VEXT_MARK_KEY)
        w, h = im.size
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGBA")
        resized = im.resize((max(1, round(w * height / h)), height), Image.Resampling.LANCZOS)
    info = PngImagePlugin.PngInfo()
    if mark:
        info.add_text(_VEXT_MARK_KEY, f"{mark.split('@')[0]}@{height}")
    buf = io.BytesIO()
    resized.save(buf, "PNG", pnginfo=info, optimize=True)
    return buf.getvalue()


def _resize_one(fst: Path, height: int, pics: "_Pictures") -> str:
    """Resizes the thumbnail an .fst shows (its .nfo's Bitmap, else <stem>.png):
    "resized", "current", or the reason it couldn't. One VEXT didn't make is
    backed up first (the day's backup folder)."""
    nfo = fst.with_suffix(".nfo")
    bitmap = _bitmap_of(nfo) if nfo.is_file() else None
    png = fst.parent / bitmap if bitmap and (fst.parent / bitmap).is_file() else fst.with_suffix(".png")
    if not png.is_file():
        return "no picture to resize"
    size, mark = _png_info(png)
    if size is None:
        return "the picture can't be read"
    if size[1] == height:
        return "current"
    try:
        if mark is None:
            pics._back_up(png)
        _write_hidden(png, _resized_bytes(png, height))
        if _bitmap_of(nfo) != png.name:
            _write_bitmap_nfo(nfo, png.name)
    except OSError as exc:
        return f"couldn't write it: {exc}"
    return "resized"


def resize_pictures(conn, fst_paths: list[str], height: int) -> dict:
    """Changes only the size of these .fst files' thumbnails (the same image,
    FL's or VEXT's) to `height`. Answers {"resized", "current", "skipped":
    [{"fst", "name", "reason"}], "backup", "entries"}."""
    if height not in THUMB_SIZES:
        raise ValueError(f"height must be one of {sorted(THUMB_SIZES)}, got {height!r}")
    root = _plugin_db_root(conn).resolve()
    name_index = _build_name_index(conn)
    waves = _waves_index(conn)
    pics = _Pictures(root, True, True, height)
    resized = current = 0
    skipped, found, seen = [], [], set()
    for raw in fst_paths:
        checked = _browse_fst(root, raw)
        if isinstance(checked, str):
            skipped.append({"fst": raw, "name": Path(raw).stem, "reason": checked})
            continue
        fst, section = checked
        if str(fst).lower() in seen:
            continue
        seen.add(str(fst).lower())
        found.append((fst, section))
        result = _resize_one(fst, height, pics)
        if result == "resized":
            resized += 1
        elif result == "current":
            current += 1
        else:
            skipped.append({"fst": raw, "name": fst.stem, "reason": result})
    entries = [_entry_check(conn, section, root / section, fst, name_index, waves) for fst, section in found]
    return {"resized": resized, "current": current, "skipped": skipped,
            "backup": str(pics.backup_dir) if pics.backup_dir else None, "entries": entries}


def resize_all_pictures(conn, height: int) -> dict:
    """Every thumbnail in the browse tree at `height` (the same images: FL's,
    the owner's or VEXT's). Answers {"resized", "current", "no_picture",
    "failed", "backup"}."""
    if height not in THUMB_SIZES:
        raise ValueError(f"height must be one of {sorted(THUMB_SIZES)}, got {height!r}")
    root = _plugin_db_root(conn).resolve()
    pics = _Pictures(root, True, True, height)
    counts = {"resized": 0, "current": 0, "no_picture": 0, "failed": 0}
    for section in _SECTIONS:
        section_dir = root / section
        if not section_dir.is_dir():
            continue
        for fst in sorted(section_dir.rglob("*.fst")):
            result = _resize_one(fst, height, pics)
            key = result if result in ("resized", "current") else (
                "no_picture" if result == "no picture to resize" else "failed")
            counts[key] += 1
    return {**counts, "backup": str(pics.backup_dir) if pics.backup_dir else None}


def _shared_picture(pic: Path, own_nfo: Path) -> bool:
    """Another plugin's .nfo in the same folder shows `pic` too."""
    return any(n != own_nfo and (_bitmap_of(n) or "").lower() == pic.name.lower()
               for n in pic.parent.glob("*.nfo"))


def remove_entries(conn, fst_paths: list[str]) -> dict:
    """Takes .fst entries VEXT doesn't recognize out of FL Studio's browser:
    each .fst with its .nfo and its picture moves to Plugin database/
    Removed.vext-<UTC time>/ (same relative path), outside the browse tree.
    Nothing is deleted: moving the files back restores the entry. A plugin VEXT
    recognizes (by its name or a link) is refused: its entry is VEXT's to file,
    not to remove. A picture another plugin's .nfo also uses stays.

    Answers {"removed": [<.fst path>], "skipped": [{"fst", "name", "reason"}],
    "folder": <the Removed folder> | None}."""
    root = _plugin_db_root(conn).resolve()
    name_index = _build_name_index(conn)
    waves = _waves_index(conn)
    dest_root = root / f"Removed.vext-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    removed, skipped = [], []

    def _skip(raw: str, reason: str) -> None:
        skipped.append({"fst": raw, "name": Path(raw).stem, "reason": reason})

    for raw in fst_paths:
        checked = _browse_fst(root, raw)
        if isinstance(checked, str):
            _skip(raw, checked)
            continue
        fst = checked[0]
        row = _plugin_for_stem(conn, fst.stem, name_index, waves)
        if row is not None:
            _skip(raw, f"VEXT recognizes it as {row['name']}: only unrecognized entries can be removed")
            continue
        nfo = fst.with_suffix(".nfo")
        files = [fst]
        if nfo.is_file():
            files.append(nfo)
        bitmap = _bitmap_of(nfo) if nfo.is_file() else None
        for pic in (fst.with_suffix(".png"), fst.parent / bitmap if bitmap else None):
            if pic is not None and pic.is_file() and pic not in files and not _shared_picture(pic, nfo):
                files.append(pic)
        try:
            for f in files:
                target = dest_root / f.relative_to(root)
                target.parent.mkdir(parents=True, exist_ok=True)
                _retry_on_lock(lambda f=f, target=target: shutil.move(str(f), str(target)),
                               f"Couldn't move {f.name}")
        except OSError as exc:
            _skip(raw, f"couldn't move it: {exc}")
            continue
        removed.append(str(fst))
    return {"removed": removed, "skipped": skipped, "folder": str(dest_root) if removed else None}


def set_ignored(conn, name: str, ignored: bool) -> list[dict]:
    """Ignores an FL plugin name VEXT doesn't recognize (or stops ignoring it).
    Answers the _entry_check of every browse-tree .fst with that name."""
    dbmod.set_fl_ignored(conn, name, ignored)
    conn.commit()
    root = _plugin_db_root(conn).resolve()
    name_index = _build_name_index(conn)
    waves = _waves_index(conn)
    return [_entry_check(conn, section, root / section, fst, name_index, waves)
            for fst, section in _named_fsts(root, name)]


def add_detected(conn, items: list[dict], height: int, pictures: bool = True) -> dict:
    """Not filed -> filed: for each {"plugin_id", "nfo"} (a VEXT plugin and FL's own
    .nfo for it in Installed), copies FL's .fst into the browse tree, in the plugin's
    vendor folder (Effects/<Vendor>, the section FL keeps it in), links FL's name to
    the plugin when VEXT wouldn't match it by name, and gives it VEXT's picture at
    `height` (hidden .png and .nfo). An entry FL's database doesn't have
    (no .fst beside the .nfo) is skipped with "scan it with FL Studio's Plugin Manager".
    FL's Installed folder is only read. Answers {"added": [<entry>], "skipped":
    [{"plugin_id", "name", "reason"}]}."""
    if height not in THUMB_SIZES:
        raise ValueError(f"height must be one of {sorted(THUMB_SIZES)}, got {height!r}")
    root = _plugin_db_root(conn).resolve()
    installed = _installed_root(conn).resolve()
    name_index = _build_name_index(conn)
    waves = _waves_index(conn)
    pics = _Pictures(root, pictures, False, height)
    added, skipped = [], []
    for item in items:
        plugin_id = item.get("plugin_id")
        row = dbmod.get_plugin(conn, plugin_id) if isinstance(plugin_id, int) else None
        label = row["name"] if row else str(plugin_id)

        def _skip(reason: str, plugin_id=plugin_id, label=label) -> None:
            skipped.append({"plugin_id": plugin_id, "name": label, "reason": reason})

        if row is None:
            _skip("not a VEXT plugin")
            continue
        try:
            nfo = Path(item.get("nfo") or "").resolve()
        except (OSError, ValueError):
            nfo = None
        if not nfo or not nfo.is_relative_to(installed) or nfo.suffix.lower() != ".nfo":
            _skip("FL Studio's plugin database has no entry for it: scan it with FL Studio's Plugin Manager")
            continue
        src = nfo.with_suffix(".fst")
        section = next((s for s in _SECTIONS if nfo.relative_to(installed).parts[:1] == (s,)), None)
        if section is None or not src.is_file():
            _skip("FL Studio's plugin database has no entry for it: scan it with FL Studio's Plugin Manager")
            continue
        folder = root / section
        if row["vendor"]:
            folder = folder / _safe_folder_name(row["vendor"])
        dest = folder / src.name
        if dest.exists():
            _skip(f"already in {section}: {dest.relative_to(root)}")
            continue
        try:
            folder.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(dest))
            # FL's name for it may not be VEXT's ("Choir" / "AVOX Choir"): the plugin is known here, so
            # the new entry gets a link and is recognized from now on.
            known = _plugin_for_stem(conn, dest.stem, name_index, waves)
            if known is None or known["id"] != row["id"]:
                dbmod.set_fl_link(conn, dest.stem, row["id"])
                conn.commit()
                name_index = _build_name_index(conn)
            picture = picture_for_host_path(row["host_path"])
            if picture:
                pics.place(dest, picture)
        except OSError as exc:
            _skip(f"couldn't copy it: {exc}")
            continue
        added.append(_entry_check(conn, section, root / section, dest, name_index, waves))
    return {"added": added, "skipped": skipped}


_LINK_STOP_WORDS = frozenset({"x64", "x86", "64", "32", "bit", "vst", "vst2", "vst3", "clap", "aax",
                              "stereo", "mono", "plugin", "fx"})
_LINK_MIN_SCORE = 0.35


def _link_key(name: str) -> tuple[str, frozenset[str]]:
    """(normalized name, its words) for matching an FL name to a VEXT name:
    lowercase, no bracketed part ("(x64)", "[VST3]"), no format or bitness
    words, and no punctuation."""
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    text = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", text)
    words = [w for w in re.findall(r"[a-z0-9]+", text) if w not in _LINK_STOP_WORDS]
    return "".join(words), frozenset(words)


def _link_score(a: tuple[str, frozenset[str]], b: tuple[str, frozenset[str]]) -> float:
    """The best of: text similarity, shared words (Jaccard), and 0.9 when one
    normalized name contains the other (both at least 3 characters long)."""
    (na, wa), (nb, wb) = a, b
    if not na or not nb:
        return 0.0
    best = difflib.SequenceMatcher(None, na, nb).ratio()
    best = max(best, len(wa & wb) / len(wa | wb))
    if min(len(na), len(nb)) >= 3 and (na in nb or nb in na):
        best = max(best, 0.9)
    return best


_FORMAT_BY_EXT = {".vst3": "VST3", ".clap": "CLAP", ".dll": "VST2", ".bundle": "WAVES"}


def link_candidates(conn, name: str, limit: int = 10, include_ignored: bool = False) -> list[dict]:
    """VEXT plugins that might be the one an FL name means, best first: every
    non-quarantined plugin in a format FL can load, scored by _link_score()
    (at least _LINK_MIN_SCORE). With include_ignored, the plugins the owner
    ignored in VEXT too (they aren't in the catalog: "id" None, "ignored" True;
    linking one brings it back). Read-only."""
    query = _link_key(name)
    if not query[0]:
        return []
    quarantined = {p.lower() for p in dbmod.quarantined_host_paths(conn)}
    scored = []
    for r in dbmod.all_plugins(conn):
        if r["format"] not in _FL_FORMATS or r["host_path"].lower() in quarantined:
            continue
        score = _link_score(query, _link_key(r["name"]))
        if score >= _LINK_MIN_SCORE:
            scored.append((score, r))
    out = [
        (score, {"id": r["id"], "name": r["name"], "vendor": r["vendor"], "format": r["format"],
                 "host_path": r["host_path"], "score": round(score, 2), "ignored": False,
                 "has_picture": picture_for_host_path(r["host_path"]) is not None})
        for score, r in scored
    ]
    if include_ignored:
        for r in ignored_plugins(conn):
            score = _link_score(query, _link_key(r["name"]))
            if score >= _LINK_MIN_SCORE:
                out.append((score, {**r, "score": round(score, 2)}))
    out.sort(key=lambda t: (-t[0], t[1]["name"].lower()))
    return [c for _score, c in out[:limit]]


def ignored_plugins(conn, q: str = "") -> list[dict]:
    """The plugins the owner ignored in VEXT (out of the catalog), shaped like a
    link candidate, optionally filtered by a text in the name or path."""
    q = q.strip().lower()
    out = []
    for r in dbmod.list_ignored_plugin_paths(conn):
        if q and q not in r["name"].lower() and q not in r["host_path"].lower():
            continue
        out.append({"id": None, "name": r["name"], "vendor": None, "ignored": True,
                    "format": _FORMAT_BY_EXT.get(Path(r["host_path"]).suffix.lower(), ""),
                    "host_path": r["host_path"],
                    "has_picture": picture_for_host_path(r["host_path"]) is not None})
    return out


def _named_fsts(root: Path, name: str) -> list[tuple[Path, str]]:
    """Every .fst called `name` (any case) under Effects or Generators, with its section."""
    found = []
    for section in _SECTIONS:
        section_dir = root / section
        if section_dir.is_dir():
            found += [(p, section) for p in sorted(section_dir.rglob("*.fst")) if p.stem.lower() == name.lower()]
    return found


def set_link(conn, name: str, plugin_id: int | None, fix_picture: bool = False, height: int = THUMB_HEIGHT) -> dict:
    """Links the FL plugin name (an .fst stem) to a VEXT plugin, or unlinks it
    when plugin_id is None. With fix_picture, also fixes the pictures of every
    .fst with that name at `height`. Answers {"entries", "fixed"}: "entries" is
    _entry_check() of every browse-tree .fst with that name."""
    name = name.strip()
    if not name:
        raise ValueError("the FL plugin name is empty")
    dbmod.set_fl_link(conn, name, plugin_id)
    root = _plugin_db_root(conn).resolve()
    files = _named_fsts(root, name)
    fixed = 0
    if fix_picture and plugin_id is not None and files:
        fixed = fix_pictures(conn, [str(p) for p, _ in files], height)["fixed"]
    name_index = _build_name_index(conn)
    waves = _waves_index(conn)
    entries = [_entry_check(conn, section, root / section, fst, name_index, waves) for fst, section in files]
    return {"entries": entries, "fixed": fixed}


def plugin_manager_path() -> str | None:
    """FL Studio's own Plugin Manager (System/Tools/Plugin Manager/
    PluginManager.exe in FL's install folder), or None if it isn't found."""
    candidates = []
    if _FL_PATH:
        candidates.append(Path(_FL_PATH) / "System" / "Tools" / "Plugin Manager" / "PluginManager.exe")
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if base:
            candidates.extend(sorted(Path(base, "Image-Line").glob("*/System/Tools/Plugin Manager/PluginManager.exe"),
                                     reverse=True))
    return next((str(p) for p in candidates if p.is_file()), None)
