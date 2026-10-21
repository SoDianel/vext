# -*- mode: python ; coding: utf-8 -*-
#
# The 32-bit plugin host (host32_main.py), built by tools/build_host32.ps1 with
# the 32-bit Python in .python-x86 (never with .venv-native: PyInstaller builds
# for the interpreter that runs it). One folder, not one file: a onefile exe
# unpacks itself on every start, and each 32-bit plugin starts its own host.
#
# Same rule as vext-backend.spec: no private Microsoft C++ runtime in the bundle
# (plugins share one per process; a stale private copy crashed every plugin
# once). The installer installs Microsoft's x86 runtime (vc_redist.x86.exe,
# tools/fetch_redist.ps1), and tools/check_bundle.py checks the result.
from PyInstaller.utils.hooks import collect_all, collect_submodules

datas = []
binaries = []
hiddenimports = collect_submodules('app.hosting') + ['app.analysis_tap']
for package in ('sounddevice', '_sounddevice_data', 'PIL'):
    tmp_ret = collect_all(package)
    datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]

a = Analysis(
    ['host32_main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Not in the 32-bit Python, and never needed by this host (only the 64-bit one).
    excludes=['pedalboard', 'av', 'fastapi', 'uvicorn', 'pydantic', 'websockets', 'tkinter'],
    noarchive=False,
    optimize=0,
)

_CRT_DLLS = {"msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll", "concrt140.dll"}
a.binaries = [b for b in a.binaries if b[0].lower() not in _CRT_DLLS]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='vext-host32',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='host32',
)
