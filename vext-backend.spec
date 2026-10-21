# -*- mode: python ; coding: utf-8 -*-
#
# Maintained by hand (see build_sidecar.ps1 / build_release.ps1, which run
# `pyinstaller vext-backend.spec` instead of the long --collect-all CLI form
# they used before) specifically to carry the `a.binaries` filter below —
# PyInstaller regenerates a throwaway version of this file from CLI flags on
# every plain `pyinstaller ... sidecar_main.py` run, which would silently
# drop a hand edit the next time someone changes a --collect-all flag.
#
# The filter: every plugin-open crashed with the exact same access violation
# (0xc0000005) inside a bundled MSVCP140.dll — confirmed via Windows's own
# crash log (Get-WinEvent, Application Error / id 1000), same fixed offset
# every time regardless of which plugin, which ruled out a per-plugin cause.
# PyInstaller's dependency walk picked up a stray old MSVCP140.dll
# (14.16.27033.0, a 2019 VS2017 build) from somewhere on this machine's build
# PATH — several Python installs and other tools share this PATH, any one of
# which may vendor its own copy — and bundles it into the onefile EXE, where
# it *shadows* the correct, newer one already in System32 (14.51.x here) for
# every process launched from that EXE. Pedalboard's native plugin-hosting
# code apparently isn't ABI-compatible with that specific old copy. Stripping
# it (and its usual siblings) from the bundle makes Windows fall through to
# the already-installed system redistributable instead — the normal way a
# Windows app is supposed to get its C++ runtime anyway, rather than carrying
# a private copy that can be older than what the machine already has.
# VEXT's installer guarantees that system copy: it ships Microsoft's current
# runtime installers and runs them when one is missing or older
# (tools/fetch_redist.ps1, desktop/src-tauri/installer/), the app checks it at
# every start (app/runtime_check.py, Repair), and the build fails if a private
# copy sneaks back into the bundle (tools/check_bundle.py).
from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.hooks import collect_all

datas = [('app/static', 'app/static')]
binaries = []
hiddenimports = []
hiddenimports += collect_submodules('app.hosting')
tmp_ret = collect_all('fastapi')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('uvicorn')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('pydantic')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('pedalboard')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('sounddevice')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('_sounddevice_data')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('av')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('websockets')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('PIL')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['sidecar_main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

# See the module docstring above — drop any bundled copy of the core VC++
# runtime DLLs (bare names only, e.g. not numpy's own hash-suffixed vendored
# copies, which are harmless/isolated) so the system's own copy is used.
_CRT_DLLS = {"msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll", "concrt140.dll"}
a.binaries = [b for b in a.binaries if b[0].lower() not in _CRT_DLLS]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='vext-backend',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
