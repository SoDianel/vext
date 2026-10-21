# FL Studio Sync: fix pictures, link .fst files, standard sizes, full list, a real interface (2026-10-20, owner)

Repo: the VEXT repository root. Backend: Python (FastAPI, Pillow, sqlite) in `app/`.
Frontend: vanilla JS/CSS in `app/static/` (no build step; classic scripts, IIFE modules that use only
`window.VEXT` and the DOM). Match the style of the code around you: short plain-English comments that say
*why*, two-space indent and single quotes in JS, docstrings and type hints in Python.
Background: AGENTS.md sections "FL Studio browse-tree sync" and "FL Studio Sync: pictures as thumbnails,
new-only vs complete, browse tree check" (read them).

## Owner's words

"It is working but there is no intuitive way to fix the problems:
- Add a fix image feature (the new standards now are 256 px (Large), 128 px (Medium) and 64 px (Compact))
  and they are taken from VEXT's images database.
- Add a feature to link .fst files to plugins in VEXT for the unrecognized ones, maybe due to the name or
  something similar.
- Remove the picture size problem: it is not one, except for the ones that can be standardized to the sizes
  I mentioned.
- Let the plugins be listed completely.
- Improve the GUI of the FL Studio Sync module: it looks like a form but it is an interface; better UI/UX."

Owner's choices: **one global thumbnail size** (Large 256 / Medium 128 / Compact 64, used by Apply and by
every Fix); layout **status header + two tabs: Organize | Check & fix**; built by Haiku agents.

## Hard rules for every agent

- **Never commit, never create branches, never run git write commands.** Leave changes uncommitted.
- **Never touch the real FL Studio folder or the real VEXT data** in tests: use temp folders.
- Only edit the files your task owns. If another file must change, say so in your report.
- Colours: never add a hex colour. Palette tokens from `app/static/style.css` (`--accent`, `--accent-fg`,
  `--accent-soft`, `--accent-text`, `--fg`, `--fg-2`, `--fg-3`, `--bg`, `--bg-content`, `--bg-subtle`,
  `--bg-control`, `--bg-hover`, `--bg-popover`, `--separator`, `--separator-strong`, `--shadow-1/2`,
  `--radius-s/m`, status `--ok`, `--warn`, `--danger` and their `-soft` variants if present) or
  `color-mix()` of them. A pressed segment / selected chip must be clearly visible in dark mode: use
  `background: var(--accent); color: var(--accent-fg)` for it (light and dark).
- JS: `node --check` every file you touch. Python: `.venv-dev\Scripts\python.exe -m pytest <tests> -q` and
  `ruff check <your files>`.

## Already pre-wired by the coordinator (don't redo)

- `app/static/index.html`: `#panel-flsync` is now only a skeleton:
  ```html
  <div class="fls" id="flsync-root">
    <header class="fls-head" id="flsync-head"></header>                 <!-- Agent 2 fills -->
    <div class="fls-tabs" role="tablist">                                <!-- Agent 2 wires -->
      <button role="tab" class="fls-tab" data-fls-tab="organize" id="fls-tab-organize" …>Organize</button>
      <button role="tab" class="fls-tab" data-fls-tab="check" id="fls-tab-check" …>Check &amp; fix
        <span class="fls-tab-count" id="fls-tab-count" hidden></span></button>
    </div>
    <div class="fls-panel" id="flsync-organize" role="tabpanel"></div>   <!-- Agent 2 fills -->
    <div class="fls-panel" id="flsync-check" role="tabpanel" hidden></div> <!-- Agent 3 fills -->
  </div>
  ```
  It loads `css/flsync.css`, `css/flsync-check.css`, `js/flsync-check.js`, then `js/flsync.js`.
- `app/static/app.js`: the old FL Sync code is **removed**; the router calls `window.VEXT.flSync.load()`
  every time the section is shown (only when FL Studio is installed). The old `.flsync-*` CSS in
  `style.css` is removed.
