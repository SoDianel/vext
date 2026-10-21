# Analyzer section: settings window, knobs, contrast, timeline layout, test signals (2026-10-20, owner)

Repo: the VEXT repository root. Frontend is vanilla JS/CSS in `app/static/` (no build step,
no framework, IIFE modules that hang off `window.VEXT`). Read the top comment of each file you touch and
match its style: same comment density (short, plain-English comments that say *why*), same naming
(`els`, `params`, `setParam`, `V.toast`), `const`/arrow functions, two-space indent, single quotes.

Hard rules for every agent:
- **Never commit, never create branches, never run git write commands.** Leave changes uncommitted.
- **Colours:** never add a new hex colour. Use the palette tokens from `app/static/style.css`
  (`--accent`, `--accent-fg`, `--accent-soft`, `--accent-text`, `--fg`, `--fg-2`, `--fg-3`, `--bg-content`,
  `--bg-subtle`, `--bg-control`, `--bg-popover`, `--separator`, `--separator-strong`, `--shadow-1/2`,
  `--radius-s/m`) or `color-mix()` of them.
- Dark-theme CSS uses the existing two-selector pattern (see `css/analyzer.css` lines 48–61):
  `@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) X { … } }` **and**
  `:root[data-theme="dark"] X { … }`.
- Only edit the files your task owns (listed per task). If you think another file must change, say so in
  your final report instead of editing it.
- Check syntax with `node --check <file>` for every JS file you touch. No console errors.
- Space must keep playing / pausing the Playground anywhere in the section (see `analyzer.js spaceIsOurs`):
  don't swallow Space in your widgets (a knob is a `div role="slider"`, not an input, so Space still reaches
  the section; typed `<input>`s are exempt already).

Existing code to know: `js/analyzer.js` (the section shell, settings row, meters, transport),
`js/analyzer-dsp.js` (`VEXT.AnalyzerDSP`: `normalize`, `LIMITS`, `BLOCKS`, `WINDOWS`, `FPS`, `AVG_TIMES`,
`HISTORIES`, `fmtInt`, `fmtHzShort`, `MINUS`), `js/analyzer-timeline.js` (the waveform strip on top),
`js/audio-bus.js` (`VEXT.audioBus.pushLocal`), `js/analyzer-tap-worklet.js` (the AudioWorklet that turns
page audio into Analyzer frames), `js/plugin-frame.js` lines 282–400 (an existing SVG knob: arc maths and
vertical drag to copy the feel from), `css/analyzer.css`.

---

## Feature 1 — Settings move into a detached (floating) window; Freq and Range become knobs

**Owner's words:** "Move all the specific configurations (block, window, avg, etc.) into a detached window,
a configuration button opens and closes the window. Freq and range are knobs with fixed values from the
lowest to the highest values already standardized; double clicking them lets you add the value manually."

### Behaviour
- The settings row (`.an-settings`, built by `renderSettings()`) no longer sits in the page flow. It lives
  in a **floating panel** `.an-setwin` over the section (non-modal: the views stay live and visible behind
  it, so changes are seen as they happen). It holds exactly what the row holds today, still "only what the
  view uses" (Block, Window, Avg, Meter, Refresh, Freq, Range, History).
- A **Settings** button (`data-act="settings"`, gear icon + "Settings", `aria-pressed`, `aria-expanded`,
  `aria-controls`) in the bar, just before Freeze, opens and closes it. Pressed look while open.
- The panel: a header (title "Analyzer settings" + the view's name, e.g. "· Spectrum", and a × close button
  `data-act="settings-close"`), then the fields laid out as a compact grid (label left, control right;
  the four knobs in one row: Freq Lo, Freq Hi, Range Lo, Range Hi).
- **Draggable by its header** (pointer events + `setPointerCapture`; ignore drags that start on a button).
  Kept inside the `.an` root's box (clamped on drag and on window resize). Position and open/closed state
  are remembered in `localStorage` key `pa-analyzer-setwin` as `{open, x, y}` (x / y relative to `.an`).
  Default position: top-right corner of the views area, 8 px in.
- Closes on: the Settings button, ×, Escape while focus is inside the panel. **Not** on outside clicks
  (it is a tool window). When it closes with focus inside, focus returns to the Settings button.
- `z-index` above the panes and the hover readout (> 3), below the Capture menu (30) — use 20.
- Changing the view (Spectrum → Imager) re-renders the panel's fields (it already does via
  `renderSettings()`); the panel stays open.
