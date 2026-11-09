# Build only VEXT's own native adapter; Image-Line binaries stay in their installation.
$ErrorActionPreference = 'Stop'
$root = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$output = Join-Path $root 'build\fl-native'
$source = Join-Path $root 'app\hosting\fl_native.cpp'
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
$vs = & $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if ($LASTEXITCODE -ne 0 -or -not $vs) { throw 'Visual C++ x64 tools not found' }
$vcvars = Join-Path $vs 'VC\Auxiliary\Build\vcvars64.bat'
New-Item -ItemType Directory -Force -Path $output | Out-Null
Push-Location $output
try {
    $command = 'call "' + $vcvars + '" && cl /nologo /std:c++17 /O2 /MD /EHsc /LD "' + $source + '" /Fe:vext-fl-native.dll /link /INCREMENTAL:NO'
    $ErrorActionPreference = 'Continue'
    & $env:ComSpec /d /c $command *> (Join-Path $output 'build.log')
    $ErrorActionPreference = 'Stop'
    if ($LASTEXITCODE -ne 0) { throw "Native adapter compilation failed; see $output\build.log" }
    Get-FileHash -LiteralPath (Join-Path $output 'vext-fl-native.dll') -Algorithm SHA256
} finally { Pop-Location }
