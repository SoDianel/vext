# VEXT

VEXT is a local catalog and cleanup tool for the audio plugins installed on a Windows PC, with a place to
try them out. It is made by Dianel Records (creator: SoDianel) and is free software under
GPL-3.0-or-later. This file is the single source of truth for people and AI agents working in the repo:
what the project is, how it is built, and the rules that do not bend. `CLAUDE.md` points here.

The long development log (decisions, bugs found and how, per-feature notes) is kept outside the public
repo, in the git-ignored `notes/DEV_LOG.md`. Anything a contributor needs to know is summarized below; if a
rule or a decision is missing here, ask in an issue.

## What it does

1. **Inventory** every VST3, VST2, CLAP, AAX, Waves and FL Studio native plugin in the known folders (and,
   if asked, a whole-drive scan).
2. **Deduplicate**: the same plugin in several formats or install locations. Format priority is
   `CLAP > VST3 > VST2`; a 64-bit install beats its 32-bit twin; a newer Waves version folder beats an older
   one (`app/dedup.py`).
3. **Classify and enrich**: vendor (canonical names, website, status), category tags, type, description,
   links, pictures.
4. **Organize** files into `<Vendor>\` subfolders; flag corrupted files and AAX plugins (Pro Tools).
5. **FL Studio Sync**: build FL Studio's plugin browser tree from the catalog (`app/fl_reorganize.py`).
6. **Play**: host plugins (VST3, VST2, CLAP, Waves, 32-bit) in the Playground (Browser, Audio, Signal) and
   look at them in the Analyzer.

## Safety rules (non-negotiable)

- Scanning and metadata extraction are **read-only**.
- Any file **move, rename or delete** (dedup, reorganize, AAX or corrupted-file removal) must be
  1. **proposed** first (a database row only; nothing on disk changes),
  2. **explicitly confirmed** in the UI (bulk actions get one count-based confirm), never auto-applied on a
     scan, and never proposed on top of an unresolved action for the same plugin,
  3. written to the audit log (`actions.log`) with the reason.
- **Deletion has two tiers.** "Delete" moves the file to the quarantine (`<data>\quarantine\<original path>`).
  Only from the Quarantine tab can an item be restored or permanently deleted. Nothing permanently deletes a
  plugin that is still in its original place. (Out-of-scope entries and the Storage page's caches are the
  documented exceptions, each behind its own confirm.)
- The database keeps **working state, not history**: an action row is deleted when it resolves. History lives
  only in `actions.log`.
- Never delete a WaveShell host file or another shared-runtime file; they are excluded from the catalog.
- Windows file protection (TrustedInstaller-owned folders) is never bypassed automatically. Running VEXT as
  Administrator is the user's choice, offered only after a real "Permission denied".
- The backend listens on `127.0.0.1` only and requires the per-launch token the desktop shell passes
  (`app/security.py`). No telemetry, no accounts. Nothing leaves the PC unless the user asks (a store link, a
  picture search, or an optional AI research agent they installed themselves).

## Architecture

- **Backend**: Python 3.12+, FastAPI (`app/web.py`), SQLite (`<data>\plugins.db`), no ORM. One scan pipeline
  (`app/scan_service.py:run_scan`) with two entry points: the web UI and `python -m app.main scan`. A rescan
  upserts by path, so plugin ids and action links stay stable; new columns go through `app/db.py:_migrate`
  (add them to `_PLUGIN_COLUMNS_ADDED_LATER`, not only to `SCHEMA`).
- **Metadata**: read real file data first: `moduleinfo.json` in VST3/AAX bundles, then the PE version
  resource (`pefile`). Corrupted files are flagged, not skipped.
- **Frontend**: vanilla HTML, CSS and JS in `app/static/` (no framework, no build step). It is the only copy:
  `desktop/copy-frontend.js` mirrors it into the Tauri app.
- **Desktop shell**: Tauri 2 (`desktop/`, Rust in `desktop/src-tauri/`). It bundles the frontend, starts the
  backend as a sidecar (`vext-backend.exe`, PyInstaller; `sidecar_main.py`), and opens Explorer, dialogs and
  the in-app browser. Data lives in `%APPDATA%\com.vext.app`; the `VEXT_ROOT` environment variable overrides
  it (use a scratch folder for tests and screenshots).
- **Plugin hosting** (`app/plugin_host.py`, `app/host_worker.py`, `app/hosting/`): each preview runs in a
  separate worker process, so a crashing plugin cannot take VEXT down. `pedalboard` hosts VST3 (and Waves
  through WaveShell); VST2 and CLAP are hosted with `ctypes` (`app/hosting/vst2.py`, `clap.py`); MIDI is
  WinMM. **32-bit plugins** run in `host32\vext-host32.exe`, a 32-bit Python bundle (no pedalboard, no PyAV)
  built by `tools\build_host32.ps1` from `host32_main.py`.
- **Runtimes**: the installer brings WebView2 and Microsoft's Visual C++ runtimes if missing. **The PyInstaller
  bundles must not carry their own `msvcp140.dll`/`vcruntime140*.dll`** (a private copy once crashed every
  plugin): `vext-backend.spec` strips them and `tools\check_bundle.py` fails the build if one slips in. The
  app checks the runtime at start and shows a Repair banner (`app/runtime_check.py`).
- **Paths and names**: nothing in the code may assume a particular user name, drive or folder layout. Use the
  environment (`USERPROFILE`, `SystemDrive`) or a Configuration setting.

## Repository layout

| Path | What |
|---|---|
| `app/` | Backend and (in `app/static/`) the frontend |
| `desktop/` | Tauri shell; `src-tauri/` Rust, `installer/` NSIS hooks and the runtime installer script |
| `sidecar_main.py`, `host32_main.py`, `*.spec` | PyInstaller entry points and specs |
| `tools/` | Build helpers, tests (`test_*.py`), clean-PC test (`sandbox/`), release scripts. See `tools/README.md` |
| `sources/` | Logo master and exports |
| `specs/` | Short design briefs of larger features |
| `build_release.ps1`, `build_sidecar.ps1`, `update_logo.ps1` | Build pipeline |

## Build and release pipeline

`LOCAL_SETUP.md` has the commands; `tools/README.md` describes each helper.

- `.\build_release.ps1` builds everything into `release\`: logo refresh, Microsoft redistributables
  (`tools\fetch_redist.ps1`, signature-checked), backend sidecar + bundle check, 32-bit host, Tauri shell and
  NSIS installer (`VEXT-<version>-setup.exe`), a portable folder, `SHA256SUMS.txt`. It fails if the versions
  in `tauri.conf.json`, `Cargo.toml` and `package.json` differ, and it signs only when `VEXT_SIGN_*` is set.
- No MSI: an MSI cannot run the runtime installers.
- `.\tools\sandbox\run.ps1` is the clean-PC test (Windows Sandbox): it must PASS before a release.
- `.\tools\publish_release.ps1` zips the portable folder, writes the notes from `CHANGELOG.md` and creates a
  **draft** GitHub Release with the setup.exe, the portable zip, `SHA256SUMS.txt`, LICENSE and notices. The
  maintainer publishes the draft by hand. Releases are free; support is an optional donation.
- Work on topic branches and open pull requests. GitHub **Rebase and merge** is the only merge method;
  never use merge commits or squash merges. `main` has linear history: do not amend or force-push it.
  Rebase a topic branch onto `origin/main` when needed; force-with-lease is only for that topic branch.
- `tools\bump_version.ps1` updates the manifests, lockfiles and changelog together;
  `tools\check_version.ps1` validates them. CI runs on every branch push and PR. Every `main` push runs
  `.github/workflows/build.yml`, retaining the installer, portable ZIP, checksums and `BUILD_PROVENANCE.json`
  for 90 days. Builds are not cancelled by later pushes. Tag `vX.Y.Z` or dispatch `release.yml` to prepare
  a draft from a commit already on `main`. Publishing remains a separate manual step.
- Release assets must come from a clean, recorded source commit and pass checksum verification. A Sandbox
  PASS applies only to the exact installer tested; test the downloaded CI installer, not a local rebuild.
- Rebuild the sidecar after any change under `app/` or `sidecar_main.py`; the frontend is re-mirrored on
  every `tauri dev` / `tauri build`.
- `THIRD_PARTY_NOTICES.md` lists what the builds bundle; refresh it with `python tools/gen_notices.py` and by
  hand when `requirements*.txt` changes. The builds are GPL-3.0 as a whole because of `pedalboard`.

## Official color palette

| Name | Hex | RGB |
|---|---|---|
| Black | `#0a0908` | 10, 9, 8 |
| Jet Black | `#22333b` | 34, 51, 59 |
| Cool Steel | `#8a9497` | 138, 148, 151 |
| White Smoke | `#f2f4f3` | 242, 244, 243 |
| Dusty Taupe | `#a9927d` | 169, 146, 125 |

