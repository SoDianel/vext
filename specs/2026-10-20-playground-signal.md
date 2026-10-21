# Playground › Signal tab: test signals through the plugin (2026-10-20, owner)

Repo: the VEXT repository root. Backend: Python (FastAPI, numpy) in `app/`. Frontend: vanilla
JS/CSS in `app/static/` (no build step, IIFE modules hanging off `window.VEXT`). Read the top comment of
each file you touch and match its style: short plain-English comments that say *why*, two-space indent in
JS, single quotes, `const` / arrow functions; Python with type hints and docstrings like
`app/playground_audio.py`.

## Owner's words

"The signal should be processed by the plugins and recorded, so the best way to do this is to move the
Signal Test section to the Playground module with a bunch of features like many oscillators, wavetables,
detune, phase and more technical tools to create signals and test plugins. The play section in the
Analyzer keeps its functionality."

Owner's choices: **oscillators + one scope**; **rendered, plays as a file** (the backend renders the
designed signal to a WAV and the Playground plays it as its file: plugin, delay compensation, loop,
Record, Capture, the Analyzer's Play / timeline all work unchanged; a change re-renders and resumes at the
same place with a short gap); built by Haiku agents.

## Hard rules for every agent

- **Never commit, never create branches, never run git write commands.** Leave changes uncommitted.
- Only edit the files your task owns. If another file must change, say so in your report.
- Colours: never add a hex colour. Palette tokens from `app/static/style.css` (`--accent`, `--accent-fg`,
  `--accent-soft`, `--accent-text`, `--fg`, `--fg-2`, `--fg-3`, `--bg-content`, `--bg-subtle`,
  `--bg-control`, `--bg-popover`, `--separator`, `--separator-strong`, `--shadow-1/2`, `--radius-s/m`) or
  `color-mix()` of them. Dark-theme rules (when needed) use both
  `@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) X {…} }` and
  `:root[data-theme="dark"] X {…}`.
- JS: `node --check` every file you touch. Python: `.venv-dev\Scripts\python.exe -m pytest <tests>` and
  `ruff check <files>` (ruff is installed as a uv tool: `ruff check app/signal_gen.py`).
- Space plays / pauses the Playground (its own key handler): don't swallow Space in widgets. Knobs are
  `div role="slider"` (`VEXT.AnalyzerKnob`), not inputs, so Space still reaches the Playground.

## Already pre-wired by the coordinator (don't redo)

- Analyzer: the in-page test-signal strip is **removed** (`analyzer-generator.js/.css` deleted, hooks gone).
  The Analyzer's Play / Pause and timeline act on the Playground's file, which can now be a signal.
- `app/static/index.html` loads `css/playground-signal.css`, `css/signal-scope.css`,
  `js/signal-scope.js`, `js/playground-signal.js` (after `js/analyzer-knob.js`).
- `app/static/js/playground.js`:
  - Tabs **Browser | Audio | Signal** (`.pg-tab[data-src="signal"]`). The Signal tab plays the file too
    (`source === 'file'`, `fileView === 'signal'`); `localStorage pa-pg-tab` = `browser|file|signal`.
  - Panel markup: `.pg-panel.pg-panel-signal` > `.pg-signal` (the designer goes here) + `.pg-signal-wave`
    (the Playground moves its single waveform view `.pg-wave` — the file and its recording lane with
    Record — into it while the Signal tab shows, and back into the Audio panel otherwise).
    `.pg-web` gets `is-tab-signal`. The deck below (Play, Stop, Loop, Loop All, times) is shared.
  - On first show it calls `V.PlaygroundSignal.create(els.signal, host)` and then `signal.show(bool)`
    every time the tab is shown / hidden. `host`:
    ```js
    host.render(patch)    // Promise<{audio, preview, info}>: POST /api/playground/signal, opens the WAV as the
                          // Playground's file; if it was playing it plays again from the same time once decoded.
                          // Throws Error('Stop recording before changing the signal.') while Record runs.
    host.fileIsSignal()   // the loaded file is a rendered signal (…\signals\signal-*.wav)
    host.fileName()       // the loaded file's display name, or null
    host.hasRecording()   // the file has a recording (it is cleared by a new file)
    host.recording()      // Record is running now
    host.playing()        // the file plays now
    host.setLoop('off'|'sel'|'all'), host.loopMode()
    ```

