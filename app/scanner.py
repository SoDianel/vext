"""Walks configured plugin roots and produces plugin records (read-only)."""

import re
from datetime import datetime, timezone
from pathlib import Path

from app import metadata
from app.categories import resolve_subcategory_tokens
from app.config import ScanRoot

# Non-plugin resource files that sit alongside real plugin units in these folders
# (Native Instruments-style .aqu/.N2S/.XML/.ENG, Waves-style .data.lnk, OS cruft).
_IGNORE_SUFFIXES = {".xml", ".aqu", ".n2s", ".eng", ".aut", ".lnk", ".ini", ".ico", ".nfo", ".rtf"}
_IGNORE_NAMES = {"desktop.ini", "folder.ico"}
# WaveShell*.vst3/.dll and WavesLib*.dll are Waves' shared runtime hosts, not something
# a user picks or manages individually (see AGENTS.md's Waves section) — deleting/moving
# one breaks every Waves plugin at once, and they don't belong in a per-plugin catalog
# either. WavesLib*.dll confirmed real via a stray-files audit: sits loose directly in
# each "Plug-Ins Vxx" version folder (unlike WaveShell, which lives inside VST3/VST2).
_IGNORE_PREFIXES = ("waveshell", "waveslib")

_SUFFIX_STRIP_RE = re.compile(r"[\s_-]*(x64|x86|64|32|win|vst3?|clap)\s*$", re.IGNORECASE)
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def normalize_name(name: str) -> str:
    base = Path(name).stem
    prev = None
    while prev != base:
        prev = base
        base = _SUFFIX_STRIP_RE.sub("", base).strip()
    return _NON_ALNUM_RE.sub("", base.lower())


def _should_ignore(entry: Path) -> bool:
    if entry.name.lower() in _IGNORE_NAMES:
        return True
    if entry.is_file() and entry.suffix.lower() in _IGNORE_SUFFIXES:
        return True
    if entry.stem.lower().startswith(_IGNORE_PREFIXES):
        return True
    return False


def _derive_type(categories: list | None, default_type: str) -> str:
    if categories:
        joined = " ".join(categories).lower()
        if "instrument" in joined:
            return "synth"
        return "effect"
    return default_type


# Acustica Audio's shared white-label engine reports this literal string as
# the product name for every plugin built on it — not a per-product name at
# all, so it's always wrong, whether it comes from moduleinfo.json's "Name"
# field (bundles that have one) or straight from the PE binary's own
# ProductName resource (confirmed real: STONE.vst3/STONEZL.vst3 have no
# moduleinfo.json at all, just an older ACQUA.engine/ACQUA.xml layout, and
# their binary's ProductName is literally "NUW"). 31 other Acustica plugins
# already relied on FL Studio's own name resolution to fix this per-scan
# (see AGENTS.md); this extends the same fix to the source data itself, so a
# newly-installed one is named correctly on the very first scan, without
# needing FL Studio to have detected it first.
_BAD_PRODUCT_NAMES = {"NUW"}


def _clean_name(name: str | None, fallback_name: str) -> str:
    return fallback_name if not name or name in _BAD_PRODUCT_NAMES else name


def _record_from_moduleinfo(info: dict, fallback_name: str) -> dict:
    factory = info.get("Factory Info", {})
    classes = info.get("Classes", [])
    categories = []
    for c in classes:
        if c.get("Category") == "Audio Module Class":
            categories = c.get("Sub Categories", []) or categories
    return {
        "name": _clean_name(info.get("Name"), fallback_name),
        "vendor": factory.get("Vendor"),
        "version": info.get("Version"),
        "categories": categories,
    }


def _process_bundle(entry: Path, root: ScanRoot) -> dict:
    """A .vst3/.aaxplugin folder-style bundle."""
    info = metadata.read_module_info(entry)
    pe_data = None
    status, reason = "ok", None

    binary = metadata.find_bundle_binary(entry, entry.name)
    if binary is not None:
        try:
            pe_data = metadata.parse_pe_version_info(binary)
        except metadata.CorruptedFile as exc:
            status, reason = "corrupted", f"PE parse failed on {binary.name}: {exc}"
    else:
        status, reason = "needs_review", "no platform binary found inside bundle (possibly incomplete install)"

    if info:
        base = _record_from_moduleinfo(info, entry.stem)
        vendor_source = "moduleinfo" if base["vendor"] else None
    else:
        base = {
            "name": _clean_name((pe_data or {}).get("product_name"), entry.stem),
            "vendor": (pe_data or {}).get("company"),
            "version": (pe_data or {}).get("product_version") or (pe_data or {}).get("file_version"),
            "categories": [],
        }
        vendor_source = "binary" if base["vendor"] else None

    if root.format == "AAX":
        status = "needs_review"
        reason = (reason + "; " if reason else "") + "Pro Tools/AAX format — Pro Tools not part of this workflow, candidate for removal"

    return {
        "name": base["name"],
        "vendor": base["vendor"],
        "vendor_source": vendor_source,
        "version": base["version"],
        "categories": base["categories"],
        "status": status,
        "status_reason": reason,
        "size_bytes": metadata.dir_size_bytes(entry),
        "bitness": (pe_data or {}).get("bitness"),
    }