- Defined once in `app/static/style.css` as `--vext-black`, `--vext-jet`, `--vext-steel`, `--vext-smoke`,
  `--vext-taupe`. Every semantic token is one of them or a `color-mix()` of them. **Do not add new hex
  colors**: mix the palette instead.
- Light theme: White Smoke surfaces, Black / Jet Black text. Dark theme: Black / Jet Black surfaces, White
  Smoke text, Cool Steel for secondary text. Accent: Dusty Taupe fills with Black text (6.7:1). Taupe as text
  on light surfaces is only 2.7:1, so text uses `--accent-text`.
- Charts (`css/dashboard.css`) use fixed slots (taupe, the far end of the lightness range, steel, a taupe
  mix); identity also rides on legends and direct labels because the palette is near-neutral.
- Outside the palette on purpose: status colors (`--ok`, `--warn`, `--danger`) and third-party logos.

## Logo

- Master: `sources/logo.vectorcraft` (layer "Text" = the V E X T letters; **kept by the maintainer, not in the
  public repo**: without it `update_logo.ps1` keeps the committed `logo-mark.svg`). **Artboard 2 (the last) is
  the current app logo.** Exports are square transparent PNGs `sources/logo_app_<size>.png` (108, 540, 1080).
- Every app resource comes from it: `update_logo.ps1` (run first by `build_release.ps1`) makes
  `desktop/src-tauri/icons/*` from the largest export (`tauri icon`), `app/static/logo.png` from the smallest
  at least 108 px, and `app/static/logo-mark.svg` (`make_logo_svg.py`, near-white fills as `currentColor` so
  it follows the theme). Do not edit those outputs by hand: change the export and rebuild.

