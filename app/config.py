"""Plugin locations: where the scanner looks, detected from this machine.

Nothing is hardcoded to one user or one drive. Each standard location is found
in this order, and the first hit wins:

  1. Windows registry, where a product or the Steinberg/VST convention records
     its folder (FL Studio's uninstall entry, the VST2 "VSTPluginsPath" value).
  2. The standard folder, resolved from the environment variables Windows sets
     (ProgramFiles, CommonProgramFiles, ...) so 32/64-bit and non-C: installs work.
  3. A fixed default, so the row always exists and can be edited by hand.

Each location has a stable `key` (e.g. "vst3_x64") that survives re-detection,
so a user's edits and enable/disable choices stay attached to the right row.
The Configuration tab lets the user override any of them. Products added later
(Ableton, etc.) can be added as custom paths there.

Data file locations (DB, DATABASE.md, quarantine, logs) resolve relative to
VEXT_ROOT, which the desktop shell sets to the per-user app data folder
(desktop/src-tauri/src/lib.rs). Without it (`python -m app.main scan`, local dev)
it falls back to that same folder, %APPDATA%/com.vext.app, so dev and CLI runs
share the installed app's database and never write into the source tree.
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path

try:
    import winreg
except ImportError:  # not Windows: detection falls back to folders/defaults
    winreg = None


@dataclass(frozen=True)
class ScanRoot:
    container_path: str    # always == host_path now; see module docstring
    host_path: str
    format: str            # CLAP / VST3 / VST2 / WAVES / AAX / FL-NATIVE
    default_type: str      # "effect" / "synth" / "unknown" — heuristic fallback
    extensions: tuple      # plugin-unit extensions to look for at this root
    recurse_vendor_dirs: bool = True   # look one extra level deep for vendor subfolders


@dataclass(frozen=True)
class Location:
    key: str               # stable id, e.g. "vst3_x64" — survives re-detection
    label: str             # shown in the Configuration tab
    group: str             # "FL Studio" / "Plugin formats" / "Waves"
    host_path: str         # best detected path (or the default when nothing is found)
    detected_from: str     # "registry" / "folder" / "default"
    format: str
    default_type: str
    extensions: tuple
    recurse_vendor_dirs: bool = True


PROGRAM_FILES = os.environ.get("ProgramFiles", r"C:\Program Files")
PROGRAM_FILES_X86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
COMMON_FILES = os.environ.get("CommonProgramW6432", os.path.join(PROGRAM_FILES, "Common Files"))
COMMON_FILES_X86 = os.environ.get("CommonProgramFiles(x86)", os.path.join(PROGRAM_FILES_X86, "Common Files"))
USER_HOME = os.path.expanduser("~")

_UNINSTALL_KEYS = (
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
)
_FL_VERSION_RE = re.compile(r"FL Studio (\d[\d.]*)$")


def _registry_read(subkey: str, value: str, wow64: int = 0) -> str:
    """Read one string value from HKLM, or "" if it isn't there. `wow64` picks
    the registry view (KEY_WOW64_32KEY / KEY_WOW64_64KEY), so an x86 and an x64
    lookup of the same key see their own copies."""
    if winreg is None:
        return ""
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, subkey, 0, winreg.KEY_READ | wow64) as key:
            data, _ = winreg.QueryValueEx(key, value)
            return str(data).strip()
    except OSError:
        return ""


def _registry_uninstall_entries():
    """Yield (DisplayName, UninstallString) for every installed program, from
    both the 64-bit and 32-bit uninstall hives."""
    if winreg is None:
        return
    for base in _UNINSTALL_KEYS:
        try:
            root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base, 0, winreg.KEY_READ)
        except OSError:
            continue
        with root:
            index = 0
            while True:
                try:
                    name = winreg.EnumKey(root, index)
                except OSError:
                    break
                index += 1
                try:
                    with winreg.OpenKey(root, name) as entry:
                        display, _ = winreg.QueryValueEx(entry, "DisplayName")
                        try:
                            uninstall, _ = winreg.QueryValueEx(entry, "UninstallString")
                        except OSError:
                            uninstall = ""
                        yield str(display), str(uninstall)
                except OSError:
                    continue


def _fl_version_tuple(name: str) -> tuple:
    match = _FL_VERSION_RE.match(name)
    return tuple(int(p) for p in match.group(1).split(".")) if match else ()


def _fl_studio_candidates() -> list[tuple[str, str]]:
    """(install folder, source) for every FL Studio found, newest version first.
    Registry first (it's where the installer records the real location), then a
    folder search under Program Files for machines where the entry is missing."""
    found: dict[str, str] = {}
    for display, uninstall in _registry_uninstall_entries():
        if not _FL_VERSION_RE.match(display) or not uninstall:
            continue
        exe = uninstall.strip().strip('"')
        folder = str(Path(exe).parent)
        if (Path(folder) / "Plugins").is_dir():
            found.setdefault(folder, "registry")
    base = Path(PROGRAM_FILES) / "Image-Line"
    if base.is_dir():
        for install in base.glob("FL Studio *"):
            if _FL_VERSION_RE.match(install.name) and (install / "Plugins").is_dir():
                found.setdefault(str(install), "folder")
    ordered = sorted(found.items(), key=lambda kv: _fl_version_tuple(Path(kv[0]).name), reverse=True)
    return ordered


def detect_fl_studio_root(override: str = "") -> tuple[str, str]:
    """(install folder, how it was found). `override` is the user's own choice
    from the Configuration tab; when set it wins over detection."""
    if override.strip():
        return override.strip(), "manual"
    candidates = _fl_studio_candidates()
    if candidates:
        return candidates[0]
    return str(Path(PROGRAM_FILES) / "Image-Line" / "FL Studio"), "default"


def detect_locations(fl_studio_override: str = "") -> list[Location]:
    """Every standard plugin location for this machine, in display order."""
    fl_root, fl_source = detect_fl_studio_root(fl_studio_override)
    fl_plugins = Path(fl_root) / "Plugins" / "Fruity"

    vst_x64 = _registry_read(r"SOFTWARE\VST", "VSTPluginsPath", winreg.KEY_WOW64_64KEY) if winreg else ""
    vst_x32 = _registry_read(r"SOFTWARE\VST", "VSTPluginsPath", winreg.KEY_WOW64_32KEY) if winreg else ""
    common_vst2_x64 = os.path.join(COMMON_FILES, "VST2")
    common_vst2_x32 = os.path.join(COMMON_FILES_X86, "VST2")

    locations = [
        Location("fl_effects", "FL Studio · Effects", "FL Studio",
                 str(fl_plugins / "Effects"), fl_source, "FL-NATIVE", "effect", (), False),
        Location("fl_generators", "FL Studio · Generators", "FL Studio",
                 str(fl_plugins / "Generators"), fl_source, "FL-NATIVE", "synth", (), False),
        # Fixed format folders: the user can change their paths, but they can't be removed.
        Location("vst_x64", "VST · 64-bit", "Format folders",
                 vst_x64 or os.path.join(PROGRAM_FILES, "VstPlugins"),
                 "registry" if vst_x64 else "default", "VST2", "unknown", (".dll",)),
        Location("vst_x32", "VST · 32-bit", "Format folders",
                 vst_x32 or os.path.join(PROGRAM_FILES_X86, "VstPlugins"),
                 "registry" if vst_x32 else "default", "VST2", "unknown", (".dll",)),
        Location("vst2_x64", "VST2 · 64-bit", "Format folders",
                 common_vst2_x64, "default", "VST2", "unknown", (".dll",)),
        Location("vst2_x32", "VST2 · 32-bit", "Format folders",
                 common_vst2_x32, "default", "VST2", "unknown", (".dll",)),
        Location("vst3_x64", "VST3 · 64-bit", "Format folders",
                 os.path.join(COMMON_FILES, "VST3"), "default", "VST3", "unknown", (".vst3",)),
        Location("vst3_x32", "VST3 · 32-bit", "Format folders",
                 os.path.join(COMMON_FILES_X86, "VST3"), "default", "VST3", "unknown", (".vst3",)),
        # CLAP has no 32-bit folder convention and 64-bit hosts (FL Studio 2026
        # included) don't load 32-bit CLAP, so only the 64-bit folder is standard.
        Location("clap_x64", "CLAP · 64-bit", "Format folders",
                 os.path.join(COMMON_FILES, "CLAP"), "default", "CLAP", "unknown", (".clap",)),
        Location("aax_x64", "AAX · 64-bit", "Format folders",
                 os.path.join(COMMON_FILES, "Avid", "Audio", "Plug-Ins"), "default", "AAX", "unknown",
                 (".aaxplugin",), False),
        Location("aax_x32", "AAX · 32-bit", "Format folders",
                 os.path.join(COMMON_FILES_X86, "Avid", "Audio", "Plug-Ins"), "default", "AAX", "unknown",
                 (".aaxplugin",), False),
    ]
    for version in ("V12", "V13", "V16", "V17"):
        locations.append(Location(
            f"waves_{version.lower()}", f"Waves {version[1:]}", "Waves",
            os.path.join(PROGRAM_FILES_X86, "Waves", f"Plug-Ins {version}"), "default",
            "WAVES", "effect", (".bundle",), False,
        ))
    return locations


# Standard locations the user can edit but never remove (no Remove, no Edit dialog).
FIXED_LOCATION_KEYS = frozenset({
    "vst_x64", "vst_x32", "vst2_x64", "vst2_x32", "vst3_x64", "vst3_x32",
    "clap_x64", "aax_x64", "aax_x32",
    # FL Studio rows are driven by the FL Studio folder setting, so they aren't removable either.
    "fl_effects", "fl_generators",
})


# Compatibility view for code that still reads a flat list. The scanner itself
# reads the database (seeded from detect_locations()), not this constant.
ROOTS = [
    ScanRoot(container_path=loc.host_path, host_path=loc.host_path, format=loc.format,
             default_type=loc.default_type, extensions=loc.extensions,
             recurse_vendor_dirs=loc.recurse_vendor_dirs)
    for loc in detect_locations()
]

FORMAT_PRIORITY = {"CLAP": 3, "VST3": 2, "VST2": 1, "WAVES": 0, "AAX": 0, "FL-NATIVE": 0}

# VEXT_ROOT is set by the desktop shell to the app data folder; the fallback for
# `python -m app.main scan` / dev use is that same per-user folder (not the cwd).
_APP_DATA = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "com.vext.app"
PROJECT_ROOT = Path(os.environ.get("VEXT_ROOT", _APP_DATA))
DATA_DIR = PROJECT_ROOT / "data"
# A fresh install (or a new app data folder) has no data/ yet; create it here so
# the first database open never fails.
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = str(DATA_DIR / "plugins.db")
DATABASE_MD_PATH = str(PROJECT_ROOT / "DATABASE.md")
LOG_PATH = str(DATA_DIR / "actions.log")
QUARANTINE_DIR = str(DATA_DIR / "quarantine")

# Plugin previews: editor screenshots saved from the host window ("Set as plugin
# picture"). Per-user and regenerable.
IMAGES_DIR = PROJECT_ROOT / "images"

# FL Studio's own plugin database (it actually queries each plugin for its real
# name/vendor, which our static metadata reading sometimes can't get right for
# shared white-label engines — see AGENTS.md). Lives under the user's Documents
# folder; the Configuration tab can override it if Documents is redirected.
FL_PLUGIN_DB_ROOT = os.path.join(
    USER_HOME, "Documents", "Image-Line", "FL Studio", "Presets", "Plugin database", "Installed"
)

# Whole system drive — only ever walked when the user explicitly opts into a
# full scan (see app/full_scan.py); not part of the normal fast targeted rescan.
FULL_C_ROOT = os.environ.get("SystemDrive", "C:") + "\\"


# ---------- About ----------
# Shown in the sidebar (next to the logo) and the About window (GET /api/about).
APP_NAME = "VEXT"
APP_COMPANY = "Dianel Records"
APP_CREATOR = "SoDianel"


def app_version() -> str:
    """The version: the desktop shell passes its own (VEXT_VERSION, from
    tauri.conf.json) to the sidecar; a dev backend reads tauri.conf.json."""
    if os.environ.get("VEXT_VERSION"):
        return os.environ["VEXT_VERSION"]
    try:
        import json

        conf = Path(__file__).resolve().parent.parent / "desktop" / "src-tauri" / "tauri.conf.json"
        return json.loads(conf.read_text(encoding="utf-8")).get("version") or ""
    except (OSError, ValueError):
        return ""
