# Build and release tools

Helpers used by `build_release.ps1` / `build_sidecar.ps1` (they run them for you),
and the clean-PC test. Background: AGENTS.md, "Self-contained install".

| Tool | What it does | Run alone |
|---|---|---|
| `fetch_redist.ps1` | Downloads what VEXT's installer carries from Microsoft: the Visual C++ runtimes (14.x newest channel x64 and x86 + 2013 / 2012 / 2010 x64) into `desktop\src-tauri\redist\`, and the WebView2 offline installer into `desktop\src-tauri\webview2\` (both git-ignored). Every file must be validly signed by Microsoft or it's deleted and the build stops. Copies `desktop\src-tauri\installer\install-runtimes.ps1` next to the runtimes. | `.\tools\fetch_redist.ps1` (`-Refresh` downloads again: do it before each release, the 14.x runtime moves) |
| `check_bundle.py` | Fails if the PyInstaller backend (`dist\vext-backend.exe`) carries its own copy of the C++ runtime (`msvcp140.dll`, `vcruntime140*.dll`, …). A private copy shadows Windows' and once crashed every plugin; `vext-backend.spec` strips them. Also takes a one-folder bundle (`dist\host32`). | `.venv-native\Scripts\python.exe tools\check_bundle.py dist\vext-backend.exe` |
| `setup_x86_host.ps1` | Unpacks Python's official 32-bit build (NuGet `pythonx86`, signature-checked) into `.python-x86\` and installs `requirements-x86.txt`: the interpreter of the 32-bit plugin host (AGENTS.md "32-bit plugins"). A dev backend uses it as soon as it exists. | `.\tools\setup_x86_host.ps1` (`-Refresh` starts over) |
| `build_host32.ps1` | Builds the 32-bit plugin host (`vext-host32.spec`, one folder) with that Python, checks it with `check_bundle.py`, and copies it to `desktop\src-tauri\host32\` (an installer resource, git-ignored). | `.\tools\build_host32.ps1` |
| `sandbox\` | The clean-PC test: installs `release\VEXT-*-setup.exe` in Windows Sandbox (fresh Windows, no runtimes, no internet), checks the runtimes, the app's own runtime check, that a second install has nothing to do, and that Xfer OTT loads. See `sandbox\README.md`. | `.\tools\sandbox\run.ps1` → `tools\sandbox\results\result.json` (PASS / FAIL) |

`check_version.ps1` validates Tauri, npm and Cargo manifests and locks; `-Tag vX.Y.Z` also checks the
release notes. `bump_version.ps1 -Version X.Y.Z` updates them together (`-DryRun` previews).
`publish_release.ps1` verifies checksums and clean source provenance, packages the portable ZIP once, and
creates a draft pinned to the build commit. `-ReleaseDir` accepts a downloaded CI artifact; `-SkipGitHub`
prepares locally. It never replaces an existing release. See LOCAL_SETUP.md for the branch/rebase policy,
90-day build artifacts, and testing the exact downloaded installer before publication.

Related, outside this folder:

- `desktop\src-tauri\installer\installer-hooks.nsh` — the setup's post-install step:
  checks WebView2 (registry or files) and the runtimes (registry / DLLs), and runs
  `install-runtimes.ps1` elevated only when something is missing (one permission prompt).
- `desktop\src-tauri\installer\install-runtimes.ps1` — installs / repairs them
  (`-Repair`, `-WebView2 <installer>`, `-DryRun` to see what it would do, `-Log <file>`).
  The app's **Repair** button (`app/runtime_check.py`) runs it too.
- `update_logo.ps1` + `make_logo_svg.py` (repo root) — regenerate the app icons and the
  in-app logo from `sources\`; `build_release.ps1` runs it first.
