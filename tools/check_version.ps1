# Fails unless every place that carries VEXT's version agrees, and (for a release) the changelog is ready.
#
#   .\tools\check_version.ps1                  # versions agree
#   .\tools\check_version.ps1 -Tag v1.2.0      # ...and the tag equals them, CHANGELOG.md has a dated
#                                              #    "## [1.2.0] - YYYY-MM-DD" section with content
#
# Used by build_release.ps1 (versions only) and by the release workflow (with the tag).
# The source of truth is desktop\src-tauri\tauri.conf.json (app\config.py reads it); tools\bump_version.ps1
# changes all of them together.
param([string]$Tag = "")

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$tauri = Join-Path $root "desktop\src-tauri"
# Windows PowerShell 5.1 cannot deserialize npm's empty root-package key.
$npmLock = ((Get-Content (Join-Path $root "desktop\package-lock.json") -Raw) -replace '""\s*:', '"__vext_root__":') | ConvertFrom-Json

$versions = [ordered]@{
    "tauri.conf.json" = (Get-Content (Join-Path $tauri "tauri.conf.json") -Raw | ConvertFrom-Json).version
    "package.json"    = (Get-Content (Join-Path $root "desktop\package.json") -Raw | ConvertFrom-Json).version
    "package-lock.json" = $npmLock.version
    "package-lock root" = $npmLock.packages.__vext_root__.version
    "Cargo.toml"      = (Select-String -Path (Join-Path $tauri "Cargo.toml") -Pattern '^version\s*=\s*"([^"]+)"' | Select-Object -First 1).Matches[0].Groups[1].Value
    "Cargo.lock"      = [regex]::Match((Get-Content (Join-Path $tauri "Cargo.lock") -Raw), '(?m)^name = "vext"\r?\nversion = "([^"]+)"').Groups[1].Value
}
$version = $versions["tauri.conf.json"]
$bad = @($versions.GetEnumerator() | Where-Object { $_.Value -ne $version })
if ($bad) {
    $versions.GetEnumerator() | ForEach-Object { Write-Host ("  {0,-16} {1}" -f $_.Key, $_.Value) }
    throw "Version mismatch. Run .\tools\bump_version.ps1 -Version <x.y.z> to set them all."
}
if ($version -notmatch '^\d+\.\d+\.\d+$') { throw "Version '$version' is not x.y.z" }
Write-Host "Version ${version}: tauri.conf.json, npm manifests and locks, Cargo.toml and Cargo.lock agree"

if ($Tag) {
    if ($Tag -ne "v$version") { throw "Tag $Tag does not match the version $version (expected v$version)." }
    $changelog = Get-Content (Join-Path $root "CHANGELOG.md") -Raw
    $m = [regex]::Match($changelog, "(?ms)^## \[$([regex]::Escape($version))\] - (\S+)[^\r\n]*\r?\n(.*?)(?=^## \[|\z)")
    if (-not $m.Success) { throw "CHANGELOG.md has no '## [$version] - <date>' section." }
    if ($m.Groups[1].Value -notmatch '^\d{4}-\d{2}-\d{2}$') { throw "CHANGELOG.md [$version] has no release date (found '$($m.Groups[1].Value)'). Run tools\bump_version.ps1." }
    if ($m.Groups[2].Value.Trim().Length -lt 20) { throw "CHANGELOG.md [$version] is empty." }
    Write-Host "CHANGELOG.md [$version] is dated $($m.Groups[1].Value) and has content"
}
