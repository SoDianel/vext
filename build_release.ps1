# Builds a complete, distributable VEXT for Windows 10/11 (x64) and
# collects every file a user needs into .\release\:
#
#   release\VEXT-<version>-setup.exe        Installer (per-user). Carries everything a
#                                           PC needs: WebView2 (offline) and Microsoft's
#                                           Visual C++ runtimes (installed if missing or
#                                           older; Windows asks for permission once)
#   release\VEXT-<version>-portable\        Unzip-and-run folder (vext.exe +
#                                           vext-backend.exe, which must sit together,
#                                           and redist\ for the in-app runtime Repair)
#   release\SHA256SUMS.txt                         Checksums for every shipped file
#
# Also checks the version (tauri.conf.json = Cargo.toml = package.json) and, when
# VEXT_SIGN_PFX or VEXT_SIGN_AZURE_DLIB is set, signs the exes and installer (tools\sign_file.ps1).
#
# Steps: 0) refresh the logo files from sources\ (update_logo.ps1) and fetch
# Microsoft's runtime installers (tools\fetch_redist.ps1, signature-checked),
# 1) bundle the Python backend into a single sidecar exe (PyInstaller) and check
# it carries no private C++ runtime (tools\check_bundle.py), and the 32-bit plugin
# host into host32\ (tools\build_host32.ps1, same check), 2) build the Tauri
# desktop shell and its installer, 3) collect and verify.
# No MSI: an MSI can't run the runtime installers; setup.exe can.
# Run from the repository root:  .\build_release.ps1
#
# Prerequisites (one-time, see LOCAL_SETUP.md): Python 3.10+, Node.js, Rust (rustup),
# and the MSVC C++ Build Tools.

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$desktop = Join-Path $root "desktop"
$tauriDir = Join-Path $desktop "src-tauri"
$release = Join-Path $root "release"

# Native tools write progress to stderr. Under $ErrorActionPreference = "Stop"
# PowerShell turns that into a terminating error, so run them through cmd and
# judge success by exit code only.
function Invoke-Native([string]$label, [string]$command) {
    Write-Host "==> $label"
    cmd /c "$command 2>&1"
    if ($LASTEXITCODE -ne 0) { throw "$label failed (exit $LASTEXITCODE)" }
}

$version = (Get-Content (Join-Path $tauriDir "tauri.conf.json") -Raw | ConvertFrom-Json).version

# One version everywhere: tauri.conf.json is the source (app/config.py reads it), and the Cargo and
# npm manifests must agree with it, or the installer and the app would show different numbers.
$cargoVersion = (Select-String -Path (Join-Path $tauriDir "Cargo.toml") -Pattern '^version\s*=\s*"([^"]+)"' | Select-Object -First 1).Matches[0].Groups[1].Value
$npmVersion = (Get-Content (Join-Path $desktop "package.json") -Raw | ConvertFrom-Json).version
if ($cargoVersion -ne $version -or $npmVersion -ne $version) {
    throw "Version mismatch: tauri.conf.json $version, Cargo.toml $cargoVersion, package.json $npmVersion. Make them equal and rerun."
}

# Optional code signing: runs only when VEXT_SIGN_PFX or VEXT_SIGN_AZURE_DLIB is set (tools\sign_file.ps1).
$signing = [bool]($env:VEXT_SIGN_PFX -or $env:VEXT_SIGN_AZURE_DLIB)
$signScript = Join-Path $root "tools\sign_file.ps1"
if ($signing) { Write-Host "==> Code signing is on" } else { Write-Host "==> Code signing is off (no VEXT_SIGN_* variables): the builds will be unsigned" -ForegroundColor Yellow }
$venvPython = Join-Path $root ".venv-native\Scripts\python.exe"

# 0) Logo: app icons and the in-app logo from sources\ ----------------------
& (Join-Path $root "update_logo.ps1")
& (Join-Path $root "tools\fetch_redist.ps1")

# 1) Python backend -> sidecar ----------------------------------------------
if (-not (Test-Path $venvPython)) {
    Invoke-Native "Creating .venv-native" "python -m venv `"$root\.venv-native`""
    Invoke-Native "Installing backend dependencies" "`"$venvPython`" -m pip install --upgrade pip"
    Invoke-Native "Installing backend dependencies" "`"$venvPython`" -m pip install -r `"$root\requirements.txt`" pyinstaller"
}
Push-Location $root
try {
    # Runs the checked-in vext-backend.spec (not the plain CLI-flags form) — it
    # carries a binaries filter that strips a stale, ABI-incompatible bundled
    # MSVCP140.dll; see the spec file's own header comment for why.
    Invoke-Native "Bundling backend sidecar (PyInstaller)" "`"$venvPython`" -m PyInstaller --noconfirm vext-backend.spec"
    # A private copy of the C++ runtime in the bundle crashed every plugin once: never again.
    Invoke-Native "Checking the bundle for a private C++ runtime" "`"$venvPython`" tools\check_bundle.py dist\vext-backend.exe"
} finally { Pop-Location }

$sidecarDir = Join-Path $tauriDir "binaries"
New-Item -ItemType Directory -Force -Path $sidecarDir | Out-Null
$sidecar = Join-Path $sidecarDir "vext-backend-x86_64-pc-windows-msvc.exe"
Copy-Item (Join-Path $root "dist\vext-backend.exe") $sidecar -Force

