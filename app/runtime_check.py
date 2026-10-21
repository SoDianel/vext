"""Is Microsoft's Visual C++ runtime on this PC good enough to host plugins?

Every plugin VEXT opens loads into a worker process together with pedalboard,
and all of them share ONE msvcp140.dll per process: Windows' copy (VEXT
deliberately carries none, tools/check_bundle.py). Newer copies run code
built with older compilers, never the reverse: since 14.40 (Visual Studio
17.10) code built with the new compiler crashes inside an older msvcp140
(the std::mutex change), so an old, missing or damaged runtime makes plugins
crash on open. VEXT's installer installs Microsoft's current runtime
(desktop/src-tauri/installer/install-runtimes.ps1); this module checks what's
there at every start and runs the same script again for the Repair button.

  status() -> {ok, problem, version, path, minimum, registry, legacy, repair}
  repair() -> {ok, code, restart, message}   (asks Windows for permission)
"""

import ctypes
import logging
import os
import subprocess
import sys
import winreg
from ctypes import wintypes
from pathlib import Path

log = logging.getLogger(__name__)

MINIMUM = (14, 40)  # the std::mutex ABI change: anything older crashes current plugins
DLLS = ("msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll")
# Older runtimes some older plugins need (installed with VEXT; informative here). Checked by
# their DLL: their x64 installers register under WOW6432Node, which a 64-bit lookup misses.
LEGACY = {"2013": "msvcr120.dll", "2012": "msvcr110.dll", "2010": "msvcr100.dll"}
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_version = ctypes.WinDLL("version", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_version.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
_version.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
_version.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p),
                                    ctypes.POINTER(wintypes.UINT)]
_kernel32.GetModuleFileNameW.argtypes = [wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]


class _FixedFileInfo(ctypes.Structure):
    _fields_ = [("dwSignature", wintypes.DWORD), ("dwStrucVersion", wintypes.DWORD),
                ("dwFileVersionMS", wintypes.DWORD), ("dwFileVersionLS", wintypes.DWORD),
                ("dwProductVersionMS", wintypes.DWORD), ("dwProductVersionLS", wintypes.DWORD)]


def file_version(path: str) -> tuple | None:
    """(major, minor, build, revision) of a PE file, or None."""
    size = _version.GetFileVersionInfoSizeW(path, None)
    if not size:
        return None
    buf = ctypes.create_string_buffer(size)
    if not _version.GetFileVersionInfoW(path, 0, size, buf):
        return None
    ptr, length = ctypes.c_void_p(), wintypes.UINT()
    if not _version.VerQueryValueW(buf, "\\", ctypes.byref(ptr), ctypes.byref(length)) or not ptr.value:
        return None
    info = ctypes.cast(ptr, ctypes.POINTER(_FixedFileInfo)).contents
    ms, ls = info.dwFileVersionMS, info.dwFileVersionLS
    return (ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF)


def _loaded(name: str) -> tuple[str | None, str | None]:
    """(path, error) of `name` as this process's loader resolves it: the copy plugins get."""
    try:
        dll = ctypes.WinDLL(name)
    except OSError as e:
        return None, str(e)
    buf = ctypes.create_unicode_buffer(1024)
    n = _kernel32.GetModuleFileNameW(dll._handle, buf, len(buf))
    return (buf.value if n else None), None


def _reg(key: str) -> dict:
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
            out = {}
            for name in ("Installed", "Version", "Major", "Minor", "Bld"):
                try:
                    out[name] = winreg.QueryValueEx(k, name)[0]
                except OSError:
                    pass
            return out
    except OSError:
        return {}


def redist_dir() -> Path | None:
    """Where the shipped runtime installers are: next to the backend (installed and
    portable builds), else the build's desktop/src-tauri/redist (a dev run)."""
    here = Path(sys.executable).parent if getattr(sys, "frozen", False) else None
    for d in filter(None, [here and here / "redist",
                           Path(__file__).resolve().parent.parent / "desktop" / "src-tauri" / "redist"]):
        if (d / "install-runtimes.ps1").exists() and (d / "vc_redist.x64.exe").exists():
            return d
    return None


def _fmt(v) -> str:
    return ".".join(str(x) for x in v) if v else ""


def status() -> dict:
    found, problem = {}, ""
    for name in DLLS:
        path, error = _loaded(name)
        version = file_version(path) if path else None
        found[name] = {"path": path, "version": _fmt(version), "error": error}
        if not path:
            problem = problem or f"{name} is missing ({error})"
        elif not version:
            problem = problem or f"{name} looks damaged (no readable version): {path}"
    main = found["msvcp140.dll"]
    version = tuple(int(x) for x in main["version"].split(".")) if main["version"] else None
    if not problem and version and version[:2] < MINIMUM:
        problem = (f"msvcp140.dll is {main['version']}, too old: plugins need {MINIMUM[0]}.{MINIMUM[1]} or newer "
                   f"({main['path']})")
    reg = _reg(r"SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64")
    if not problem and reg.get("Installed") != 1:
        # The DLLs answer but Windows has no record of the runtime: a copy dropped in by
        # some program, not an install that Windows Update keeps current.
        problem = "the Visual C++ runtime isn't registered as installed (a stray copy of its files)"
    system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    legacy = {year: (system32 / dll).exists() for year, dll in LEGACY.items()}
    shipped = None
    d = redist_dir()
    if d:
        shipped = _fmt(file_version(str(d / "vc_redist.x64.exe")))
    return {"ok": not problem, "problem": problem, "version": main["version"], "path": main["path"],
            "minimum": f"{MINIMUM[0]}.{MINIMUM[1]}", "dlls": found, "registry": reg.get("Version") or "",
            "legacy": legacy, "repair": bool(d), "shipped": shipped}


def repair() -> dict:
    """Runs the shipped install-runtimes.ps1 -Repair elevated (Windows asks first)
    and waits for it. 3010: done, Windows wants a restart."""
    d = redist_dir()
    if not d:
        return {"ok": False, "code": None, "restart": False,
                "message": "This copy of VEXT doesn't include the runtime installers (redist folder). "
                           "Install VEXT with its setup.exe, or install Microsoft's Visual C++ "
                           "Redistributable (x64) from microsoft.com."}
    script, logfile = d / "install-runtimes.ps1", Path(os.environ.get("TEMP", str(d))) / "vext-runtimes.log"
    inner = (f"-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File \"{script}\" -Repair "
             f"-Log \"{logfile}\"")
    command = ("try { $p = Start-Process powershell.exe -Verb RunAs -Wait -PassThru -WindowStyle Hidden "
               f"-ArgumentList '{inner}'; exit $p.ExitCode }} catch {{ exit 1223 }}")
    try:
        done = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
                              creationflags=_NO_WINDOW, capture_output=True, timeout=900)
        code = done.returncode
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "code": None, "restart": False, "message": f"Couldn't run the repair: {e}"}
    if code == 1223:
        return {"ok": False, "code": code, "restart": False,
                "message": "Windows didn't give permission, so nothing was changed."}
    if code not in (0, 3010):
        log.warning("Visual C++ runtime repair failed (exit %s); see %s", code, logfile)
        return {"ok": False, "code": code, "restart": False,
                "message": f"The repair didn't finish (exit {code}). Details: {logfile}"}
    log.info("Visual C++ runtime repaired (exit %s)", code)
    return {"ok": True, "code": code, "restart": True,
            "message": "The Visual C++ runtime is repaired. Restart VEXT"
                       + (" and Windows" if code == 3010 else "") + " so plugins use it."}
