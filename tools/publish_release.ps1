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
# It never tags, pushes, publishes or changes visibility. The draft pins the recorded build commit;
# GitHub creates the tag when the maintainer publishes it. It refuses to overwrite an existing release.
param(
    [string]$Target = "",      # defaults to the exact commit in BUILD_PROVENANCE.json
    [string]$ReleaseDir = "",  # also accepts a downloaded Windows build artifact (ZIP, no folder)
    [switch]$SkipGitHub
)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$release = if ($ReleaseDir) { [IO.Path]::GetFullPath($ReleaseDir) } else { Join-Path $root "release" }
$version = (Get-Content (Join-Path $root "desktop\src-tauri\tauri.conf.json") -Raw | ConvertFrom-Json).version
$tag = "v$version"
$setup = Join-Path $release "VEXT-$version-setup.exe"
$portable = Join-Path $release "VEXT-$version-portable"
$zip = Join-Path $release "VEXT-$version-portable.zip"
$sums = Join-Path $release "SHA256SUMS.txt"
$provenance = Join-Path $release "BUILD_PROVENANCE.json"

foreach ($p in @($setup, $sums, $provenance, (Join-Path $release "LICENSE"), (Join-Path $release "THIRD_PARTY_NOTICES.md"))) {
    if (-not (Test-Path $p)) { throw "Missing $p. Run .\build_release.ps1 first." }
}
if (-not (Test-Path $portable) -and -not (Test-Path $zip)) { throw "Missing portable folder or ZIP" }

$build = Get-Content $provenance -Raw | ConvertFrom-Json
if ($build.schema_version -ne 1 -or $build.version -ne $version -or $build.commit -notmatch '^[0-9a-f]{40}$') {
    throw "Invalid build provenance or version mismatch"
}
if ($build.source_dirty -isnot [bool] -or $build.source_dirty) { throw "Release source was dirty or was not recorded; build from a clean commit" }
if (-not $Target) { $Target = $build.commit }
$targetCommit = git -C $root rev-parse --verify "$Target^{commit}"
if ($LASTEXITCODE -ne 0 -or $targetCommit -ne $build.commit) { throw "Target does not match the recorded build commit" }
$checkoutCommit = git -C $root rev-parse HEAD
if ($LASTEXITCODE -ne 0 -or $checkoutCommit -ne $build.commit) { throw "Check out the recorded build commit before preparing its release" }

# 1) Checksums -------------------------------------------------------------
Write-Host "==> Verifying SHA256SUMS.txt"
$bad = 0
$listed = @{}
Add-Type -AssemblyName System.IO.Compression.FileSystem
$archive = if (-not (Test-Path $portable) -and (Test-Path $zip)) { [IO.Compression.ZipFile]::OpenRead($zip) } else { $null }
try {
    foreach ($line in Get-Content $sums) {
        if (-not $line.Trim()) { continue }
        if ($line -notmatch '^([0-9a-fA-F]{64})\s+(.+)$') { throw "Malformed checksum line" }
        $hash, $rel = $Matches[1], $Matches[2].Replace('\', '/')
        if ($listed.ContainsKey($rel)) { throw "Duplicate checksum: $rel" }
        $listed[$rel] = $true
        $file = [IO.Path]::GetFullPath((Join-Path $release $rel))
        if (-not $file.StartsWith($release.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) { throw "Unsafe checksum path: $rel" }
        if (Test-Path -LiteralPath $file -PathType Leaf) {
            $actual = (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash
        } elseif ($archive -and $rel.StartsWith("VEXT-$version-portable/")) {
            $entries = @($archive.Entries | Where-Object { $_.FullName.Replace('\', '/') -eq $rel })
            if ($entries.Count -ne 1) { throw "Missing or duplicate portable ZIP entry: $rel" }
            $entry = $entries[0]
            $stream = $entry.Open()
            $sha = [Security.Cryptography.SHA256]::Create()
            try { $actual = ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-', '') }
            finally { $sha.Dispose(); $stream.Dispose() }
        } else { throw "Missing checksummed file: $rel" }
        if ($actual -ne $hash) { Write-Host "CHANGED  $rel" -ForegroundColor Red; $bad++ }
    }
    foreach ($required in @("VEXT-$version-setup.exe", "BUILD_PROVENANCE.json", "LICENSE", "THIRD_PARTY_NOTICES.md")) {
        if (-not $listed.ContainsKey($required)) { throw "Missing checksum for $required" }
    }
    if (Test-Path $zip) {
        if (-not $listed.ContainsKey((Split-Path $zip -Leaf))) { throw "Missing portable ZIP checksum" }
    }
    if (Test-Path $portable) {
        foreach ($file in Get-ChildItem $portable -Recurse -File) {
            $rel = $file.FullName.Substring($release.Length + 1).Replace('\', '/')
            if (-not $listed.ContainsKey($rel)) { throw "Unlisted portable file: $rel" }
        }
    } elseif ($archive) {
        foreach ($entry in $archive.Entries) {
            if ($entry.Name -and -not $listed.ContainsKey($entry.FullName.Replace('\', '/'))) { throw "Unlisted ZIP entry: $($entry.FullName)" }
        }
    }
} finally { if ($archive) { $archive.Dispose() } }
if ($bad) { throw "$bad file(s) do not match SHA256SUMS.txt. Rebuild instead of patching files by hand." }

# Package only once: reruns keep the exact ZIP and checksum already reviewed.
if (-not (Test-Path $zip)) {
    Compress-Archive -Path $portable -DestinationPath $zip -CompressionLevel Optimal
    $zipLine = "$((Get-FileHash $zip -Algorithm SHA256).Hash)  $(Split-Path $zip -Leaf)"
    Add-Content $sums $zipLine -Encoding utf8
}

# 2) Portable zip ------------------------------------------------------------
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

Source commit: $($build.commit). See ``BUILD_PROVENANCE.json`` for the build record.
Before publication, run the clean-PC Sandbox test on this exact downloaded installer and retain its hash
with the PASS result. A local rebuild or an older Sandbox PASS does not validate these assets.
"@ | Set-Content $notes -Encoding utf8
Write-Host "==> Notes: $notes"

# 4) Draft GitHub Release ----------------------------------------------------
$assets = @($setup, $zip, $sums, $provenance, (Join-Path $release "LICENSE"), (Join-Path $release "THIRD_PARTY_NOTICES.md"))
if ($SkipGitHub) {
    Write-Host "==> Skipped the GitHub draft (-SkipGitHub). It would run:"
    Write-Host "    gh release create $tag --draft --target $Target --title `"VEXT $version`" --notes-file `"$notes`" $($assets -join ' ')"
} else {
    & (Join-Path $PSScriptRoot "check_version.ps1") -Tag $tag
    $releases = & gh release list --limit 1000 --json tagName
    if ($LASTEXITCODE -ne 0) { throw "Could not check existing releases" }
    if (($releases | ConvertFrom-Json) | Where-Object tagName -eq $tag) {
        throw "Release $tag already exists. Review it on GitHub; this tool never replaces release assets."
    }
    Write-Host "==> Creating DRAFT release $tag"
    & gh release create $tag --draft --target $build.commit --title "VEXT $version" --notes-file $notes @assets
    if ($LASTEXITCODE -ne 0) { throw "gh release create failed (exit $LASTEXITCODE)" }
    Write-Host "Draft created. Review it on GitHub and publish it by hand." -ForegroundColor Green
}
