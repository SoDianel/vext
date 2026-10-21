# Clean-PC test (Windows Sandbox)

Installs the latest `release\VEXT-*-setup.exe` in Windows Sandbox: a throwaway
Windows with no Visual C++ runtime and nothing of VEXT, wiped when its window
closes. It checks that the installer brings everything a new PC needs and that
a real plugin opens.

1. Once, enable Windows Sandbox (needs administrator rights and a restart), in
   an administrator PowerShell:
   `Enable-WindowsOptionalFeature -Online -FeatureName Containers-DisposableClientVM -All`
2. Build: `.\build_release.ps1`
3. Run: `.\tools\sandbox\run.ps1` (writes the `.wsb` with this machine's paths
   and opens the Sandbox). Allow the permission prompt *inside* the Sandbox if
   one appears: that's the installer asking to install the runtimes.
4. Results: `tools\sandbox\results\result.json` (`verdict` PASS / FAIL) and
   `log.txt`. Close the Sandbox window when it says "Verdict".

Mapped read-only: `release\` and `C:\Program Files\Common Files\VST3\XFER`
(Xfer OTT, the plugin probed). Change `$plugins` in `run.ps1` to probe another.
