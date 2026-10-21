# Downloads Microsoft's Visual C++ runtime installers that VEXT ships inside its
# own installer (desktop\src-tauri\redist\, git-ignored), so a PC never depends
# on whatever runtime it happens to have. Plugins load into VEXT's process and
# all share ONE copy of msvcp140.dll per process: newer copies run older
# plugins, older copies crash newer ones. So VEXT installs (or repairs)
# Microsoft's current system-wide runtime instead of carrying a private copy
# that ages (see AGENTS.md "Self-contained install").
#
#   vc_redist.x64.exe          2015-2026 (14.x, the newest channel): VEXT itself and today's plugins
#   vc_redist.x86.exe          the same, 32-bit: the 32-bit plugin host (host32\) and 32-bit plugins
#   vcredist_2013_x64.exe      2013 (12.0, msvcr120.dll):  older plugins
#   vcredist_2012_x64.exe      2012 (11.0, msvcr110.dll):  older plugins
#   vcredist_2010_x64.exe      2010 (10.0, msvcr100.dll):  older plugins
#
# Each file must carry a valid Authenticode signature from Microsoft, or the
# build stops. Run alone:  .\tools\fetch_redist.ps1 [-Refresh]
# -Refresh downloads them again (do it for each release: the 14.x one moves).

param([switch]$Refresh)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$dest = Join-Path $root "desktop\src-tauri\redist"
New-Item -ItemType Directory -Force -Path $dest | Out-Null

$files = [ordered]@{
    "vc_redist.x64.exe"     = "https://aka.ms/vs/18/release/vc_redist.x64.exe"
    "vc_redist.x86.exe"     = "https://aka.ms/vs/18/release/vc_redist.x86.exe"
    "vcredist_2013_x64.exe" = "https://aka.ms/highdpimfc2013x64enu"
    "vcredist_2012_x64.exe" = "https://download.microsoft.com/download/1/6/B/16B06F60-3B20-4FF2-B699-5E9B7962F9AE/VSU_4/vcredist_x64.exe"
    "vcredist_2010_x64.exe" = "https://download.microsoft.com/download/1/6/5/165255E7-1014-4D0A-B094-B6A430A6BFFC/vcredist_x64.exe"
}

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
foreach ($name in $files.Keys) {
    $path = Join-Path $dest $name
    if ($Refresh -or -not (Test-Path $path)) {
        Write-Host "==> Downloading $name"
        $tmp = "$path.part"
        Invoke-WebRequest -UseBasicParsing -Uri $files[$name] -OutFile $tmp
        Move-Item -Force $tmp $path
    }
    $sig = Get-AuthenticodeSignature $path
    if ($sig.Status -ne "Valid" -or $sig.SignerCertificate.Subject -notmatch "O=Microsoft Corporation") {
        Remove-Item -Force $path
        throw "$name isn't validly signed by Microsoft ($($sig.Status)): deleted, not shipped"
    }
    $ver = (Get-Item $path).VersionInfo.ProductVersion
    Write-Host ("    {0,-24} {1}  signed by Microsoft" -f $name, $ver)
}

# WebView2 (draws VEXT's window), Microsoft's offline x64 installer. Not a resource:
# installer-hooks.nsh embeds it in setup.exe and unpacks it only when a PC really lacks
# WebView2 (Tauri's own WebView2 step aborts the whole install when this installer
# refuses, e.g. where Windows ships WebView2 without its EdgeUpdate registry entry).
$wvDir = Join-Path $root "desktop\src-tauri\webview2"
New-Item -ItemType Directory -Force -Path $wvDir | Out-Null
$wv = Join-Path $wvDir "MicrosoftEdgeWebView2RuntimeInstallerX64.exe"
if ($Refresh -or -not (Test-Path $wv)) {
    Write-Host "==> Downloading the WebView2 offline installer"
    Invoke-WebRequest -UseBasicParsing -Uri "https://go.microsoft.com/fwlink/?linkid=2124701" -OutFile "$wv.part"
    Move-Item -Force "$wv.part" $wv
}
$sig = Get-AuthenticodeSignature $wv
if ($sig.Status -ne "Valid" -or $sig.SignerCertificate.Subject -notmatch "O=Microsoft Corporation") {
    Remove-Item -Force $wv
    throw "The WebView2 installer isn't validly signed by Microsoft ($($sig.Status)): deleted, not shipped"
}
Write-Host ("    {0,-24} {1}  signed by Microsoft" -f "WebView2 (offline)", (Get-Item $wv).VersionInfo.ProductVersion)

# The elevated script that runs them (installer hook and in-app Repair) ships beside them.
Copy-Item -Force (Join-Path $root "desktop\src-tauri\installer\install-runtimes.ps1") $dest
Write-Host "    install-runtimes.ps1     (copied)"
