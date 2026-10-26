# Runs inside Windows Sandbox (tools\sandbox\vext-clean-pc.wsb): a fresh
# Windows with no Visual C++ runtime, no plugins and nothing of VEXT. Installs
# VEXT from release\ the way a user would, then checks what a new PC gets:
#   1. setup.exe installs silently and brings the Visual C++ runtimes
#   2. VEXT's backend starts and reports the runtime as ok
#   3. a real plugin (Xfer OTT, mapped read-only from the host) loads and
#      processes audio in VEXT's plugin host
# Results: C:\results\result.json and log.txt (the host's tools\sandbox\results).

$ErrorActionPreference = "Continue"
$out = "C:\results"
New-Item -ItemType Directory -Force -Path $out | Out-Null
$logFile = Join-Path $out "log.txt"
function Say([string]$t) { "{0}  {1}" -f (Get-Date -Format "HH:mm:ss"), $t | Tee-Object -FilePath $logFile -Append }
$result = [ordered]@{}

function Runtimes {
    $base = [Microsoft.Win32.RegistryKey]::OpenBaseKey("LocalMachine", "Registry64")
    $r = [ordered]@{}
    foreach ($k in @("14.0\VC\Runtimes\x64", "12.0\VC\Runtimes\x64", "11.0\VC\Runtimes\x64", "10.0\VC\VCRedist\x64")) {
        $key = $base.OpenSubKey("SOFTWARE\Microsoft\VisualStudio\$k")
        $r[$k] = if ($key) { "$($key.GetValue('Installed')) $($key.GetValue('Version'))" } else { "absent" }
    }
    $r
}

Say "Before install: $((Runtimes | ConvertTo-Json -Compress))"
$result.runtimes_before = Runtimes
$result.msvcp140_before = Test-Path "$env:windir\System32\msvcp140.dll"

# WebView2 as the installer sees it (EdgeUpdate "pv"), and as it really is (its files).
function WebView2 {
    $guid = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    [ordered]@{
        hklm_wow = (Get-ItemProperty "HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\$guid" -ErrorAction SilentlyContinue).pv
        hklm     = (Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\EdgeUpdate\Clients\$guid" -ErrorAction SilentlyContinue).pv
        hkcu     = (Get-ItemProperty "HKCU:\SOFTWARE\Microsoft\EdgeUpdate\Clients\$guid" -ErrorAction SilentlyContinue).pv
        files    = @(Get-ChildItem "${env:ProgramFiles(x86)}\Microsoft\EdgeWebView\Application", "$env:LOCALAPPDATA\Microsoft\EdgeWebView\Application" -Filter "msedgewebview2.exe" -Recurse -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName })
    }
}
$result.webview2_before = WebView2
Say "WebView2 before: $(($result.webview2_before) | ConvertTo-Json -Compress)"
$wv = "C:\test\diag\MicrosoftEdgeWebView2RuntimeInstallerX64.exe"
if (Test-Path $wv) {
    # What Microsoft's installer says here, run exactly as VEXT's setup runs it (not elevated).
    $w = Start-Process -FilePath $wv -ArgumentList "/silent", "/install" -Wait -PassThru
    $result.webview2_installer_exit = $w.ExitCode
    Say ("WebView2 offline installer (not elevated): exit {0} (0x{0:X8})" -f $w.ExitCode)
    $result.webview2_after_own_install = WebView2
    Say "WebView2 after it: $(($result.webview2_after_own_install) | ConvertTo-Json -Compress)"
}

