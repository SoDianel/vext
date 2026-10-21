# Installs (or repairs) the Microsoft Visual C++ runtimes VEXT ships in its
# redist folder. Run elevated, once, by the installer (NSIS_HOOK_POSTINSTALL in
# installer-hooks.nsh) and by Configuration's / the warning's "Repair" button
# (app/runtime_check.py). One script, so Windows asks for permission only once.
#
#   powershell -ExecutionPolicy Bypass -File install-runtimes.ps1 [-Repair] [-Log <file>] [-DryRun]
#              [-WebView2 <MicrosoftEdgeWebView2RuntimeInstallerX64.exe>]
#
# Without -Repair it only installs what's missing or older than the copy
# shipped; with -Repair it reinstalls the 14.x runtime (a damaged one) too.
# -WebView2 also installs WebView2 with that installer when it's really missing
# (the setup hook passes it only then; present = EdgeUpdate's registry entry OR its files).
# -DryRun only says what it would run (no elevation needed; for tests).
# Exit code: 0 done, 3010 done but Windows wants a restart, 1 something failed.

param([switch]$Repair, [string]$Log = "", [switch]$DryRun, [string]$WebView2 = "")

$ErrorActionPreference = "Continue"
$here = $PSScriptRoot
if (-not $Log) { $Log = Join-Path $env:TEMP "vext-runtimes.log" }

function Say([string]$text) {
    $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $text
    try { Add-Content -Path $Log -Value $line -Encoding utf8 } catch { }
}

function Reg64([string]$key, [string]$name) {
    try {
        $base = [Microsoft.Win32.RegistryKey]::OpenBaseKey("LocalMachine", "Registry64")
        $k = $base.OpenSubKey($key)
        if ($k) { return $k.GetValue($name) }
    } catch { }
    return $null
}

# Runs one installer; returns its exit code (1638 = a newer one is already there).
function Run([string]$file, [string]$arguments) {
    $path = Join-Path $here $file
    if (-not (Test-Path $path)) { Say "missing: $file"; return 1 }
    Say "running $file $arguments"
    if ($DryRun) { Write-Host "would run: $file $arguments"; return 0 }
    $p = Start-Process -FilePath $path -ArgumentList $arguments -Wait -PassThru -WindowStyle Hidden
    Say "  -> exit $($p.ExitCode)"
    return $p.ExitCode
}

$result = 0
function Note([int]$code) {
    if ($code -eq 3010 -or $code -eq 1641) { if ($script:result -eq 0) { $script:result = 3010 } }
    elseif ($code -ne 0 -and $code -ne 1638 -and $code -ne 1618) { $script:result = 1 }
}

Say "VEXT runtimes: start (repair: $Repair)"

# WebView2 first: without it VEXT has no window at all.
function HasWebView2 {
    $guid = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    foreach ($k in @("HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\$guid", "HKCU:\SOFTWARE\Microsoft\EdgeUpdate\Clients\$guid")) {
        $pv = (Get-ItemProperty $k -ErrorAction SilentlyContinue).pv
        if ($pv -and $pv -ne "0.0.0.0") { return $true }
    }
    foreach ($d in @("${env:ProgramFiles(x86)}\Microsoft\EdgeWebView\Application", "$env:LOCALAPPDATA\Microsoft\EdgeWebView\Application")) {
        if (Get-ChildItem $d -Filter "msedgewebview2.exe" -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1) { return $true }
    }
    return $false
}
if ($WebView2) {
    if (HasWebView2) {
        Say "WebView2: already there"
    } elseif (-not (Test-Path $WebView2)) {
        Say "WebView2: missing, and its installer isn't at $WebView2"; $result = 1
    } elseif ($DryRun) {
        Write-Host "would run: WebView2 /silent /install"
    } else {
        Say "running WebView2 /silent /install"
        $w = Start-Process -FilePath $WebView2 -ArgumentList "/silent", "/install" -Wait -PassThru -WindowStyle Hidden
        Say ("  -> exit {0} (0x{0:X8}); present now: {1}" -f $w.ExitCode, (HasWebView2))
        if (-not (HasWebView2)) { $result = 1 }
    }
}

