# Playground › Signal tab, simplified (2026-10-22, owner)

Follows `specs/2026-10-20-playground-signal.md` (read its "Hard rules for every agent": they all still apply:
no git writes, only your files, no hex colours, `node --check` / `pytest` / `ruff check`, Space belongs to
the Playground). Repo: the VEXT repository root.

## Owner's words

- Remove the 4 oscillators limit: only 1 oscillator.
- Remove Burst and Staircase.
- Oscillator, Noise, Impulse are now tabs (y-scroll if needed).
- The signal visualizer (the scope) is full height in the window (dynamic, no scroll).
- No audio wave view in the Signal tab: a button like the Browser tab's **Record** instead.
- Tab order: **Browser | Signal | Audio**.
- Modulation: **removed completely** (Sweep, Vibrato, Tremolo).
- Record (owner's choice): **like the Browser tab**: it records what comes out of the plugin while the
  signal plays; Stop opens that recording as the Audio tab's file and shows the Audio tab.

## The new patch (`app/signal_gen.py`; all fields optional, `clean_patch` fills these defaults)

```jsonc
{
  "seconds": 10, "rate": 48000, "level_db": -18, "normalize": true, "channels": "stereo", "seam_ms": 20,
  "osc": {                 // ONE oscillator (was "oscillators": [0..4])
    "on": true, "wave": "sine", "table": "basic", "position": 0, "width": 0.5, "freq": 1000,
    "semitones": 0, "detune": 0, "phase": 0, "unison": 1, "unison_detune": 15, "unison_spread": 0.5,
    "random_phase": false, "level_db": 0, "pan": 0, "invert": false
  },                       // no sweep / vibrato / tremolo any more
  "noise":   { "on": false, "color": "pink", "level_db": 0, "stereo": "same" },
  "impulse": { "on": false, "every_ms": 1000, "level_db": 0, "polarity": "positive" },
  "dc": 0
}                          // no "gate", no "steps"
```
Ranges are unchanged from the old spec. Unknown keys are dropped as before (so an old `oscillators`,
`gate` or `steps` is ignored by the backend; the frontend migrates its saved patch, see Task 2).
Silence (nothing on) is allowed.

---

## Task 1 — Backend (Agent 1)

Files you own: `app/signal_gen.py`, `app/browser_record.py`, `app/web.py` (only the signal and
browser-record endpoints and their body models), `tools/test_signal_gen.py`, new `tools/test_signal_record.py`.

### signal_gen.py
- Patch schema above: `osc` is one object; remove the `oscillators` list, `gate`, `steps`, and per-osc
  `sweep` / `vibrato` / `tremolo` from `clean_patch`, `render`, the summary and every helper that only served
  them (delete dead code; keep the band-limited wavetable oscillator, unison, noise, impulse, normalize,
  channels, seam, dc, the voices × frames cap and `info.note`).
- The seam crossfade now always applies when `seam_ms > 0` (the "skip when a sweep / steps is on" rule goes).
- `summary`: `Sine 1 kHz`, `Saw 110 Hz ×7`, `Pink noise`, `Sine 1 kHz + pink noise`, `Impulses every 1 s`,
  `Silence`. The cache file name stays the sha1 of the clean patch (new schema = new hashes; fine).
- Update the module docstring.

### Record for the Signal tab (browser_record.py + web.py)
Generalize the browser recording so it can record the Signal tab's file through the plugin:
- `start(source: str = "browser")`: `source` is `"browser"` (today's behaviour, modes LIVE + BROWSER) or
  `"signal"` (mode `analysis_tap.FILE` only: the file playing through the open plugin; `after` = what comes
  out). Keep the chosen modes on the job and use them in `_run` (`analysis_tap.pick(job.modes)`,
  `if mode in job.modes`). Only the browser source calls `analysis_tap.set_recording(True/False)` (it asks
  the pages to send the browser's frames; a signal doesn't need that).
- The job's name: `Signal recording <YYYY-MM-DD HH.MM.SS>.wav` for `"signal"` (file name
  `signal-<YYYYmmdd-HHMMSS>.wav` in `CAPTURES`), unchanged for `"browser"`.
- `status()` gains `"source": "browser" | "signal" | None` (None when no job). `NOTHING` per source:
  signal → `"Nothing was recorded: play the signal through a plugin"`.
- `through` is the plugin's name for the signal source too (it always goes through one).
- A change of the file (the signal rendered again while recording) restarts the worker's ring (`gen`
  bump): the recording must go on across it (the cursor follows the new gen from frame 0; check
  `TapReader.read`'s behaviour on a gen change and handle it). A rate change still ends it, as today.
- web.py: `BrowserRecordBody` gains `source: str = "browser"` (400 unless `browser` or `signal`); pass it to
  `start`. Stop is unchanged (opens the file as the Audio tab's file). Update the docstrings.

### Tests
- `tools/test_signal_gen.py`: drop the gate / steps / sweep / multi-osc tests; keep and adapt the rest to
  `osc`; add: `clean_patch` drops `oscillators` / `gate` / `steps` / `osc.sweep`; seam always applied when
  > 0; `osc.on = false` with noise off and impulse off renders silence (peak `null` dB).
- `tools/test_signal_record.py` (pytest, temp `VEXT_ROOT` like the other tests): with a fake ring
  (`analysis_tap.InProcessTap` or whatever the module offers) written in FILE mode, `start("signal")`
  records it and ignores a ring in BROWSER mode; `start("browser")` ignores FILE mode; `status()["source"]`.
  If faking the rings is impractical, test `_run`'s mode filter through the smallest seam you can and say so.

---

## Task 2 — The designer and the scope (Agent 2)

Files you own: `app/static/js/playground-signal.js`, `app/static/css/playground-signal.css`,
`app/static/css/signal-scope.css` (layout only), `app/static/js/signal-scope.js` (only if needed for
sizing).

### Patch
- `base()` / `osc()` / `fill()` follow the new schema (`osc` object; no gate, steps, sweep, vibrato,
  tremolo). `loadPatch` migrates an old saved patch: `osc = oscillators[0]` if present, else
  `{...osc(), on: false}` when `oscillators` was an empty list; drop the rest.
- Presets that need removed features go: Log sweep, Linear sweep, SMPTE IMD, CCIF IMD, Beating, Tone burst,
  Level staircase. Noise presets, Impulse train and Silence set `osc.on = false` instead of an empty list.
  Remove the empty groups (Sweeps, Distortion, Timing → Impulse train moves to "Other").

### Layout (the Signal panel never scrolls as a whole)
```
┌ .pg-signal (row, fills the panel: flex 1, min-height 0, overflow hidden) ──────────────┐
│ ┌ left column (flex 1 1 auto, column, min-height 0) ──────┐ ┌ scope (≈38 %, min 280) ┐ │
│ │ top bar (wraps) + info line                             │ │ header row              │ │
│ │ source tabs:  [● Oscillator] [Noise] [Impulse]          │ │                         │ │
│ │ ┌ tab panel (flex 1, min-height 0, overflow-y auto) ──┐ │ │ canvas: fills ALL the   │ │
│ │ │ the selected source's controls                      │ │ │ remaining height        │ │
│ │ └─────────────────────────────────────────────────────┘ │ │                         │ │
│ └─────────────────────────────────────────────────────────┘ └─────────────────────────┘ │
└────────────────────────────────────────────────────────────────────────────────────────┘
```
- The scope column spans the panel's full height (top to bottom, beside the top bar too) and resizes with
  the window; its canvas takes all the height left under its header (no fixed height, the existing
  `min-height: 220px` is the floor). Check `signal-scope.css` `.ss` / canvas wrapper flex rules so the
  canvas really grows (`flex: 1 1 auto; min-height: 0`), and that its `ResizeObserver` redraws.
- Narrow (container < 900 px): one column: bar, tabs, tab panel (`flex: 1 1 auto; min-height: 120px`,
  scrolls), scope below (`flex: 1 1 45%; min-height: 220px`). Still no scroll on the panel itself.
- Remove `.pg-signal-wave` rules (Task 3 removes the element).
- Source tabs: a `role="tablist"` of three `role="tab"` buttons (`aria-controls`, roving `tabIndex`,
  ArrowLeft / ArrowRight move like `.pg-tablist`, no Space handling) styled like the app's segmented
  control or the `.pg-tab` look, whichever matches better. Each tab label has a small dot (`--accent` when
  that source is on, `--separator-strong` when off) so you see what sounds without opening the tab. The
  selected tab is kept in `localStorage pa-pg-signal-src` (`osc|noise|impulse`, default `osc`).
- Each tab panel: a header row with its title and its **On** switch, then its controls (the old card
  bodies; an Off source is dimmed 0.55 but editable). Oscillator: no "+ Oscillator", no ×, no "Osc 1"
  numbering (title "Oscillator"), no Modulation row; keep Wave, Table / Import / Position, Pulse width,
  Freq / Semi / Detune / Phase / Level / Pan, Invert, Random phase, Unison. Noise and Impulse: as before.
  Burst and Staircase cards are gone.
- Delete every constant, helper and CSS rule that only served removed parts (`MS`, `STEP_DB`, `LFO_*`,
  `LAWS`, `DB_FULL` if unused, `.pgs-osc-grid`, `.pgs-add`, `.pgs-x`, `.pgs-mod*`, `.pgs-fx-grid`, the
  empty hint…). Update the file's top comments.

### Behaviour changes
- `host.recording()` now also covers the new Record (Task 3 makes it true while either recording runs).
  **While the Signal Record runs, renders are allowed** (that is the point: change the signal while
  recording); only the Audio tab's own record (`host.fileRecording()`, new) still holds renders back with
  "Recording: changes apply when it stops". Use `host.fileRecording()` where the code used
  `host.recording()`.
- Everything else (Live, Render, the confirm before replacing a non-signal file, loop All after the first
  render) stays.

---

## Task 3 — The Playground (Agent 3)

Files you own: `app/static/js/playground.js`, `app/static/css/playground.css`.

1. **Tab order** Browser | Signal | Audio in the markup (`.pg-tablist`); arrow keys follow `els.srcBtns`
   order, so nothing else changes. Signal tab title: drop "sweeps, bursts": "Test signals (an oscillator,
   wavetables, noise, impulses): play them through the plugin and record what comes out".
2. **No waveform in the Signal tab**: remove `.pg-signal-wave` from the markup and `els.signalWave`; the
   `.pg-wave` never moves (it stays in the Audio panel); `applyTab` loses the move. `.pg-panel-signal`
   holds only `.pg-signal`.
3. **Record in the Signal tab** = the Browser tab's Record, generalized. One `brec` state with
   `brec.source = 'browser' | 'signal'` (the backend has one job at a time):
   - The deck's `.pg-brec` shows on the Browser tab and the Signal tab (not the Audio tab). Today
     `.pg-deck.is-file .pg-brec { display:none }` hides it for both file tabs: add an `is-signal` class on
     the deck (or use `.pg-web.is-tab-signal`) so it shows on Signal.
   - Signal blocker (button disabled with the reason as its title): no rendered signal loaded
     (`!signalHost.fileIsSignal()` → "Render a signal first."), no plugin picked / not open / an instrument
     (reuse `recordBlocker()`'s wording), the Audio tab's own record is armed.
   - Start (signal): `POST /api/playground/browser-record {on: true, source: 'signal'}`; then, if the file
     isn't playing through the plugin, start it (like `toggleRecord` does: `startPlayback` / `resumePlayback`).
     No "replaces the Audio tab's file" confirm is needed when the loaded file is the signal itself; keep the
     confirm when it would replace something else (it can't here, but keep the code path honest). No
     plugin hint dialog (a signal always records after the plugin: that is the point).
   - Poll, time, level meter: as the browser one. Title while on: "Stop recording: it becomes the Audio
     tab's file".
   - Stop: as `stopBrowserRecord` (opens the result as the Audio tab's file, switches to the Audio tab);
     the toast says "Recorded m:ss from the signal (after <plugin>) — it's the Audio tab's file now". Don't
     pause the browser page for a signal recording. Stop playback of the signal before opening the result
     (`beforeNewFile()` already does).
   - Leaving the tab that records ends the recording (Browser → elsewhere ends a browser one, as today;
     Signal → elsewhere ends a signal one). `brecResume` restores `brec.source` from the status's `source`.
   - `signalHost.recording()` = the file record **or** a signal Record running; new
     `signalHost.fileRecording()` = the file record only (`host && host.recording`).
   - `renderSignal`'s guard stays on the file record only; a signal re-render during a signal Record is
     allowed (the backend recording follows the restarted stream).
4. Update comments that mention the moved waveform / tab order (`applyTab`, `setSource`, the header).

---

## Coordinator (after the agents)
`CHANGELOG.md` (Signal tab: one oscillator, tabs, no Burst / Staircase / modulation, Record, new tab order),
`AGENTS.md` "What it does" 6 (Browser, Signal, Audio). Run every check; read the three reports.

## Done means
Every touched JS file passes `node --check`; `pytest tools/test_signal_gen.py` and
`tools/test_signal_record.py` (one file per process) pass; `ruff check` clean on touched Python. A short
report: what changed with file:line pointers, anything not done or uncertain.

---

# Round 2 (2026-10-22, owner)

## Owner's words
- Remove Table.
- Include a master volume in Signal to increase the amplitude.
- Alt + double-click resets a control to its default value.
- Whenever the app closes, all the values in Signal and the recorded audio are reset / cleared.

## Decisions (coordinator)
- **Table** = the oscillator's `wavetable` wave, its Table list, Import… and Position. Gone from the UI,
  the patch, the backend (`tables()`, the built-in and user tables, `GET /api/playground/signal/tables`,
  `POST /api/playground/signal/wavetable`, `cache/playground/wavetables/`). The band-limited mip tables that
  render sine / triangle / saw / square / pulse stay (that is how every wave is drawn, not a feature).
  A patch with `wave: "wavetable"` falls back to `sine`.
- **Master**: new patch field `master_db`, −24..+24 dB, default 0, step 0.5 typed / 1 on the knob. Applied
  after Normalize / Level (and after the channels mode and seam), before `dc` and the final clip to ±1, so
  turning it up makes the signal louder and, past 0 dBFS, clips (the info line already says "clipped").
  UI: a **Master** knob in the top bar, right after Level, `+6.0 dB` / `−3.0 dB` / `0.0 dB` text. Summary
  unchanged.
- **Alt + double-click** resets to the default:
  - `VEXT.AnalyzerKnob.create` gains an option `reset` (a number). Alt + double-click on a knob with
    `reset` sets it to that value and calls `onChange` (no text entry). Without `reset`, Alt + double-click
    does what a double-click does today. Plain double-click is unchanged. Update the file's top comment.
  - The Signal designer passes `reset` for every knob (the value from `base()` / `osc()` / the noise and
    impulse defaults). Alt + double-click on a Signal segmented control or select resets that field too
    (a toggle switch: not needed). Every knob's tooltip / aria description mentions "Alt + double-click:
    reset" once (put it in the knob's `title`).
  - Analyzer knobs (`js/analyzer.js`): pass `reset` with each knob's default if the defaults are at hand
    there (they are the Analyzer settings' defaults); if that is not simple, leave them and say so.
- **Reset when the app closes**:
  - Frontend: the Signal tab's values live in **sessionStorage** instead of localStorage (the patch
    `pa-pg-signal`, the source tab `pa-pg-signal-src`, Live `pa-pg-signal-live`, the scope `pa-pg-scope`);
    the "Don't ask again" answer `pa-pg-signal-rec-ok` stays in localStorage (a preference, not a value).
    On create, remove the old localStorage copies of the moved keys once. sessionStorage lives as long as
    the window: a reload keeps the values, closing the app drops them.
  - Backend: `playground_audio.reset_session()` clears the Audio tab completely: the loaded file and its
    edits (the state goes back to empty, `state.json` deleted) and every file VEXT made under
    `cache/playground/` (decoded copies `source-*`, recordings `recording-*`, `heard-*`, `upload-*` folders,
    `captures/`, `signals/`, the old `wavetables/`). Never a file outside that folder (the user's original
    audio file is not touched). Called from a `startup` event in `app/web.py` **before** anything restores
    the Audio tab (so even after a crash or a kill the next launch starts clean), and best-effort from the
    `shutdown` event. Make sure `_restore()` can't bring the old file back after the reset.

## Round 2 tasks
- **Agent 1 (backend)**: `app/signal_gen.py`, `app/playground_audio.py`, `app/web.py` (the signal
  endpoints and the startup / shutdown events only), `tools/test_signal_gen.py`, new
  `tools/test_playground_reset.py`. Tests: `wavetable` falls back to sine; `master_db` +6 on a −18 dBFS
  sine gives −12 ±0.1; +24 on a 0 dBFS sine clips and sets `clipped`; `clean_patch` clamps `master_db`;
  `reset_session()` deletes the cache files and the state, and leaves a file outside the cache alone.
- **Agent 2 (frontend)**: `app/static/js/playground-signal.js`, `app/static/css/playground-signal.css`,
  `app/static/js/signal-scope.js`, `app/static/js/analyzer-knob.js`, `app/static/js/analyzer.js` (only
  the `reset` values).