- The old markup and code were kept for reference in `specs/2026-10-20-fl-sync-old-ui.txt` during the work (deleted after):
  every behaviour there (confirm texts, overrides on root-bound plugins, dangerous wipe + backup, the check's
  filters, the repair button) must survive in the new UI unless this spec changes it.
- Useful `window.VEXT` members (app.js, "runtime contract"): `api(path, opts)` (JSON, throws `Error` with
  `.message`), `toast(msg, isError)`, `escHtml`, `confirmDialog(msg) → Promise<bool>`,
  `handleActionError(err)`, `isTauri()`, `revealInExplorer(path)`, `showPluginModal(id, {tab})` (the plugin
  sheet: it has the picture tools, "Find online" etc.), `settings()`, `navigate(section)`. VEXT fires
  `document` event `vext:pictures` `{detail: {ids}}` when plugin pictures change.

---

## Task 1 — Backend (Agent 1)

Files you own: `app/fl_reorganize.py`, `app/db.py` (only the new `fl_links` table + its helpers, added the
way other late tables are added — see `_migrate` / the `CREATE TABLE IF NOT EXISTS` blocks), `app/web.py`
(only the `/api/fl-sync/...` endpoints and their body models), `tools/test_fl_sync_fix.py` (new).

### Standard thumbnail sizes
- `THUMB_SIZES = {256: "Large", 128: "Medium", 64: "Compact"}` (height in px → name). The width follows
  the picture's aspect ratio, as today.
- `_thumbnail_bytes(src, stamp, height)` takes the height. The VEXT mark becomes `"<mtime>@<height>"`.
  A thumbnail counts as **current** when its mark equals the new stamp, or when it has a legacy mark
  (`"<mtime>"`, no `@`) equal to the picture's mtime **and** its height equals the wanted height.
- `_Pictures(root, enabled, replace, height=256)`; `apply(..., thumb_height=256)`; `FlSyncApply` gains
  `thumb_size: int = 256` (anything else than 256 / 128 / 64 → 422 / 409).

### The check (`inventory`, `_entry_check`)
- **No more "picture is N px tall (not 256)" problem.** New entry fields:
  `size_name` ("Large" / "Medium" / "Compact" when the height is one of the three, else null),
  `can_standardize` (true when the picture's height is **not** one of the three **and** VEXT has a picture
  for the plugin). Only then add the issue `"picture is N px tall: VEXT can make it a standard size"`.
  A non-standard picture with no VEXT picture is not a problem (no issue).
- New fields: `linked` (true when the plugin was found through a manual link), `key` (the .fst stem,
  lowercased: what a link is stored under).
- `counts` gains `can_standardize` and `linked`.
- Everything else in an entry stays (`section`, `folder`, `name`, `fst`, `nfo`, `picture`, `size`,
  `source`, `plugin_id`, `plugin_name`, `vendor`, `vext_picture`, `issues`).

### Fix pictures
`fix_pictures(conn, fst_paths: list[str], height: int) -> dict` and
`POST /api/fl-sync/fix-pictures {"fst": [...], "size": 256|128|64}`:
- Each path must resolve **inside** the Plugin database folder (`_plugin_db_root`), end in `.fst` and exist;
  otherwise it is skipped with a reason (never raise for one bad path).