# 2015-2026 (14.x): VEXT and today's plugins. Compared with the copy shipped.
$shipped = $null
# major.minor.build only: the registry has no revision, and [version] counts a missing
# revision as older (14.51.36247 < 14.51.36247.0), which would reinstall every time.
try {
    $v = [version](Get-Item (Join-Path $here "vc_redist.x64.exe")).VersionInfo.ProductVersion
    $shipped = [version]::new($v.Major, $v.Minor, $v.Build)
} catch { }
$key = "SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64"
$installed = $null
if ((Reg64 $key "Installed") -eq 1) {
    $installed = [version]::new([int](Reg64 $key "Major"), [int](Reg64 $key "Minor"), [int](Reg64 $key "Bld"))
}
Say "14.x installed: $installed, shipped: $shipped"
if ($Repair) {
    $verb = if ($installed -and $shipped -and $installed -ge $shipped) { "/repair" } else { "/install" }
    Note (Run "vc_redist.x64.exe" "$verb /passive /norestart")
} elseif (-not $installed -or ($shipped -and $installed -lt $shipped)) {
    Note (Run "vc_redist.x64.exe" "/install /passive /norestart")
}

# The same 14.x runtime, 32-bit: the 32-bit plugin host (host32\vext-host32.exe) and
# the 32-bit plugins it loads. It registers in the 32-bit view (WOW6432Node).
$shipped86 = $null
try {
    $v = [version](Get-Item (Join-Path $here "vc_redist.x86.exe")).VersionInfo.ProductVersion
    $shipped86 = [version]::new($v.Major, $v.Minor, $v.Build)
} catch { }
$key86 = "SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\x86"
$installed86 = $null
if ((Reg64 $key86 "Installed") -eq 1) {
    $installed86 = [version]::new([int](Reg64 $key86 "Major"), [int](Reg64 $key86 "Minor"), [int](Reg64 $key86 "Bld"))
}
Say "14.x x86 installed: $installed86, shipped: $shipped86"
if ($shipped86) {
    if ($Repair) {
        $verb = if ($installed86 -and $installed86 -ge $shipped86) { "/repair" } else { "/install" }
        Note (Run "vc_redist.x86.exe" "$verb /passive /norestart")
    } elseif (-not $installed86 -or $installed86 -lt $shipped86) {
        Note (Run "vc_redist.x86.exe" "/install /passive /norestart")
    }
}

# Older runtimes, for older plugins: installed when missing (repaired with -Repair).
# Present = its DLL in the 64-bit System32. Not the registry alone: these x64
# installers are 32-bit programs and register under WOW6432Node, which a 64-bit
# lookup never sees (a Sandbox test installed all three and still read "absent").
$sys64 = if ([Environment]::Is64BitProcess) { "$env:windir\System32" } else { "$env:windir\Sysnative" }
$legacy = @(
    @{ File = "vcredist_2013_x64.exe"; Dll = "msvcr120.dll"; Args = "/install /passive /norestart"; Fix = "/repair /passive /norestart" },
    @{ File = "vcredist_2012_x64.exe"; Dll = "msvcr110.dll"; Args = "/install /passive /norestart"; Fix = "/repair /passive /norestart" },
    @{ File = "vcredist_2010_x64.exe"; Dll = "msvcr100.dll"; Args = "/passive /norestart"; Fix = "/passive /norestart" }
)
foreach ($r in $legacy) {
    $have = Test-Path (Join-Path $sys64 $r.Dll)
    Say "$($r.File): installed $have"
    if (-not $have) { Note (Run $r.File $r.Args) }
    elseif ($Repair) { Note (Run $r.File $r.Fix) }
}

Say "VEXT runtimes: done, exit $result"
exit $result