## Working in the repo

- **Tests** are scripts in `tools/test_*.py`. Each sets its own temporary `VEXT_ROOT`, `LOCALAPPDATA` and
  fake FL Studio folders, never real data. **Run one file per process.**
- **Style**: match the surrounding code (names, comment density, idiom). Comments say why. Keep user-facing
  text short and plain. New behavior that touches files needs a confirm step.
- **Never** use a real user's data folder, plugin list or FL Studio folder for tests, screenshots or fixtures.
  No personal paths, names or e-mails in code, docs, specs or commit messages.
- Decisions that change behavior go into `CHANGELOG.md` (user-visible) and, when they explain a trap for the
  next person, into a short note in this file.

## Dev tools and token budget

**Rule for AI agents: before using a tool that spends tokens or runs long, recommend it and wait for the
maintainer's OK.** Say which tool, why, and a rough cost. Do not launch subagents or workflows unasked.
Do not commit, push, tag or publish unless asked.

| Tool | Cost | Ask first? |
|---|---|---|
| `ruff`, `basedpyright`, `pytest`, `cargo clippy`, `pip-audit`, `cargo-audit`, `gitleaks` | Local, free | No |
| Graphify (`graphify query / explain / path / update .`) | Local, no LLM | No |
| Graphify `extract`, `cluster-only`, `label`, `/graphify`, `--mode deep` | Many model calls | **Yes** |
| Review skills (`code-review`, `security-review`, `simplify`), the Playwright MCP, `computer-use`, subagents, workflows | Many model calls | **Yes** |

Graphify output (`graphify-out/`) and `.venv-dev/` are git-ignored. Do not re-add Graphify's automatic
hooks (`graphify claude install`, `graphify hook install`) without the maintainer's OK: they force graph use
on every search and read.

## Roadmap and limits

- Windows 10/11 x64 only. FL Studio is optional.
- Not done: Linux/macOS, ARM, an auto-updater (a notify-only check linking to the releases page is the plan, never a
  silent installer), signed builds from CI.
- Plugins can be 32-bit VST2 only in the 32-bit host; 32-bit VST3/CLAP are not supported.
