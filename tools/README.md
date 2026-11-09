# Build and release tools

Run commands from the repository root. Helpers are grouped by purpose:

| Folder | Purpose |
|---|---|
| `build/` | Backend/native/desktop builds, runtime downloads, bundle validation |
| `release/` | Versioning, signing, third-party notices, draft release preparation |
| `branding/` | Logo and icon generation |
| `tests/` | Python and JavaScript checks, including opt-in native-plugin fixtures |
| `sandbox/` | Windows Sandbox installer and clean-PC checks |

Keep generated output in the ignored root `build/`, `dist/`, `release/` or `.scratch/`;
the source directories `tools/build/` and `tools/release/` remain tracked.
Tests resolve the repository from their own location and use disposable data.
Run one Python test file per process. Installed-native tests require explicit
`--installed-root`; do not add them to automatic test loops.

| Tool | What it does | Run alone |
|---|---|---|
| `build/build_release.ps1` | Builds the sidecar, adapter, 32-bit host, Tauri installer, portable folder/ZIP, provenance and checksums. Local testing builds record dirty source; publication rejects it. | `.\tools\build\build_release.ps1` |
| `build/build_sidecar.ps1` | Rebuilds the development backend, native adapter and 32-bit host after backend changes. | `.\tools\build\build_sidecar.ps1` |
| `branding/update_logo.ps1` | Refreshes icons and frontend logos from the maintained exports, using `branding/make_logo_svg.py` when the private master is available. | `.\tools\branding\update_logo.ps1` |
| `build/build_fl_native.ps1` | Builds VEXT's own x64 FL native adapter into `build/fl-native/` for the sidecar. Uses installed Image-Line files only at runtime; none are bundled. | `.\tools\build\build_fl_native.ps1` |
| `build/fetch_redist.ps1` | Downloads what VEXT's installer carries from Microsoft: the Visual C++ runtimes (14.x newest channel x64 and x86 + 2013 / 2012 / 2010 x64) into `desktop\src-tauri\redist\`, and the WebView2 offline installer into `desktop\src-tauri\webview2\` (both git-ignored). Every file must be validly signed by Microsoft or it's deleted and the build stops. Copies `desktop\src-tauri\installer\install-runtimes.ps1` next to the runtimes. | `.\tools\build\fetch_redist.ps1` (`-Refresh` downloads again: do it before each release, the 14.x runtime moves) |
| `build/check_bundle.py` | Fails if the PyInstaller backend (`dist\vext-backend.exe`) carries its own copy of the C++ runtime (`msvcp140.dll`, `vcruntime140*.dll`, …). A private copy shadows Windows' and once crashed every plugin; `vext-backend.spec` strips them. Also takes a one-folder bundle (`dist\host32`). | `.venv-native\Scripts\python.exe tools\build\check_bundle.py dist\vext-backend.exe` |
| `build/setup_x86_host.ps1` | Unpacks Python's official 32-bit build (NuGet `pythonx86`, signature-checked) into `.python-x86\` and installs `requirements-x86.txt`: the interpreter of the 32-bit plugin host (see `build/build_host32.ps1`). A dev backend uses it as soon as it exists. | `.\tools\build\setup_x86_host.ps1` (`-Refresh` starts over) |
| `build/build_host32.ps1` | Builds the 32-bit plugin host (`vext-host32.spec`, one folder) with that Python, checks it with `build/check_bundle.py`, and copies it to `desktop\src-tauri\host32\` (an installer resource, git-ignored). | `.\tools\build\build_host32.ps1` |
| `sandbox\` | The clean-PC test: installs `release\VEXT-*-setup.exe` in Windows Sandbox (fresh Windows, no runtimes, no internet), checks the runtimes, the app's own runtime check, that a second install has nothing to do, and that Xfer OTT loads. See `sandbox\README.md`. | `.\tools\sandbox\run.ps1` → `tools\sandbox\results\result.json` (PASS / FAIL) |

The testing series starts at `0.9.0-beta.1`; `release/bump_version.ps1` accepts stable and
alpha/beta/rc versions. Publication still requires clean source and exact-installer Sandbox validation.

`tests/test_fl_native.py` checks synthetic native identity and capability guards.
`node tools/tests/test_fl_native_warnings.js` checks persistent, escaped compatibility notices and Monitor state.
`test_fl_native_audition.py --installed-root <Plugins/Fruity>` checks Newtime/Newtone Monitor
on/off/restart, bypass-mute, Analyzer samples, no MIDI/page transport, capture and cleanup
at three rates with generated clips and a muted device substitute.

`tests/test_fl_native_audio.py` skips installed-binary checks by default. Pass
`--installed-root <FL installation's Plugins/Fruity folder>` to test the audio-enabled
effects in isolated processes with hidden editors and no audio device. It checks
44.1/48/96 kHz rendering, bypass, recorded samples and Analyzer taps; Balance also
checks native volume changes and stereo isolation. No real VEXT or FL user data is used.