# 1. Install, silently, as a user would (the installer asks for permission once if runtimes are missing).
$setup = Get-ChildItem "C:\release" -Filter "VEXT-*-setup.exe" | Select-Object -First 1
$result.installer = $setup.Name
$result.installer_sha256 = (Get-FileHash $setup.FullName -Algorithm SHA256).Hash
$result.tested_at_utc = [DateTime]::UtcNow.ToString("o")
Say "Installing $($setup.Name)"
$p = Start-Process -FilePath $setup.FullName -ArgumentList "/S" -Wait -PassThru
$result.setup_exit = $p.ExitCode
Say "setup exit $($p.ExitCode)"
$result.runtimes_after = Runtimes
Say "After install: $(($result.runtimes_after) | ConvertTo-Json -Compress)"
$backend = Get-ChildItem "$env:LOCALAPPDATA", "$env:ProgramFiles" -Recurse -Filter "vext-backend.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
$result.installed_backend = if ($backend) { $backend.FullName } else { $null }
$result.redist_installed = if ($backend) { Test-Path (Join-Path $backend.DirectoryName "redist\vc_redist.x64.exe") } else { $false }
Say "Backend: $($result.installed_backend)"
$rtLog = if ($backend) { Join-Path $backend.DirectoryName "redist\install-runtimes.log" } else { "" }
if ($rtLog -and (Test-Path $rtLog)) { Copy-Item $rtLog (Join-Path $out "install-runtimes.log") }

if ($backend) {
    # 2. The backend's own runtime check.
    $env:VEXT_BACKEND_PORT = "8765"
    $env:VEXT_ROOT = "C:\vext-root"
    $srv = Start-Process -FilePath $backend.FullName -PassThru -WindowStyle Hidden
    $rt = $null
    for ($i = 0; $i -lt 90 -and -not $rt; $i++) {
        Start-Sleep -Seconds 1
        try { $rt = Invoke-RestMethod "http://127.0.0.1:8765/api/system/runtime" -TimeoutSec 3 } catch { }
    }
    $result.runtime_check = $rt
    # Installing again (an update) must find nothing left to do: no second permission prompt.
    $again = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $backend.DirectoryName "redist\install-runtimes.ps1") -DryRun -Log (Join-Path $out "dryrun-after.log") *>&1 | Out-String
    $result.second_install_would_run = @($again -split "`r?`n" | Where-Object { $_ -match "would run" })
    Say "A second install would run: $(if ($result.second_install_would_run.Count) { $result.second_install_would_run -join '; ' } else { 'nothing' })"
    Say "Runtime check: $($rt | ConvertTo-Json -Compress -Depth 4)"
    Stop-Process -Id $srv.Id -Force -ErrorAction SilentlyContinue

    # 3. A real plugin through VEXT's plugin host (probe: load + process audio, then exit).
    $plugin = "C:\plugins\OTT.vst3"
    if (Test-Path $plugin) {
        Say "Probing $plugin"
        $probe = Start-Process -FilePath $backend.FullName -ArgumentList "--host-worker", "`"$plugin`"", "--format", "VST3", "--probe" `
            -Wait -PassThru -RedirectStandardOutput (Join-Path $out "probe-stdout.txt") -RedirectStandardError (Join-Path $out "probe-stderr.txt") -WindowStyle Hidden
        $result.probe_exit = $probe.ExitCode
        $result.probe_events = Get-Content (Join-Path $out "probe-stdout.txt") -ErrorAction SilentlyContinue | Where-Object { $_ -match '"event"' }
        Say ("Probe exit {0} (0x{0:X8})" -f $probe.ExitCode)
    } else {
        Say "No plugin mapped at ${plugin}: probe skipped"
    }
}

$ok = ($result.setup_exit -eq 0) -and $result.runtime_check -and $result.runtime_check.ok -and ($result.probe_exit -eq 0) -and ($result.second_install_would_run.Count -eq 0) -and $result.runtime_check.legacy.'2013' -and $result.runtime_check.legacy.'2012' -and $result.runtime_check.legacy.'2010'
$result.verdict = if ($ok) { "PASS" } else { "FAIL" }
Say "Verdict: $($result.verdict)"
$result | ConvertTo-Json -Depth 6 | Set-Content (Join-Path $out "result.json") -Encoding utf8
