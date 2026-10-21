# Prepares a VEXT release after build_release.ps1. Run it only when you mean to release.
#
#   .\tools\publish_release.ps1                 # checks, zip, notes, DRAFT GitHub Release
#   .\tools\publish_release.ps1 -SkipGitHub     # everything except the GitHub step (a dry run)
#
# What it does:
#   1. Verifies release\SHA256SUMS.txt against the files in release\.
#   2. Zips the portable folder (release\VEXT-<version>-portable.zip) and adds it to SHA256SUMS.txt.
#   3. Writes release\RELEASE_NOTES.md from the matching CHANGELOG.md section.
#   4. Creates a DRAFT GitHub Release with `gh` carrying the setup.exe, the portable zip, SHA256SUMS.txt,
#      LICENSE and THIRD_PARTY_NOTICES.md. You review and publish the draft by hand.
#
# It never tags, pushes, publishes or changes visibility. The draft's tag is created by GitHub only when
# you publish the draft.
param(
    [string]$Target = "main",   # branch the tag will point at when the draft is published
    [switch]$SkipGitHub
)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$release = Join-Path $root "release"
$version = (Get-Content (Join-Path $root "desktop\src-tauri\tauri.conf.json") -Raw | ConvertFrom-Json).version
$tag = "v$version"
$setup = Join-Path $release "VEXT-$version-setup.exe"
$portable = Join-Path $release "VEXT-$version-portable"
$zip = Join-Path $release "VEXT-$version-portable.zip"
$sums = Join-Path $release "SHA256SUMS.txt"

foreach ($p in @($setup, $portable, $sums)) {
    if (-not (Test-Path $p)) { throw "Missing $p. Run .\build_release.ps1 first." }
}

# 1) Checksums -------------------------------------------------------------
Write-Host "==> Verifying SHA256SUMS.txt"
$bad = 0
foreach ($line in Get-Content $sums) {
    if (-not $line.Trim()) { continue }
    $hash, $rel = $line -split "\s+", 2
    $file = Join-Path $release $rel.Trim()
    if (-not (Test-Path $file)) { Write-Host "MISSING  $rel" -ForegroundColor Red; $bad++; continue }
    if ((Get-FileHash $file -Algorithm SHA256).Hash -ne $hash) { Write-Host "CHANGED  $rel" -ForegroundColor Red; $bad++ }
}
if ($bad) { throw "$bad file(s) do not match SHA256SUMS.txt. Rebuild instead of patching files by hand." }

# 2) Portable zip ------------------------------------------------------------
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path $portable -DestinationPath $zip -CompressionLevel Optimal
$zipLine = "$((Get-FileHash $zip -Algorithm SHA256).Hash)  $(Split-Path $zip -Leaf)"
$lines = @(Get-Content $sums | Where-Object { $_ -notmatch [regex]::Escape((Split-Path $zip -Leaf)) })
($lines + $zipLine) | Set-Content $sums -Encoding utf8
Write-Host "==> Release files"
Get-ChildItem $release -File | ForEach-Object { "{0,10:N1} MB  {1}" -f ($_.Length / 1MB), $_.Name }

# 3) Release notes from CHANGELOG.md ---------------------------------------
$changelog = Get-Content (Join-Path $root "CHANGELOG.md") -Raw
$m = [regex]::Match($changelog, "(?ms)^## \[$([regex]::Escape($version))\][^\r\n]*\r?\n(.*?)(?=^## \[|\z)")
if (-not $m.Success) { throw "CHANGELOG.md has no '## [$version]' section." }
$notes = Join-Path $release "RELEASE_NOTES.md"
@"
$($m.Groups[1].Value.Trim())

---
**Install:** run ``VEXT-$version-setup.exe`` (per-user, brings the runtimes a new PC needs), or unzip
``VEXT-$version-portable.zip`` and run ``VEXT.exe``. Check a download against ``SHA256SUMS.txt``.
Unsigned builds show a Windows SmartScreen warning ("More info", then "Run anyway").
Licenses: ``LICENSE`` (GPL-3.0-or-later) and ``THIRD_PARTY_NOTICES.md``.
"@ | Set-Content $notes -Encoding utf8
Write-Host "==> Notes: $notes"

# 4) Draft GitHub Release ----------------------------------------------------
$assets = @($setup, $zip, $sums, (Join-Path $root "LICENSE"), (Join-Path $root "THIRD_PARTY_NOTICES.md"))
if ($SkipGitHub) {
    Write-Host "==> Skipped the GitHub draft (-SkipGitHub). It would run:"
    Write-Host "    gh release create $tag --draft --target $Target --title `"VEXT $version`" --notes-file `"$notes`" $($assets -join ' ')"
} else {
    Write-Host "==> Creating DRAFT release $tag"
    & gh release create $tag --draft --target $Target --title "VEXT $version" --notes-file $notes @assets
    if ($LASTEXITCODE -ne 0) { throw "gh release create failed (exit $LASTEXITCODE)" }
    Write-Host "Draft created. Review it on GitHub and publish it by hand." -ForegroundColor Green
}
