# Opens Windows Sandbox with VEXT's clean-PC test (see README.md here).
$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
$root = Split-Path (Split-Path $here -Parent) -Parent
$release = Join-Path $root "release"
$plugins = "C:\Program Files\Common Files\VST3\XFER"
$results = Join-Path $here "results"

if (-not (Test-Path "$env:windir\System32\WindowsSandbox.exe")) {
    throw "Windows Sandbox isn't enabled. In an administrator PowerShell: Enable-WindowsOptionalFeature -Online -FeatureName Containers-DisposableClientVM -All (then restart)."
}
if (-not (Get-ChildItem $release -Filter "VEXT-*-setup.exe" -ErrorAction SilentlyContinue)) {
    throw "No release\VEXT-*-setup.exe: run .\build_release.ps1 first."
}
# Empty it, but keep the folder: a Sandbox that just closed can still hold it for a while.
New-Item -ItemType Directory -Force -Path $results | Out-Null
Get-ChildItem $results -Force | Remove-Item -Recurse -Force

$wsb = Join-Path $env:TEMP "vext-clean-pc.wsb"
@"
<Configuration>
  <Networking>Disable</Networking>
  <MappedFolders>
    <MappedFolder><HostFolder>$release</HostFolder><SandboxFolder>C:\release</SandboxFolder><ReadOnly>true</ReadOnly></MappedFolder>
    <MappedFolder><HostFolder>$plugins</HostFolder><SandboxFolder>C:\plugins</SandboxFolder><ReadOnly>true</ReadOnly></MappedFolder>
    <MappedFolder><HostFolder>$here</HostFolder><SandboxFolder>C:\test</SandboxFolder><ReadOnly>true</ReadOnly></MappedFolder>
    <MappedFolder><HostFolder>$results</HostFolder><SandboxFolder>C:\results</SandboxFolder><ReadOnly>false</ReadOnly></MappedFolder>
  </MappedFolders>
  <LogonCommand>
    <Command>powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Normal -File C:\test\sandbox_test.ps1</Command>
  </LogonCommand>
</Configuration>
"@ | Set-Content -Path $wsb -Encoding utf8
Write-Host "Opening Windows Sandbox (networking off: proves the installer needs nothing from the internet)."
Write-Host "Results will appear in $results"
Start-Process $wsb
