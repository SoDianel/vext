"""Metadata extraction: PE version resources and VST3/AAX bundle moduleinfo.json."""

import json
import re
from pathlib import Path

import pefile


class CorruptedFile(Exception):
    pass


def _strip_trailing_commas(text: str) -> str:
    # moduleinfo.json is JSON-with-trailing-commas, not strict JSON.
    return re.sub(r",(\s*[}\]])", r"\1", text)


def read_module_info(bundle_dir: Path) -> dict | None:
    """Read Contents/Resources/moduleinfo.json from a VST3/AAX bundle, if present."""
    candidate = bundle_dir / "Contents" / "Resources" / "moduleinfo.json"
    if not candidate.is_file():
        return None
    try:
        raw = candidate.read_text(encoding="utf-8-sig", errors="replace")
        return json.loads(_strip_trailing_commas(raw))
    except (OSError, json.JSONDecodeError):
        return None


def find_bundle_binary(bundle_dir: Path, name_hint: str) -> Path | None:
    """Find the actual PE binary inside a .vst3/.aaxplugin bundle folder."""
    contents = bundle_dir / "Contents"
    if not contents.is_dir():
        return None
    # Known subdirs first (fast path), then fall back to any file with a matching suffix.
    for sub in ("x86_64-win", "x86-win", "x64", "Win32", "Win64"):
        candidate_dir = contents / sub
        if candidate_dir.is_dir():
            for f in candidate_dir.iterdir():
                if f.is_file() and f.suffix.lower() == Path(name_hint).suffix.lower():
                    return f
    for f in contents.rglob("*"):
        if f.is_file() and f.suffix.lower() == Path(name_hint).suffix.lower():
            return f
    return None


def parse_pe_version_info(pe_path: Path) -> dict:
    """Extract CompanyName / ProductName / FileDescription / ProductVersion from a PE file.

    Raises CorruptedFile if the file can't be parsed as a PE binary at all.
    """
    result = {
        "company": None,
        "product_name": None,
        "file_description": None,
        "product_version": None,
        "file_version": None,
        "bitness": None,
    }
    try:
        pe = pefile.PE(str(pe_path), fast_load=True)
        pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_RESOURCE"]])
    except pefile.PEFormatError as exc:
        raise CorruptedFile(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - any other parse failure = flag, don't crash the scan
        raise CorruptedFile(str(exc)) from exc

    # PE Machine field is the authoritative 32/64-bit signal (0x14c = i386, 0x8664 = x64,
    # 0xAA64 = ARM64); filenames like "APCx86.dll" and "(x86)" folders are only conventions.
    machine = getattr(getattr(pe, "FILE_HEADER", None), "Machine", None)
    if machine == 0x14C:
        result["bitness"] = 32
    elif machine in (0x8664, 0xAA64):
        result["bitness"] = 64

    try:
        if hasattr(pe, "FileInfo"):
            for fi_list in pe.FileInfo:
                for fi in fi_list:
                    if fi.Key == b"StringFileInfo":
                        for st in fi.StringTable:
                            entries = {k.decode(errors="replace"): v.decode(errors="replace")
                                       for k, v in st.entries.items()}
                            result["company"] = entries.get("CompanyName") or result["company"]
                            result["product_name"] = entries.get("ProductName") or result["product_name"]
                            result["file_description"] = entries.get("FileDescription") or result["file_description"]
                            result["product_version"] = entries.get("ProductVersion") or result["product_version"]
                            result["file_version"] = entries.get("FileVersion") or result["file_version"]
    except Exception:  # noqa: BLE001 - missing version resource is common and not corruption
        pass
    finally:
        pe.close()

    return result


def dir_size_bytes(path: Path) -> int:
    total = 0
    for f in path.rglob("*"):
        if f.is_file():
            try:
                total += f.stat().st_size
            except OSError:
                pass
    return total
