# Sets up the 32-bit Python that hosts 32-bit plugins (see AGENTS.md "32-bit
# plugins"): Python's official 32-bit build (the NuGet "pythonx86" package, no
# installer, nothing registered on the PC) unpacked into .python-x86\, plus
# requirements-x86.txt. A dev backend (python -m app.web / app.main) uses it as
# soon as it exists (plugin_host.x86_worker); tools\build_host32.ps1 bundles it
# for the installer and adds PyInstaller here.
#
#   .\tools\setup_x86_host.ps1 [-Refresh]
#
# -Refresh deletes .python-x86 and sets it up again. Keep $version equal to the
# 64-bit Python that builds the backend (.venv-native).

param([switch]$Refresh)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$target = Join-Path $root ".python-x86"
$python = Join-Path $target "python.exe"
$version = "3.14.6"

if ($Refresh -and (Test-Path $target)) { Remove-Item -Recurse -Force $target }

if (-not (Test-Path $python)) {
    $work = Join-Path $env:TEMP ("vext-pythonx86-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Force -Path $work | Out-Null
    try {
        Write-Host "==> Downloading 32-bit Python $version (NuGet pythonx86)"
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $pkg = Join-Path $work "pythonx86.zip"
        Invoke-WebRequest -UseBasicParsing -Uri "https://www.nuget.org/api/v2/package/pythonx86/$version" -OutFile $pkg
        Expand-Archive -Path $pkg -DestinationPath (Join-Path $work "pkg")
        $exe = Join-Path $work "pkg\tools\python.exe"
        $sig = Get-AuthenticodeSignature $exe
        if ($sig.Status -ne "Valid" -or $sig.SignerCertificate.Subject -notmatch "O=Python Software Foundation") {
            throw "python.exe in the package isn't validly signed by the Python Software Foundation ($($sig.Status))"
        }
        Move-Item (Join-Path $work "pkg\tools") $target
    } finally {
        Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
    }
}

$bits = & $python -c "import struct; print(struct.calcsize('P') * 8)"
if ($bits -ne "32") { throw "$python is not a 32-bit Python ($bits-bit)" }

Write-Host "==> Installing requirements-x86.txt"
& $python -m ensurepip --upgrade | Out-Null
& $python -m pip install --disable-pip-version-check --no-warn-script-location --quiet -r (Join-Path $root "requirements-x86.txt")
if ($LASTEXITCODE -ne 0) { throw "pip install failed (exit $LASTEXITCODE)" }

Write-Host "Done: $python ($(& $python -c 'import sys; print(sys.version.split()[0])'), 32-bit)"