# The 32-bit plugin host -> desktop\src-tauri\host32 (a bundle resource: $INSTDIR\host32)
& (Join-Path $root "tools\build_host32.ps1")

# 2) Desktop shell and installers -----------------------------------------
Push-Location $desktop
try {
    # With signing on, Tauri signs VEXT.exe and the installer itself through a throwaway config
    # (*.local.json is git-ignored).
    $tauriArgs = ""
    if ($signing) {
        $signCfg = Join-Path $tauriDir "sign.local.json"
        $cmd = "powershell -NoProfile -ExecutionPolicy Bypass -File `"$signScript`" `"%1`""
        @{ bundle = @{ windows = @{ signCommand = $cmd } } } | ConvertTo-Json -Depth 5 | Set-Content $signCfg -Encoding utf8
        $tauriArgs = " -- --config src-tauri/sign.local.json"
    }
    try {
        Invoke-Native "Building desktop app and installers (Tauri)" "npm run tauri build$tauriArgs"
    } finally {
        if ($signing) { Remove-Item (Join-Path $tauriDir "sign.local.json") -ErrorAction SilentlyContinue }
    }
} finally { Pop-Location }

$bundle = Join-Path $tauriDir "target\release\bundle"
$exe = Join-Path $tauriDir "target\release\vext.exe"
$nsis = Get-ChildItem (Join-Path $bundle "nsis") -Filter "*-setup.exe" | Select-Object -First 1
if (-not $nsis -or -not (Test-Path $exe)) {
    throw "Expected installer or exe output is missing under $bundle"
}

# 3) Collect into release\ --------------------------------------------------
# Emptied, not deleted: Windows Sandbox (tools\sandbox) maps this folder and can hold it.
New-Item -ItemType Directory -Force -Path $release | Out-Null
Get-ChildItem $release -Force | Remove-Item -Recurse -Force

$setupName = "VEXT-$version-setup.exe"
Copy-Item $nsis.FullName (Join-Path $release $setupName)

# The portable build is two files that must sit side by side: the Tauri shell
# starts the backend from its own folder. Tauri names the sidecar without the
# target triple at runtime, so ship it under that name.
$portable = Join-Path $release "VEXT-$version-portable"
New-Item -ItemType Directory -Force -Path $portable | Out-Null
Copy-Item $exe (Join-Path $portable "VEXT.exe")
Copy-Item $sidecar (Join-Path $portable "vext-backend.exe")
Copy-Item (Join-Path $tauriDir "redist") (Join-Path $portable "redist") -Recurse
Copy-Item (Join-Path $tauriDir "host32") (Join-Path $portable "host32") -Recurse
Copy-Item (Join-Path $root "LICENSE") $portable
Copy-Item (Join-Path $root "THIRD_PARTY_NOTICES.md") $portable
@"
VEXT $version (portable)

Keep VEXT.exe, vext-backend.exe and the host32 folder (it opens 32-bit
plugins) in the same folder, then run VEXT.exe. Nothing is installed and no admin rights are needed for scanning.
Data (database, quarantine, logs) is stored in %APPDATA%\com.vext.app.

Requires Microsoft Edge WebView2 (preinstalled on Windows 11 and current Windows 10)
and Microsoft's Visual C++ runtime. If VEXT says the runtime is missing or damaged,
its Repair button installs it from redist\ (keep that folder next to VEXT.exe).
The setup.exe installer brings both by itself.
"@ | Set-Content -Path (Join-Path $portable "README.txt") -Encoding utf8

# Verify nothing required is missing before reporting success.
$required = @(
    (Join-Path $release $setupName),
    (Join-Path $portable "LICENSE"),
    (Join-Path $portable "THIRD_PARTY_NOTICES.md"),
    (Join-Path $portable "redist\vc_redist.x64.exe"),
    (Join-Path $portable "redist\vc_redist.x86.exe"),
    (Join-Path $portable "host32\vext-host32.exe"),
    (Join-Path $portable "redist\install-runtimes.ps1"),
    (Join-Path $portable "VEXT.exe"),
    (Join-Path $portable "vext-backend.exe")
)
foreach ($f in $required) { if (-not (Test-Path $f)) { throw "Missing release file: $f" } }

if ($signing) {
    foreach ($f in @((Join-Path $release $setupName), (Join-Path $portable "VEXT.exe"), (Join-Path $portable "vext-backend.exe"))) {
        $sig = Get-AuthenticodeSignature $f
        if ($sig.Status -ne "Valid") { throw "Signature check failed for ${f}: $($sig.Status)" }
    }
}

$sums = Get-ChildItem $release -Recurse -File | Where-Object { $_.Name -ne "SHA256SUMS.txt" } | ForEach-Object {
    $hash = (Get-FileHash $_.FullName -Algorithm SHA256).Hash
    "$hash  $($_.FullName.Substring($release.Length + 1).Replace('\', '/'))"
}
$sums | Set-Content -Path (Join-Path $release "SHA256SUMS.txt") -Encoding utf8

Write-Host ""
Write-Host "Release $version ready in $release" -ForegroundColor Green
Get-ChildItem $release -Recurse -File | ForEach-Object {
    "{0,10:N1} MB  {1}" -f ($_.Length / 1MB), $_.FullName.Substring($release.Length + 1)
}
