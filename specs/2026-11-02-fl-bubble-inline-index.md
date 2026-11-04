# FL bubble: inline preset index, Installed database only

Owner request (2026-11-02): the bubble's index showed every plugin twice, and it opened as a separate
window. The index must list only FL's default database (`Plugin database\Installed`) and appear inside
the bubble's own frame on hover, closing back to the bubble when the pointer leaves.

## 1. Installed database only (backend)

FL keeps the same presets twice: `Installed\{Effects,Generators}\{VST,VST3,CLAP,Fruity}\*.fst` and the
user-filed trees `Effects\...` / `Generators\...` beside `Installed`. Filed copies are rewritten by FL,
so the byte-identical dedup missed most of them (on the maintainer's PC: 892 Installed, 896 filed).

- `launchable()` reads only `Installed\<section>\<format>\*.fst`. Filed trees are not scanned.
- `preset_by_key()` (key-based load, drag and preset image) accepts only `Installed/<section>/<format>/<name>.fst`.
  A filed key is rejected with "Choose a plugin preset from FL's Installed plugin database."
- Catalog enrichment, quarantine/corruption checks and the main window's `presets(plugin_id)` path
  (including its manual-link fallback) are unchanged.

## 2. The index lives inside the bubble window

There is no `fl-launcher` window any more. `fl-bubble.html` contains the circle and the index panel.

### Canvas and native clipping (`fl_bubble.rs`)

Logical px. `R = 36` (bubble radius incl. ring), `GAP = 10`, `MARGIN = 8`,
`PANEL_MAX = (880, 600)`, `PANEL_MIN = (520, 360)`.

- Canvas: `(PANEL_MAX.w + GAP + 2R + 2*MARGIN, PANEL_MAX.h + 2*MARGIN)`, capped to the monitor's work
  area in logical px. Computed at creation and on every reflow (the monitor may change); `State` stores it.
- Anchor column: right (`offset = (W - MARGIN - R, H/2)`, panel opens to the left) when the canvas fits
  left of the centre without clamping, else left (`offset = (MARGIN + R, H/2)`, panel opens right).
  `place()` still clamps; the screen anchor is preserved.
- `panel_rect(canvas, offset)` (pure, unit-tested): pick the side with more room; width is that room
  clamped to `[PANEL_MIN.w, PANEL_MAX.w]` and to `W - 2*MARGIN`; height `min(PANEL_MAX.h, H - 2*MARGIN)`,
  vertically centred on the anchor and clamped inside the canvas. On a tiny screen the panel may overlap
  the circle; the circle is drawn above it.
- Hover never resizes or moves the HWND. Closed: elliptic region around the anchor (as before).
  Open: `CombineRgn(ellipse, round-rect(panel, 12 px radius), RGN_OR)`. Windows owns the region after a
  successful `SetWindowRgn`; delete it only on failure.
- `fl_bubble_expand(expanded, reflow)` returns `{cx, cy, panel: {x, y, w, h}, side: "left"|"right"}`.

### IPC

- `fl_focus` and `fl_drag_preset` accept `main` and `fl-bubble`. `fl-bubble.json` grants
  `allow-fl-focus`, `allow-fl-drag-preset`, `allow-fl-bubble-expand`, `allow-fl-bubble-cursor`,
  `core:window:allow-start-dragging`.
- Removed: `fl_launcher_open`, `fl_launcher_close`, `capabilities/fl-launcher.json`, `fl-launcher.html`.

### Page behaviour

- Hover on the circle for 200 ms, or click it, opens the panel. A drag release never opens it.
- The panel closes 400 ms after the pointer leaves both the circle and the panel (the panel rect
  inflated by 12 px, so the gap between them counts as inside), or on the close button or Escape. It
  does not close during a native preset drag into FL or while the bubble itself is being dragged.
- The VEXT circle stays draggable while open; after the drag, reflow refits canvas and panel.
- Connection status is a pill in the panel header: green Connected, yellow Waiting, red FL closed. It is
  the only colored status. The footer keeps the FL result and the connection message as text.
- Search, Effects/Instruments scope, Category | Vendor sidebar rows and the three icon views keep
  working exactly as in the former window (`fl-launcher.js`), including confirm, "don't ask again",
  focus handoff and exact-key drag. Each open refreshes the listing in the background.
- The window is created unfocused; clicking into the panel activates it for typing. Hover does not
  steal focus.
