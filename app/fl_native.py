"""Read-only eligibility and isolated workspace for experimental native plugins."""

import hashlib
import json
import os
import shutil
import sys
import uuid
from functools import lru_cache
from pathlib import Path

from app.config import PROJECT_ROOT

ADAPTER_VERSION = 4

EDITOR_NOTE = "Experimental FL native editor only. Audio, MIDI and Analyzer routing are unavailable. Settings are isolated from FL Studio."
CLOCK_NOTE = "Experimental FL native clock. Follows Playground tempo and playback; no audio processing."
MIDI_NOTE = "Experimental FL native instrument. MIDI, audio and Analyzer are available. Settings are isolated from FL Studio."
AUDIO_NOTE = "Experimental FL native effect. Audio and Analyzer are available. Settings are isolated from FL Studio."
AUDITION_NOTE = "Experimental FL native editor playback. Monitor uses the editor?s own clip and transport; MIDI and browser/file routing are unavailable."
MANIFEST = Path(__file__).parent / "hosting" / "fl-native-compat.json"
WORKSPACES = PROJECT_ROOT / "cache" / "fl-native"
REGISTRY_ROOT = r"Software\VEXT\NativeEditors"


class NativeUnavailable(ValueError):
    pass


def adapter_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "fl-native" / "vext-fl-native.dll"
    return Path(__file__).resolve().parents[1] / "build" / "fl-native" / "vext-fl-native.dll"


@lru_cache(maxsize=1)
def compatibility() -> dict:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if data.get("adapter") != ADAPTER_VERSION:
        raise NativeUnavailable("The native compatibility table needs to match the adapter version.")
    return {p["relative"].casefold(): p for p in data["plugins"]}


@lru_cache(maxsize=256)
def _digest(path: str, size: int, mtime_ns: int, ctime_ns: int) -> str:
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def resolve(host_path: str, *, refresh: bool = False) -> tuple[Path, Path, dict]:
    """Select only a recorded primary binary at its exact Installed native path."""
    candidate = Path(host_path)
    folder = candidate if candidate.is_dir() else candidate.parent
    if folder.parent.name.casefold() not in ("effects", "generators"):
        raise NativeUnavailable("The native plugin folder is outside FL Studio's Effects/Generators tree.")
    root = folder.parent.parent
    if root.name.casefold() != "fruity" or root.parent.name.casefold() != "plugins":
        raise NativeUnavailable("The native plugin folder is outside FL Studio's Plugins/Fruity tree.")
    prefix = f"{folder.parent.name}/{folder.name}/".casefold()
    entries = [e for key, e in compatibility().items() if key.startswith(prefix)]
    if len(entries) != 1:
        raise NativeUnavailable("This FL native plugin has not been validated for the experimental editor.")
    entry = entries[0]
    binary = folder / entry["relative"].split("/")[-1]
    if not candidate.is_dir() and os.path.normcase(str(candidate.absolute())) != os.path.normcase(str(binary.absolute())):
        raise NativeUnavailable("The selected binary is not this plugin's validated native editor.")
    try:
        if binary.is_symlink() or any(p.is_junction() for p in (folder, folder.parent, root)):
            raise NativeUnavailable("Linked native plugin folders are not supported by the experimental editor.")
        st = binary.stat()
        if refresh:
            with binary.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
        else:
            digest = _digest(str(binary), st.st_size, st.st_mtime_ns, st.st_ctime_ns)
    except OSError as exc:
        raise NativeUnavailable("The native plugin binary is missing or cannot be read.") from exc
    if digest != entry["sha256"]:
        raise NativeUnavailable("This native plugin version has not been validated for the experimental editor.")
    if not entry["editor"]:
        raise NativeUnavailable(entry["reason"])
    if not adapter_path().is_file():
        raise NativeUnavailable("The native editor adapter is missing. Rebuild or reinstall VEXT.")
    return binary, root, entry


def limitations(entry: dict) -> list[dict]:
    """Presentation only: these notices never grant host capabilities."""
    return [dict(item) for item in entry.get("limitations", [])]


def issue(host_path: str) -> dict | None:
    try:
        resolve(host_path)
    except (NativeUnavailable, OSError, ValueError) as exc:
        return {"code": "native_unavailable", "reason": str(exc)}
    return None


def workspace_path(value: str) -> Path:
    path = Path(value).absolute()
    if path.parent.resolve() != WORKSPACES.resolve() or len(path.name) != 32:
        raise ValueError("invalid native editor workspace")
    if any(c not in "0123456789abcdef" for c in path.name):
        raise ValueError("invalid native editor workspace")
    if path.is_symlink() or path.is_junction():
        raise ValueError("linked native editor workspace")
    return path


def new_workspace() -> str:
    return str(WORKSPACES / uuid.uuid4().hex)


def cleanup(value: str) -> None:
    """Called only once the worker is gone, including crashes and forced closes."""
    import winreg

    path = workspace_path(value)
    key = REGISTRY_ROOT + "\\" + path.name

    def remove_key(name):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, name, 0, winreg.KEY_READ | winreg.KEY_WRITE) as handle:
                while True:
                    try:
                        child = winreg.EnumKey(handle, 0)
                    except OSError as exc:
                        if exc.winerror != 259:
                            raise
                        break
                    remove_key(name + "\\" + child)
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, name)
        except FileNotFoundError:
            pass

    remove_key(key)
    if path.exists():
        shutil.rmtree(path)


def _prepare_patcher_list(workspace: Path) -> None:
    # A preset's filename cannot establish which binaries or graph it loads.
    # Reserve isolated picker folders until nested targets can be validated.
    for kind in ("generators", "effects"):
        (workspace / "patcher-list" / kind).mkdir(parents=True, exist_ok=True)


def prepare_resources(workspace: Path, factory_root: Path, entry: dict) -> None:
    if Path(entry["relative"]).stem.casefold() == "patcher_x64":
        _prepare_patcher_list(workspace)
        return
    if Path(entry["relative"]).stem.casefold() not in ("fl studio mobile rack fx_x64", "fl studio mobile rack_x64", "fl studio mobile_x64"):
        return
    source = factory_root / "Generators" / "FL Studio Mobile" / "Installed"
    destination = workspace / "path-selector-1048"
    # Only the observed editor dependencies, without samples, catalogs or settings.
    resources = ("SampleVertexShader.cso", "SamplePixelShader.cso", "TexturePixelShader.cso",
                 "CirclePixelShader.cso", "FeatherPixelShader.cso", "FontPixelShader.cso",
                 "title.png", "wrap.png", "gamepad.png", "gamepad_fill.png",
                 "oscdata.dat", "FilterTable_Ctf_Res.data", "Fonts/OpenSans_SemiCondensed-SemiBold.ttf")
    for name in resources:
        item = source / name
        # Validate every parent too: a font below a junction must not escape.
        for parent in (item, *item.parents):
            if parent.is_symlink() or parent.is_junction():
                raise NativeUnavailable("An installed editor resource leaves its expected folder.")
        if not item.is_file() or item.stat().st_size > 1024 * 1024:
            raise NativeUnavailable("An installed editor resource is missing or exceeds its limit.")
        copied = destination / name
        copied.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(item, copied)
