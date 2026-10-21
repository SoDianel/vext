# Spec: Plugin buffer (render-ahead) — gapless sound through heavy plugins

Owner's report (2026-10-19):
1. Heavy plugins (iZotope RX…) make the Audio tab's playback chop / artifact; recording is affected too.
2. A plugin on the Browser tab's sound, then going to the Analyzer: the sound is heavily cut, "in steps".

Owner's direction: solve it with a latency feature, so the audio is always gapless.

## Diagnosis

### 1. File playback (Audio tab) — `app/host_worker.py:Player._run_file`
One thread does everything per block, in series: decode (PyAV) → envelope / mix → **plugin** → recorder →
tap → `out.write()` (blocking). The device only has `stream_latency()` queued ahead of it: **1 block**
(10.7 ms at 512 / 48 kHz; 3 blocks in Safe Mode). Any block whose processing takes longer than the time
queued ahead underruns the device = an audible gap.

Heavy spectral plugins (iZotope RX De-noise / De-reverb / Spectral…) are *bursty*: they run an FFT every
N host blocks, so most `process()` calls are cheap and every few calls one costs 20–100 ms. Their **average**
is below real time, their **peaks** are far above one block. A bigger device buffer only partly helps (and
ASIO ignores the asked latency entirely). What DAWs do: run the plugin **ahead** of the device into a FIFO
(Reaper "anticipative FX", Cubase "ASIO-Guard", Bitwig/Studio One "dual buffer"), so the peaks are
absorbed by the FIFO and only the average has to be real time.

The recording (`_Recorder`) is written from the plugin's output at file time, so the *file* is normally
gapless; what's heard while recording (and playing the recording back along into the plugin, `mix`) is
what chops. The fix covers both; a test proves the recorded file stays bit-exact with a spiky plugin.

### 2. Browser through a plugin, then the Analyzer — two causes stacked
- **The browser page gets starved when hidden.** Leaving the Playground hides the browser webview
  (`browser.rs:browser_place` → `wv.hide()` = WebView2 `IsVisible=false`). Chromium then backgrounds that
  renderer (lower process priority, throttling). The page's capture runs on a **ScriptProcessorNode**
  (`browser_capture.js`, main thread, 1024 frames = 21 ms). A starved main thread misses ScriptProcessor
  callbacks and those blocks are **lost**; the rest arrive in bursts. The Analyzer section also starts
  drawing at 60 fps in the main webview at that moment, competing for CPU with the plugin.
- **The worker's live jitter buffer is small and rigid** (`_LiveBuffer`: ~64 ms target, re-primes on every
  underrun = silence until 64 ms are back, drops audio beyond target + 3 blocks). Bursty arrival = underrun
  → silence → refill → play → underrun… = "in steps". The backend relay (`browser_audio.py`) also drops
  whole frames once 96 KB (~0.25 s) is pending to the worker.
- On top: the heavy plugin's peaks hit the live path exactly like the file path (same serial loop).

## The solution: a **Plugin buffer** (render-ahead), plus a browser path that doesn't starve

### A. Worker engine — render thread + output thread with a FIFO between them (`app/host_worker.py`)

File and browser playback are split into two threads:
- **render thread**: source → plugin → `_RenderQueue` (runs ahead of the device by up to *target* frames);
- **output thread** (the existing `_run_file` / `_run_live` thread, which opens and owns the stream):
  takes chunks from the queue as the device needs them, writes the analysis tap, test tone, test gain,
  peak, `out.write()`, the heard position and the periodic events.

Instruments (`_run_instrument`) are **unchanged** (MIDI played live needs the lowest latency; out of scope).

#### The setting
`audio_plugin_buffer` = `"auto"` (default) | `"25"` | `"50"` | `"100"` | `"200"` | `"400"` | `"800"` (ms).
Sent to the worker in `<AUDIO>` as `"buffer"`. Changing it restarts what plays (like the other audio keys).