- Narrow window (≤ 760 px): the panel is at most `calc(100% - 16px)` wide and scrolls inside itself if taller
  than the section.

### Knobs (`js/analyzer-knob.js`, new, `VEXT.AnalyzerKnob`)
API (Feature 4 also uses this, keep it exactly):
```js
const k = VEXT.AnalyzerKnob.create({
  label: 'Lo',               // short text under the knob
  ariaLabel: 'Frequency low (Hz)',
  values: [ ... ],           // the fixed steps, ascending
  value: 20,                 // current value (may sit between steps if it was typed)
  min: 0, max: 30000,        // what a typed value is clamped to
  format: (v) => '20',       // the text shown under the knob and in aria-valuetext
  parse: (text) => number,   // optional; default parseFloat, accepting a trailing k/K (x1000)
  onChange: (v) => {},       // called on every step while dragging and on a typed commit
});
k.el        // the root element to insert (div.ak)
k.set(v)    // show a value (no onChange)
k.setDisabled(bool)
k.destroy()
```
- Look: a 34 px SVG knob (track arc + value arc + tick, 270° sweep, like `plugin-frame.js drawKnob`), the
  value text below it (tabular numbers), the label below that in `--fg-3`. Value arc in `--accent`.
  CSS classes `.ak`, `.ak-svg`, `.ak-track`, `.ak-arc`, `.ak-tick`, `.ak-val`, `.ak-label`, `.ak-edit`;
  put the CSS in `css/analyzer-settings.css`.
- Position = index into `values`; a value between two steps is drawn at the interpolated fraction; below the
  first / above the last clamps to the ends.
- **Drag** up/down: one step per 6 px from where the press started (snaps to steps; a typed in-between value
  snaps to the nearest step once dragged). **Wheel** over the knob: ±1 step (preventDefault).
  **Keyboard** (`role="slider"`, `tabindex="0"`, `aria-valuemin/max/now/valuetext`): Up/Right +1,
  Down/Left −1, PageUp/PageDown ±3, Home/End first/last, Enter opens the typing box.
- **Double-click** (or Enter): the value text is replaced by a small `<input class="ak-edit" type="text"
  inputmode="decimal">` with the current value selected. Enter or blur commits (parse → clamp min..max →
  `onChange`), Escape cancels. An unparseable entry is ignored (the old value shows again).
- `onChange` returns nothing; the caller normalizes (`D.normalize` keeps the Lo / Hi gap) and calls
  `k.set()` with the value that was actually kept — so the knob always shows the real parameter.

### Fixed steps (put them in `analyzer-knob.js` as `VEXT.AnalyzerKnob.STEPS`)
- `ISO` = the ISO 266 third-octave series 10 Hz … 20 kHz:
  10, 12.5, 16, 20, 25, 31.5, 40, 50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 630, 800,
  1000, 1250, 1600, 2000, 2500, 3150, 4000, 5000, 6300, 8000, 10000, 12500, 16000, 20000.
- `freqLo` = `[0, ...ISO up to 16000]` (0 = from DC, the axis already supports it).
- `freqHi` = `[...ISO from 20, 22000, 24000, 30000]` (30000 = `LIMITS.freqHi[1]`).
- `range` = −120 to +18 every 6 dB (both Range knobs; +20, the limit, can still be typed).
- `level` = −60 to 0 every 3 dB (Feature 4).
- `tone` = ISO from 20 to 20000 (Feature 4).
- Formats: frequency `0`, `31.5`, `630`, `1k`, `1.25k`, `20k` (unit "Hz" goes in the field label, not
  each value); dB with `D.fmtInt` (true minus sign), one decimal only for typed non-integers.

### Files (Agent A owns)
`js/analyzer-knob.js` (new), `css/analyzer-settings.css` (new), `js/analyzer.js` — only `renderSettings`,
`syncControls`, `onChange`, the settings-window open/close/drag code, and the `.an-setwin` markup in
`shellHtml()` (already stubbed, see below). `index.html` already loads both new files.

---

## Feature 2 — Selected buttons must be clearly visible in dark mode

**Owner's words:** "When selecting buttons such as analyzer type, signal or layout, also the loop section, in
dark mode, the difference between selected or not is not visible, use a more contrasting color from the
palette."

- Today the pressed segment (`.an .segmented button[aria-pressed="true"]`) gets `--bg-control`, which in dark
  is almost the same as the strip's `--bg-subtle`.