---

## Task 1 — Backend renderer (Agent 1)

Files you own: `app/signal_gen.py` (new), `app/web.py` (only the new endpoints + body models, placed right
after the `/api/playground/audio/...` endpoints), `tools/test_signal_gen.py` (new, pytest).

### Endpoints
- `POST /api/playground/signal` body `{"patch": {...}}` → `signal_gen.clean_patch(patch)` (clamps, fills
  defaults, never raises for out-of-range numbers; raises `ValueError` only for a patch that isn't an object)
  → `signal_gen.render(clean)` → write a float32 stereo WAV to
  `PROJECT_ROOT/cache/playground/signals/signal-<sha1 of the clean patch json, 12 hex>.wav` (reuse the file if
  it exists; keep at most the 6 newest files in that folder, delete older ones — never the one being
  opened) → `_stop_audio_tab_playback()` → `playground_audio.open_file(path, "Signal – <summary>.wav")`.
  Answer `{"audio": <that state>, "preview": {"rate": r, "l": [...], "r": [...]}, "info": {...}}`.
  Errors: `ValueError` → 409 like `_audio_call`.
  - `preview`: 4096 frames of the rendered signal starting at 0.5 s (or at 0 when shorter), floats rounded to
    5 decimals.
  - `info`: `{seconds, rate, peak_db, rms_db, crest_db, clipped (bool), summary}`; dB values rounded to 0.1,
    `-inf` → `null`. `summary` like `Sine 1 kHz`, `Saw 110 Hz ×7`, `Pink noise`, `Sine sweep 20 Hz–20 kHz`,
    `2 oscillators + pink noise`, `1 kHz burst`.
- `GET /api/playground/signal/tables` → `{"tables": [{"name", "label", "frames", "user": bool}]}`.
- `POST /api/playground/signal/wavetable` body `{"path": "<a .wav file>"}` → imports a user wavetable:
  read with the `wave` module or numpy (16/24/32-bit int and 32-bit float PCM; first channel), cut into
  2048-sample frames (at most 256; a file shorter than 2048 samples is one frame resampled to 2048 by linear
  interpolation), save `cache/playground/wavetables/<safe name>.npy`, answer the table list. 409 on a bad file.

### WAV writing
Use `host_worker.float_wav_header(channels, rate, frames)` if importing `app.host_worker` is cheap; else
write the 44-byte IEEE-float header yourself (format 3). Little-endian float32 interleaved.

### The patch (all fields optional; `clean_patch` fills the defaults shown)
```jsonc
{
  "seconds": 10,          // 0.5..60 — length of the file
  "rate": 48000,          // 44100 | 48000 | 88200 | 96000
  "level_db": -18,        // -80..0: the mix's peak after Normalize (or the master gain when normalize=false)
  "normalize": true,      // scale the mix (before gate / steps) so its peak = level_db
  "channels": "stereo",   // stereo (pans apply) | mono (L = R = (L+R)/2) | left | right | invert (mono, R = -L)
  "seam_ms": 20,          // 0..200: crossfade the end into the start so the file loops without a click
  "oscillators": [        // 0..4 items
    {
      "on": true,
      "wave": "sine",     // sine | triangle | saw | square | pulse | wavetable
      "table": "basic",   // wavetable name (wave = "wavetable")
      "position": 0,      // 0..1: where in the table (frames morph linearly)
      "width": 0.5,       // 0.01..0.99: pulse width (wave = "pulse")
      "freq": 1000,       // 1..24000 Hz (the start frequency when sweeping)
      "semitones": 0,     // -48..48
      "detune": 0,        // -100..100 cents
      "phase": 0,         // 0..360 degrees: start phase
      "unison": 1,        // 1..8 voices
      "unison_detune": 15,// 0..100 cents: the outer voices are ± half of this, the rest spread evenly
      "unison_spread": 0.5, // 0..1: stereo spread of the voices (0 = all centre)
      "random_phase": false, // unison voices start at random phases (seeded: same patch = same file)
      "level_db": 0,      // -60..0 relative level
      "pan": 0,           // -1..1 (equal-power)
      "invert": false,    // polarity
      "sweep": { "on": false, "to": 20000, "law": "log" },   // glide from freq to `to` over the whole file: log | linear
      "vibrato": { "rate": 0, "depth": 0 },  // Hz 0..20, cents 0..1200 (sine LFO on pitch)
      "tremolo": { "rate": 0, "depth": 0 }   // Hz 0..20, 0..1 (sine LFO on level)
    }
  ],
  "noise":   { "on": false, "color": "pink", "level_db": 0, "stereo": "same" },   // white|pink|brown|blue|violet ; same|independent
  "impulse": { "on": false, "every_ms": 1000, "level_db": 0, "polarity": "positive" }, // 10..5000 ms; positive|negative|alternate
  "gate":    { "on": false, "on_ms": 200, "off_ms": 800, "attack_ms": 1, "release_ms": 1 },  // tone bursts (1..5000 ms each), oscillators + noise only
  "steps":   { "on": false, "from_db": -60, "to_db": 0, "step_db": 6, "step_ms": 500 },     // level staircase relative to level_db, then repeats; whole mix
  "dc": 0                 // -0.5..0.5 added at the very end
}
```
Defaults when `oscillators` is missing: one Sine 1000 Hz, everything else off. An empty list is allowed
(noise / impulse only). Unknown keys are dropped. Seed the random generator from the clean patch's hash, so
the same patch gives the same file.

### How to render (numpy, vectorized — no per-sample Python loops)
- **Band-limited wavetable oscillator for every waveform.** One cycle = `N = 2048` samples. Build a
  waveform's frames as naive single cycles (sine; triangle; saw `2*x-1`; square; pulse with width;
  wavetable frames), `rfft` each frame, and make **mip levels**: level k keeps harmonics `1..H_k` where
  `H_k = 1024 >> k` (k = 0..10), `irfft` back to N samples. Cache per (waveform, width, table) in memory.
