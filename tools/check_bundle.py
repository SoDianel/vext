"""Fails the build if the PyInstaller bundle carries its own copy of the
Microsoft C++ runtime.

Plugins load into VEXT's processes and share one msvcp140.dll per process. A
private copy inside the bundle shadows the system's and ages: the 2017 copy
(14.16) a build once picked up from the PATH crashed every plugin. VEXT's
installer installs Microsoft's current runtime instead (tools/fetch_redist.ps1),
and vext-backend.spec / vext-host32.spec strip the DLLs; this checks the result.

  python tools/check_bundle.py dist/vext-backend.exe
  python tools/check_bundle.py dist/host32          (a one-folder bundle)
"""

import sys
from pathlib import PurePath

# Bare names only: numpy and friends vendor their own hash-suffixed copies
# (msvcp140-<hash>.dll), loaded under that private name, which can't shadow anything.
FORBIDDEN = {
    "msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll", "msvcp140_atomic_wait.dll",
    "msvcp140_codecvt_ids.dll", "vcruntime140.dll", "vcruntime140_1.dll", "concrt140.dll",
    "vccorlib140.dll", "msvcr120.dll", "msvcp120.dll", "msvcr110.dll", "msvcp110.dll",
    "msvcr100.dll", "msvcp100.dll",
}


def main(path: str) -> int:
    import os

    if os.path.isdir(path):  # a one-folder bundle (the 32-bit host, vext-host32.spec)
        names = [os.path.relpath(os.path.join(d, f), path) for d, _, files in os.walk(path) for f in files]
    else:
        from PyInstaller.archive.readers import CArchiveReader

        names = list(CArchiveReader(path).toc)
    found = sorted(n for n in names if PurePath(n.replace("\\", "/")).name.lower() in FORBIDDEN)
    if found:
        print(f"{path} carries a private C++ runtime: " + ", ".join(found))
        print("Strip it in vext-backend.spec (_CRT_DLLS); the installer provides the runtime.")
        return 1
    print(f"{path}: no private C++ runtime ({len(names)} entries checked)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "dist/vext-backend.exe"))