- Find the plugin (`_plugin_for_stem`, links included) and its VEXT picture (`picture_for_host_path`).
  No plugin → skipped "not recognized by VEXT: link it first"; no picture → skipped "VEXT has no picture for
  <name>". Otherwise write the thumbnail at `height` exactly like `_Pictures.place` (hidden `.png`, hidden
  `.nfo` `Bitmap=<stem>.png`). A thumbnail VEXT didn't make is **backed up first** to
  `Plugin database\Thumbnails.vext-backup-<UTC time>\` (same scheme as `_Pictures._back_up`) and then
  replaced (fixing is an explicit request). An existing `.nfo` pointing to another bitmap is rewritten to
  the new `.png`.
- Answer `{"fixed": n, "skipped": [{"fst", "name", "reason"}], "backup": "<folder>" | null,
  "entries": [<_entry_check of every fst asked for that exists>]}`.

### Link an .fst to a VEXT plugin
- New table `fl_links (name TEXT PRIMARY KEY COLLATE NOCASE, plugin_id INTEGER NOT NULL REFERENCES
  plugins(id) ON DELETE CASCADE, created_at TEXT NOT NULL)`; `name` = the .fst stem (FL's plugin name).
  Helpers in `db.py`: `fl_links(conn) -> dict[str, int]` (lowercased name → plugin_id) and
  `set_fl_link(conn, name, plugin_id | None)` (None deletes).
- Links win everywhere a name is matched: `_build_name_index` (links override, even an ambiguous name),
  `_plugin_for_stem` (links first), and `_plan_entry` (when path / filename found nothing, try the link
  for `parsed["name"]` and for the `.nfo` stem; then it is "matched" and gets filed). Waves entries keep
  their own matching unless a link exists.
- `link_candidates(conn, name, limit=10)` and `GET /api/fl-sync/link-candidates?name=` →
  `{"candidates": [{"id", "name", "vendor", "format", "host_path", "score", "has_picture"}]}`:
  fuzzy matches over every non-quarantined plugin in an FL-loadable format. Normalize both sides
  (lowercase; drop bracketed parts like `(x64)`, `[VST3]`; drop words `x64 x86 64 32 bit vst vst2 vst3
  clap aax stereo mono plugin fx`; drop non-alphanumerics), then score = the best of
  `difflib.SequenceMatcher` ratio, token Jaccard, and 0.9 when one normalized name contains the other
  (min length 3). Keep score ≥ 0.35, best first, round to 2 decimals.
- `POST /api/fl-sync/link {"name": "<fst stem>", "plugin_id": int | null, "fix_picture": false,
  "size": 256}` → stores / removes the link; with `fix_picture` also fixes the pictures of every `.fst`
  with that stem. Answer `{"entries": [<_entry_check of every .fst in Effects/Generators with that stem>],
  "fixed": n}`. Unknown plugin id → 404.

### Tests (`tools/test_fl_sync_fix.py`, pytest)
Build everything in a temp folder: set `VEXT_ROOT` to a temp dir **before** importing `app` modules (see
`tools/test_signal_gen.py` for how), create a sqlite DB via `app.db` helpers with a few plugin rows,
point the `fl_plugin_db_root` setting at a temp `...\Plugin database\Installed`, create `Effects\<Vendor>\
<Name>.fst` files, `.nfo` / `.png` thumbnails (Pillow) and VEXT pictures in `IMAGES_DIR` under the key
`picture_for_host_path` expects. Cover: the three standard heights give no size issue; 100 px with a VEXT
picture gives `can_standardize` + the issue; 100 px without one gives no issue; fix-pictures at 128 writes a
128-px hidden png + nfo and backs up a non-VEXT one; a path outside the Plugin database is skipped; a link
makes an unrecognized `.fst` recognized (inventory + `_plugin_for_stem`) and survives a new connection;
`link_candidates("FabFilter Pro-Q 3 (x64)")` ranks "Pro-Q 3" first; apply with `thumb_height=64` makes
64-px thumbnails and a second run reports them current; a legacy mark at the same height counts as current.
Endpoints through FastAPI's `TestClient` (as in `tools/test_signal_gen.py`).

---

## Task 2 — Header, tabs and the Organize tab (Agent 2)

Files you own: `app/static/js/flsync.js` (new), `app/static/css/flsync.css` (new).

`window.VEXT.flSync = { load(), thumbSize() }`. `load()` runs every time the section is shown (build the
UI once, then just refresh). `thumbSize()` → 256 | 128 | 64 (the global choice, `localStorage
pa-fls-thumb-size`, default 256).

### Header (`#flsync-head`)
An interface, not a form: a compact status bar.
- Left: title "FL Studio Sync" and one line under it: the Plugin database folder (from the last inventory's
  `root`, else "FL Studio's Plugin database"), with **Open folder** (`POST /api/fl-sync/open-database`) and
  **Open FL Plugin Manager** (`POST /api/fl-sync/open-plugin-manager`; toast: "Run Find installed plugins
  there, then Check again.").
- Right: **Thumbnails** segmented `Large 256 · Medium 128 · Compact 64` (the global size; a short hint
  "Height in FL's browser; width follows the picture").
- Under it: status tiles from the last check (listen to `document` event `vext:flsync-inventory`,
  `detail = {counts, root}`): `.fst files`, `Problems`, `No picture`, `Can standardize`, `Not recognized`,
  `Not in FL`, `Not filed`. Each tile is a button: click → switch to Check & fix and
  `VEXT.flSyncCheck.show('<filter>')` (filters: `all`, `issues`, `nopicture`, `standardize`, `unknown`,
  `notinfl`, `notfiled`). Before any check: one muted tile "Not checked yet — Check now" that opens the
  Check & fix tab. `#fls-tab-count` shows the problem count (hidden at 0).

### Tabs
`.fls-tab` buttons (WAI tabs pattern, arrow keys), `aria-selected`, the selected one clearly marked
(accent underline or fill). Remember the tab in `localStorage pa-fls-tab`. The first time Check & fix is
shown call `VEXT.flSyncCheck.mount(document.getElementById('flsync-check'))` (once), then
`VEXT.flSyncCheck.show()`.

### Organize tab (`#flsync-organize`)
Same features as the old form (see the reference file), laid out as an interface:
- A grid of **option cards** (2–3 columns, one column under 760 px), each with a title, a one-line
  explanation and its control:
  - **Sections** — two toggle chips `Effects` `Generators` (at least one on).
  - **Organize by** — segmented `Vendor · Category` (+ "a plugin can land in more than one category").
  - **Plugins** — segmented `Only new · Complete` with the explanation of each (from the old titles).
  - **Thumbnails** — switch "Add VEXT pictures" (shows the global size: "Large (256 px)"), switch
    "Replace thumbnails VEXT didn't make (backed up first)".
- **Advanced** (a collapsed `<details>` card styled as a danger zone): "Wipe and rebuild from scratch" and
  "Save a backup of the current folder first".
- A **sticky action bar** at the bottom of the tab: a plain sentence of what will happen, e.g. "Files the
  new plugins of Effects and Generators by vendor, with Large thumbnails." + **Preview** + **Apply**
  (Apply only after a Preview of the current choices, exactly like before; any change disables it again).
- **Preview result**: per section a row of stat tiles (Detected / In New, Would be filed, Left at the root,
  Names synced, With a VEXT picture) and, when there are root-bound plugins, a table "Left at the root —
  pick a destination (optional)" with the name, the reason and the destination `<select>` (overrides,
  same data shape as before: `{Effects: {key: value}, Generators: {...}}`).
