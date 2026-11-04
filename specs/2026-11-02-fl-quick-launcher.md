# FL quick launcher bubble, DAW panels and FL setup move

Status: specified 2026-11-02; implementation delegated. Builds on `2026-11-02-fl-studio-launch.md`
(preset resolver, controller bridge, native drag). Local work only: no commits, pushes or builds.

## What the owner asked for

1. Configuration > DAWs becomes collapsible dropdown panels, one per DAW, with the DAWs' original logos.
2. The FL controller script setup (MIDI output, install/update, test) moves from FL Studio Sync into the
   FL Studio panel of Configuration > DAWs.
3. The FL connection indicator moves from the toolbar into the FL Studio Sync page.
4. "Load in FL Studio…" disappears from each plugin (detail modal button and context menu). Sending to
   FL becomes a DAW-only function: a pop-up tool, enabled from FL Studio Sync.
5. The pop-up tool is a small always-on-top transparent bubble. On hover it opens a radial menu:
   center circle = FL status; left half-ring = Categories; right half-ring = Vendors (owner sketch:
   a "U" of two quarter-rings under the center circle, gap at 6 o'clock, labels above each part).
   Each ring shows at most 7 items and scrolls for more.
6. Clicking a ring item opens a launcher window with every matching plugin, with pictures. Click a
   plugin = send to FL through the controller script; dragging a card = native drop into FL.
7. The bubble works only while at least one DAW is configured (today: FL Studio).

## Decisions (owner can override)

- "Configured" = FL Studio is installed (`fl_studio_installed`) **and** the VEXT controller is installed
  and current (`setup_info.installed and not update_available`). Click-to-load still needs a live
  connection; drag works without one.
- Click-to-load keeps a confirmation, but inline in the launcher (not a modal), with a
  "Don't ask again until VEXT closes" checkbox. The existing destination warnings stay.
- Dragging a catalog card from the main window into FL stays (it is not a button). Only click-to-load
  entry points are removed from the main window.
- The center circle is status only (not a button). Categories/vendors list only plugins FL knows
  (`fl_detected = 1`), not AAX, not corrupted, not quarantined. Empty groups are hidden.
- Logos are the vendors' own marks, shipped as files and shown through `<img>` (no inline script can run),
  listed with their source in `THIRD_PARTY_NOTICES.md`. They are outside the palette on purpose (AGENTS.md).

## Contracts (all workers code against these)

### Settings (`app/web.py`)

| Key | Default | Values |
|---|---|---|
| `fl_bubble_enabled` | `"0"` | `"0"`, `"1"` (add to `_SETTING_CHOICES`) |

Bubble position is not a backend setting: the shell persists it (see Rust).

### Backend (`app/fl_launch.py`, `app/fl_launch_api.py`)

`GET /api/fl-launch/menu` →
```json
{"configured": true, "reason": "",
 "categories": [{"slug": "synth", "name": "Synth", "count": 12}],
 "vendors": [{"name": "FabFilter", "count": 9}]}
```
`reason` is a short user sentence when `configured` is false (e.g. "Install the VEXT controller in
Configuration > DAWs > FL Studio."). Lists are sorted by name, case-insensitive, `count > 0` only.

`GET /api/fl-launch/plugins?kind=category|vendor&value=<slug or vendor name>` →
```json
{"title": "FabFilter", "plugins": [{"id": 7, "name": "Pro-Q 3", "vendor": "FabFilter",
  "plugin_type": "Effect", "format": "VST3", "has_image": true}]}
```
Same eligibility filter as the menu (one helper, `launchable(conn)`), sorted by name. Unknown `kind` → 400.
Both are read-only GETs; quarantined host paths come from `db.quarantined_host_paths`.

Existing routes are unchanged: `/presets/{id}`, `/load`, `/status`, `/connection`, `/setup`,
`/install`, `/ping`, `/drag-resolve`.

### Shared preset helper (`app/static/js/fl-preset.js`, new)

Page-agnostic; no dependency on `window.VEXT`. Loaded by `index.html` and `fl-launcher.html`.
```js
window.VextFlPreset.create({ api, esc, toast }) → {
  preset(id, drag = false) → Promise<preset|null>,  // same semantics as today's fl-launch.js preset()
  choosePreset(items) → Promise<preset|null>,       // the existing <dialog class="fl-launch-dialog">
  startDrag(id, isHeld) → Promise<void>,           // resolve, then invoke('fl_drag_preset') if isHeld()
}
```
Move the bodies from today's `fl-launch.js` (`chosen` map, `preset`, `choosePreset`, drag tail). Keep the
"Preset selected. Drag the card again." behavior and the held-button check before `fl_drag_preset`.

### Tauri shell (Rust)

New module `desktop/src-tauri/src/fl_bubble.rs`. Window labels `fl-bubble` and `fl-launcher`.

| Command | Allowed caller label | Does |
|---|---|---|
| `fl_bubble_set(enabled: bool)` | `main` | Create or destroy `fl-bubble` (and destroy `fl-launcher` when disabling). Idempotent. |
| `fl_bubble_expand(expanded: bool) -> {cx, cy}` | `fl-bubble` | Resize between collapsed 72×72 and expanded 380×300 logical px, keeping the bubble's screen center fixed; clamp the expanded rect to the current monitor work area; return the bubble center inside the window (logical px). |
| `fl_bubble_cursor() -> {cx, cy}` | `fl-bubble` | Cursor relative to the bubble window (logical px). The expanded menu closes from this (outside the ring for 350 ms), not from DOM mouseleave: the window resizes under the pointer (one atomic `SetWindowPos`) and its transparent gaps are unreliable for hit-testing. |
| `fl_launcher_open(kind, value, title)` | `fl-bubble` | Open `fl-launcher` (or reuse it: emit `fl-launcher:show` `{kind, value, title}` to it and focus it). `kind` must be `category`/`vendor`; strings ≤ 200 chars. |
| `fl_launcher_close()` | `fl-launcher` | Close it. |

Every command checks `window.label()` like `fl_focus` does. `fl_focus` and `fl_drag_preset` now accept
`main` **or** `fl-launcher`; nothing else (the browser and imgsearch webviews stay without IPC).

Bubble window: `WebviewUrl::App("fl-bubble.html")`, `.transparent(true)`, `.decorations(false)`,
`.shadow(false)`, `.always_on_top(true)`, `.skip_taskbar(true)`, `.resizable(false)`,
`.focused(false)`, no owner (it must stay when VEXT is minimized), `initialization_script(config().init_script())`,
`additional_browser_args(crate::WEBVIEW_ARGS)`, `on_navigation` limited to `tauri.localhost`,
`on_new_window` deny — same hardening as `plugin_frame::create_window`.
Initial position: saved center, else the primary monitor work area's right edge minus 100 px, at
vertical middle. Persist the center (physical px) to `<app config dir>/fl-bubble.json` `{"x":..,"y":..}`
on user moves (`WindowEvent::Moved` for `fl-bubble`), ignoring moves within 300 ms after the shell's
own resize/reposition; center = window position + current center offset.

Launcher window: `fl-launcher.html?kind=..&value=..&title=..` (URL-encoded), 560×640 logical,
min 420×360, `.decorations(false)`, `.shadow(true)`, `.always_on_top(true)`, `.skip_taskbar(true)`,
resizable, same hardening. Placed beside the bubble (left if it fits, else right), clamped to the work area.

When `main` is destroyed, destroy `fl-bubble` and `fl-launcher` first (otherwise the app never exits).
Register the commands in `lib.rs` and `build.rs` (`AppManifest` command list). Capabilities:
- `default.json` (+ `allow-fl-bubble-set`)
- new `fl-bubble.json`: webviews `["fl-bubble"]`: `core:default`, `core:window:allow-start-dragging`,
  `allow-fl-bubble-expand`, `allow-fl-launcher-open`
- new `fl-launcher.json`: webviews `["fl-launcher"]`: `core:default`, `core:window:allow-start-dragging`,
  `allow-fl-focus`, `allow-fl-drag-preset`, `allow-fl-launcher-close`

## UI

### Configuration > DAWs (main window)

One `<details class="daw-panel" data-daw="...">` per DAW. Summary row: logo (`<img src="/static/img/daws/<key>.svg">`,
40 px), name, status badge, chevron. Open state remembered in `localStorage` (`pa-daw-open`), FL open by default.
- **FL Studio**: install folder (today's row), plugin database path, FL-NATIVE folders, then a
  **Controller script** subsection: today's `fl-launch.js` setup panel (MIDI output select, Install/Update,
  Test connection, controller path, hints, status line), mounted into `#fl-controller-root` inside this panel.
  Status badge: "Configured" (ok) / "Controller not installed" (warn) / "Not found" (muted).
- **Ableton Live, Cubase, REAPER, Studio One, Bitwig Studio**: collapsed, disabled, "Not supported yet".
Remove the old "Other DAWs" subsection and the inline-SVG `DAW_LOGOS` (fallback to a lettered tile if an image fails to load).

### FL Studio Sync (main window)

Remove `#fl-launch-root` from the page. Add, above the Organize / Check & fix tabs, an `#fl-tools` row:
- **Connection visor**: the light + text that used to sit in the toolbar (closed / running-disconnected /
  connected, message as tooltip), plus a "Set up" link to Configuration > DAWs when not configured.
- **Quick launcher**: a switch "Show the FL launcher bubble" bound to `fl_bubble_enabled`. Disabled with the
  `/menu` `reason` when not configured. Turning it on/off saves the setting and invokes `fl_bubble_set`.
  Hint: "Hover the bubble for categories and vendors; click a plugin to send it to FL, or drag it into FL."
  Outside the desktop app the switch is disabled ("Desktop app only").

Remove the toolbar `#fl-connection` button. On app start (desktop only), if `fl_bubble_enabled == "1"` and
`/menu` says configured, invoke `fl_bubble_set(true)`. When FL presence or the controller changes
(`applyFlStudioPresence`, after Install/Update) re-evaluate and invoke `fl_bubble_set(false)` if no longer
configured (the setting stays on, so it comes back once configured).

Remove "Load in FL Studio…" from the plugin modal (`app.js`) and the context menu (`contextmenu.js`).
`V.flLaunch` keeps `mount` (controller setup, now in DAWs), `refreshConnection` (now feeding the visor and
polling only while FL Studio Sync is visible) and card dragging (through `VextFlPreset`).

### Bubble (`fl-bubble.html`, `js/fl-bubble.js`, `css/fl-bubble.css`)

Transparent page (`html, body { background: transparent }`), no scrollbars, `user-select: none`, theme from
`localStorage pa-theme` like `plugin-frame.html`. Palette tokens from `style.css`; ring fill is
`--vext-jet` with a `--vext-black` 2 px stroke (owner's sketch), labels on White Smoke tiles with a taupe border.

- **Collapsed** (window 72×72): a 56 px circle with the VEXT mark (owner, 2026-11-02) and a status ring (ok / warn / steel).
  `data-tauri-drag-region` on it, so it can be moved. Pointer-down cancels pending hover expansion.
- **Expand** after 200 ms hover: `invoke('fl_bubble_expand', {expanded: true})`, then draw around the
  returned `{cx, cy}`. Keep the collapsed circle visible until resize succeeds. Fetch menu data
  independently. **Collapse** after 350 ms outside the menu, using serialized native cursor polls;
  DOM `mouseleave` is unreliable during resize. Defer close while a wheel scroll is in progress.
- **Expanded geometry** (SVG, logical px, angles clockwise from 12 o'clock): center circle r = 46 (FL logo,
  status color, status word under it), "FL STATUS" label tile above it. Ring inner r = 70, outer r = 172.
  Left ring = Categories, 184°–270°; right ring = Vendors, 90°–176° (8° gap at 6 o'clock). Labels
  "CATEGORIES" / "VENDORS" above the ring ends at 9 and 3 o'clock.
- **Items**: 7 slots per ring, evenly spaced along the mid-radius (121), each a horizontal text button
  (11 px, max 92 px, ellipsis, full name in `title`). Hover highlights with taupe. Wheel over a ring shifts
  that ring's window by one item per notch (clamped); a thin scroll-position arc on the ring's outer edge
  when it has more than 7 items. Count shown as a small suffix only if it fits.
- Click an item → `invoke('fl_launcher_open', {kind, value, title})`.
- Data: `/api/fl-launch/menu` on load and every expand; `/api/fl-launch/connection` every 2 s.
  Not configured → the center shows "Set up FL in VEXT" and rings show nothing.

### Launcher (`fl-launcher.html`, `js/fl-launcher.js`, `css/fl-launcher.css`)

Own title bar (drag region, title "Vendor · FabFilter" / "Category · Synth", count, close button →
`fl_launcher_close`). A filter box. A responsive grid of cards: picture (`/api/plugins/<id>/image`, same
URL form as `app.js`; placeholder tile when `has_image` is false), name, vendor, type and format badges.
Footer: connection light + message and the last FL result.

- **Click a card**: `preset(id)` (chooser if ambiguous) → inline confirm bar with today's destination text
  (Generators may replace the selected channel; Effects open on the selected mixer insert) and the
  "Don't ask again until VEXT closes" checkbox (sessionStorage) → `invoke('fl_focus')` → `POST /load` →
  poll `/status` like `waitForFL`. Disabled with the connection message while disconnected.
- **Drag a card** (8 px threshold, left button held): `startDrag(id, () => held)`. Suppress the click
  after a drag. Exposes `window.VextFlLauncher = { load, setQuery }` for the node test.
- Listens for `fl-launcher:show` and reloads the list for the new query.

## Files and ownership

| Worker | Files |
|---|---|
| Rust shell | `desktop/src-tauri/src/fl_bubble.rs` (new), `lib.rs`, `build.rs`, `fl_focus.rs`, `fl_drag.rs`, `capabilities/*.json` |
| Bubble + launcher + backend | `app/static/fl-bubble.html`, `fl-launcher.html`, `js/fl-bubble.js`, `js/fl-launcher.js`, `js/fl-preset.js`, `css/fl-bubble.css`, `css/fl-launcher.css`, `app/fl_launch.py`, `app/fl_launch_api.py`, `app/web.py` (setting only), `tools/test_fl_launch.py`, `tools/test_fl_launcher_ui.mjs` (new) |
| Main window UI | `app/static/index.html`, `app.js`, `js/contextmenu.js`, `js/flsync.js`, `js/fl-launch.js`, `css/fl-launch.css`, `css/flsync.css`, `style.css` (DAW panel rules), `tools/test_fl_launch_ui.mjs` |
| Logos | `app/static/img/daws/{fl,ableton,cubase,reaper,studioone,bitwig}.svg` (or `.png`), `THIRD_PARTY_NOTICES.md` |

AGENTS.md (FL section: `fl-launcher` may call `fl_focus`/`fl_drag_preset`) and CHANGELOG are updated by the coordinator.

## Verification

- `python tools/test_fl_launch.py` (one process): `/menu` and `/plugins` eligibility (AAX, corrupted,
  quarantined, not FL-detected excluded), `configured` false without the controller, bad `kind` → 400.
- `node tools/test_fl_launcher_ui.mjs`: focus before load, cancel = no load, "don't ask again", drag only
  while held. `node tools/test_fl_launch_ui.mjs` trimmed to drag behavior.
- `cargo check` (and `cargo clippy`) in `desktop/src-tauri`.
- `node --check` on every changed JS file; `ruff check app tools`.
- Live check in the desktop app (bubble hover, scroll, launcher click/drag into FL) remains a manual step
  for the owner.


## Bubble fix decisions (2026-11-02)

Toggle the SVG hidden attribute with removeAttribute/setAttribute. SVGElement has no HTML hidden
reflection; svg.hidden = false leaves [hidden] in place and the radial menu remains invisible. The
regression harness deliberately models SVG attributes separately from inert JS properties.

Keep logical window sizes and the screen-center anchor contract. Serialize native geometry changes on
the window thread and commit the stored offset/expanded flag only on success. Never hold the state mutex
during native window calls. Frontend visibility follows successful placement; command errors remain
visible in console/tooltip and leave the current controls usable. Menu HTTP cannot gate rendering or
cursor tracking. Ignore hover while transitioning or dragging. Initial placement reads the shell's
clamped collapsed center as well.

Paint a disc behind the menu with 1% Jet Black alpha (palette mix) to give WebView2 a native hit surface
across the center/ring gap; SVG item rectangles use pointer-events: all. The native cursor remains the
source for the outside timeout. This surface and native resize paint timing need live desktop checking;
the owner authorized code and automated checks only for this pass. Automated coverage lives in
tools/test_fl_bubble_ui.mjs; launcher focus/confirmation/drag behavior is unchanged.


## Owner-approved refinement (2026-11-02; supersedes original UI/data contracts above)

The owner verified the rebuilt hover fix, then requested a UI improvement and approved retaining curved
selectors with fully horizontal names. Expanded geometry is now 580 x 400 logical px with the anchor at
(290, 86); collapsed remains 72 x 72 with a 56 px VEXT center. Each curved selector shows five generous
40 px rows with horizontal text, wrapping, counts, keyboard focus, neutral taupe hover and range/scroll
hints. An opening fade lasts 180 ms; reduced-motion preference disables animation/transitions. No new
palette colors are added. The upper status pill alone shows green Connected, yellow Waiting or red FL
closed; the VEXT center and selector items stay neutral.

Drag the center in either layout. Expanded dragging starts after a 6 px threshold using the existing
window start-dragging capability. Suspend pointer-close checks during dragging. Persist native user
moves in both layouts, still ignoring shell moves. Window labels/command restrictions and main-window
shutdown behavior are unchanged.

The launcher is now FST-first: enumerate Installed/Effects|Generators/VST|VST3|CLAP|Fruity plus filed
Effects/Generators at any depth. No FST means no launcher card, regardless of fl_detected. FL-only FSTs
need no VEXT catalog row. The exact relative path is the identity used for loading and dragging. Combine
same-section, same-stem byte-identical filed mirrors (Installed preferred), never similar names or
nonidentical files. Catalog association uses actual NFO binary paths or explicit manual links, and is
optional enrichment. Effects/Generators groups always exist; unknown vendors use Unassigned. Existing
catalog categories enrich verified matches. Known corrupted/quarantined entries stay excluded, including
filed mirrors with no NFO that match an Installed FST byte-for-byte.

GET /plugins returns id (nullable), key, name (actual FST stem), vendor, plugin_type, format, has_image,
and has_preset_image. GET /preset?key validates one FST under the allowed trees; GET /preset-image?key
serves only an existing PNG inside the configured database. Catalog images remain a fallback. POST
/load and /drag-resolve accept plugin_id=null plus the exact key for the launcher; main-window catalog
drag continues to use a real plugin ID. The native fl_drag_preset command accepts an optional ID but
still calls the authenticated backend to resolve the key. Confirmations, foreground handoff, held-button
checks and no automatic load retries are preserved. The controller protocol is unchanged.

Automated checks cover FL-only preset listing/load/drag, no-catalog and NFO-free entries, exact mirror
deduplication, missing files, rejected path escapes/nonplugin trees, bounded images, quarantine bypass
attempts, open-center dragging, horizontal markup and all three status states. Live verification is
left to the owner; no computer-use session is run in this pass.


## Motion and latency correction (2026-11-02; supersedes native resize contract above)

Owner reports: the logo briefly moves to a corner on hover; a menu opened at an edge stays cropped after
being dragged; plugins arrive after the launcher opens; entrance/scroll motion is abrupt.

Keep the native bubble canvas at 580 x 400 in both states. Its closed input/paint region is a 72 logical
px ellipse around the real anchor; opening removes the region. SetWindowRgn runs on the window thread;
release the HRGN only on failure, as Windows takes ownership on success. Never resize or reposition on
hover. Keep the same HTML logo circle visible in both states. Only selectors/status animate. This
avoids the native-origin/DOM repaint race rather than hiding it behind a fade.

Selector layout has a bounded origin independent of the anchor, so a near-edge anchor cannot push a
row outside the viewport. Reflow the canvas on drag completion and after debounced native move events;
clamp to the monitor work area and preserve the screen anchor. The circle uses the same 6 px native
drag threshold in both states; pointer capture keeps releases observable. A native clipping check and
cross-monitor DPI interaction still require desktop verification by the owner.

Selector rows live in a 216 px clipped viewport. Render the full column once and transition its
translateY by 44 px per wheel step, 220 ms easing, in either direction. Retarget ongoing transitions for
continuous input; do not recreate the SVG on wheel events. Only visible rows are in the tab order.
Entrance fades/slides over 280 ms; logo hover scales the mark gently. Reduced motion disables these
transitions. Unchanged menu/status polling must not rebuild row DOM and restart animation.

GET /menu?include_plugins=true additionally returns the same listing records and category memberships
used by the menu. The bubble warms this on startup/hover and every 5 s and writes one same-origin
launcher snapshot stamped with the per-launch token. The launcher paints its selected group immediately
from a matching snapshot no older than 15 s, then refreshes HTTP data. No snapshot displays Loading FL
presets instead of a blank grid. A snapshot is presentation only: connection, confirmation, foreground
handoff, exact-key validation and native held-button checks remain authoritative.

Backend listing reuse is keyed by preset/NFO/PNG stats, resolved paths, catalog rows, category metadata,
manual links, quarantine and picture index. FST hashes are separately keyed by nanosecond file stats;
file removal/edits and catalog/quarantine changes invalidate the listing on the next request. This
removes redundant content reads and matching between menu and launcher requests without caching load
authorization. Automated regressions cover invalidation and cold/prefetched launcher behavior.


## Full FL preset index (2026-11-02; supersedes curved selector UI)

Owner requested replacing the bubble's scrollable selectors with a full index, using the supplied
sidebar reference. Clicking the persistent bubble opens/reuses fl-launcher with kind=all and an empty
value. Hover remains a status pill (green Connected, yellow Waiting, red FL closed). No category/vendor
rows or wheel handler remain in the bubble. The same native clipping mechanism exposes only the circle
and status pill on hover; dragging has a threshold and click suppression after release. Keyboard
Enter/Space also opens the index. No large window opens merely on hover.

The index opens at 960 x 720 logical px, minimum 680 x 460, beside the actual circle rather than the
transparent canvas bounds. Clamp its size and position to the monitor work area. It retains its existing
fl-launcher label, native capabilities, destruction on main exit and always-on-top behavior. Native
fl_launcher_open accepts all, category and vendor for compatibility; IPC privileges are unchanged.

A search field sits above the content. The left sidebar has All/Effects/Instruments type scope,
Category | Vendor buttons and all group rows with names/counts plus All plugins. Both the sidebar and
results scroll independently. Groups are not dropdowns and are not limited to five rows. The right side
has Cards (pictures), List (thumbnail rows) and Compact list (dense rows without pictures), remembered in
localStorage. Selecting a group/type or changing search filters the full in-memory listing without a
new HTTP request. Counts are scoped to current search/type while group names remain visible. Switching
Category/Vendor clears the previous group; Clear filters resets group, type and search. Ctrl/Cmd+F
focuses search. Name, vendor, category, format and relative FST key are searchable; multiple words all
must match. Sorting is alphabetical with exact-key tie-breaking.

The index reads the complete GET /menu?include_plugins=true snapshot, with the same per-launch warm
snapshot for immediate first paint. An unchanged HTTP refresh does not rebuild cached cards. There is
no new backend listing source, filesystem mutation or controller revision. Catalog-independent .fst
entries remain included; all views keep exact keys, inline confirmation, connection gating, fl_focus
before POST /load, native dragging only while held, and no automatic retry.

Node checks cover all entries on first open, local category/vendor/type/search filtering, clearing,
three view modes, compact rows omitting images, persisted view, no extra listing request on browsing,
status-only bubble hover, opening the all-index on click and suppressing drag-end clicks. Existing
confirmation, focus, disconnected, exact-key and drag tests remain in place.
