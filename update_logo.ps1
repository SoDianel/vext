# Makes every logo file VEXT ships from the exports in sources\ (the owner's
# master is sources\logo.vectorcraft). Only the APP logo is used (owner,
# 2026-10-15): the full-bleed letters on the vector file's last artboard,
# exported as logo_app_<size>.png (square, transparent). Other exports in
# sources\ (logo_<size>.png with the background, older versions) are unused.
#
#   desktop\src-tauri\icons\*   App icons: exe, taskbar, window, installers.
#                               From the largest sources\logo_app_<size>.png,
#                               via `tauri icon` (only the desktop set is kept).
#   app\static\logo.png         Favicon. The smallest app-logo export
#                               of at least 108 px (sharp up to 300%).
#   app\static\logo-mark.svg    The same mark for the sidebar's logo button and
#                               picture placeholders, drawn from the last
#                               artboard of sources\logo.vectorcraft
#                               by make_logo_svg.py: vector, and its White
#                               Smoke letters follow the theme's text colour
#                               (on a light page the PNG's would vanish).
#
# build_release.ps1 runs this first, so a changed export ships with the next
# build. Files whose content didn't change are left alone (an unchanged icon
# doesn't make cargo rebuild the shell). Run alone:  .\update_logo.ps1

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$sources = Join-Path $root "sources"
$desktop = Join-Path $root "desktop"
$icons = Join-Path $desktop "src-tauri\icons"
$webLogo = Join-Path $root "app\static\logo.png"
$webMin = 108

$found = foreach ($f in Get-ChildItem $sources -Filter "logo_app_*.png" -File) {
    if ($f.BaseName -match '^logo_app_(\d+)$') { [pscustomobject]@{ Size = [int]$Matches[1]; Path = $f.FullName } }
}
$exports = @($found | Sort-Object Size)
if (-not $exports) { throw "No app logo exports found: expected sources\logo_app_<size>.png (e.g. logo_app_1080.png)" }
$master = $exports[-1]
$web = $exports | Where-Object { $_.Size -ge $webMin } | Select-Object -First 1
if (-not $web) { $web = $master }

# Copies $src to $dst only when the bytes differ; returns whether it did.
function Update-File([string]$src, [string]$dst) {
    if ((Test-Path $dst) -and (Get-FileHash $src).Hash -eq (Get-FileHash $dst).Hash) { return $false }
    Copy-Item $src $dst -Force
    return $true
}

Write-Host "==> Logo: icons from $(Split-Path $master.Path -Leaf), in-app logo from $(Split-Path $web.Path -Leaf)"

# `tauri icon` also writes android\ and ios\ sets: generate into a scratch
# folder and keep only the desktop files (the top level).
$tmp = Join-Path ([System.IO.Path]::GetTempPath()) "vext-icons-$PID"
if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
Push-Location $desktop
try {
    cmd /c "npx tauri icon `"$($master.Path)`" -o `"$tmp`" 2>&1" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "tauri icon failed (exit $LASTEXITCODE)" }
} finally { Pop-Location }

$changed = @()
foreach ($f in Get-ChildItem $tmp -File | Where-Object Extension -ne ".icns") {
    if (Update-File $f.FullName (Join-Path $icons $f.Name)) { $changed += "icons\$($f.Name)" }
}
# icon.icns (macOS) differs on every run even from the same source: take it
# only along with a real change, or the shell would rebuild every time.
$icns = Join-Path $icons "icon.icns"
if ($changed -or -not (Test-Path $icns)) {
    Copy-Item (Join-Path $tmp "icon.icns") $icns -Force
    $changed += "icons\icon.icns"
}
Remove-Item $tmp -Recurse -Force
if (Update-File $web.Path $webLogo) { $changed += "app\static\logo.png" }

$python = Join-Path $root ".venv-native\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = "python" }
# The vector master is kept by the maintainer and is not in the public repo: without it the committed
# app\static\logo-mark.svg is used as it is.
$master = Join-Path $sources "logo.vectorcraft"
if (Test-Path $master) {
    $markTmp = Join-Path ([System.IO.Path]::GetTempPath()) "vext-logo-mark-$PID.svg"
    & $python (Join-Path $root "make_logo_svg.py") $master $markTmp
    if ($LASTEXITCODE -ne 0) { throw "make_logo_svg.py failed (exit $LASTEXITCODE)" }
    if (Update-File $markTmp (Join-Path $root "app\static\logo-mark.svg")) { $changed += "app\static\logo-mark.svg" }
    Remove-Item $markTmp -Force
} else {
    Write-Host "    sources\logo.vectorcraft not present: keeping app\static\logo-mark.svg"
}

if ($changed) { Write-Host "    updated: $($changed -join ', ')" } else { Write-Host "    already up to date" }