def _process_flat_file(entry: Path, root: ScanRoot) -> dict:
    """A plain PE file plugin: .vst3 (non-bundle), .dll (VST2), .clap."""
    status, reason = "ok", None
    pe_data = {}
    try:
        pe_data = metadata.parse_pe_version_info(entry)
    except metadata.CorruptedFile as exc:
        status, reason = "corrupted", f"PE parse failed: {exc}"

    return {
        "name": _clean_name(pe_data.get("product_name"), entry.stem),
        "vendor": pe_data.get("company"),
        "vendor_source": "binary" if pe_data.get("company") else None,
        "version": pe_data.get("product_version") or pe_data.get("file_version"),
        "categories": [],
        "status": status,
        "status_reason": reason,
        "size_bytes": entry.stat().st_size,
        "bitness": pe_data.get("bitness"),
    }


def _process_waves_bundle(entry: Path, root: ScanRoot) -> dict:
    # root.host_path is a Windows-style string; on the Linux container pathlib won't
    # split on backslashes, so pull the last segment manually.
    version_hint = root.host_path.replace("/", "\\").split("\\")[-1]  # e.g. "Plug-Ins V16"
    return {
        "name": entry.stem,
        "vendor": "Waves Audio",
        "vendor_source": "default",
        "version": version_hint,
        "categories": [],
        "status": "ok",
        "status_reason": None,
        "size_bytes": metadata.dir_size_bytes(entry),
    }


def _process_fl_native(entry: Path, root: ScanRoot) -> dict:
    status, reason = "ok", None
    pe_data = {}
    inner_dll = None
    for f in entry.glob("*.dll"):
        inner_dll = f
        break
    if inner_dll is not None:
        try:
            pe_data = metadata.parse_pe_version_info(inner_dll)
        except metadata.CorruptedFile as exc:
            status, reason = "corrupted", f"PE parse failed on {inner_dll.name}: {exc}"
    else:
        status, reason = "needs_review", "no binary found inside FL native plugin folder"

    return {
        "name": entry.name,
        "vendor": pe_data.get("company") or "Image-Line",
        "vendor_source": "binary" if pe_data.get("company") else "default",
        "version": pe_data.get("product_version") or pe_data.get("file_version"),
        "categories": [],
        "status": status,
        "status_reason": reason,
        "size_bytes": metadata.dir_size_bytes(entry),
    }


def _host_path(root: ScanRoot, entry: Path) -> str:
    rel = entry.relative_to(root.container_path)
    return str(Path(root.host_path) / rel).replace("/", "\\")


def _process_entry(entry: Path, root: ScanRoot) -> dict | None:
    if _should_ignore(entry):
        return None

    if root.format == "WAVES":
        if entry.is_dir() and entry.suffix.lower() == ".bundle":
            fields = _process_waves_bundle(entry, root)
        else:
            return None
    elif root.format == "FL-NATIVE":
        if entry.is_dir():
            fields = _process_fl_native(entry, root)
        else:
            return None
    elif entry.suffix.lower() in root.extensions:
        fields = _process_bundle(entry, root) if entry.is_dir() else _process_flat_file(entry, root)
    else:
        return None

    fields.setdefault("bitness", None)
    fields["plugin_type"] = _derive_type(fields["categories"], root.default_type)
    fields["category_slugs"] = resolve_subcategory_tokens(fields["categories"]) if fields["categories"] else []
    fields["categories"] = ", ".join(fields["categories"]) if fields["categories"] else None
    fields["format"] = root.format
    fields["host_path"] = _host_path(root, entry)
    fields["normalized_name"] = normalize_name(fields["name"])
    fields["scanned_at"] = datetime.now(timezone.utc).isoformat()
    return fields


_MAX_VENDOR_SUBFOLDER_DEPTH = 3  # e.g. Celemony/Melodyne/Melodyne.vst3 needs 2; one more for margin


def _walk_for_plugins(dir_path: Path, root: ScanRoot, depth_remaining: int) -> list[dict]:
    """Looks for plugin units under dir_path, recursing into non-matching
    subfolders up to depth_remaining levels — some vendors nest an extra
    <Product>/ folder between the vendor folder and the actual plugin unit
    (confirmed real: Celemony/Melodyne/Melodyne.vst3, GuitarML/NeuralPi/
    NeuralPi.vst3, Harrison/Harrison_MasteringEQ/Harrison_MasteringEQ.vst3 —
    a single level of recursion missed all four of these entirely; found via
    a one-off audit for stray non-plugin files that turned up real plugins
    invisible to the catalog instead, see AGENTS.md)."""
    records = []
    if depth_remaining <= 0:
        return records
    for entry in sorted(dir_path.iterdir()):
        if _should_ignore(entry):
            continue
        if entry.is_dir() and entry.suffix.lower() not in root.extensions:
            records.extend(_walk_for_plugins(entry, root, depth_remaining - 1))
            continue
        rec = _process_entry(entry, root)
        if rec:
            records.append(rec)
    return records


def scan_root(root: ScanRoot) -> list[dict]:
    base = Path(root.container_path)
    if not base.is_dir():
        return []

    records = []
    for entry in sorted(base.iterdir()):
        if _should_ignore(entry):
            continue

        if entry.is_dir() and root.recurse_vendor_dirs and entry.suffix.lower() not in root.extensions and root.format not in ("WAVES", "FL-NATIVE"):
            # vendor-named subfolder holding nested plugin units, e.g. VST3/Accusonus/*.vst3
            # (or, some vendors, one level deeper still — see _walk_for_plugins)
            records.extend(_walk_for_plugins(entry, root, _MAX_VENDOR_SUBFOLDER_DEPTH))
            continue

        rec = _process_entry(entry, root)
        if rec:
            records.append(rec)

    return records
