# Rebuilds the Python backend into the Tauri sidecar binary. Run this after
# any change under app/ (or sidecar_main.py itself) and before `tauri dev`/
# `tauri build` — desktop/src-tauri/binaries/ is gitignored (build output,
# not source), so a fresh checkout needs this run at least once too.
#
# See AGENTS.md's "Docker-to-native migration" section for why this exists:
# the backend now runs as a native Windows process (PyInstaller-bundled),
# spawned by the Tauri app itself, instead of in a Docker container.

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

if (-not (Test-Path "$root\.venv-native")) {
    Write-Host "Creating .venv-native (one-time)..."
    python -m venv "$root\.venv-native"
    & "$root\.venv-native\Scripts\python.exe" -m pip install --quiet --upgrade pip
    & "$root\.venv-native\Scripts\python.exe" -m pip install --quiet -r "$root\requirements.txt" pyinstaller
}

Write-Host "Building backend sidecar..."
Push-Location $root
# Runs the checked-in vext-backend.spec (not the plain CLI-flags form) — it carries a
# binaries filter that strips a stale, ABI-incompatible bundled MSVCP140.dll that used
# to crash every plugin open with the same access violation; see the spec's own header.
& "$root\.venv-native\Scripts\python.exe" -m PyInstaller --noconfirm vext-backend.spec
# Never ship a private C++ runtime (it crashed every plugin once): the installer provides it.
& "$root\.venv-native\Scripts\python.exe" tools\check_bundle.py dist\vext-backend.exe
if ($LASTEXITCODE -ne 0) { Pop-Location; throw "The bundle carries a private C++ runtime" }
Pop-Location

$dest = "$root\desktop\src-tauri\binaries"
New-Item -ItemType Directory -Force -Path $dest | Out-Null
Copy-Item "$root\dist\vext-backend.exe" "$dest\vext-backend-x86_64-pc-windows-msvc.exe" -Force

Write-Host "Done: $dest\vext-backend-x86_64-pc-windows-msvc.exe"

# The 32-bit plugin host (32-bit plugins), shipped beside the backend as host32\.
& "$root\tools\build_host32.ps1"