- **Dark theme only**, inside `.an` (covers View, Signal, Layout, Meter, the timeline's Loop, and the test
  signal section's segments): the pressed segment is `background: var(--accent)` (Dusty Taupe),
  `color: var(--accent-fg)` (Black, 6.7:1), no grey shadow, `font-weight: 600`. Hover on a pressed one:
  `--accent-hover`. Unpressed segments keep their look, but their text goes to `--fg-2` → `--fg` on hover.
  A disabled pressed segment keeps the accent at the existing 0.45 opacity.
- Same treatment in dark for `.an button.an-btn[aria-pressed="true"]` (Freeze, Settings, the generator's
  Start when on): accent fill + accent-fg text instead of the soft tint.
- Light theme: unchanged.

### Files (Agent B owns) — `css/analyzer.css`.

---

## Feature 3 — Play before the waveform, Loop above it, the waveform runs to the section's end

**Owner's words:** "Move the play button to the beginning, before the wave window, Loop control also moves on
the top of the waveform window, let the window width go to the end of the module."

New timeline markup (`analyzer-timeline.js create(box)`):
```
.an-tl
  .an-tl-top      title · "0:12 / 3:45" (cur / dur) · spacer · "Loop" Off|Selection|All
  .an-tl-row
    .an-tl-lead   (empty: the Analyzer puts its Play / Pause button here)
    .an-tl-track  (flex: 1 1 auto — runs to the right edge of the section, above the meters' end)
```
- `create()` returns `lead` (the `.an-tl-lead` element) in its API object alongside `feed`, `recolor`,
  `destroy`. Keep every existing class the code queries (`.an-tl-title`, `.an-tl-cur`, `.an-tl-dur`,
  `.an-tl-track`, `.an-tl-canvas`, `.an-tl-sel`, `.an-tl-head`, `.an-tl-msg`, `[data-loop]`).
- The title may now take up to ~60% of the top row (ellipsis). The track is 44 px tall.
- The Play button in the lead (`.an-transport`, already rendered by `analyzer.js renderTransport()`):
  44 px tall, matches the track height, icon + "Play"/"Pause" text, min-width 84 px; `--accent` fill /
  `--accent-fg` text when it can play (it's the main action), normal disabled look otherwise.
- The source line (`.an-source`) no longer carries the transport.

### Files (Agent B owns) — `js/analyzer-timeline.js`, `css/analyzer.css`. (`analyzer.js` is already wired
to move the transport into `timeline.lead`; see "Pre-wired" below.)

---

## Feature 4 — Test signals: tones and noises between the waveform and the analyzer bar

**Owner's words:** "Include a small section between the waveform section and the analyzer type of pure
signals: sine wave, square, triangle, etc. Playground audio (YouTube or audio) can't be played together with
the pure signal. This section should also include the classic noises (white, brown, pink, and other popular
audio signals for testing software and hardware)."

### What it is
A compact strip `.an-gen` (one row that wraps) generated **in the page** with Web Audio, heard on the default
output, and fed to the Analyzer through `VEXT.audioBus.pushLocal` (Before = After: it does not go through a
plugin — the source line says "No plugin: Before and After are the same.").

Layout, left to right:
`[▶ Test signal / ■ Stop]  Tone: Sine Square Triangle Saw · Sweep Impulse · Noise: White Pink Brown  [Freq knob] [Level knob]  Channels: Both L R Inverted`

- **Types:** Sine, Square, Triangle, Saw (`OscillatorNode`, band-limited); **Sweep** = logarithmic sine sweep
  20 Hz → 20 kHz over 10 s, looping (generated `AudioBuffer`, exponential sine sweep, 5 ms fade at each
  end); **Impulse** = one single-sample click per second (looping 1 s buffer); **White**, **Pink** (Paul
  Kellet's refined filter), **Brown** (leaky integrated white, ×3.5 then normalized) — 10 s stereo-identical
  looping buffers made once and cached. Every buffer is normalized to peak 1.0. One type is selected at a
  time across all three groups (three `.segmented an-seg` groups with `data-gen-type` buttons,
  `aria-pressed`).
- **Freq** knob (`VEXT.AnalyzerKnob`, steps `STEPS.tone`, typed 1 … 20000, default 1000) — enabled only for
  Sine / Square / Triangle / Saw. **Level** knob (`STEPS.level`, typed −90 … 0 dBFS peak, default −18).
  Label: "dBFS".
- **Channels:** Both (same in L and R), L, R, Inverted (R = −L, out of phase: useful for Correlation and
  Imager). Graph: source → level gain → `ChannelMerger(2)` through two gains (L, R) whose values are
  (1,1) / (1,0) / (0,1) / (1,−1) → `ctx.destination` and → the tap.
- **Tap:** `ctx.audioWorklet.addModule('/static/js/analyzer-tap-worklet.js')`, then
  `new AudioWorkletNode(ctx, 'vext-analyzer-tap', { numberOfInputs: 2, numberOfOutputs: 0, channelCount: 2,
  channelCountMode: 'explicit' })`; connect the merger to inputs 0 **and** 1; `port.postMessage({on: true})`
  while playing, `{on: false}` when stopped; `port.onmessage` → `V.audioBus.pushLocal({rate: ctx.sampleRate,
  before: d.before, after: d.after})` (same as `playground.js buildGraph`). Prefix the URL with nothing
  (the Tauri shell serves `/static/...` itself).
- **No clicks:** start ramps the level gain 0 → level in 20 ms; stop ramps to 0 in 20 ms, then stops the
  source. Freq and Level changes while playing use `setTargetAtTime(…, 0.015)`. A type or channel change
  while playing crossfades (old source fades out 20 ms, new one fades in).
- One `AudioContext` made on the first Start (a user gesture), `suspend()`ed 1 s after Stop, closed in
  `destroy()`.
- Settings `{type, freq, level, channels}` saved in `localStorage` key `pa-analyzer-gen`; never auto-starts.

### Never together with the Playground
- **Start:** if `V.playground && V.playground.playing()` → `await V.playground.playPause()` first (pauses the
  Audio tab's file, or the browser's video), then start.
- **While playing**, every 250 ms: if `V.playground.playing()` becomes true (the user pressed Play, Space,
  or played in the Playground), stop the test signal at once (ramp) and show the note "Stopped: the
  Playground is playing" for 3 s in `.an-gen-note`.
- Leaving the section stops it (`destroy()`).

### Module API (`js/analyzer-generator.js`, new, `VEXT.AnalyzerGenerator`)
```js
const gen = VEXT.AnalyzerGenerator.create(box);   // box = the .an-gen element
gen.playing()    // bool
gen.describe()   // e.g. "a test signal (Pink noise, −18 dBFS, both channels)" — for the source line
gen.stop()       // ramp down and stop (no-op when stopped)
gen.destroy()    // stop, close the context, remove listeners, empty the box
```

### Files (Agent C owns) — `js/analyzer-generator.js` (new), `css/analyzer-generator.css` (new).
`index.html` already loads them; `analyzer.js` already creates / destroys it and asks `describe()`.

---

## Pre-wired by the coordinator (already done before the agents start)
- `index.html`: loads `css/analyzer-settings.css`, `css/analyzer-generator.css`, `js/analyzer-knob.js`,
  `js/analyzer-generator.js` (knob and generator before `analyzer.js`).
- `analyzer.js shellHtml()`: order is timeline → `.an-gen` → bar (with the Settings button) → source line →
  main; the `.an-setwin` panel (`#an-setwin`, header `.an-setwin-head` with `.an-setwin-title`,
  `.an-setwin-view` (empty: put the view's name there), `.an-setwin-x` `data-act="settings-close"`, then
  `.an-settings`) is a direct child of `.an` after `.an-main`, `hidden` by default. Agent A's CSS must give
  `.an` `position: relative` and the panel `position: absolute`. The Settings button
  (`.an-setbtn`, `data-act="settings"`) is in the bar before Freeze; `GEAR` icon const exists.
  `onClick` does not handle `settings` / `settings-close` yet — Agent A adds that.
  `.an-gen` is a direct child of `.an` between `.an-timeline` and `.an-bar` (Agent C's CSS should hide
  it when `:empty`).
  `build()` creates the generator (`V.AnalyzerGenerator.create(q('.an-gen'))` if loaded) and moves the
  transport into `timeline.lead` (falls back to the source line if there's no `lead`); `teardown()`
  destroys the generator; `sourceInfo()` says the generator's `describe()` while it plays;
  `toggleTransport()` stops the generator before playing the Playground.

## Done means
- `node --check` passes on every touched JS file.
- Light and dark both checked by reading the CSS (no new hex colours).
- A short report: what changed, file:line of the main pieces, anything not done or unsure.
