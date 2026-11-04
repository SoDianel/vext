# Fix the FL launcher bubble (hover / expand does not work)

Status: rebuilt locally and verified working by the owner, 2026-11-02.
The owner subsequently requested UI/data refinements; see the refinement section in the quick-launcher spec.
The initial fix pass used automated checks; the later explicitly requested release rebuild passed bundle and checksum checks.

**Read first, in this order:**
1. `AGENTS.md`: the project rules (safety, palette, tests, build, the "Dev tools and token budget" approval rule).
2. `specs/2026-11-02-fl-quick-launcher.md`: **the design this fix must keep.** It covers the contracts, window sizes, ring geometry, permissions and file ownership.
3. `specs/2026-11-02-fl-studio-launch.md`: background only. It covers the FL controller bridge, the preset resolver and native drag, which the launcher reuses.

Local work only: no commits, pushes or publication unless the owner asks. The branch is
`SoDianel/vext-release-draft`, and the worktree has many uncommitted changes that belong to this feature.

## Owner-reported symptoms

- First release build: the bubble appears. **On hover it moves and disappears.**
- Second release build, after the attempted fixes below: **"still not working"**, with no further detail.
  Reproduce it yourself first and write down the exact behavior.

## Expected behavior (acceptance)

1. With the "Show the FL launcher bubble" switch on (FL Studio Sync), a 56 px round bubble with the VEXT mark
   floats above all windows, including FL Studio. It survives VEXT being minimized, and it can be dragged to
   a new place that persists across restarts.
2. Hovering the bubble for about 200 ms opens the radial menu **in place**. The center circle stays exactly
   where the bubble was, with no jump and no flicker loop. The menu stays open while the pointer is anywhere
   on the center circle, the gap ring or the two quarter-rings, and the mouse wheel scrolls a ring.
3. Moving the pointer off the menu closes it after about 350 ms. It returns to the same bubble spot.
4. Clicking a ring item opens the `fl-launcher` window beside the bubble. Clicking a plugin there (after the
   inline confirm) sends it to FL. Dragging a plugin card into FL drops its preset.
5. Near screen edges, the expanded menu stays fully on screen (clamped). The bubble center may then shift
   inside the window, but not on screen.
6. VEXT exits normally when its main window closes, taking the bubble and launcher with it.

## What is implemented (current state)

### Rust: `desktop/src-tauri/src/fl_bubble.rs`
- **Window labels:** `fl-bubble` and `fl-launcher`. Pages: `static/fl-bubble.html` and `static/fl-launcher.html`.
  `desktop/copy-frontend.js` mirrors `app/static/` into `desktop/dist-mirror/static/`.
- **Bubble window builder:** `.transparent(true)`, `.decorations(false)`, `.shadow(false)`,
  `.always_on_top(true)`, `.skip_taskbar(true)`, `.resizable(false)`, `.focused(false)`, `.visible(false)`,
  then placed and shown. It has no owner. Hardening is the same as `plugin_frame::create_window`:
  - `initialization_script(config().init_script())`, which provides `window.__VEXT__ = {apiBase, token}`;
  - `WEBVIEW_ARGS`;
  - navigation only to `tauri.localhost`;
  - new windows denied.
- **Sizes (logical px):** collapsed 72×72, center offset (36, 36); expanded 380×300, center (190, 110).
- **State** (`FlBubble` managed state): center offset in the window, an expanded flag, and `shell_move_until`.
  `shell_move_until` marks moves within 300 ms of the shell's own `SetWindowPos` as not the user's.