```python
PLUGIN_BUFFERS = ("auto", "25", "50", "100", "200", "400", "800")  # audio_plugin_buffer choices (ms)
AUTO_START_S = 0.06      # Auto's first target
AUTO_MAX_S = 1.0         # Auto never grows past this
BROWSER_CHUNK = 2048     # frames per message from browser_capture.js (its ScriptProcessor size): keep in step
LIVE_IDLE_S = 0.5        # no browser input for this long: silence is fed through the plugin (tails ring out)


def render_target(setting, rate: int, block: int, live: bool, learned_s: float = 0.0) -> tuple[int, bool]:
    """(target frames, adaptive) for the Plugin buffer setting. Never below two blocks; the browser
    path never below two of its chunks (its messages arrive one chunk at a time)."""
    floor = max(2 * block, 2 * BROWSER_CHUNK) if live else 2 * block
    if str(setting or "auto") == "auto":
        return max(floor, int(max(AUTO_START_S, learned_s) * rate)), True
    try:
        ms = int(setting)
    except (TypeError, ValueError):
        ms = int(AUTO_START_S * 1000)
    return max(floor, int(ms * rate / 1000)), False
```

#### `_RenderQueue` (new class, next to `_LiveBuffer`)

Reference implementation (use as is; adjust only if a test shows a bug):

```python
class _Chunk:
    """One rendered block: `sound` (2, n) float32 = what goes to the device (after IN, plugin, OUT,
    Auto-Balance); `before` = what went into the plugin (the analysis tap's Before), `at` = the file time
    of its first frame (None: not from the file), `device` = `sound` at the device's rate (the browser
    path resamples when the device refused the browser's rate), `end` = the source finished (no audio)."""

    __slots__ = ("sound", "before", "device", "at", "n", "bypass", "end")

    def __init__(self, sound=None, before=None, at=None, bypass=False, end=False, device=None):
        self.sound, self.before, self.at, self.bypass, self.end = sound, before, at, bypass, end
        self.device = sound if device is None else device
        self.n = 0 if sound is None else int(sound.shape[1])


class _RenderQueue:
    """The Plugin buffer: processed audio waiting for the device. The render thread runs the plugin
    ahead of the device by up to `target` frames, so a block that takes longer than real time (iZotope
    RX's FFT frames, a busy moment) is absorbed instead of heard as a gap. The output thread takes
    chunks as the device needs them. Output starts (and restarts after running dry) only once `target`
    frames are queued, or the source's end is. Running dry while playing is a *starve*: Auto then grows
    `target` (x1.5 + a block, up to AUTO_MAX_S). `flush` drops what wasn't heard (seek, pause) and bumps
    `epoch`: a chunk rendered for an older epoch is refused by `put`."""

    def __init__(self, rate: int, block: int, target: int, adaptive: bool):
        self.rate, self.block = rate, block
        self.target, self.adaptive = target, adaptive
        self.max = max(target, int(AUTO_MAX_S * rate))
        self._cond = threading.Condition()
        self._items: collections.deque = collections.deque()
        self.frames = 0
        self.epoch = 0
        self.seek_to: float | None = None   # set by flush(seek_to=): the render thread seeks there first
        self.primed = False
        self.ended = False
        self.closed = False
        self.starves = 0
        # Render statistics since the last take_stats(): seconds spent rendering, seconds of audio rendered,
        # the slowest chunk.
        self._busy = self._audio = self._worst = 0.0

    def wait_room(self, timeout: float) -> bool:
        """Render thread: True once there's room for another chunk (or the queue closed)."""
        with self._cond:
            if self.frames < self.target or self.closed:
                return True
            self._cond.wait(timeout)
            return self.frames < self.target or self.closed

    def take_seek(self) -> tuple[int, float | None]:
        """Render thread, before each chunk: (the epoch to render for, a seek to apply first or None)."""
        with self._cond:
            seek, self.seek_to = self.seek_to, None
            return self.epoch, seek

    def put(self, chunk: _Chunk, epoch: int) -> bool:
        with self._cond:
            if epoch != self.epoch or self.closed:
                return False  # flushed meanwhile: never heard
            self._items.append(chunk)
            self.frames += chunk.n
            self.ended = self.ended or chunk.end
            self._cond.notify_all()
            return True

    def get(self) -> _Chunk | None:
        """Output thread (never blocks): the next chunk, or None = play silence (priming or starved)."""
        with self._cond:
            if not self.primed:
                if self.frames >= self.target or (self.ended and self._items):
                    self.primed = True
                else:
                    return None
            if not self._items:
                self.primed = False
                self.starves += 1
                if self.adaptive:
                    self.target = min(self.max, int(self.target * 1.5) + self.block)
                    self._cond.notify_all()
                return None
            chunk = self._items.popleft()
            self.frames -= chunk.n
            self._cond.notify_all()
            return chunk

    def flush(self, seek_to: float | None = None) -> None:
        with self._cond:
            self._items.clear()
            self.frames, self.primed, self.ended = 0, False, False
            self.epoch += 1
            self.seek_to = seek_to
            self._cond.notify_all()

    def wait_flush(self, epoch: int, timeout: float) -> None:
        """Render thread, after queuing the end: waits for a flush (a seek / loop back) or the close."""
        with self._cond:
            if self.epoch == epoch and not self.closed:
                self._cond.wait(timeout)

    def grow(self, frames: int) -> None:
        """Auto: a chunk took long enough that the buffer may not cover the next one: grow before it starves."""
        with self._cond:
            if self.adaptive and frames > self.target:
                self.target = min(self.max, frames)
                self._cond.notify_all()

    def note_render(self, seconds: float, frames: int) -> None:
        with self._cond:
            self._busy += seconds
            self._audio += frames / self.rate
            self._worst = max(self._worst, seconds)

    def take_stats(self) -> dict:
        """For the periodic events: the buffer, its fill, starves, and the plugin's load since the last call
        (load_pct = render time / audio time: above 100 the plugin can't keep up at all, no buffer helps)."""
        with self._cond:
            load = 100.0 * self._busy / self._audio if self._audio else 0.0
            worst = self._worst
            self._busy = self._audio = self._worst = 0.0
            return {"ahead_ms": round(self.frames * 1000 / self.rate, 1),
                    "target_ms": round(self.target * 1000 / self.rate, 1),
                    "starves": self.starves, "load_pct": round(load, 1), "render_peak_ms": round(worst * 1000, 1)}

    def close(self) -> None:
        with self._cond:
            self.closed = True
            self._cond.notify_all()
```

