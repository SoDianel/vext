# Sets the version everywhere and prepares CHANGELOG.md for a release. Changes files only: no commit,
# no tag (you do those by hand, see LOCAL_SETUP.md "Releasing").
#
#   .\tools\bump_version.ps1 -Version 1.1.0            # tauri.conf.json, package.json (+lock), Cargo.toml (+lock),
#                                                      # CHANGELOG.md ([Unreleased] becomes [1.1.0] - today)
#   .\tools\bump_version.ps1 -Version 1.1.0 -DryRun    # show what would change
param(
    [Parameter(Mandatory = $true)][string]$Version,
    [string]$Date = (Get-Date -Format "yyyy-MM-dd"),
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
if ($Version -notmatch '^\d+\.\d+\.\d+$') { throw "Version must be x.y.z (got '$Version')" }
if ($Date -notmatch '^\d{4}-\d{2}-\d{2}$') { throw "Date must be YYYY-MM-DD" }
$root = Split-Path $PSScriptRoot -Parent
$tauri = Join-Path $root "desktop\src-tauri"
$utf8 = New-Object System.Text.UTF8Encoding($false)
$old = (Get-Content (Join-Path $tauri "tauri.conf.json") -Raw | ConvertFrom-Json).version

function Edit-File([string]$Path, [scriptblock]$Change) {
    $text = [IO.File]::ReadAllText($Path, $utf8)
    $new = & $Change $text
    if ($new -eq $text) { Write-Host "  unchanged  $(Split-Path $Path -Leaf)"; return }
    Write-Host "  updated    $(Resolve-Path $Path -Relative)"
    if (-not $DryRun) { [IO.File]::WriteAllText($Path, $new, $utf8) }
}

Write-Host "Version $old -> $Version$(if ($DryRun) { ' (dry run)' })"
Edit-File (Join-Path $tauri "tauri.conf.json") { param($t) [regex]::new('("version"\s*:\s*")[^"]+(")').Replace($t, "`${1}$Version`${2}", 1) }
Edit-File (Join-Path $root "desktop\package.json") { param($t) [regex]::new('("version"\s*:\s*")[^"]+(")').Replace($t, "`${1}$Version`${2}", 1) }
Edit-File (Join-Path $root "desktop\package-lock.json") { param($t) [regex]::new('(?m)^(\s*"version"\s*:\s*")[^"]+(",?\r?\n)').Replace($t, "`${1}$Version`${2}", 2) }
Edit-File (Join-Path $tauri "Cargo.toml") { param($t) [regex]::new('(?m)^(version\s*=\s*")[^"]+(")').Replace($t, "`${1}$Version`${2}", 1) }
Edit-File (Join-Path $tauri "Cargo.lock") { param($t) [regex]::new('(?m)^(name = "vext"\r?\nversion = ")[^"]+(")').Replace($t, "`${1}$Version`${2}", 1) }

Edit-File (Join-Path $root "CHANGELOG.md") {
    param($t)
    $nl = if ($t.Contains("`r`n")) { "`r`n" } else { "`n" }
    $repo = "https://github.com/SoDianel/vext"
    $section = [regex]::Match($t, "(?m)^## \[$([regex]::Escape($Version))\][^\r\n]*(?=\r?$)")
    if ($section.Success) {
        # Already written (for example the first release): just date it.
        $t = [regex]::new("(?m)^(## \[$([regex]::Escape($Version))\]) - [^\r\n]*(?=\r?$)").Replace($t, "`${1} - $Date", 1)
    } else {
        $unreleased = [regex]::Match($t, "(?ms)^## \[Unreleased\][^\r\n]*\r?\n(.*?)(?=^## \[|\z)")
        if (-not $unreleased.Success -or $unreleased.Groups[1].Value.Trim().Length -eq 0) {
            throw "CHANGELOG.md has nothing under [Unreleased] to release as $Version."
        }
        $t = [regex]::new("(?m)^## \[Unreleased\][^\r\n]*(?=\r?$)").Replace($t, "## [Unreleased]$nl$nl## [$Version] - $Date", 1)
        $t = [regex]::new("(?m)^\[Unreleased\]: [^\r\n]*(?=\r?$)").Replace($t, "[Unreleased]: $repo/compare/v$Version...HEAD$nl[$Version]: $repo/compare/v$old...v$Version", 1)
    }
    $t
}

Write-Host ""
Write-Host "Next (by hand): review 'git diff', commit on your topic branch, open a PR, and use GitHub Rebase and merge. Tag v$Version on main after CI passes." -ForegroundColor Green