- **Commands:**
  - `fl_bubble_set(enabled)`: callable from `main` only.
  - `fl_bubble_expand(expanded) -> {cx, cy}`: callable from `fl-bubble` only. It computes the screen center
    from `outer_position` plus the stored offset, picks the new size, and clamps the rect to the monitor
    work area. It then applies the size and position in **one** `SetWindowPos(SWP_NOZORDER | SWP_NOACTIVATE)`
    via `window.hwnd()`, and returns the new in-window center.
  - `fl_bubble_cursor() -> {cx, cy}`: callable from `fl-bubble` only. Returns
    `window.cursor_position() - outer_position`, divided by the scale factor.
  - `fl_launcher_open(kind, value, title)`: callable from `fl-bubble` only. Creates the launcher, or reuses it
    by emitting `fl-launcher:show` and focusing it.
  - `fl_launcher_close()`: callable from `fl-launcher` only.
  - `fl_focus` and `fl_drag_preset` now accept the `main` **or** `fl-launcher` labels.
- **Position persistence:** `<app_config_dir>/fl-bubble.json` stores the center in physical px. It is written
  on `WindowEvent::Moved` for `fl-bubble` only when the move is the user's: collapsed, visible, not
  minimized, and not inside the shell-move window.
- **Shutdown:** when `main` is destroyed, `fl-bubble` and `fl-launcher` are destroyed first. This happens in
  `fl_bubble::on_window_event`, called from `lib.rs`.
- **Registration:**
  - commands in `lib.rs` (`generate_handler!`) and `build.rs` (the `AppManifest` command list);
  - capabilities `capabilities/fl-bubble.json` (`core:default`, `core:window:allow-start-dragging`,
    `allow-fl-bubble-expand`, `allow-fl-bubble-cursor`, `allow-fl-launcher-open`) and
    `capabilities/fl-launcher.json`;
  - `default.json` gains `allow-fl-bubble-set`.

### Bubble page: `app/static/fl-bubble.html`, `js/fl-bubble.js`, `css/fl-bubble.css`
- **Markup:** `#fb-collapsed` is a div with `data-tauri-drag-region`, containing the VEXT mark as
  `<svg><use href="/static/logo-mark.svg#mark"/></svg>`. `#fb-svg` is a 380×300 SVG, hidden while collapsed.
- **CSS:** `html, body { background: transparent !important }`, plus `user-select: none` and no overflow.
  Colors are palette tokens only (`--vext-*`, `--ok`, `--warn`).
- **JS flow:**
  - Hover: `mouseenter` on `#fb-collapsed` starts a 200 ms timer, then `expand()` runs:
    - It sets `center = (190, 110)`, hides the circle, shows the SVG and calls `render()` **before** resizing.
    - Then it runs `invoke('fl_bubble_expand', {expanded: true})` and loads `/api/fl-launch/menu` in parallel.
    - It re-renders at the returned center and starts `watch()`.
  - `watch()`: every 120 ms, `invoke('fl_bubble_cursor')`. "Inside" means within the window and within
    `R_OUT + 16` of the center. After 350 ms outside, and no wheel scroll in the last 400 ms, it calls
    `collapse()`.
  - `collapse()`: `invoke('fl_bubble_expand', {expanded: false})`, then hides the SVG, places `#fb-collapsed`
    at the returned center minus 28 (inline `left`/`top`), and shows it.
  - Window resizes are serialized through a promise queue (`windowQueue`). A `generation` counter drops late
    expand answers.
  - Data: `/api/fl-launch/connection` is polled every 2 s and sets `data-status` (ok/warn/steel) on `<html>`.
    API calls use `fetch(apiBase + path, { headers: { 'X-VEXT-Token': token } })`.
  - Outside Tauri (no `window.__TAURI__`), the expanded menu is drawn once as a browser preview.
- **Ring geometry:** angles clockwise from 12 o'clock. Center r = 46; ring inner 70, outer 172. Categories
  span 184°–270°, Vendors 90°–176°. There are 7 slots per ring at radius 121, with labels rotated radially.

### Launcher, backend and main window (believed fine; not verified live)
- **Launcher:** `fl-launcher.html`, `js/fl-launcher.js`, `css/fl-launcher.css`, and the shared
  `js/fl-preset.js` (`window.VextFlPreset`).
