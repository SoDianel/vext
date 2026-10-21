"""Configuration › Storage: what VEXT keeps that can go to free space, measured,
and clearing it (GET /api/storage, POST /api/storage/clear).

Two kinds. **Cache**: files VEXT or the web view make again when needed (the web
cache, the Playground's temporary audio, picture suggestions). **Backups**: copies
VEXT made before changing FL Studio's Plugin database (replaced thumbnails, entries
taken out of FL's browser, browse-tree copies saved before a wipe). Deleting a
backup is permanent, so the page says so and asks first.

Never touched here: the catalog, VEXT's own plugin pictures (images\\), user
wavetables, the Quarantine (its own tab), the file the Playground has open now and
its recording, logs, and the web view's sign-ins and site settings (only its
cache folders go). A file in use (the web view runs, a plugin plays) is left and
counted as not freed.
"""

import os
from pathlib import Path

from app.config import PROJECT_ROOT

_PLAYGROUND = PROJECT_ROOT / "cache" / "playground"
_SUGGESTIONS = PROJECT_ROOT / "cache" / "pictures" / "suggestions"
# The desktop shell's WebView2 profile (the app's pages and the Playground's browser share it).
_WEBVIEW = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "com.vext.app" / "EBWebView"
# Only cache folders: cookies, logins, local storage and site settings live elsewhere in the profile.
_WEB_CACHE = ("Default/Cache", "Default/Code Cache", "Default/GPUCache", "Default/Service Worker/CacheStorage",
              "Default/Service Worker/ScriptCache", "GrShaderCache", "ShaderCache", "GPUPersistentCache")

ITEMS = [
    {"key": "web-cache", "kind": "cache", "label": "Web cache",
     "detail": "Pages and media the Playground's browser and VEXT's own window downloaded. "
               "Sign-ins and site settings stay. Some files stay while VEXT is open."},
    {"key": "playground-temp", "kind": "cache", "label": "Playground's temporary audio",
     "detail": "Decoded copies of files no longer open, earlier test signals and browser recordings, "
               "uploads. The file open now and its recording stay."},
    {"key": "picture-suggestions", "kind": "cache", "label": "Picture suggestions",
     "detail": "Pictures found online and not picked yet. Find online gets them again."},
    {"key": "fl-thumbnail-backups", "kind": "backup", "label": "FL Studio thumbnail backups",
     "detail": "The thumbnails VEXT replaced (Thumbnails.vext-backup-…). Deleting them can't be undone."},
    {"key": "fl-removed", "kind": "backup", "label": "Entries removed from FL Studio's browser",
     "detail": "Removed.vext-… (moving them back restores an entry). Deleting them can't be undone."},
    {"key": "fl-tree-backups", "kind": "backup", "label": "FL Studio browse-tree backups",
     "detail": "Copies of Effects / Generators saved before a wipe and rebuild. Deleting them can't be undone."},
]
_KEYS = {i["key"] for i in ITEMS}


def _files_under(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    if not path.is_dir():
        return []
    out = []
    for dirpath, _dirs, names in os.walk(path):
        out.extend(Path(dirpath) / n for n in names)
    return out


def _playground_keep() -> set[str]:
    """The Playground's files in use now: its state, the open file's decoded copy,
    its original when that is a cached one, its recording and what plays it."""
    from app import playground_audio

    keep = {str(_PLAYGROUND / "state.json").lower()}
    try:
        st = playground_audio.state()
    except Exception:  # noqa: BLE001 - no state means nothing to protect but state.json
        return keep
    for key in ("source_path", "path"):
        if st.get(key):
            keep.add(str(Path(st[key])).lower())
    rec = st.get("recording") or {}
    if rec.get("path"):
        keep.add(str(Path(rec["path"])).lower())
    token = Path(st.get("source_path") or "").stem.removeprefix("source-")
    if token:
        keep.update(str(p).lower() for p in _PLAYGROUND.glob(f"heard-{token}*"))
    return keep


def _targets(key: str, fl_root: Path | None) -> list[Path]:
    """The folders or files an item clears."""
    if key == "web-cache":
        return [_WEBVIEW / sub for sub in _WEB_CACHE]
    if key == "picture-suggestions":
        return [_SUGGESTIONS]
    if key == "playground-temp":
        keep = _playground_keep()
        out = []
        for name in ("captures", "signals"):
            out += [p for p in _files_under(_PLAYGROUND / name) if str(p).lower() not in keep]
        if _PLAYGROUND.is_dir():
            out += [p for p in _PLAYGROUND.iterdir() if p.is_file() and str(p).lower() not in keep
                    and p.name.startswith(("source-", "recording-", "upload-", "heard-"))]
        return out
    if fl_root is None or not fl_root.is_dir():
        return []
    if key == "fl-thumbnail-backups":
        return sorted(fl_root.glob("Thumbnails.vext-backup-*"))
    if key == "fl-removed":
        return sorted(fl_root.glob("Removed.vext-*"))
    if key == "fl-tree-backups":
        return sorted(p for p in fl_root.glob("*.vext-backup-*") if not p.name.startswith("Thumbnails."))
    return []


def _location(key: str, fl_root: Path | None) -> str:
    if key == "web-cache":
        return str(_WEBVIEW)
    if key == "playground-temp":
        return str(_PLAYGROUND)
    if key == "picture-suggestions":
        return str(_SUGGESTIONS)
    return str(fl_root) if fl_root else ""


def measure(fl_root: Path | None) -> dict:
    """Every item with its size in bytes, its file count and where it is."""
    items, total = [], 0
    for item in ITEMS:
        size = files = 0
        for target in _targets(item["key"], fl_root):
            for f in _files_under(target):
                try:
                    size += f.stat().st_size
                    files += 1
                except OSError:
                    pass
        total += size
        items.append({**item, "size": size, "files": files, "location": _location(item["key"], fl_root)})
    return {"items": items, "total": total}


def _remove_tree(path: Path) -> tuple[int, int, int]:
    """Deletes what it can under `path`: (bytes freed, files deleted, files left)."""
    freed = done = left = 0
    for f in _files_under(path):
        try:
            size = f.stat().st_size
            if os.name == "nt":
                os.chmod(f, 0o666)   # a read-only or hidden attribute would refuse the delete
            f.unlink()
            freed += size
            done += 1
        except OSError:
            left += 1
    if path.is_dir():
        # Empty folders go too (deepest first); one still holding a file in use stays.
        for dirpath, _dirs, _names in sorted(os.walk(path), key=lambda t: -len(t[0])):
            try:
                os.rmdir(dirpath)
            except OSError:
                pass
    return freed, done, left


def clear(keys: list[str], fl_root: Path | None) -> dict:
    """Clears the items asked for. Answers per item {key, freed, files, left}."""
    results, total = [], 0
    for key in keys:
        if key not in _KEYS:
            raise ValueError(f"unknown storage item {key!r}")
        freed = done = left = 0
        for target in _targets(key, fl_root):
            f, d, lft = _remove_tree(target)
            freed, done, left = freed + f, done + d, left + lft
        if key == "picture-suggestions":
            _SUGGESTIONS.mkdir(parents=True, exist_ok=True)   # the picker writes here again
        total += freed
        results.append({"key": key, "freed": freed, "files": done, "left": left})
    return {"results": results, "freed": total}


def open_target(key: str, fl_root: Path | None) -> str:
    """The folder to show in Explorer for an item."""
    if key not in _KEYS:
        raise ValueError(f"unknown storage item {key!r}")
    return _location(key, fl_root)