#### File playback (`_run_file`) — split

`_run_file` (the output thread) keeps: COM init, `_open_stream`, creating `decoder` / `along` (so a decode
failure is reported exactly as now), the `playing` event, MIDI listen on/off, the tap start, the reports,
and the final `stopped` event / `record_off()`. New:

1. After the stream is open: `target, adaptive = render_target(audio.get("buffer"), rate, block, False, self._learned_s)`;
   `queue = _RenderQueue(rate, block, target, adaptive)`. Add `"buffer": str(audio.get("buffer") or "auto")`
   and `"target_ms"` to `info` (the stream info in `playing` / host state).
2. Start `threading.Thread(target=self._render_file, args=(gen, queue, decoder, along, rate, block, start_s), daemon=True)`.
   From that moment **the render thread owns `decoder` and `along`** (it closes them in its own `finally`;
   `_run_file` only closes them if the render thread never started).
3. Output loop (replaces the old loop):
   - Under `self._lock`: gen check, take `self._seek_to`, read `self._paused`.
   - Seek: `queue.flush(seek_to=seek_to)`.
   - Paused: as now (`out.abort()`, `running = False`) then `queue.flush(seek_to=self.position)` (what was
     heard; the render thread re-renders from there while paused, so resume is instant); wait on
     `self._wake` as now; `continue`.
   - `if not running: out.start()`.
   - `chunk = queue.get()`. `None` → write `block` frames of silence (position doesn't move).
     `chunk.end` → `finished = True; break`.
   - Real chunk: tap write `tap.write(chunk.before, chunk.sound, chunk.bypass)` (moved here from the render
     side so the Analyzer shows what is **heard now**, not what was rendered ahead), 3 ms linear fade-in if
     the previous write was silence (no click after a starve / seek), `self.tone.mix`, peak, test gain,
     `out.write` (True → `xruns += 1`), then `heard = max(chunk.at, chunk.at + chunk.n / rate - out.latency)`
     → `self.position`.
   - Every `REPORT_EVERY`: the `position` event as now **plus** `**queue.take_stats()` and
     `latency_ms = round((out.latency + queue.target / rate) * 1000, 1)`.
4. On exit (any path): `queue.close()`, join the render thread (timeout 3 s), and if `adaptive`:
   `self._learned_s = max(self._learned_s, queue.target / rate)` (the next play with this plugin starts
   with what it needed; never shrinks while the worker lives). `finished` → `out.stop()` (lets the last
   buffered blocks play out) and the end position, as now.

`_render_file(gen, queue, decoder, along, rate, block, start_s)` (new method, render thread): `_com_init`,
`_pro_audio_thread`; `base, written = start_s, 0`; loop while `gen == self._gen` and not `queue.closed`:
- `epoch, seek = queue.take_seek()`; a seek → `decoder.seek`, `along.seek`, `base, written = seek, 0`, `ended = False`.
- If the end marker was already queued for this epoch (`ended`): `queue.wait_flush(epoch, 0.1)` (no
  rendering), `continue`.
- `if not queue.wait_room(0.1): continue`.
- Under `self._lock`: bypass, envelope, loop, dry, until, mix (exactly as the old loop); drop `along` when
  `mix` became None (as now).
- **The old loop's body moves here unchanged**: `decoder.read(block)`, `at`, the loop cut / jump-back,
  `until` cut, envelope, mix, dry vs `_through`, the recorder write (under the lock, as now). The two
  `finished = True; break` places become: `queue.put(_Chunk(end=True), epoch); ended = True; continue`.
- Time only the processing part with `time.perf_counter()` → `queue.note_render(dt, n)`; if
  `dt > 0.5 * queue.target / rate` → `queue.grow(int(2 * dt * rate) + block)`.
- `queue.put(_Chunk(sound, before=(sound if dry else data), at=at, bypass=bypass), epoch)`; `written += n`.
- `finally`: close `decoder` and `along`, `_normal_thread`, `_com_done`.
- An exception: `self._say(gen, "error", message=f"Playback failed: {e}")` and `queue.put(_Chunk(end=True), epoch)`
  so the output thread ends instead of playing silence forever.

#### Browser playback (`_run_live`) — split the same way

- `_LiveBuffer` becomes a plain FIFO for the browser's frames (the priming moves to the queue):
  - remove `pull` and the `_primed` / `target` logic; add `take(n)` → a (2, n) float32 block only when at least
    `n` frames are buffered, else `None` (no underrun accounting), and `wait(timeout)` (a `Condition` on its
    lock, notified by `push`);
  - `configure(block, limit)`; `push` drops the oldest audio beyond `limit` (down to `limit // 2`, counted in
    `dropped`) — only reached when the browser's clock runs faster than the device's for a long time;
  - `stats(rate)` keeps its keys (`frames_in, dropped, underruns, buffer_ms, peak_in`); `underruns` now
    comes from the queue (`queue.starves`, passed in).
- `_run_live`: open the stream, `target, adaptive = render_target(audio.get("buffer"), rate, block, True, self._learned_live_s)`,
  `buffer.configure(block, limit=2 * target + 4 * BROWSER_CHUNK)`, the queue, start `_render_live`, then the
  output loop: `chunk = queue.get()`; None → silence of `block` frames at the device's rate; else tap write
  (`chunk.before`, `chunk.sound`, bypass — the tap stays at the browser's rate), 3 ms fade-in after silence,
  tone, peak, gain, `out.write(chunk.device)`. The `live` event adds `**queue.take_stats()`,
  `underruns=queue.starves`, and `latency_ms = round((queue.frames + buffered) / rate * 1000 + out.latency * 1000, 1)`.
  Learned target kept in `self._learned_live_s` (separate from the file's).
- `_render_live(gen, queue, buffer, rate, block, resample)`: COM init + Pro Audio; `last_input = time.monotonic()`;
  loop: `if not queue.wait_room(0.05): continue`; `data = buffer.take(block)`; got data → `last_input = now`;
  no data → if `now - last_input < LIVE_IDLE_S`: `buffer.wait(block / rate)`, `continue`; else `data = silence`
  (nothing arrives: the page went away; silence still goes through the plugin so tails ring out, and the
  queue stays full so input resuming is heard at the buffer's latency, not with a gap). Process with
  `_through` (timed, `note_render` / `grow` as in the file path), resample for the device if needed (`resample`
  moves here: `_Chunk(sound, before=data, bypass=bypass, device=resampled)`), `queue.put`.

#### Shared rules
- **One plugin call at a time**: add `self._plugin_lock = threading.Lock()` and hold it inside `Player._process`
  around the plugin call. A render thread that outlives a timed-out join can then never call the plugin at
  the same time as the next playback's render thread (or the instrument loop).
- **GIL**: in the worker's `_run` (before playback can start), `sys.setswitchinterval(0.001)`: the output
  thread waits at most ~1 ms for the interpreter while the render thread runs Python code. (pedalboard and
  ctypes release the GIL inside plugin calls.)
- `_commands`: the `audio` dict also takes `"buffer"`. `reconfigure` / `play` from `reconfigure` keep passing it.
- Docstring at the top of `host_worker.py`: `<AUDIO>` gains `"buffer":"auto|25..800"`; `position` / `live`
  events gain `ahead_ms, target_ms, starves, load_pct, render_peak_ms` (and `position` gains `latency_ms`).
- What changes for the owner, by design: knob / Bypass / IN / OUT / fade edits are heard after the buffer
  (Auto starts at 60 ms); seek, pause, stop and Play are immediate (the queue is flushed). The recorder may
  have written up to one buffer beyond what was heard when Record stops (punch-in semantics: harmless).
  MIDI sent to VST2/CLAP effects reaches them at render time (one buffer early relative to the sound).

#### Required test (new file `tools/test_render_ahead.py`, runs without a device or a real plugin)
- A fake plugin object (`name`, `is_instrument=False`, `__call__(data, rate, reset=False)` returning
  `data * 0.5`, sleeping 60 ms on every 8th call and 1 ms otherwise) and a fake output stream
  (`start/stop/abort/close`, `latency = block/rate`, `write(a)` appends to a list and sleeps `len(a)/rate`
  minus elapsed to emulate a real-time device; returns True when called later than the device needed it).
  Monkeypatch `Player._open_stream` to return it, `analysis_tap` not needed (no `tap` command).
- File test (1–2 s generated WAV via `float_wav_header` + numpy, 48 kHz, block 512): with `buffer="auto"`
  the concatenated non-silent output equals the input × 0.5 (allclose) with **no gap** (no zero run inside
  the audio), `xruns == 0`; with the old behaviour emulated (`buffer="25"` and the spike) gaps or starves
  are reported (starves > 0). Also: record on → the recorded WAV's samples equal input × 0.5 exactly.
- Seek / pause / resume / loop / until still behave: position after `until` = end; a loop plays the
  selection twice when allowed to run 2× its length.
- Live test: push the browser's frames in bursts (e.g. 4 chunks of 2048 every 170 ms) through
  `_LiveBuffer.push`, run `live()` 2 s: after priming no starves with Auto, output = input × 0.5 in order.
- Run with `.venv-native\Scripts\python.exe tools/test_render_ahead.py` (or the dev venv) and print PASS/FAIL.

### B. Settings, backend state and UI

- `app/web.py`: `_SETTINGS_DEFAULTS["audio_plugin_buffer"] = "auto"` (with a comment like the others),
  `_SETTING_CHOICES["audio_plugin_buffer"] = host_worker.PLUGIN_BUFFERS`, add it to `_AUDIO_KEYS` and to
  `_playback_settings()` (`"buffer": v["audio_plugin_buffer"]`).
- `app/plugin_host.py`: `_audio_settings()` default gains `"buffer": "auto"`; `_audio_event` merges, besides
  `xruns, peak_out, latency_ms`, the new `ahead_ms, target_ms, starves, load_pct, render_peak_ms` into
  `s["audio"]`; the state's comment for `audio` lists them.
- **Configuration › Audio** (`app/static/js/audio-settings.js`, its CSS if needed): in "Sample rate and buffer",
  a **Plugin buffer** select after Buffer size: `Auto (recommended)`, `25 ms` … `800 ms`; saved like the
  others (`put('audio_plugin_buffer', …)`, `values` default `'auto'`). Hint under it: "Heavy plugins
  (iZotope RX, spectral and linear-phase processors) are run ahead of the sound by this much, so their
  slow moments never reach your ears as gaps. Auto starts at 60 ms and grows only if a plugin needs it.
  Knobs, Bypass and edits are heard after this delay; Play, Stop and seeking are immediate. Instruments
  aren't buffered." `latencyHtml()` adds the plugin buffer to the total it explains.
- **Playground** (`app/static/js/playground.js` + `css/playground.css`): `normalizeHost` already passes
  `s.audio` through. While a file or the browser plays through a plugin, show a compact, quiet readout
  in the Audio tab's transport row and in the Browser tab's status area (wherever the existing live /
  playing info is shown — keep the existing style): `Buffer 120 ms · CPU 64 %` (target_ms, load_pct),
  plus ` · 3 dropouts` when `starves + xruns > 0`. When `load_pct > 100` for two reports in a row, a
  warning line: "<plugin> needs more CPU than real time at this buffer size: it will stutter whatever the
  Plugin buffer. Raise Buffer size in Configuration › Audio." Tooltip on the readout: "Plugin buffer: how
  far ahead of the sound the plugin runs. CPU: the plugin's processing time as a share of real time."
- No new endpoints.

### C. Browser path (`desktop/src-tauri/…`, `app/browser_audio.py`)

- **Keep the hidden browser running at full speed.** One constant in `lib.rs`:
  `pub const WEBVIEW_ARGS: &str = "<the default Tauri/wry args> --disable-renderer-backgrounding --disable-background-timer-throttling --disable-backgrounding-occluded-windows";`
  Read wry's source in the cargo registry (`~/.cargo/registry/src/*/wry-*/src/webview2/mod.rs`, the version
  in `Cargo.lock`) for the exact default string it uses when no args are given (it is replaced, not
  extended, when you set yours: currently `--disable-features=msWebOOUI,msPdfOOUI,msSmartScreenProtection`)
  and keep it at the front. **Every** webview must get the identical string, because they share one
  WebView2 environment / user-data folder and WebView2 refuses a second environment with other options:
  the main window (`WebviewWindowBuilder::from_config(...)` in `lib.rs` → `.additional_browser_args(WEBVIEW_ARGS)`),
  the browser (`browser.rs`), the image search (`imgsearch.rs`) and the plugin frame (`plugin_frame.rs`).
  `cargo check` in `desktop/src-tauri` must pass (don't build the installer).
- `browser_capture.js`: `const CHUNK = 2048;` (comment: must match `host_worker.BROWSER_CHUNK`) for the
  ScriptProcessor (was 1024: twice the slack for a busy page); `latencyHint: 'playback'`; **lost-block
  detection**: in `onaudioprocess`, compare `e.playbackTime` with the previous one + `CHUNK / ctx.sampleRate`;
  a jump of more than half a chunk counts the missing chunks in `gaps`; at most once a second while `gaps`
  changed, `ws.send(JSON.stringify({ gaps }))` (text). Only while `routed || tapped` (as the sending is).
- `app/browser_audio.py`: `MAX_PENDING_BYTES` from 96 KB to 384 KB (~1 s at 48 kHz: a burst after the page
  stalled is delivered, not dropped; the worker's buffer handles the rest). Text messages: parse
  `{"gaps": n}` into `conn.gaps`; `status()` adds `page_gaps` (sum over connections); a `logging` warning
  (to the Log section) at most every 10 s when a page reports new gaps: "The browser page lost N audio
  blocks (it was too busy to capture them): what you hear has gaps."
- Update the module docstrings (`browser_capture.js` header, `browser_audio.py`) for the new text message.

### D. Documentation
A new section at the end of `AGENTS.md`: "Plugin buffer: gapless sound through heavy plugins (2026-10-19,
owner)", with the diagnosis, the design, the setting, the events, the browser flags and what was tested
(and what wasn't: the desktop shell, by ear). Written after the code is in.

## Out of scope (possible follow-ups)
- Plugin delay compensation (aligning the recording with the original by the plugin's reported latency).
- Render-ahead for instruments.
- An offline "Render the file through the plugin" for plugins whose `load_pct` is above 100.
- Using VST3 `kPrefetch` instead of `kRealtime` while rendering ahead.
