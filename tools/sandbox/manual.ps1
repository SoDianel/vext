# Opens a clean Windows Sandbox with the latest installer already on its Desktop, so you can install
# VEXT by hand as a normal user (no script runs in it): double-click VEXT-<version>-setup.exe, then
# start VEXT from the Start menu and try it.
#
#   .\tools\sandbox\manual.ps1              # no internet (proves the installer needs nothing online)
#   .\tools\sandbox\manual.ps1 -Online      # with internet (for the in-app web features)
#
# Close any Sandbox that is already open first: Windows runs only one at a time. Everything inside is
# wiped when its window closes. release\ is mapped read-only; nothing is written to this PC.
param([switch]$Online)

$ErrorActionPreference = "Stop"
$release = Join-Path (Split-Path $PSScriptRoot -Parent | Split-Path -Parent) "release"
$setup = Get-ChildItem $release -Filter "VEXT-*-setup.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $setup) { throw "No release\VEXT-*-setup.exe: run .\tools\build\build_release.ps1 first." }
if (Get-Process WindowsSandbox, WindowsSandboxRemoteSession -ErrorAction SilentlyContinue) {
    throw "A Windows Sandbox is already open. Close its window and run this again."
}

$net = if ($Online) { "Default" } else { "Disable" }
$wsb = Join-Path $env:TEMP "vext-manual.wsb"
@"
<Configuration>
  <Networking>$net</Networking>
  <MappedFolders>
    <MappedFolder><HostFolder>$release</HostFolder><SandboxFolder>C:\release</SandboxFolder><ReadOnly>true</ReadOnly></MappedFolder>
  </MappedFolders>
  <LogonCommand>
    <Command>powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Copy-Item 'C:\release\$($setup.Name)' ([Environment]::GetFolderPath('Desktop')) -Force"</Command>
  </LogonCommand>
</Configuration>
"@ | Set-Content -Path $wsb -Encoding utf8
Write-Host "Opening Windows Sandbox (network: $net). $($setup.Name) will appear on its Desktop in a few seconds."
Start-Process $wsb