- **Applied result**: stat tiles (Filed, Left at the root, Moved, Skipped, Renamed, Assignments) + a
  thumbnails line (added / updated / current / kept / missing) + backup paths, as before.
- Apply sends everything the old one did plus `thumb_size: VEXT.flSync.thumbSize()`; after a successful
  apply call `VEXT.flSyncCheck && VEXT.flSyncCheck.refresh()` (it re-runs the check only if one ran).

---

## Task 3 — The Check & fix tab (Agent 3)

Files you own: `app/static/js/flsync-check.js` (new), `app/static/css/flsync-check.css` (new).

`window.VEXT.flSyncCheck = { mount(el), show(filter?), refresh(), inventory() }`:
`mount` builds the UI into `el` once; `show(filter)` runs the check the first time (later shows keep the
last one), and selects `filter` if given; `refresh()` re-runs the check if one ran before; `inventory()`
→ the last inventory or null. After every inventory load dispatch
`document.dispatchEvent(new CustomEvent('vext:flsync-inventory', {detail: {counts, root}}))`.
The global size: `(VEXT.flSync && VEXT.flSync.thumbSize()) || 256`.

### Layout
- **Toolbar** (sticky at the top of the tab): filter chips with counts — `All`, `Problems`, `No picture`,
  `Can standardize`, `Not recognized`, `Linked`, `VEXT not in FL`, `Not filed`, `Orphan files` (filter keys
  `all`, `issues`, `nopicture`, `standardize`, `unknown`, `linked`, `notinfl`, `notfiled`, `orphans`);
  a search box (name, VEXT name, vendor, folder); Section `All · Effects · Generators`; **Check again**;
  **Fix .nfo and hidden files** (the old repair, same confirm text, `POST /api/fl-sync/repair`).
  Default filter: `all`.
