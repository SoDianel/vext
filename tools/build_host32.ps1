# Builds the 32-bit plugin host (vext-host32.spec) into desktop\src-tauri\host32\,
# which the installer ships as $INSTDIR\host32\ (tauri.conf.json > bundle >
# resources). The backend starts host32\vext-host32.exe for 32-bit plugins
# (plugin_host.x86_worker). Run by build_sidecar.ps1 and build_release.ps1;
# alone: .\tools\build_host32.ps1

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$python = Join-Path $root ".python-x86\python.exe"

& (Join-Path $PSScriptRoot "setup_x86_host.ps1")
& $python -m pip install --disable-pip-version-check --no-warn-script-location --quiet pyinstaller
if ($LASTEXITCODE -ne 0) { throw "Installing PyInstaller into .python-x86 failed" }

Write-Host "==> Building the 32-bit plugin host (PyInstaller)"
Push-Location $root
try {
    # Native tools write progress to stderr: run through cmd, judge by exit code.
    cmd /c "`"$python`" -m PyInstaller --noconfirm --workpath build\host32 --distpath dist vext-host32.spec 2>&1"
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed (exit $LASTEXITCODE)" }
    # Never ship a private C++ runtime (see vext-backend.spec): the installer provides it.
    & $python tools\check_bundle.py dist\host32
    if ($LASTEXITCODE -ne 0) { throw "The 32-bit host carries a private C++ runtime" }
} finally { Pop-Location }

$dest = Join-Path $root "desktop\src-tauri\host32"
if (Test-Path $dest) { Remove-Item -Recurse -Force $dest }
Copy-Item (Join-Path $root "dist\host32") $dest -Recurse
Write-Host "Done: $dest\vext-host32.exe"
