"""What a plugin binary needs from Windows to load: its architecture and the
DLLs it imports that can't be found. Static (pefile), so it never runs plugin
code; used to explain a failed load ("a DLL it needs is missing: X.dll") and by
the worker to add the plugin's own folders to the DLL search path.

VST3 bundles (`Name.vst3\\Contents\\x86_64-win\\Name.vst3`) are resolved to
their binary here too.
"""

import functools
import os
import sys

MACHINES = {0x8664: "x64", 0x14C: "x86", 0xAA64: "arm64"}
# This process's own architecture: the 32-bit worker hosts the "x86" plugins.
PROCESS_ARCH = "x64" if sys.maxsize > 2**32 else "x86"
_SYSTEM = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
_WINDOWS = os.environ.get("SystemRoot", r"C:\Windows")


def vst3_binary(path: str) -> str | None:
    """The DLL inside a VST3: the file itself, or a bundle's x86_64-win binary
    (the bundle's own name first, else the only .vst3 there). None if absent."""
    if os.path.isfile(path):
        return path
    if not os.path.isdir(path):
        return None
    folder = os.path.join(path, "Contents", "x86_64-win")
    if not os.path.isdir(folder):
        return None
    own = os.path.join(folder, os.path.basename(path))
    if os.path.isfile(own):
        return own
    found = [f for f in os.listdir(folder) if f.lower().endswith(".vst3") and os.path.isfile(os.path.join(folder, f))]
    return os.path.join(folder, found[0]) if len(found) == 1 else None


def bundle_arches(path: str) -> list[str]:
    """The Contents\\<arch> folders of a VST3 bundle (e.g. ['x86-win'] for a 32-bit only one)."""
    contents = os.path.join(path, "Contents")
    if not os.path.isdir(contents):
        return []
    return sorted(d for d in os.listdir(contents) if d.endswith("-win") and os.path.isdir(os.path.join(contents, d)))


def search_dirs(binary: str) -> list[str]:
    """Folders worth adding to the DLL search path for a plugin: the binary's
    own folder, the bundle's folder for a VST3 bundle, and the vendor's
    folder above it (several vendors keep shared DLLs there)."""
    out = []
    folder = os.path.dirname(os.path.abspath(binary))
    out.append(folder)
    parts = folder.split(os.sep)
    low = [p.lower() for p in parts]
    if "contents" in low:  # ...\Name.vst3\Contents\x86_64-win
        i = len(low) - 1 - low[::-1].index("contents")
        bundle = os.sep.join(parts[:i])
        out += [bundle, os.path.dirname(bundle)]
    seen, unique = set(), []
    for d in out:
        key = d.lower()
        if d and key not in seen and os.path.isdir(d):
            seen.add(key)
            unique.append(d)
    # Never the shared VST3 / CLAP / VstPlugins roots themselves: other vendors' DLLs live under them.
    roots = {"vst3", "clap", "vstplugins", "vst2", "vst", "common files", "program files", "program files (x86)"}
    return [d for d in unique if os.path.basename(d).lower() not in roots]


@functools.lru_cache(maxsize=None)
def _known_dlls() -> frozenset:
    try:
        import winreg

        names = set()
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SYSTEM\CurrentControlSet\Control\Session Manager\KnownDLLs") as key:
            i = 0
            while True:
                try:
                    _, value, _ = winreg.EnumValue(key, i)
                except OSError:
                    break
                if isinstance(value, str):
                    names.add(value.lower())
                i += 1
        return frozenset(names)
    except OSError:
        return frozenset()


def _pe(path: str):
    import pefile

    pe = pefile.PE(path, fast_load=True)
    pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"],
                                           pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT"]])
    return pe


def architecture(path: str) -> str | None:
    """'x64', 'x86', 'arm64' or None (not a PE file)."""
    try:
        import pefile

        pe = pefile.PE(path, fast_load=True)
        try:
            return MACHINES.get(pe.FILE_HEADER.Machine, hex(pe.FILE_HEADER.Machine))
        finally:
            pe.close()
    except Exception:  # noqa: BLE001
        return None


def wrong_arch(path: str) -> str | None:
    """Why this process can't load `path` because of its architecture, or None."""
    arch = architecture(path)
    if arch is None or arch == PROCESS_ARCH:
        return None
    if arch == "x86":
        return "it is a 32-bit plugin (this host is 64-bit)"
    if arch == "x64":
        return "it is a 64-bit plugin (this host is 32-bit)"
    return f"it is built for {arch}, not {PROCESS_ARCH}"


def imports(path: str) -> tuple[list[str], list[str]]:
    """(static imports, delay-loaded imports) of a PE file."""
    pe = _pe(path)
    try:
        static = [e.dll.decode("ascii", "replace") for e in getattr(pe, "DIRECTORY_ENTRY_IMPORT", [])]
        delayed = [e.dll.decode("ascii", "replace") for e in getattr(pe, "DIRECTORY_ENTRY_DELAY_IMPORT", [])]
        return static, delayed
    finally:
        pe.close()


def _resolve(name: str, dirs: list[str]) -> str | None:
    low = name.lower()
    if low.startswith(("api-ms-win-", "ext-ms-")):
        return "apiset"  # virtual: resolved by the API set schema
    if low in _known_dlls():
        return os.path.join(_SYSTEM, name)
    for d in dirs:
        candidate = os.path.join(d, name)
        if os.path.isfile(candidate):
            return candidate
    return None


def missing_dlls(binary: str, extra_dirs: tuple = (), include_delayed: bool = False, depth: int = 3) -> list[str]:
    """The DLLs `binary` (or a DLL it brings along) imports that aren't in its
    folder, the extra folders, the interpreter's folder, System32, Windows or
    PATH. Delay-loaded ones only on request: they are often optional."""
    dirs = [os.path.dirname(os.path.abspath(binary)), *extra_dirs,
            os.path.dirname(sys.executable), getattr(sys, "_base_executable", "") and
            os.path.dirname(sys._base_executable), _SYSTEM, _WINDOWS,
            *[p for p in os.environ.get("PATH", "").split(os.pathsep) if p]]
    dirs = [d for d in dirs if d]
    missing, seen = [], set()

    def visit(path: str, level: int) -> None:
        try:
            static, delayed = imports(path)
        except Exception:  # noqa: BLE001 - not a PE file / unreadable: nothing to say
            return
        for name in static + (delayed if include_delayed else []):
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            found = _resolve(name, dirs)
            if found is None:
                missing.append(name)
            elif level < depth and found != "apiset" and not found.lower().startswith(_WINDOWS.lower()):
                visit(found, level + 1)  # a DLL shipped with the plugin: check what it needs too

    visit(binary, 0)
    return missing