`release/check_version.ps1` validates Tauri, npm and Cargo manifests and locks; `-Tag vX.Y.Z` also checks the
release notes. `bump_version.ps1 -Version X.Y.Z` updates them together (`-DryRun` previews).
`release/publish_release.ps1` verifies checksums and clean source provenance, packages the portable ZIP once, and
creates a draft pinned to the build commit. `-ReleaseDir` accepts a downloaded CI artifact; `-SkipGitHub`
prepares locally. It never replaces an existing release. See LOCAL_SETUP.md for the branch/rebase policy,
90-day build artifacts, and testing the exact downloaded installer before publication.

Installer integration:

- `desktop\src-tauri\installer\installer-hooks.nsh` — the setup's post-install step:
  checks WebView2 (registry or files) and the runtimes (registry / DLLs), and runs
  `install-runtimes.ps1` elevated only when something is missing (one permission prompt).
- `desktop\src-tauri\installer\install-runtimes.ps1` — installs / repairs them
  (`-Repair`, `-WebView2 <installer>`, `-DryRun` to see what it would do, `-Log <file>`).
  The app's **Repair** button (`app/runtime_check.py`) runs it too.
- `branding/update_logo.ps1` + `branding/make_logo_svg.py` (in `tools/branding/`) — regenerate the app icons and the
  in-app logo from `sources\`; `build/build_release.ps1` runs it first.

Vocodex's opt-in effect fixture uses its hash-validated native L-R checkbox through local
window messages (no system pointer input), requires both stereo inputs, and leaves the
normal startup mode unchanged. Convolver loads a generated two-tap IR with dry muted;
wet mute must silence it. Scratcher stays outside the audio suite until host sample
loading is implemented; passing dry input alone is insufficient.

`test_fl_native_midi.py --installed-root <Plugins/Fruity>` validates enabled native instruments
with isolated hidden editors and no audio device: MIDI timing, notes, sustain, polyphony,
release tails, panic and reset at 44.1/48/96 kHz. `--plugin` selects an exact relative binary.
`--block-size 64|512|1024` exercises different render blocks. Kepler, Kepler Exo and
Transistor Bass and SoundFont Player also check queued notes and paused/playing clocks immediately after reset.
Native editor preview callbacks 20/21/56, tempo/pause/seek, and 3x Osc / FL Keys
A4 and bend checks are included. Direct mixing callbacks cover absent optional buses,
ramp continuity and old/new pan endpoints. File resolution rejects missing/nonlocal paths and leaves selected
files unchanged; suspended output drops notes/previews, preserves clock state and resumes ticks.
Drumaxx uses explicit pad mappings and SoundFont Player an installed factory SF2 fixture.
Slicex loads a generated eight-second WAV and keeps slicing local, without piano-roll export.
Both polyphony pitches must sound independently before their combined trial. Tails must decay within 30 rendered seconds.

`test_fl_native_container.py --installed-root <Plugins/Fruity>` checks native instrument,
effect and editor-only container handoffs, reveal, manual capture, processing controls,
MIDI/Analyzer and worker cleanup using a silent output substitute.
Its 54 cases include both Patcher variants, all three Mobile editors and five VFX
mapping/envelope editors, with
VFX Script refused before worker startup. Mobile's installed resources are copied
through an explicit UI/DSP allowlist; the guard test excludes catalogs, settings,
samples and binaries.

`test_fl_native_editor_timing.py --installed-root <Plugins/Fruity>` checks Newtone's
static rate/tempo/signature clock-period metadata at 44.1/48/96 kHz without running
audio, MIDI or a clock. Its private wrapper layout is tied to the validated binary hash.

`test_fl_native_clock.py --installed-root <Plugins/Fruity>` verifies Big Clock's
clock-only tempo, elapsed time, relocation and pause without an audio stream.
`node tools/tests/test_playground_transport.js` checks paused and idle page seeks, seek bounds,
comparison alignment and plugin clock ownership without a browser or audio device.
`tests/test_tempo.py` checks local pulse detection, noise/tone/silence fallback, uneven
live stereo blocks, manual override/persistence, transport ownership and API authentication.
Current native capabilities and user-facing limits are listed in `docs/FL_NATIVE_COMPATIBILITY.md`.
