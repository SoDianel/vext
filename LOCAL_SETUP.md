# Local setup & operations notes

Local-only reference for running and maintaining this app on this machine. This is a real git repo
now (linked to a Codex cloud session too — see `AGENTS.md`), but this file specifically is still
fine to add real paths/notes to freely.

For what this project *is* and why it's built this way, see [AGENTS.md](AGENTS.md). This file is
just "how do I start it / where's the data / how do I fix it."

**No Docker anymore** (see AGENTS.md's "Docker-to-native migration") — the backend runs natively on
Windows, either directly via Python (dev) or bundled as a Tauri sidecar (the packaged `.exe`). This
also means "run as Administrator" now genuinely works as a fix for the Windows-permission-denied
error some plugin locations trigger (see AGENTS.md) — elevating the whole app elevates the backend
too, since it's a real child process now, not something running inside a container.

## Prerequisites

- **Python 3.12 or newer** (native, on Windows; tested with 3.14,
  64-bit). No Docker Desktop needed anymore.
- **Rust** (`rustup`) and the **MSVC C++ Build Tools** (`Desktop development with C++` workload) —
  for the desktop app. If setting this up fresh elsewhere:
  ```
  winget install --id Rustlang.Rustup -e
  winget install --id Microsoft.VisualStudio.2022.BuildTools -e --override "--quiet --wait --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"
  ```
  (open a new terminal afterward so PATH picks up `cargo`/`rustc`)
- **Node/npm** — for the Tauri CLI.

## Running the backend directly (fastest for iterating on `app/`)

```
python -m venv .venv-native        # one-time
.venv-native\Scripts\pip install -r requirements.txt
.venv-native\Scripts\python -m uvicorn app.web:app --host 127.0.0.1 --port 8000
```
Then open **http://localhost:8000**. This is the quickest loop for backend changes — no rebuild
step, just restart uvicorn (or use `--reload` for auto-restart on file changes). Paths resolve
relative to the current working directory by default (`VEXT_ROOT` env var overrides this
— see `app/config.py` — matters for the packaged sidecar, not for this dev loop as long as you run
it from the repo root).

32-bit plugins open through a 32-bit Python (AGENTS.md "32-bit plugins"). Set it up once,
and the dev backend uses it by itself:
```
.\tools\setup_x86_host.ps1         # -> .python-x86\ (git-ignored)
```

CLI-only scan, no web server:
```
.venv-native\Scripts\python -m app.main scan
```

## Running the desktop app

```
cd desktop
npm install        # first time only
npm run tauri dev
```
This spawns the backend as a **sidecar** process automatically (see `desktop/src-tauri/src/lib.rs`)
— you do **not** need to start uvicorn yourself first. The sidecar binary must already exist at
`desktop/src-tauri/binaries/vext-backend-x86_64-pc-windows-msvc.exe` — build/rebuild it
with:
```
.\build_sidecar.ps1
```
**Rebuild the sidecar after any change under `app/`** (or `sidecar_main.py`) — unlike the frontend
(which `copy-frontend.js` re-mirrors automatically on every `tauri dev`/`tauri build`), the sidecar
is a separate compiled binary that doesn't auto-rebuild. `build_sidecar.ps1` creates `.venv-native`
on first run if it doesn't exist yet.

**Known first-launch quirk (already handled, don't remove the fix)**: the sidecar takes a moment
after the window appears to finish starting its HTTP listener, so the very first page load can lose
that race and show a blank UI. `app/static/app.js:waitForBackend()` polls `/api/stats` before running
`init()` specifically to cover this — confirmed real via CDP (a plain page reload after the race
always worked, proving it wasn't a persistent failure).

### Running as Administrator (fixes the "Permission denied" file error)

Some plugin files under `Program Files\Common Files\...` are Windows-protected (TrustedInstaller/
SYSTEM-owned — see AGENTS.md for the full ACL investigation) and quarantine/reorganize/delete on them
fails with a clear "Windows denied deleting the original file (Permission denied)" error. This is
intentional Windows behavior, not a bug — the fix is to run this app elevated, which is now your own
deliberate choice to make, same as any other app:
- Right-click the `.exe` → "Run as Administrator", **or**
- When an action fails with that specific error, the app itself offers a "Relaunch as Administrator
  and try again?" prompt (`relaunch_elevated` Tauri command) — only appears after a real failure,
  never automatic, never on every launch.

## Compiling to a standalone `.exe`

```
.\build_release.ps1
```

It fetches Microsoft's runtime and WebView2 installers (`tools\fetch_redist.ps1`, signature-checked),
bundles the backend (`vext-backend.spec`) and checks it carries no private C++ runtime
(`tools\check_bundle.py`), builds the 32-bit plugin host (`tools\build_host32.ps1`, which sets up
`.python-x86` on first run), then builds the Tauri shell and its installer. See `tools\README.md`.

**Output** (`release\`):
- `VEXT-<version>-setup.exe` — the installer to hand to anyone (per-user; Start Menu entry,
  uninstaller). It brings everything a new PC needs: WebView2 (offline) and Microsoft's Visual C++
  runtimes, installed only when missing or older (Windows asks for permission once). No MSI: an
  MSI can't run those installers.
- `VEXT-<version>-portable\` — `VEXT.exe` + `vext-backend.exe` + `host32\` (the 32-bit plugin
  host; all must sit together) + `redist\` for the in-app runtime Repair.
- `SHA256SUMS.txt`.

**A standalone package**: the backend (Python + all deps), the frontend and the native shell are all
bundled. Data (database, quarantine, cache) lives in `%APPDATA%\com.vext.app`. At every start VEXT
checks the C++ runtime plugins will use and shows a **Repair** banner if it's missing, too old or
damaged.

**Clean-PC test**: `.\tools\sandbox\run.ps1` installs the release in Windows Sandbox (fresh Windows,
no runtimes, no internet) and reports PASS / FAIL (`tools\sandbox\README.md`).

**When you need to rebuild:**
- Any change under `app/` or `sidecar_main.py` → `.\build_sidecar.ps1`, then rebuild the Tauri app.
- Any change under `app/static/` (frontend) → picked up automatically by `copy-frontend.js` on the
  next `tauri build`, no separate step.
- Any change under `desktop/src-tauri/` (Rust shell) → Cargo recompiles only what changed, usually
  fast.

## Where everything lives

| What | Where | Notes |
|---|---|---|
| SQLite database | `data/plugins.db` | Source of truth. Delete it and rescan any time to start clean. |
| Human-readable export | `DATABASE.md` (repo root) | Regenerated on every scan. |
| Quarantined files | `data/quarantine/` | Mirrors the original path (e.g. `data/quarantine/Program Files/Common Files/VST3/Foo.vst3`). Nothing here was auto-deleted — this tool only ever *moves* files here; permanently deleting them is on you (or use "Delete All Forever" in the Quarantine tab). |
| Pending/active actions | inside `plugins.db`, table `action_log` | Only ever holds *current* state — proposed-but-not-applied, or applied-quarantine-awaiting-restore/delete. Not a history: once an action resolves (restore, permanent delete, reorganize), its row is deleted. |
| Terminal-action audit log | `data/actions.log` | Plain text, append-only (`app/applog.py`). Every restore, permanent delete, reorganize, and quarantine-entry-removed-because-the-file-vanished-outside-the-app event is logged here with full detail — the UI only ever shows a generic one-line toast for these, by design (nothing "historical" is kept in the app itself). |
| This app's code | `app/` | `web.py` (FastAPI), `static/` (frontend), rest is the scanner/enrichment pipeline. |
| Backend sidecar source | `sidecar_main.py` (repo root) | PyInstaller entrypoint — imports `app.web:app` directly (not as a string) so PyInstaller's static analysis bundles it; see `build_sidecar.ps1`. |
| Built sidecar binary | `desktop/src-tauri/binaries/` | Gitignored (build output). Regenerate with `build_sidecar.ps1`. |
| 32-bit plugin host | `host32_main.py`, `vext-host32.spec`; built into `desktop/src-tauri/host32/` | Gitignored build output, from `tools\build_host32.ps1` (run by `build_sidecar.ps1`). Its 32-bit Python lives in `.python-x86\` (`tools\setup_x86_host.ps1`). |

## Running a scan

Two ways — same underlying code either way (`app/scan_service.py:run_scan`):

- **From the web UI**: click "Rescan now" in the top bar.
- **From the command line**: `python -m app.main scan` (with `.venv-native` active).

Either way this regenerates `data/plugins.db` and `DATABASE.md` from scratch — it's a full
rescan-and-replace of the `plugins` table (not incremental), so anything from a previous run that
no longer matches disk (e.g. you uninstalled a plugin) disappears, and manually-applied
reorganize/quarantine moves are picked up as the plugin's new location automatically. This also
means: if you delete a live plugin directly (outside the app), the next rescan just won't see it
anymore — always a fresh analysis, nothing stale carried forward.

Running natively is noticeably faster than the old Docker setup — no more bind-mount-through-WSL2
overhead, which used to be the documented bottleneck (several minutes for ~800 plugins).

There's also a **"Sync" button** next to "Rescan now" — much lighter, doesn't touch the plugin
directories at all. It only checks whether anything currently in quarantine has had its file
deleted directly (e.g. you emptied `data/quarantine/...` yourself in Explorer) and, if so, drops
that entry from the registry so it stops showing up anywhere in the app. A full rescan does this
check too, automatically, every time — Sync is just for when you want it reflected immediately
without waiting on/triggering a full rescan.

There's also an **"Include full C: scan"** checkbox next to Rescan — see AGENTS.md's full-scan
section; a genuine whole-drive walk, opt-in and slow by nature, for finding plugins installed
somewhere unexpected.

## Inspecting the database directly

Just a normal SQLite file now, no container needed at all:
```
.venv-native\Scripts\python -c "
import sqlite3
conn = sqlite3.connect('data/plugins.db')
for row in conn.execute('SELECT name, vendor, format FROM plugins LIMIT 10'):
    print(row)
"
```
Or point a real SQLite browser (e.g. DB Browser for SQLite) directly at `data\plugins.db`.

## Adding or changing a scanned plugin directory

Just add/edit a `ScanRoot(...)` entry in `app/config.py`'s `ROOTS` list with the real Windows path —
no docker-compose mount to keep in sync anymore, since the app reads the filesystem directly. Then
rescan.

For a one-off extra path without editing code, use the **"Add path to scope"** feature in the Out of
scope tab instead (see AGENTS.md) — same effect, no code change, persisted in the database.

## Changing the port

Web app defaults to `8000`. Change the `--port` in the uvicorn command (dev) or in
`sidecar_main.py`'s `uvicorn.run(...)` call (packaged) — and `API_BASE` in `app/static/app.js` to
match.

## Safety model (recap — see AGENTS.md for the full rationale)

- Scanning is always read-only.
- Every cleanup action (dedup removal, AAX removal, corrupted-file removal, vendor-folder
  reorganization) is **propose → review → explicit apply**. Proposing writes a DB row only.
  Applying is the only thing that touches a real file, and the UI asks you to confirm first.
- "Delete" is never a real delete — it's a move into `data/quarantine/`. You decide when (or
  whether) to actually remove something from there. (Out-of-scope items are the one exception —
  see AGENTS.md, deliberate and explicitly requested.)
- Windows file-permission protection (TrustedInstaller-owned paths) is never bypassed
  automatically — elevating is always the owner's own explicit choice. See AGENTS.md.

## Troubleshooting

- **Port 8000 already in use**: see "Changing the port" above, or find and stop whatever's using it.
- **Vendor names look unresolved**: VEXT resolves vendors from the plugin files themselves and a built-in list;
  it needs no installer archive.
- **Sidecar won't start / desktop app shows a blank UI that never resolves**: check
  `desktop/src-tauri/binaries/vext-backend-x86_64-pc-windows-msvc.exe` actually exists
  (run `build_sidecar.ps1` if not) and matches your Rust target triple (`rustc -vV` → `host:`).
- **"Permission denied" deleting a file**: see "Running as Administrator" above — this is
  intentional Windows protection, not a bug; the app will offer to relaunch elevated when it hits
  this specific error.
- **Want a totally clean slate**: delete the `data` folder and rescan. Nothing outside `data/` and
  `DATABASE.md` is ever written to by this app except inside actual plugin folders when you
  explicitly apply a reorganize/quarantine action.

## Code signing (optional)

Unsigned builds work, but Windows SmartScreen warns on first run ("Windows protected your PC"; More info ->
Run anyway). `build_release.ps1` signs the sidecar, the 32-bit host, `VEXT.exe` and the installer **only when
signing variables are set**, then checks every signature. Nothing is stored; set them in the shell that runs
the build. Needs `signtool.exe` (Windows SDK) on PATH or in `VEXT_SIGNTOOL`.

| Option | Variables | Approximate cost (check current prices) |
|---|---|---|
| **Azure Trusted Signing** (Microsoft) | `VEXT_SIGN_AZURE_DLIB` (path to `Azure.CodeSigning.Dlib.dll`), `VEXT_SIGN_AZURE_METADATA` (path to its `metadata.json`) | About 10 USD / month. Needs an Azure account and identity validation; eligibility depends on country and organization type. Reputation builds quickly. |
| **OV certificate** (.pfx from a CA) | `VEXT_SIGN_PFX`, `VEXT_SIGN_PFX_PASSWORD` | About 200-400 USD / year. SmartScreen reputation builds with downloads. |
| **EV certificate** (hardware token or cloud HSM) | same as OV when it exports a .pfx; a token needs its vendor's signtool setup | About 300-600 USD / year. Best SmartScreen start; stricter validation. |

Optional: `VEXT_SIGN_TIMESTAMP` (timestamp server URL). Details in `tools\sign_file.ps1`.

## Releasing

1. Bump the version in `desktop/src-tauri/tauri.conf.json`, `desktop/src-tauri/Cargo.toml` and
   `desktop/package.json` (the build stops if they differ) and add the section to `CHANGELOG.md`.
2. `python tools\gen_notices.py` (refreshes the Rust crate table in `THIRD_PARTY_NOTICES.md`; check the
   Python table by hand if `requirements*.txt` changed).
3. `.\tools\fetch_redist.ps1 -Refresh`, then `.\build_release.ps1`, then `.\tools\sandbox\run.ps1` (PASS needed).
4. `.\tools\publish_release.ps1`: zips the portable folder and creates a **draft** GitHub Release with the
   setup.exe, the zip, the checksums, LICENSE and notices. Publishing the draft is a manual step.
