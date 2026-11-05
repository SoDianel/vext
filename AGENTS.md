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
- **Version policy**: until the official release is announced, all work is 1.0.0. Do not bump the version;
  add changes to the `[1.0.0]` section of `CHANGELOG.md` (final state, not the fixes along the way) and keep
  `[Unreleased]` empty. The draft `v1.0.0` release is refreshed from `main` (delete the draft, dispatch
  `release.yml`), never versioned up. `[Unreleased]` and `bump_version.ps1` start after the announcement.
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

## FL Studio loading

`app/fl_launch.py` resolves actual FL `.fst` entries (Installed first), shared by click-to-load and
native outgoing drags (`desktop/src-tauri/src/fl_drag.rs`). The controller source under
`app/static/fl-controller/` is copied into FL's configured user-data folder only after a destination
preview and confirmation. `fl_launch_port` is reserved from Playground/toolbar MIDI input ownership.
Commands use MIDI SysEx request IDs and authenticated request/reply files inside the confirmed VEXT controller folder (the backend keeps connection settings under `<data>/fl-bridge`);
no second MIDI port is required. Never automatically repeat an unacknowledged load. Browser activation
is reported as a handoff, not proof that a third-party plugin finished loading. Keep preset format and
folder ancestry checks: matching a display name alone can select a different plugin.

Click-to-load requires a running FL process and a fresh authenticated controller heartbeat; process detection alone must never enable it.

Click-to-load lives only in the FL bubble's inline index (`fl_bubble.rs`, `fl-bubble.html`,
`fl-launcher.js`; `specs/2026-11-02-fl-bubble-inline-index.md`). `fl_focus` and `fl_drag_preset` accept
only the `main` and `fl-bubble` webviews; the bubble may also resize/clip itself and read the cursor.
There is no separate launcher window. The bubble window has no owner, so destroy it when `main` is
destroyed or the app never exits. It is shown only while FL Studio is installed and the current
controller is installed.

FL executes controller scripts without `__file__`; capture the settings path during script initialization, before other controllers change the working directory. Keep build outputs directly in `release/`. Stop running VEXT processes before rebuilding; never create an alternate release output folder.

FL controller callbacks must not write outside their controller folder or use rename/delete APIs. FL can raise `SystemError` for restricted file operations; handle it without repeated callback tracebacks. Controller replies use direct writes; backend readers tolerate partially written JSON.

For Browser descent, expand the selected folder then move Down to its first child. Do not send Right after explicit expansion: FL can toggle the folder closed. Preserve ancestry verification before activation.

Browser tab switches, expansion and collapse rebuild FL?s tree asynchronously. Frame waits must return from OnIdle, not just yield within the same callback. Select Plugins explicitly. Do not collapse folders during caption searches. Previous-node navigation uses FPT_Up (40); next-node navigation uses 1. Bump CONTROLLER_REVISION in both backend and controller when controller behavior changes; require matching revisions in the runtime heartbeat before loading.

Browser reset closes only the selected ancestor branch in one bounded Left burst, then settles once. Parent verification must not add toggleBrowserNode(0) after Left; Left may already collapse and ascend. Handle collapse-in-place with a bounded second Left and retain expected/observed folder diagnostics.

Click-to-load must invoke the main-webview-only native fl_focus handoff after confirmation and before sending MIDI. Validate the FL executable and foreground HWND; never force focus with injected keys or thread attachment. Stop if focus is denied. Direct Browser navigation can work while VEXT is foreground even when generic Left/Enter does not. Controller revision 8 also checks Browser focus before verification/activation.

During the maintainer-authorized script-only test, wire revision 8 stays compatible with the running backend; SCRIPT_BUILD identifies controller-only patches. Standard Installed paths validate folder sequence and node types during forward descent, reject section/format boundary crossings, stop leaf searches at folder boundaries, and require SBN_FST before activation. They must not walk back with Left after finding the preset. Generic filed/manual presets retain the conservative fallback. The running extracted script reference may be patched alongside the installed script for this local test only; packaged release artifacts remain unchanged until a requested rebuild.

FL controller diagnostics use an authenticated bounded diagnostics.json snapshot in the controller folder. Import new events once into logbuf (including traceback/context), redact the bridge secret, and keep Log clearing effective across polls. Log endpoints collect diagnostics without requiring the FL setup page to be open. Controller protocol revision 9 adds this forwarding.

SVGElement does not reflect HTMLElement.hidden: toggle the SVG hidden attribute explicitly, or
[hidden] keeps the radial menu invisible even after assigning svg.hidden = false.
FL bubble transitions keep the collapsed circle painted until native resize succeeds. Do not render
expanded coordinates in the 72 px viewport or wait for menu HTTP before tracking the pointer. Resize
on the window thread without holding the state mutex. Paint the SVG gap with a nonzero palette alpha
for native WebView2 hit-testing; DOM pointer-events alone does not paint transparent pixels.

The FL index's source is real .fst files in the configured Plugin database's Installed
Effects/Generators format folders only, including entries without catalog rows. The user-filed
Effects/Generators trees are FL's copies of the same presets: never list them, and key-based load/drag
reject filed keys. Catalog metadata only enriches verified binary/manual-link matches; never identify a
preset from a display-name guess. Key-based load and drag revalidate scope, .fst existence and known
quarantine/corruption. The VEXT center is draggable open or closed; persist user moves. Only the index
header's status pill carries connection colors. SVG hidden attribute rules still apply.
Index pictures, in order: the Installed preset's own Bitmap/PNG, the verified catalog picture, then the
single filed copy's PNG with the same section and stem (ambiguous stems get none). Pictures are
presentation only: they never identify, list or load a preset (`specs/2026-11-03-fl-index-pictures.md`).

FL bubble hover must not resize/reposition the HWND or swap the logo element. The bubble has one
fixed canvas (index panel + circle, capped to the work area) clipped by SetWindowRgn: a 72 px ellipse
when closed, ellipse OR the panel's round rect when open. Windows owns a successfully applied HRGN.
Full-surface transparency alone must not intercept the desktop around the closed bubble. Refit canvas
and panel side after native dragging, preserving the screen anchor. Listing snapshots/digests cache
presentation only; key-based loading/dragging must still revalidate files and quarantine. Snapshots are
stamped with the per-launch token and expire; they must not enable loading while disconnected.

Hover (200 ms) or click on the bubble opens the complete FL preset index inside the bubble frame. It
closes 400 ms after the pointer leaves the circle and panel, on its close button or Escape, but never
during a native preset drag or a bubble drag. Shrink the native region before hiding the panel. A drag
release must not open the index. Category/Vendor rows, type scope, text search and the icon-only
Cards/List/Compact views filter the same full FST snapshot locally; changing a group must not start a
new filesystem scan. Keep the sidebar list visible (no dropdowns), preserve exact-key load/drag in every
view, and retain per-launch snapshot and confirmation validation.