- **The list is complete**: every entry of the filter, no inner scroll box or max-height (the page
  scrolls). Render efficiently: build rows in chunks of 150 with `requestAnimationFrame` (or an
  IntersectionObserver sentinel that appends the next chunk) so 800+ rows don't block, and `loading="lazy"`
  images. A line above the list: "Showing N of M".
- **Row** (CSS grid, aligned columns, zebra-free, hover highlight): checkbox · thumbnail box (fixed 120×64
  area, the picture `object-fit: contain` from `GET /api/fl-sync/thumbnail?path=`, a small badge with
  `size_name` or "N px", "No picture" placeholder) · **FL name** + folder (`Effects\Vendor`) ·
  **VEXT plugin**: name · vendor (a "Linked" badge when `linked`), or a warn badge "Not recognized" ·
  **status**: a green "OK" badge, or the issues as short warn lines · **actions**:
  - **Fix picture** — when `vext_picture` and (`!picture` or `can_standardize` or `source !== 'vext'` or the
    size isn't the global size): `POST /api/fl-sync/fix-pictures {fst: [e.fst], size}`; then replace the row
    from `entries` (no full re-check), bump the thumbnail URL (`&v=<time>`) and update the counts locally.
  - **Set a picture…** — recognized but `!vext_picture`: `VEXT.showPluginModal(plugin_id)`; on the
    `vext:pictures` event for that id, the row offers Fix picture.
  - **Link…** — not recognized (or **Change link** / **Unlink** when `linked`): opens the link dialog.
  - a ⋯ menu or small icon button: **Show in Explorer** (`VEXT.revealInExplorer(e.fst)`, only when
    `VEXT.isTauri()`).
- **Bulk bar** (appears when rows are checked, sticky at the bottom): "N selected" · **Fix pictures (N)** at
  the global size (only the fixable ones are sent; say how many will be skipped) · **Select all fixable in
  this view** · **Clear**. Confirm before fixing more than 20, mentioning that thumbnails VEXT didn't make
  are backed up first. Show the result as a toast (fixed / skipped, backup folder).
- **Link dialog** (own modal markup appended to `document.body` with a backdrop, focus trapped, Escape and
  backdrop close it): title "Link “<FL name>” to a VEXT plugin"; the FL name and folder; **Suggestions** from
  `GET /api/fl-sync/link-candidates?name=` (name, vendor, format, a score bar, "has picture" mark, a
  **Link** button each); a search box that calls `GET /api/plugins?q=<text>` (debounced 250 ms; rows have
  `id`, `name`, `vendor`, `format`, `has_image`; show the first 30) with Link buttons; a checkbox "Also make
  its picture the thumbnail (<size name>)" (on). Link → `POST /api/fl-sync/link {name, plugin_id,
  fix_picture, size}` → replace the rows from `entries`, toast "Linked". Unlink → `{plugin_id: null}`.
- **Not in FL / Not filed / Orphans** views: simple rows (name, vendor, format, path, the explanation from
  the old UI); Not in FL rows have an **Open FL Plugin Manager** button; Not filed rows a **Go to Organize**
  button (switch tab: click `#fls-tab-organize`).
- Empty states with one sentence each ("No problems: every plugin has a working thumbnail."). Loading
  state while checking ("Checking the browse tree…").
- Keyboard: the checkboxes and buttons are real controls; the link dialog's search is focused on open.

## Done means
- Every touched JS file passes `node --check`; Python passes its tests and `ruff check` on the files you own.
- A short report: what changed with file:line pointers, anything not done or uncertain.
