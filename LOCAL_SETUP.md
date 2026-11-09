# Build and run VEXT

VEXT targets Windows 10/11 x64. The backend runs locally as Python or as the
packaged desktop sidecar. Public feature and safety information is in
[README.md](README.md); project maintenance policy is in [CONTRIBUTING.md](CONTRIBUTING.md).

## Requirements

- Python 3.12 or newer, x64.
- Node.js and npm for the Tauri CLI.
- Rust through rustup and the MSVC C++ Build Tools, including the
  Desktop development with C++ workload.
- Windows WebView2 for the desktop interface. The installer supplies missing
  WebView2 and Microsoft Visual C++ runtimes.

Run the following commands in PowerShell from the repository root:

```powershell
python -m venv .venv-native
.\.venv-native\Scripts\python.exe -m pip install -r requirements.txt pyinstaller
npm --prefix desktop ci
```

## Data and development isolation

Installed and portable builds store the database, quarantine, pictures and logs
under `%APPDATA%\com.vext.app`. `VEXT_ROOT` overrides this directory. The portable
shell keeps its default browser profile beside the executable.

For development with disposable data, set both paths before starting the app:

```powershell
$env:VEXT_ROOT = Join-Path $env:TEMP "vext-dev-$PID"
$env:WEBVIEW2_USER_DATA_FOLDER = Join-Path $env:VEXT_ROOT "WebView2"
```

Tests supply temporary roots and fake plugin folders. Never use a real plugin
inventory, FL Studio user folder or application database as a test fixture.

## Run the backend

```powershell
.\.venv-native\Scripts\python.exe -m uvicorn app.web:app --host 127.0.0.1 --port 8000
```

Open <http://localhost:8000>. The page receives a per-launch API token from the
backend. Keep the listener on localhost. Restart the server after Python changes.

For the read-only command-line scan:

```powershell
.\.venv-native\Scripts\python.exe -m app.main scan
```

## Run the desktop app

```powershell
.\tools\build\build_sidecar.ps1
Push-Location desktop
npm run tauri dev
Pop-Location
```

The desktop shell starts the backend sidecar automatically. Rebuild the sidecar
after changes under `app/` or to `sidecar_main.py`. Frontend files are mirrored
from `app/static/` on each Tauri dev/build invocation.

The sidecar build also prepares the 32-bit VST2 host. To prepare its interpreter
separately, run `.\tools\build\setup_x86_host.ps1`; it lives in ignored `.python-x86/`.
32-bit FL native engines, VST3 and CLAP are unsupported.

## Tests

Install test dependencies with
`.\.venv-native\Scripts\python.exe -m pip install pytest httpx`.
Run one Python test file per process:

```powershell
.\.venv-native\Scripts\python.exe -m pytest tools/tests/test_storage.py -q
.\.venv-native\Scripts\python.exe tools/tests/test_fl_native.py
node tools/tests/test_fl_native_warnings.js
```

Some checks are standalone scripts rather than collected pytest tests. Native
installed-binary tests require an explicit `--installed-root`; they are opt-in
and must not be added to unattended runs. See [tools/README.md](tools/README.md)
for each helper and its test boundary.

## Installer and portable build

```powershell
.\tools\build\build_release.ps1
```

The pipeline checks manifest/lockfile versions, refreshes branding, verifies
Microsoft runtime signatures, builds the native adapter, backend and 32-bit
host, and builds the Tauri shell and NSIS installer. Bundle checks reject private
Microsoft C++ runtime DLLs. Image-Line plugins and factory assets are not bundled.

Outputs are under ignored `release/`:

- `VEXT-<version>-setup.exe`;
- `VEXT-<version>-portable/` and its ZIP;
- `SHA256SUMS.txt`, `BUILD_PROVENANCE.json`, licenses and notices.

The portable folder must keep `VEXT.exe`, `vext-backend.exe`, `host32/` and
`redist/` together. Local test builds may record dirty source; published assets
require a clean recorded source commit and verified checksums.

## Versioning and maintainer releases

The testing series starts at `0.9.0-beta.1`. Use `0.x.y-beta.N` for beta builds;
`1.0.0` is reserved for the announced stable release.

`tools/release/bump_version.ps1` updates manifests, lockfiles and changelog together.
`tools/release/check_version.ps1` validates them; `-Tag v<version>` also checks the
release's dated changelog entry. Refresh notices with
`python tools/release/gen_notices.py` when dependencies change.

CI checks branch pushes and maintainer PRs. Each `main` push builds Windows
artifacts with provenance and checksums, retained for 90 days. Maintainer changes
use topic branches and Rebase and merge; merge commits and squash merges are not used.

Before publication, download the exact CI installer and run
`.\tools\sandbox\run.ps1` in a checkout containing its release files. Retain its
SHA-256 with the Sandbox PASS result. Another build or an older PASS does not
validate that installer. Windows Sandbox is not available on hosted GitHub runners.

`tools/release/publish_release.ps1` verifies clean-source provenance and checksums,
packages notes and creates a draft pinned to the build commit. `-ReleaseDir`
accepts downloaded build artifacts; `-SkipGitHub` prepares locally. Tags or an
explicit release-workflow dispatch can also prepare a draft. Publication is a
separate maintainer action. Existing releases are never overwritten by the helper.

## Optional signing

Unsigned builds display a Windows SmartScreen warning. Signing is enabled only
when configured through environment variables:

- PFX certificate: `VEXT_SIGN_PFX` and `VEXT_SIGN_PFX_PASSWORD`.
- Azure signing: `VEXT_SIGN_AZURE_DLIB` and `VEXT_SIGN_AZURE_METADATA`.
- Optional tool/timestamp overrides: `VEXT_SIGNTOOL`, `VEXT_SIGN_TIMESTAMP`.

See `tools/release/sign_file.ps1`. Keep credentials and certificates outside the repository.

## Cleanup safety and troubleshooting

Scanning is read-only. Cleanup is proposed, reviewed and confirmed before any
file move. Delete sends a plugin to quarantine; permanent deletion is available
from Quarantine. Storage caches and out-of-scope entries have their own confirmations.
Windows-protected files are never unlocked automatically; elevation is offered
only after a real permission-denied error and remains the user's choice.

If the desktop backend cannot start, rebuild the sidecar and check the app log.
For a missing runtime, use the app's Repair action with the bundled `redist/`
folder. If port 8000 is occupied, close the other local instance before retrying.