- **Backend:** `app/fl_launch.py` (`launchable`, `menu`, `plugins_for`), `app/fl_launch_api.py`
  (`GET /api/fl-launch/menu`, `GET /api/fl-launch/plugins`), and the `app/web.py` setting `fl_bubble_enabled`.
- **Main window:** `js/fl-launch.js` (controller setup in Configuration > DAWs, the FL Studio Sync visor and
  switch, card drag) and `app.js` (DAW dropdown panels).
- The main window was checked in a headless browser: no console errors apart from the expected 404 for the
  missing `img/daws/studioone.svg`.

## Attempted fixes (second build)

1. Size and position were two calls (`set_size`, then `set_position`). They became one `SetWindowPos`.
2. The menu is now drawn before the resize, instead of after it.
3. Collapse used DOM `mouseleave` on `<html>` with a 400 ms timer. It now polls the cursor position from Rust.
4. The FL logo was replaced by the VEXT mark (owner request; keep it).

These did not fix it, according to the owner.

## Hypotheses to check, most likely first

1. **Hit-testing of the transparent window.** With a WebView2 transparent background, fully transparent pixels
   may not receive mouse input, or may pass it to the window below. Check whether `mouseenter` and `click`
   reliably reach the bubble and the ring items. Check whether the gap between the center and the rings
   (r 46–70, unpainted) passes the pointer through, and whether the item hit areas (`.fb-item-bg` has
   `fill: transparent`) work. Possible fixes:
   - paint everything with near-zero alpha (for example a `color-mix(... 1%, transparent)` disc of radius
     `R_OUT` behind the menu) so the whole menu area is hit-testable;
   - or set `pointer-events: all` / `visiblePainted` correctly on the SVG.
2. **`data-tauri-drag-region` on `#fb-collapsed`.** Tauri's drag-region handler may capture the pointer and
   start an OS move on mousedown, and may interfere with hover. Consider dragging only with a modifier, or
   only from a small handle, or calling `start_dragging` explicitly after a movement threshold.
3. **The expand/collapse loop.** If `mouseenter` fires again on the collapsed circle right after a collapse,
   the menu flickers. Log every expand, collapse and cursor-poll result with timestamps (to the console in
   `tauri dev`, or to the backend log) to confirm or rule this out.
4. **Physical vs logical coordinates and DPI.** Test at 100 % and 150 % scaling.
   - `cursor_position()` is in physical desktop coordinates.
   - `outer_position()` of a frameless window should equal the client origin; check this.
   - The center offset is stored in logical px and converted back with the window's current scale factor.
5. **Commands failing silently.** `invoke` rejections are swallowed. If `expand` rejects, the code restores
   the collapsed state; if `fl_bubble_cursor` rejects, `watch()` returns and never collapses. Possible causes:
   - a permission missing from the generated ACL (check `desktop/src-tauri/gen/schemas` after build);
   - `hwnd()` or `SetWindowPos` erroring when called off the main thread.

   Surface these errors while debugging. If needed, run window operations through `run_on_main_thread`, as
   `fl_drag.rs` does.
6. **The expanded SVG is not visible.** `#fb-svg` has `width="380" height="300"` with `position: absolute`.
   Confirm that `render()` produces the paths, that `style.css` does not hide or recolor them (palette
   variables resolve in this page), and that the theme attribute is applied.

## How to debug and verify

- Run the dev app: `.\build_sidecar.ps1` (needed only if `app/` changed), then `npm run tauri dev` in
  `desktop/`.
  - Debug builds allow WebView2 DevTools. Temporarily add `window.open_devtools()` for `fl-bubble` in
    `create_bubble` (debug only; remove it afterwards).
  - Note that `fl-bubble.js` blocks the context menu.
- Stop any running VEXT before rebuilding (`Get-Process vext*`). The owner allows this.
- When launching PowerShell build scripts from a tool, use `Start-Process powershell -File ... -Wait` with
  redirected output. PowerShell 5.1 turns PyInstaller's stderr into an error when redirected with `*>&1`.
