# Signs one file with Authenticode, if signing is configured; does nothing otherwise.
# Called by build_release.ps1 for the sidecar and host32 exes and, through Tauri's
# bundle.windows.signCommand, for VEXT.exe and the installer.
#
# Configure with environment variables (nothing is read from disk, nothing is stored):
#   Certificate file (OV/EV .pfx):   VEXT_SIGN_PFX, VEXT_SIGN_PFX_PASSWORD
#   Azure Trusted Signing:           VEXT_SIGN_AZURE_DLIB (path to Azure.CodeSigning.Dlib.dll),
#                                    VEXT_SIGN_AZURE_METADATA (path to its metadata.json)
# Optional:  VEXT_SIGNTOOL (path to signtool.exe), VEXT_SIGN_TIMESTAMP (default http://timestamp.acs.microsoft.com
#            for Azure, http://timestamp.digicert.com for a .pfx)
param([Parameter(Mandatory = $true)][string]$File)

$ErrorActionPreference = "Stop"
$pfx = $env:VEXT_SIGN_PFX
$dlib = $env:VEXT_SIGN_AZURE_DLIB
if (-not $pfx -and -not $dlib) { exit 0 }

$signtool = $env:VEXT_SIGNTOOL
if (-not $signtool) {
    $signtool = Get-ChildItem "${env:ProgramFiles(x86)}\Windows Kits\10\bin\*\x64\signtool.exe" -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending | Select-Object -First 1 -ExpandProperty FullName
}
if (-not $signtool -or -not (Test-Path $signtool)) { throw "signtool.exe not found: install the Windows SDK or set VEXT_SIGNTOOL" }

if ($dlib) {
    $meta = $env:VEXT_SIGN_AZURE_METADATA
    if (-not $meta) { throw "VEXT_SIGN_AZURE_METADATA is not set" }
    $ts = if ($env:VEXT_SIGN_TIMESTAMP) { $env:VEXT_SIGN_TIMESTAMP } else { "http://timestamp.acs.microsoft.com" }
    & $signtool sign /v /fd SHA256 /tr $ts /td SHA256 /dlib $dlib /dmdf $meta $File
} else {
    $ts = if ($env:VEXT_SIGN_TIMESTAMP) { $env:VEXT_SIGN_TIMESTAMP } else { "http://timestamp.digicert.com" }
    & $signtool sign /v /fd SHA256 /tr $ts /td SHA256 /f $pfx /p $env:VEXT_SIGN_PFX_PASSWORD $File
}
if ($LASTEXITCODE -ne 0) { throw "signtool failed on $File (exit $LASTEXITCODE)" }