- Per voice: instantaneous frequency array `f[n]` (Hz) = base × 2^((semitones + (detune + voice offset +
  vibrato)/100)/12), with the sweep (log: `f0 * (f1/f0) ** (t/T)`; linear: `f0 + (f1-f0) * t/T`).
  Phase `= phase0 + cumsum(f / rate)` (mod 1). Mip level per sample from `f`: the smallest k with
  `H_k * f <= 0.45 * rate` (use `np.clip(np.ceil(np.log2(1024*f/(0.45*rate))), 0, 10)`). Read
  `table[level, idx]` with linear interpolation between `idx` and `idx+1` (wrap). Wavetable `position`
  interpolates between the two nearest frames (build the two frames' mip stacks and blend).
- Unison voice i of n: offset cents = `(i/(n-1) - 0.5) * unison_detune` (0 when n = 1); pan offset
  `(i/(n-1) - 0.5) * 2 * unison_spread`, added to the oscillator's pan and clamped; the voices are summed
  and divided by `sqrt(n)`.
- Noise (length × channels, independent per channel when `stereo = independent`): white = uniform
  `[-1, 1)`; pink = Paul Kellet's refined filter (b0..b6) applied with `scipy`-free numpy — use the
  **FFT method** for all colours instead: white noise → `rfft` → multiply by `f^(alpha/2)` (pink α = −1,
  brown α = −2, blue α = +1, violet α = +2, DC bin 0) → `irfft`; normalize to peak 1.
- Impulse: single samples of ±1 every `every_ms` (alternate flips sign each time), no band-limiting.
- Mix: sum of oscillators (each × its level and pan, equal-power: `L = cos((pan+1)π/4)`, `R = sin(...)`
  times √2 so centre is unity) + noise + impulse. Normalize: if `normalize`, scale so the peak of |mix| =
  `10^(level_db/20)` (silence stays silence); else multiply by `10^(level_db/20)`.
- Gate: an envelope of `on_ms` open / `off_ms` closed with linear attack / release ramps, applied to
  oscillators + noise (before the impulse is added). Steps: gain `10^(db/20)` where db goes from `from_db`
  towards `to_db` by `step_db` every `step_ms` (sign follows the direction), holds the last step until the
  file ends... then restarts at the next file loop (the file loops in the Playground).
- Channels mode, then seam crossfade: render `seconds + seam` long and equal-power crossfade the last
  `seam_ms` into the first `seam_ms`, so the result is exactly `seconds` long and loops cleanly (skip when
  seam = 0 or a sweep / steps is on — a sweep restarting is intended). Then `dc`, then clip to [-1, 1] and
  set `clipped`.
- Performance: 10 s at 48 kHz with 4 oscillators × 8 unison voices must render in < 2 s on a normal PC.
  Cap total work: if `voices × frames > 40e6`, reduce unison proportionally and say so in `info.note`.

### Built-in wavetables (`tables()`; each 64 frames of 2048 samples, generated, not files)
- `basic` — Sine → Triangle → Saw → Square (morph between the 4 key shapes).
- `harmonics` — frame k = sum of harmonics 1..(k+1) with 1/n amplitudes (organ-like build-up).
- `pwm` — pulse width 50 % → 2 %.
- `formant` — five vowel-like formant shapes (A E I O U) morphing (additive with Gaussian formant peaks).
- `sync` — hard-sync sweep: a saw whose slave ratio goes 1 → 8 inside one cycle.
- `fm` — 2-operator FM, modulation index 0 → 10 (ratio 1:1).
- `bitcrush` — a sine quantized from 16 levels down to 2.
Plus user tables in `cache/playground/wavetables/*.npy` (`user: true`).

### Tests (`tools/test_signal_gen.py`, pytest)
- `clean_patch` clamps and fills defaults; unknown keys dropped; a non-object raises `ValueError`.
- A 1 kHz sine at 48 kHz: peak = level_db ±0.1 dB, its FFT peak bin within 1 Hz of 1000 Hz.
- Saw at 5 kHz: no energy above 0.46 × rate beyond -60 dB relative to the fundamental (band-limited).
- Unison 7 with spread: L ≠ R; spread 0: L == R.
- Invert channels: R == -L. Left: R == 0.
- Pink noise: the slope between 100 Hz and 10 kHz octave bands is −3 dB/octave ± 1.5.
- Impulse every 100 ms over 1 s: 10 non-zero samples.
- Gate 200/800: the RMS of the closed parts is < −80 dB re the open parts.
- Same patch → identical bytes; seam: the jump between the last and first sample is no bigger than the
  largest sample-to-sample step inside the file × 1.5.
- Endpoint via FastAPI's `TestClient` with `PROJECT_ROOT`'s cache redirected to a temp dir if possible (else
  just call `signal_gen` functions): POST returns `audio`, `preview` (4096 each), `info`.

---

## Task 2 — The Signal designer UI (Agent 2)

Files you own: `app/static/js/playground-signal.js` (new), `app/static/css/playground-signal.css` (new).

`VEXT.PlaygroundSignal.create(box, host)` → `{ show(bool), destroy() }` (box = `.pg-signal`).

### Layout (`.pg-signal`, a column that scrolls inside itself; the panel below it keeps the waveform)
CSS you write also lays out the panel: `.pg-panel-signal` is a flex column; `.pg-signal` takes
`flex: 1 1 auto; min-height: 0; overflow: auto`; `.pg-signal-wave` is `flex: 0 0 auto; height: 38%;
min-height: 170px; display: flex` (the moved `.pg-wave` inside fills it; it already has `flex: 1 1 auto`).

1. **Top bar** (one wrapping row, like the Audio tab's `.pg-chrome`): **Presets ▾** (a menu button) ·
   Length (select: 1, 2, 5, 10, 20, 30, 60 s) · Rate (44.1k, 48k, 88.2k, 96k) · Level knob (dBFS, steps
   −60…0 every 1 dB, typed −80…0) · Normalize (switch, on) · Channels (segmented: Stereo · Mono · L · R ·
   Inverted) · Seam (select: Off, 10, 20, 50, 100 ms) · spacer · **Live** (switch, on: every change renders
   ~300 ms after the last one) · **Render** button (primary when there are changes not rendered yet; also
   the only way when Live is off) · an info line from the last render: `Peak −18.0 dBFS · RMS −21.0 · Crest
   3.0 dB · 10 s · 48 kHz` (+ "clipped" in `--danger` when it clipped, + `info.note`).
2. **Body** = two columns (stack under 900 px): left the **sources**, right the **scope** (≈ 38 % width,
   min 280 px).
   - **Oscillators** (1–4 cards in a responsive grid; "+ Oscillator" button, each card has × remove,
     an On switch and a name "Osc 1"):
     Wave (segmented: Sine · Tri · Saw · Square · Pulse · Table) · Table (select from
     `GET /api/playground/signal/tables`, + "Import…" which asks for a path with the Tauri dialog
     `window.__TAURI__.dialog.open({filters:[{name:'WAV',extensions:['wav']}]})` or a prompt in a plain
     browser, then `POST /api/playground/signal/wavetable`) and a Position knob (0–100 %, step 1) — only
     for Table · Width knob (1–99 %, step 1) — only for Pulse · knobs: **Freq** (`STEPS.tone` + typed
     1–24000; also accepts note names like `A4`, `C#3` → Hz, A4 = 440), **Semi** (−48…48, step 1),
     **Detune** (−100…100 cents, step 1), **Phase** (0…360°, step 15), **Level** (−60…0 dB, step 1),
     **Pan** (−100…100, step 5, shown L50 / C / R50) · Invert (toggle) · Unison row: **Voices** (1–8),
     **Spread det.** (cents: 0,1,2,3,5,7,10,15,20,25,30,40,50,75,100), **Width** (0–100 %, step 5), Random
     phase (toggle) · a collapsible **Modulation** row: Sweep (toggle, To knob = `STEPS.tone`, Law: Log ·
     Linear), Vibrato rate (Hz: 0, .1, .2, .5, 1, 2, 3, 4, 5, 6, 8, 10, 15, 20) and depth (cents: 0, 5, 10,
     20, 50, 100, 200, 1200), Tremolo rate (same Hz list) and depth (0–100 %, step 5).
   - **Noise** card: On, Color (White · Pink · Brown · Blue · Violet), Level knob, Stereo (Same · Independent).
   - **Impulse** card: On, Every (ms knob: 10, 20, 50, 100, 200, 250, 500, 1000, 2000, 5000), Level,
     Polarity (+ · − · ±).
   - **Burst** card (gate): On, On / Off / Attack / Release knobs (ms: 1, 2, 5, 10, 20, 50, 100, 200, 500,
     1000, 2000, 5000).
   - **Staircase** card (steps): On, From / To (dB −80…0 step 1), Step (dB: 1, 2, 3, 6, 10), Every (ms list
     as Burst).
   - Cards use `--bg-content`, `inset 0 0 0 1px var(--separator)`, `--radius-m`; a card that is Off is
     dimmed (0.55) but stays editable. Section labels like `.an-label` (11px, `--fg-3`).
3. **Scope** (right column): `VEXT.SignalScope.create(container)` (Task 3). Call `setPreview(r.preview)`
   after every render; `show(bool)` follows the tab.

Knobs: `VEXT.AnalyzerKnob.create({label, ariaLabel, values, value, min, max, format, onChange})` →
`{el, set, setDisabled, destroy}` (see `js/analyzer-knob.js`; `STEPS.tone`, `STEPS.level`, `fmtFreq`,
`fmtDb`, `parseText`). For Freq pass a `parse` that understands note names first, then `parseText`.

### Behaviour
- The patch (Task 1's schema) lives in `localStorage pa-pg-signal`; restored on create.
- **Render** = `host.render(patch)`. Before the **first** render from this tab, when the loaded file is
  **not** a signal (`!host.fileIsSignal() && host.fileName()`), confirm with `await V.confirmDialog(message)` (resolves true / false; the app's own modal): "Replace the Audio tab's file “<name>” (and its recording) with the signal?". A signal
  file replacing a signal file never asks, but if `host.hasRecording()` show once per session: "Changing
  the signal clears the recording of the last one." with Don't ask again (`localStorage
  pa-pg-signal-rec-ok`). While `host.recording()`: don't render; show "Recording: changes apply when it
  stops" and render once it stops (poll 300 ms while pending).
- After the first successful render, if `host.loopMode() === 'off'` call `host.setLoop('all')` (a test
  signal is meant to repeat).
- Live on: debounce 300 ms after the last change (knob drags fire many changes). One request at a time:
  changes during a render queue one more render after it. Errors → `V.toast(msg, true)`.
- **Presets** (menu; picking one replaces the patch and renders): Sine 1 kHz −18 dBFS · Sine 997 Hz −1
  dBFS (THD) · Sine 100 Hz · Square 100 Hz · Saw 110 Hz · Supersaw (Saw 110 Hz, 7 voices, 25 cents, width
  80 %) · Wavetable sweep (Table basic, position 0.5) · Log sweep 20 Hz–20 kHz (10 s) · Linear sweep 20
  Hz–20 kHz (10 s) · Pink noise · White noise · Brown noise · SMPTE IMD (60 Hz + 7 kHz at −12 dB, 4:1) ·
  CCIF IMD (19 kHz + 20 kHz, equal) · Tone burst 1 kHz (200 ms on / 800 ms off) · Level staircase 1 kHz
  (−60 → 0 dB, 6 dB / 500 ms) · Impulse train (1 per second) · Stereo phase test (Sine 1 kHz, Inverted) ·
  Beating (440 Hz + 441 Hz) · Silence (noise-floor test: no oscillators, nothing on).
- `show(false)` stops timers and hides the scope's live drawing; `destroy()` frees knobs and listeners.

---

## Task 3 — The scope (Agent 3)

Files you own: `app/static/js/signal-scope.js` (new), `app/static/css/signal-scope.css` (new).

`VEXT.SignalScope.create(container)` → `{ setPreview({rate, l, r}), show(bool), destroy() }`.

- Markup inside `container`: a header row (title "Scope", segmented **Signal · Live**, segmented
  **L/R · X‑Y**, a Time select: 1, 2, 5, 10, 20, 50, 100 ms across, **Freeze** toggle) and a canvas that
  fills the rest (min-height 220 px, `--bg-content`, `inset 0 0 0 1px var(--separator)`, `--radius-m`).
- **Signal** mode draws the last `setPreview` data. **Live** mode subscribes to `VEXT.audioBus`
  (`subscribe(fn)` / `unsubscribe(fn)`; `fn({rate, before, after, n, quiet})`, interleaved stereo Float32Arrays
  valid only during the call — copy what you keep into your own ring of 16384 frames per signal) **only while
  shown, Live is selected and not frozen**, and draws **Before and After** (the plugin's input and output)
  overlaid: Before in `--fg-3`, After in `--accent`. In Signal mode L in `--accent`, R in `--fg-2`.
- L/R: time on x across the Time span, triggered on a rising zero crossing of the first channel /
  signal (search within the newest data; free-run when none is found). Amplitude ±1 full scale with a
  centre line and ±0.5 grid lines, labels "+1", "0", "−1" in `--fg-3` 10.5px; a readout top-right: peak
  (dBFS) of what is shown. X‑Y: Lissajous (x = L, y = R in Signal mode; x = Before (mid), y = After (mid) in
  Live), square plot centred, with a faint persistence fade.
- Draw with `requestAnimationFrame` only while shown (Live: ~30 fps; Signal: redraw on change / resize).
  `ResizeObserver` on the canvas parent; canvas sized in device pixels (`devicePixelRatio`).
- Colours read from CSS: use probe elements (like `analyzer.js readTheme`) or `getComputedStyle` of
  elements with `color: var(--accent)` etc., re-read on theme change (`MutationObserver` on
  `document.documentElement` `data-theme` and the `prefers-color-scheme` media query).
- Persist mode / xy / time in `localStorage pa-pg-scope`.

## Done means
- Every touched JS file passes `node --check`; Python passes the new tests and `ruff check`.
- A short report: what changed with file:line pointers, anything not done or uncertain.