- **Checks:**
  - Rust: `cargo clippy` in `desktop/src-tauri`.
  - JS: `node --check` on every changed JS file; `node tools/test_fl_launcher_ui.mjs` and
    `node tools/test_fl_launch_ui.mjs`.
  - Backend: `.venv-dev\Scripts\python.exe -m pytest -q tools/test_fl_launch.py`.
  - Run one test file per process.
- Do the final verification in the real desktop app at 100 % and at a non-100 % display scale: bubble on top
  of FL Studio, hover open and close, wheel scroll, item click opens the launcher, launcher click and drag
  into FL.
- Release build when the owner asks: `.\build_release.ps1`. Outputs go only to `release\`.

## Constraints that must not regress (from AGENTS.md and the original spec)

- `fl_focus` and `fl_drag_preset` stay callable only from `main` and `fl-launcher`. The bubble gets no other
  commands. The browser and imgsearch webviews get no IPC.
- No owner for the bubble window, and destroy the bubble and launcher when `main` is destroyed, or the app
  never exits.
- No new hex colors: use the palette tokens.
- Click-to-load keeps its confirmation (inline in the launcher, with "Don't ask again until VEXT closes",
  stamped with the per-launch token) and the `fl_focus` handoff before `POST /load`. Never auto-repeat a load.
- Keep comments short and saying why; match the surrounding style. Most files in this repo use CRLF line
  endings, so keep each file's existing endings.
- Record the decisions that fix the bug in `specs/2026-11-02-fl-quick-launcher.md`, and add a short trap note
  to `AGENTS.md` if the cause is non-obvious (for example the transparent-window hit-testing).


## Fix pass: code and automated checks

The direct visibility bug is SVG hidden reflection: #fb-svg starts with a hidden attribute and CSS
[hidden] { display: none !important }. SVGElement has no HTMLElement.hidden reflection, so assigning
svg.hidden = false sets an inert JS property while the attribute remains. Every menu show/hide now
updates the attribute explicitly. The bubble harness models that SVG behavior, rather than giving SVG
a fake HTML hidden property, and checks the actual hidden attribute.

Code inspection found that expand hid the only painted circle before native resize completed, then drew
at (190, 110) inside a still-72 px window. The regression harness holds that IPC reply pending and requires
the collapsed circle to remain visible. Expand now waits for successful native placement before switching
the visible DOM. It uses the returned clamped center, and does not wait for menu HTTP before drawing or
starting cursor tracking. This addresses a confirmed code path; the owner's desktop symptoms have not
been reproduced live under the code-only authorization.

Native resize transactions are serialized and run on the window thread. State is committed only after
SetWindowPos succeeds; no state mutex is held during window calls. Frontend transition guards prevent
hover/drag races. Resize and cursor failures are reported to the console and in the bubble tooltip;
failed collapse retains the usable expanded controls. Cursor requests do not overlap. A 1% palette-alpha
disc covers the expanded center/ring gap, and item rectangles explicitly accept pointer input. Native
hit-testing and transient resize paint timing still need real WebView2 verification.

Passed: cargo clippy --all-targets, node --check app/static/js/fl-bubble.js,
node tools/test_fl_bubble_ui.mjs, node tools/test_fl_launcher_ui.mjs,
node tools/test_fl_launch_ui.mjs, and pytest -q tools/test_fl_launch.py (47 passed; dependency deprecations).
The new bubble harness covers pending/rejected resize, retry, returned clamp coordinates, slow menu,
gap traversal, outside close, late menu replies, wheel, item click, drag suppression, serialized cursor
polls and failed-collapse recovery. It does not simulate native Windows paint or DPI changes.

Remaining acceptance: real desktop hover/close at 100% and 150%, native transparent hit-testing, edge
placement, persisted dragging, minimize/on-top behavior, launcher click/drop into FL and process exit.
Rebuild the sidecar and mirror the frontend before that check; packaged artifacts are unchanged.
