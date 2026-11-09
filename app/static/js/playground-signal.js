// Playground › Signal tab: the test-signal designer (VEXT.PlaygroundSignal), drawn into the Signal
// tab's panel by js/playground.js. The designer's state is a patch (the schema of app/signal_gen.py):
// POST /api/playground/signal renders it to a WAV that opens as the Playground's file, so the signal
// plays through the plugin, loops and records like any file. Live renders ~300 ms after the last change
// (a knob drag sends many); one render runs at a time, and a change made during one queues one more.
// One oscillator, plus noise and impulses: the three are tabs (Oscillator · Noise · Impulse), and the
// scope (js/signal-scope.js) takes the panel's full height beside them. The patch is kept in sessionStorage
// (it lasts as long as the window; closing the app clears it); a patch saved before the single oscillator
// (an `oscillators` list) is migrated on load, and a saved `wavetable` wave falls back to sine.
// Alt + double-click on a knob, a segmented control or a select sets it back to its default.
// No control takes Space: it is the Playground's play / pause key, so it passes through.
(function () {
  const V = window.VEXT;
  if (!V || V.PlaygroundSignal || !V.AnalyzerKnob) return;
  const K = V.AnalyzerKnob;

  const KEY = 'pa-pg-signal';
  const LIVE_KEY = 'pa-pg-signal-live';
  const REC_OK_KEY = 'pa-pg-signal-rec-ok';   // a preference, not a value: stays in localStorage
  const SRC_KEY = 'pa-pg-signal-src';   // the source tab shown: osc | noise | impulse
  const LIVE_MS = 300;             // Live: render this long after the last change
  const REC_POLL_MS = 300;         // a render waiting for Record to stop
  const MINUS = '−';
  // The values above moved from localStorage to sessionStorage: their old copies are removed once.
  for (const k of [KEY, LIVE_KEY, SRC_KEY]) {
    try { localStorage.removeItem(k); } catch { /* storage blocked: nothing to remove */ }
  }

  const LENGTHS = [1, 2, 5, 10, 20, 30, 60];
  const RATES = [[44100, '44.1 kHz'], [48000, '48 kHz'], [88200, '88.2 kHz'], [96000, '96 kHz']];
  const SEAMS = [[0, 'Off'], [10, '10 ms'], [20, '20 ms'], [50, '50 ms'], [100, '100 ms']];
  const CHANNELS = [['stereo', 'Stereo'], ['mono', 'Mono'], ['left', 'L'], ['right', 'R'], ['invert', 'Inverted']];
  const WAVES = [['sine', 'Sine'], ['triangle', 'Tri'], ['saw', 'Saw'], ['square', 'Square'], ['pulse', 'Pulse']];
  const COLORS = [['white', 'White'], ['pink', 'Pink'], ['brown', 'Brown'], ['blue', 'Blue'], ['violet', 'Violet']];
  const STEREO = [['same', 'Same'], ['independent', 'Independent']];
  const POLARITY = [['positive', '+'], ['negative', MINUS], ['alternate', '±']];
  const SOURCES = [['osc', 'Oscillator'], ['noise', 'Noise'], ['impulse', 'Impulse']];

  // Knob steps (ascending lists; the knob draws a value between two steps where it falls).
  function range(a, b, step) {
    const out = [];
    for (let v = a; v <= b + 1e-9; v += step) out.push(Math.round(v * 1000) / 1000);
    return out;
  }
  const DB = range(-60, 0, 1);
  const MASTER = range(-24, 24, 1);
  const PERCENT_5 = range(0, 100, 5);
  const WIDTH = range(1, 99, 1);
  const PAN = range(-100, 100, 5);
  const SEMI = range(-48, 48, 1);
  const DETUNE = range(-100, 100, 1);
  const PHASE = range(0, 360, 15);
  const VOICES = range(1, 8, 1);
  const SPREAD = [0, 1, 2, 3, 5, 7, 10, 15, 20, 25, 30, 40, 50, 75, 100];
  const EVERY = [10, 20, 50, 100, 200, 250, 500, 1000, 2000, 5000];
  const TONE = K.STEPS.tone;

  const sgn = (v) => (v > 0 ? `+${v}` : v < 0 ? `${MINUS}${-v}` : '0');
  const pct = (v) => `${v}%`;
  const panText = (v) => (v < 0 ? `L${-v}` : v > 0 ? `R${v}` : 'C');
  const msText = (v) => (v >= 1000 ? `${v / 1000} s` : `${v} ms`);
  const ctText = (v) => `${v} ct`;
  const degText = (v) => `${v}°`;
  const dbText = (v) => (v == null ? `${MINUS}∞` : `${v < 0 ? MINUS : ''}${Math.abs(v).toFixed(1)}`);
  const masterText = (v) => `${v > 0 ? '+' : v < 0 ? MINUS : ''}${Math.abs(v).toFixed(1)} dB`;

  // Note names to Hz (scientific pitch: C4 = 261.63 Hz, A4 = 440). Sharps # or ♯, flats b or ♭.
  const NOTE_PC = { c: 0, d: 2, e: 4, f: 5, g: 7, a: 9, b: 11 };
  function parseNote(text) {
    const s = String(text).trim().replace(/−/g, '-');
    const m = /^([a-g])(#{1,2}|♯{1,2}|b{1,2}|♭{1,2})?\s*(-?\d+)$/i.exec(s);
    if (!m) return NaN;
    const acc = m[2] ? (/[#♯]/.test(m[2]) ? 1 : -1) * m[2].length : 0;
    const midi = (Number(m[3]) + 1) * 12 + NOTE_PC[m[1].toLowerCase()] + acc;
    return Math.round(440 * 2 ** ((midi - 69) / 12) * 1000) / 1000;
  }
  // The Freq knob's typed text: a note name first, then a number (1k, 440, 2,5k).
  function parseHz(text) {
    const n = parseNote(text);
    return Number.isFinite(n) ? n : K.parseText(text);
  }

  // ---------- the patch (app/signal_gen.py's schema; the backend fills and clamps anything missing) ----------
  function osc() {
    return {
      on: true, wave: 'sine', width: 0.5, freq: 1000, semitones: 0, detune: 0,
      phase: 0, unison: 1, unison_detune: 15, unison_spread: 0.5, random_phase: false, level_db: 0, pan: 0,
      invert: false,
    };
  }
  function base() {
    return {
      seconds: 10, rate: 48000, level_db: -18, master_db: 0, normalize: true, channels: 'stereo', seam_ms: 20,
      osc: osc(),
      noise: { on: false, color: 'pink', level_db: 0, stereo: 'same' },
      impulse: { on: false, every_ms: 1000, level_db: 0, polarity: 'positive' },
      dc: 0,
    };
  }
  // The defaults (read only): the values Alt + double-click resets a control to.
  const DEFAULT = base();

  // src's values where they have the type of the default, else the default (unknown keys dropped).
  function merge(def, src) {
    const out = {};
    for (const k of Object.keys(def)) {
      const d = def[k];
      const s = src && typeof src === 'object' ? src[k] : undefined;
      if (d && typeof d === 'object' && !Array.isArray(d)) out[k] = merge(d, s);
      else out[k] = typeof s === typeof d && s !== null ? s : d;
    }
    return out;
  }
  function fill(src) {
    const p = merge(base(), src);
    // A patch saved before the single oscillator had a list: its first item, or none when the list was empty.
    if (src && Array.isArray(src.oscillators) && !(src.osc && typeof src.osc === 'object')) {
      p.osc = src.oscillators.length ? merge(osc(), src.oscillators[0]) : { ...osc(), on: false };
    }
    // The Table wave is gone: a patch saved with it plays the sine.
    if (p.osc.wave === 'wavetable') p.osc.wave = 'sine';
    return p;
  }
  function loadPatch() {
    try {
      const saved = JSON.parse(sessionStorage.getItem(KEY) || 'null');
      if (saved && typeof saved === 'object') return fill(saved);
    } catch { /* a damaged value: the default signal */ }
    return base();
  }

  // The presets: each one a whole patch (the defaults plus its own changes).
  const noise = (p, color) => {
    p.osc.on = false;
    Object.assign(p.noise, { on: true, color });
  };
  const PRESETS = [
    { group: 'Tones', name: 'Sine 1 kHz, −18 dBFS', edit: () => {} },
    { group: 'Tones', name: 'Sine 997 Hz, −1 dBFS (THD)', edit: (p) => { p.level_db = -1; p.osc.freq = 997; } },
    { group: 'Tones', name: 'Sine 100 Hz', edit: (p) => { p.osc.freq = 100; } },
    { group: 'Tones', name: 'Square 100 Hz', edit: (p) => { Object.assign(p.osc, { wave: 'square', freq: 100 }); } },
    { group: 'Tones', name: 'Saw 110 Hz', edit: (p) => { Object.assign(p.osc, { wave: 'saw', freq: 110 }); } },
    { group: 'Tones', name: 'Supersaw (saw 110 Hz, 7 voices)', edit: (p) => {
      Object.assign(p.osc, { wave: 'saw', freq: 110, unison: 7, unison_detune: 25, unison_spread: 0.8 });
    } },
    { group: 'Noise', name: 'Pink noise', edit: (p) => noise(p, 'pink') },
    { group: 'Noise', name: 'White noise', edit: (p) => noise(p, 'white') },
    { group: 'Noise', name: 'Brown noise', edit: (p) => noise(p, 'brown') },
    { group: 'Stereo', name: 'Stereo phase test (sine 1 kHz, inverted)', edit: (p) => { p.channels = 'invert'; } },
    { group: 'Other', name: 'Impulse train (1 per second)', edit: (p) => { p.osc.on = false; p.impulse.on = true; } },
    { group: 'Other', name: 'Silence (noise-floor test)', edit: (p) => { p.osc.on = false; } },
  ];
  const presetPatch = (edit) => {
    const p = base();
    edit(p);
    return p;
  };

  // ---------- DOM helpers ----------
  function make(tagName, cls, text) {
    const n = document.createElement(tagName);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  const btn = (text, cls) => {
    const b = make('button', cls, text);
    b.type = 'button';
    return b;
  };
  const lab = (text) => make('span', 'pgs-label', text);
  const row = (...kids) => {
    const r = make('div', 'pgs-row');
    r.append(...kids);
    return r;
  };
  const knobRow = (...kids) => {
    const r = make('div', 'pgs-knobs');
    r.append(...kids);
    return r;
  };
  // A control that copies the patch into itself: refresh() calls sync() after every change.
  function tag(n, sync) {
    n.classList.add('pgs-ctl');
    n.pgsSync = sync;
    return n;
  }

  // ---------- the designer ----------
  function create(box, host) {
    let patch = loadPatch();
    let live = true;
    let src = 'osc';            // the source tab shown
    try {
      live = sessionStorage.getItem(LIVE_KEY) !== '0';
      const s = sessionStorage.getItem(SRC_KEY);
      if (SOURCES.some(([k]) => k === s)) src = s;
    } catch { /* storage blocked: Live on, the Oscillator tab */ }
    let renderedKey = null;     // the patch as the last render sent it; a difference is a change not rendered
    let busy = false;           // a render, or the question before it, is under way
    let queued = false;         // a change came in while busy: one render more after it
    let held = false;           // the user said no to replacing the file: only Render asks again
    let looped = false;         // the Playground's loop was set to All after the first render
    let recOk = false;          // the recording question was answered this session
    let recPending = false;     // a render waits for Record to stop
    let pending = false;        // a change is waiting for its Live render (kept while the tab is hidden)
    let shown = false;
    let destroyed = false;
    let liveTimer = null;
    let recTimer = null;
    let menuOpen = false;

    let presetBtn = null;
    let presetWrap = null;
    let menu = null;
    let renderBtn = null;
    let infoEl = null;
    let tabBtns = {};           // source key -> its tab button
    let panels = {};            // source key -> its tab panel
    let scroller = null;        // the tab panel region that scrolls
    let scope = null;
    const scopeBox = make('div', 'pgs-scope');

    function save() {
      try { sessionStorage.setItem(KEY, JSON.stringify(patch)); } catch { /* blocked: the patch is not kept */ }
    }

    // A change: keep it, bring every control in step with the patch, and plan a Live render.
    function changed() {
      save();
      syncAll();
      if (live) {
        pending = true;
        schedule();
      }
    }
    function schedule() {
      clearTimeout(liveTimer);
      liveTimer = setTimeout(() => {
        liveTimer = null;
        pending = false;
        render(false);
      }, LIVE_MS);
    }
    function syncAll() {
      for (const n of box.querySelectorAll('.pgs-ctl')) n.pgsSync();
      syncRender();
    }
    function isDirty() {
      return JSON.stringify(patch) !== renderedKey;
    }
    function syncRender() {
      if (renderBtn) renderBtn.classList.toggle('primary', isDirty());
    }

    function setInfo(text, kind) {
      infoEl.className = kind ? `pgs-info is-${kind}` : 'pgs-info';
      infoEl.textContent = text;
      infoEl.title = '';
    }
    function showInfo(i) {
      infoEl.className = 'pgs-info';
      infoEl.textContent = [
        `Peak ${dbText(i.peak_db)} dBFS`,
        `RMS ${dbText(i.rms_db)}`,
        `Crest ${i.crest_db == null ? '–' : i.crest_db.toFixed(1)} dB`,
        `${i.seconds} s`,
        `${i.rate / 1000} kHz`,
      ].join(' · ');
      if (i.clipped) infoEl.append(' · ', make('span', 'pgs-clip', 'clipped'));
      if (i.note) infoEl.append(` · ${i.note}`);
      infoEl.title = i.summary || '';
    }

    // ---------- rendering ----------
    // Before a render: ask when it would replace a file that is not a signal, and when a recording
    // of the last signal would be cleared (once per session, or for good with "Don't ask again").
    async function confirmReplace() {
      const name = host.fileName();
      if (name && !host.fileIsSignal()) {
        return V.confirmDialog(`Replace the Audio tab's file “${name}” (and its recording) with the signal?`);
      }
      if (host.hasRecording() && !recOk && localStorage.getItem(REC_OK_KEY) !== '1') {
        const answer = await askRecording();
        if (!answer.ok) return false;
        recOk = true;
        if (answer.again) {
          try { localStorage.setItem(REC_OK_KEY, '1'); } catch { /* the question comes back next session */ }
        }
      }
      return true;
    }

    // The app's modal with a "Don't ask again" tick (V.confirmDialog has only OK and Cancel).
    function askRecording() {
      return new Promise((resolve) => {
        const root = document.getElementById('confirm-root');
        root.innerHTML = '<div class="modal-backdrop" id="pgs-rec-backdrop"><div class="modal pgs-ask" role="dialog" aria-modal="true">'
          + '<p>Changing the signal clears the recording of the last one.</p>'
          + '<label class="switch-row pgs-ask-again"><input type="checkbox" class="switch" id="pgs-rec-again"><span>Don\'t ask again</span></label>'
          + '<div class="actions-row"><button type="button" id="pgs-rec-cancel">Cancel</button>'
          + '<button type="button" class="primary" id="pgs-rec-ok">OK</button></div></div></div>';
        const again = root.querySelector('#pgs-rec-again');
        const finish = (ok) => {
          document.removeEventListener('keydown', onKey, true);
          const remember = again.checked;
          root.innerHTML = '';
          resolve({ ok, again: remember });
        };
        const onKey = (e) => {
          if (e.key === 'Escape') { e.preventDefault(); finish(false); }
        };
        document.addEventListener('keydown', onKey, true);
        root.querySelector('#pgs-rec-ok').onclick = () => finish(true);
        root.querySelector('#pgs-rec-cancel').onclick = () => finish(false);
        root.querySelector('#pgs-rec-backdrop').onclick = (e) => { if (e.target.id === 'pgs-rec-backdrop') finish(false); };
        root.querySelector('#pgs-rec-ok').focus();
      });
    }

    // The Audio tab's own Record is running: the render waits for it to stop. (A Signal Record
    // does not hold renders: the recording follows the new render.)
    function waitRecording() {
      recPending = true;
      setInfo('Recording: changes apply when it stops.');
      if (shown) startRecPoll();
    }
    function startRecPoll() {
      if (recTimer) return;
      recTimer = setInterval(() => {
        if (host.fileRecording()) return;
        stopRecPoll();
        recPending = false;
        render(false);
      }, REC_POLL_MS);
    }
    function stopRecPoll() {
      clearInterval(recTimer);
      recTimer = null;
    }

    async function render(explicit) {
      if (destroyed) return;
      if (busy) {
        if (explicit) held = false;
        queued = true;
        return;
      }
      if (explicit) held = false;
      if (held) return;
      if (host.fileRecording()) { waitRecording(); return; }
      busy = true;
      const sent = JSON.stringify(patch);
      try {
        if (!(await confirmReplace())) {
          held = true;
          setInfo('Not rendered: press Render to replace the file.');
        } else {
          setInfo('Rendering…');
          const r = await host.render(JSON.parse(sent));
          renderedKey = sent;
          if (r.info) showInfo(r.info);
          if (scope && r.preview) scope.setPreview(r.preview);
          if (!looped) {
            looped = true;
            // A test signal is meant to repeat.
            if (host.loopMode() === 'off') host.setLoop('all');
          }
        }
      } catch (err) {
        const msg = (err && err.message) || String(err);
        setInfo(`Not rendered: ${msg}`, 'error');
        V.toast(msg, true);
      } finally {
        busy = false;
      }
      syncRender();
      if (queued) {
        queued = false;
        render(false);
      }
    }

    // ---------- presets ----------
    function openMenu(on) {
      if (!menu) return;
      menuOpen = on;
      menu.hidden = !on;
      presetBtn.setAttribute('aria-expanded', String(on));
      if (on) {
        document.addEventListener('pointerdown', onMenuDown, true);
        document.addEventListener('keydown', onMenuKey, true);
        menu.querySelector('.pgs-menu-item').focus();
      } else {
        document.removeEventListener('pointerdown', onMenuDown, true);
        document.removeEventListener('keydown', onMenuKey, true);
      }
    }
    function onMenuDown(e) {
      if (!presetWrap.contains(e.target)) openMenu(false);
    }
    function onMenuKey(e) {
      if (e.key !== 'Escape') return;
      e.preventDefault();
      openMenu(false);
      presetBtn.focus();
    }
    function choosePreset(n) {
      openMenu(false);
      clearTimeout(liveTimer);
      liveTimer = null;
      patch = presetPatch(PRESETS[n].edit);
      held = false;
      pending = false;
      save();
      syncAll();
      render(true);
    }

    // ---------- controls ----------
    // A knob bound to one patch field: get() reads its value, put(v) writes what the knob gives.
    // reset is the field's default (Alt + double-click); show() (optional) hides it while it doesn't apply.
    function knob(o) {
      let shownValue = o.get();
      const k = K.create({
        label: o.label,
        ariaLabel: o.aria || o.label,
        values: o.values,
        value: shownValue,
        min: o.min,
        max: o.max,
        format: o.format,
        parse: o.parse,
        reset: o.reset,
        onChange: (v) => { shownValue = v; o.put(v); changed(); },
      });
      tag(k.el, () => {
        const v = o.get();
        if (v !== shownValue) { shownValue = v; k.set(v); }
        if (o.show) k.el.hidden = !o.show();
      });
      return k.el;
    }

    // Alt + double-click on a control of one field sets the field back to its default (reset).
    function resetOnAlt(el, get, reset, put) {
      el.title = 'Alt + double-click: reset';
      el.addEventListener('dblclick', (e) => {
        if (!e.altKey) return;
        e.preventDefault();
        if (String(get()) === String(reset)) return;
        put(reset);
        changed();
      });
    }

    // Segmented buttons for one patch field; items are [value, text].
    function seg(name, items, get, put, reset) {
      const group = make('div', 'segmented pgs-seg');
      group.setAttribute('role', 'radiogroup');
      group.setAttribute('aria-label', name);
      const buttons = items.map(([, text], n) => {
        const b = btn(text, '');
        b.setAttribute('role', 'radio');
        b.addEventListener('click', () => { put(items[n][0]); changed(); });
        group.append(b);
        return b;
      });
      tag(group, () => {
        const cur = get();
        items.forEach(([v], n) => {
          const on = v === cur;
          buttons[n].classList.toggle('active', on);
          buttons[n].setAttribute('aria-checked', String(on));
        });
      });
      resetOnAlt(group, get, reset, put);
      return group;
    }

    // A dropdown for one patch field; options are [value, text]. The value is handed over as a string.
    function select(name, options, get, put, reset) {
      const sel = make('select', 'pgs-select');
      sel.setAttribute('aria-label', name);
      for (const [v, text] of options) sel.add(new Option(text, String(v)));
      sel.addEventListener('change', () => { put(sel.value); changed(); });
      tag(sel, () => { sel.value = String(get()); });
      resetOnAlt(sel, get, reset, put);
      return sel;
    }

    // A switch with its text, bound to one boolean. after() runs once the value is put (default: a change).
    function toggle(text, get, put, after = changed) {
      const lbl = make('label', 'switch-row pgs-toggle');
      const input = make('input', 'switch');
      input.type = 'checkbox';
      lbl.append(input, make('span', '', text));
      input.addEventListener('change', () => { put(input.checked); after(); });
      tag(lbl, () => { input.checked = !!get(); });
      return lbl;
    }

    // The tab panel of one source: its title, an On switch, the controls (dimmed while Off).
    function sourcePanel(key, title, parts) {
      const c = make('section', 'pgs-card');
      c.id = `pgs-panel-${key}`;
      c.setAttribute('role', 'tabpanel');
      c.setAttribute('aria-labelledby', `pgs-tab-${key}`);
      const head = make('header', 'pgs-card-head');
      head.append(make('span', 'pgs-card-title', title), toggle('On', () => patch[key].on, (v) => { patch[key].on = v; }));
      const body = make('div', 'pgs-card-body');
      body.append(...parts);
      c.append(head, body);
      tag(c, () => c.classList.toggle('is-off', !patch[key].on));
      return c;
    }

    function oscParts() {
      const o = () => patch.osc;
      const wave = seg('Wave', WAVES, () => o().wave, (v) => { o().wave = v; }, DEFAULT.osc.wave);

      const pitch = knobRow(
        knob({ label: 'Freq', values: TONE, min: 1, max: 24000, format: K.fmtFreq, parse: parseHz,
          get: () => o().freq, put: (v) => { o().freq = v; }, reset: DEFAULT.osc.freq }),
        knob({ label: 'Semi', values: SEMI, min: -48, max: 48, format: sgn,
          get: () => o().semitones, put: (v) => { o().semitones = v; }, reset: DEFAULT.osc.semitones }),
        knob({ label: 'Detune', values: DETUNE, min: -100, max: 100, format: (v) => `${sgn(v)} ct`,
          get: () => o().detune, put: (v) => { o().detune = v; }, reset: DEFAULT.osc.detune }),
        knob({ label: 'Phase', values: PHASE, min: 0, max: 360, format: degText,
          get: () => o().phase, put: (v) => { o().phase = v; }, reset: DEFAULT.osc.phase }),
        knob({ label: 'Level', values: DB, min: -60, max: 0, format: K.fmtDb,
          get: () => o().level_db, put: (v) => { o().level_db = v; }, reset: DEFAULT.osc.level_db }),
        knob({ label: 'Pan', values: PAN, min: -100, max: 100, format: panText,
          get: () => Math.round(o().pan * 100), put: (v) => { o().pan = v / 100; },
          reset: Math.round(DEFAULT.osc.pan * 100) }),
        knob({ label: 'Pulse', values: WIDTH, min: 1, max: 99, format: pct,
          get: () => Math.round(o().width * 100), put: (v) => { o().width = v / 100; },
          reset: Math.round(DEFAULT.osc.width * 100), show: () => o().wave === 'pulse' }),
      );

      const flags = row(
        toggle('Invert', () => o().invert, (v) => { o().invert = v; }),
        toggle('Random phase', () => o().random_phase, (v) => { o().random_phase = v; }),
      );
      const unison = row(
        lab('Unison'),
        knobRow(
          knob({ label: 'Voices', values: VOICES, min: 1, max: 8, format: String,
            get: () => o().unison, put: (v) => { o().unison = v; }, reset: DEFAULT.osc.unison }),
          knob({ label: 'Spread det.', values: SPREAD, min: 0, max: 100, format: ctText,
            get: () => o().unison_detune, put: (v) => { o().unison_detune = v; }, reset: DEFAULT.osc.unison_detune }),
          knob({ label: 'Width', values: PERCENT_5, min: 0, max: 100, format: pct,
            get: () => Math.round(o().unison_spread * 100), put: (v) => { o().unison_spread = v / 100; },
            reset: Math.round(DEFAULT.osc.unison_spread * 100) }),
        ),
      );

      return [row(lab('Wave'), wave), pitch, flags, unison];
    }

    function noiseParts() {
      const n = () => patch.noise;
      return [
        row(lab('Color'), seg('Noise color', COLORS, () => n().color, (v) => { n().color = v; }, DEFAULT.noise.color)),
        knobRow(knob({ label: 'Level', values: DB, min: -60, max: 0, format: K.fmtDb,
          get: () => n().level_db, put: (v) => { n().level_db = v; }, reset: DEFAULT.noise.level_db })),
        row(lab('Stereo'), seg('Noise stereo', STEREO, () => n().stereo, (v) => { n().stereo = v; }, DEFAULT.noise.stereo)),
      ];
    }

    function impulseParts() {
      const im = () => patch.impulse;
      return [
        knobRow(
          knob({ label: 'Every', values: EVERY, min: 10, max: 5000, format: msText,
            get: () => im().every_ms, put: (v) => { im().every_ms = v; }, reset: DEFAULT.impulse.every_ms }),
          knob({ label: 'Level', values: DB, min: -60, max: 0, format: K.fmtDb,
            get: () => im().level_db, put: (v) => { im().level_db = v; }, reset: DEFAULT.impulse.level_db }),
        ),
        row(lab('Polarity'), seg('Impulse polarity', POLARITY,
          () => im().polarity, (v) => { im().polarity = v; }, DEFAULT.impulse.polarity)),
      ];
    }

    // ---------- the source tabs ----------
    // Shows the picked source's panel; the others are hidden.
    function applySource() {
      for (const key of Object.keys(panels)) panels[key].hidden = key !== src;
      if (scroller) scroller.scrollTop = 0;
    }
    function pickSource(key) {
      src = key;
      try { sessionStorage.setItem(SRC_KEY, key); } catch { /* the tab is not kept */ }
      applySource();
      syncAll();
    }
    // A tab per source (WAI tabs: arrow keys move between them; Space is not taken).
    function buildTabs() {
      const list = make('div', 'pgs-tabs');
      list.setAttribute('role', 'tablist');
      list.setAttribute('aria-label', 'Signal source');
      for (const [key, title] of SOURCES) {
        const b = btn('', 'pgs-tab');
        b.id = `pgs-tab-${key}`;
        b.setAttribute('role', 'tab');
        b.setAttribute('aria-controls', `pgs-panel-${key}`);
        const dot = make('span', 'pgs-dot');
        dot.setAttribute('aria-hidden', 'true');
        b.append(dot, make('span', '', title));
        b.addEventListener('click', () => pickSource(key));
        tag(b, () => {
          const on = key === src;
          b.classList.toggle('active', on);
          b.setAttribute('aria-selected', String(on));
          b.tabIndex = on ? 0 : -1;
          // The dot shows what sounds without opening the tab.
          dot.classList.toggle('is-on', !!patch[key].on);
          b.title = `${title} is ${patch[key].on ? 'on' : 'off'}`;
        });
        tabBtns[key] = b;
        list.append(b);
      }
      list.addEventListener('keydown', (e) => {
        if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
        e.preventDefault();
        const keys = SOURCES.map(([k]) => k);
        const next = keys[(keys.indexOf(src) + (e.key === 'ArrowRight' ? 1 : keys.length - 1)) % keys.length];
        pickSource(next);
        tabBtns[next].focus();
      });
      return list;
    }

    // ---------- the page ----------
    function build() {
      box.textContent = '';
      box.classList.add('pgs-root');

      // Top bar: presets, the file's length, rate, level, master, normalize, channels, seam, Live and Render.
      const bar = make('div', 'pgs-bar');
      presetBtn = btn('Presets ▾', 'pgs-presets');
      presetBtn.setAttribute('aria-haspopup', 'menu');
      presetBtn.setAttribute('aria-expanded', 'false');
      presetBtn.addEventListener('click', () => openMenu(!menuOpen));
      menu = make('div', 'pgs-menu');
      menu.setAttribute('role', 'menu');
      menu.setAttribute('aria-label', 'Presets');
      menu.hidden = true;
      let group = '';
      PRESETS.forEach((p, n) => {
        if (p.group !== group) {
          group = p.group;
          const g = make('div', 'pgs-menu-group', group);
          g.setAttribute('role', 'presentation');
          menu.append(g);
        }
        const item = btn(p.name, 'pgs-menu-item');
        item.setAttribute('role', 'menuitem');
        item.addEventListener('click', () => choosePreset(n));
        menu.append(item);
      });
      presetWrap = make('div', 'pgs-presets-wrap');
      presetWrap.append(presetBtn, menu);

      const levelKnob = knob({ label: 'Level', aria: 'Level, dBFS', values: DB, min: -80, max: 0, format: K.fmtDb,
        get: () => patch.level_db, put: (v) => { patch.level_db = v; }, reset: DEFAULT.level_db });
      // The master gain after the render: louder past 0 dBFS clips (the info line says so).
      const masterKnob = knob({ label: 'Master', aria: 'Master gain, dB', values: MASTER, min: -24, max: 24,
        format: masterText, get: () => patch.master_db, put: (v) => { patch.master_db = v; },
        reset: DEFAULT.master_db });
      const channels = seg('Channels', CHANNELS, () => patch.channels, (v) => { patch.channels = v; },
        DEFAULT.channels);
      const spacer = make('span', 'pgs-spacer');

      // Live on with a change not rendered yet renders it; turning it on alone does not.
      const liveToggle = toggle('Live', () => live, (v) => {
        live = v;
        try { sessionStorage.setItem(LIVE_KEY, v ? '1' : '0'); } catch { /* the default next time */ }
        if (!v) { clearTimeout(liveTimer); liveTimer = null; }
      }, () => {
        if (live && isDirty()) {
          pending = true;
          schedule();
        }
      });
      liveToggle.title = 'Render about 0.3 s after each change. Off: press Render';

      renderBtn = btn('Render', 'pgs-render');
      renderBtn.title = 'Render the signal now';
      renderBtn.addEventListener('click', () => {
        clearTimeout(liveTimer);
        liveTimer = null;
        pending = false;
        render(true);
      });

      bar.append(
        presetWrap,
        field('Length', select('Length', LENGTHS.map((s) => [s, `${s} s`]),
          () => patch.seconds, (v) => { patch.seconds = Number(v); }, DEFAULT.seconds)),
        field('Rate', select('Rate', RATES, () => patch.rate, (v) => { patch.rate = Number(v); }, DEFAULT.rate)),
        levelKnob,
        masterKnob,
        toggle('Normalize', () => patch.normalize, (v) => { patch.normalize = v; }),
        channels,
        field('Seam', select('Seam', SEAMS, () => patch.seam_ms, (v) => { patch.seam_ms = Number(v); },
          DEFAULT.seam_ms)),
        spacer,
        liveToggle,
        renderBtn,
      );
      infoEl = make('div', 'pgs-info');
      infoEl.setAttribute('role', 'status');
      infoEl.setAttribute('aria-live', 'polite');
      setInfo('Not rendered yet. Press Render, or change a setting with Live on.');

      // The source panels: one of them shows at a time, in the scrolling region.
      panels = {
        osc: sourcePanel('osc', 'Oscillator', oscParts()),
        noise: sourcePanel('noise', 'Noise', noiseParts()),
        impulse: sourcePanel('impulse', 'Impulse', impulseParts()),
      };
      scroller = make('div', 'pgs-panel');
      scroller.append(panels.osc, panels.noise, panels.impulse);

      // The left column: the top bar, the info line, the source tabs, the panel. The scope is the right one.
      const main = make('div', 'pgs-main');
      main.append(bar, infoEl, buildTabs(), scroller);
      const bodyEl = make('div', 'pgs-body');
      bodyEl.append(main, scopeBox);

      box.append(bodyEl);

      // The scope is optional: without it the sources take the whole width.
      scope = null;
      if (V.SignalScope) {
        try { scope = V.SignalScope.create(scopeBox); } catch { scope = null; }
      }
      bodyEl.classList.toggle('no-scope', !scope);
      applySource();
      syncAll();
    }

    // A labelled top-bar control.
    function field(text, control) {
      const f = make('span', 'pgs-field');
      f.append(lab(text), control);
      return f;
    }

    // ---------- the public API ----------
    // The tab is shown or hidden: the scope draws only while shown, and the timers stop while hidden.
    function show(on) {
      if (destroyed) return;
      shown = !!on;
      if (scope) scope.show(shown);
      if (!shown) {
        clearTimeout(liveTimer);
        liveTimer = null;
        stopRecPoll();
        return;
      }
      if (recPending) startRecPoll();
      // A change left waiting when the tab was hidden is rendered now (nothing else renders on show).
      if (live && pending) schedule();
    }

    function destroy() {
      destroyed = true;
      clearTimeout(liveTimer);
      liveTimer = null;
      stopRecPoll();
      openMenu(false);
      if (scope) scope.destroy();
      scope = null;
      box.textContent = '';
    }

    build();
    return { show, destroy };
  }

  V.PlaygroundSignal = {
    create(box, host) {
      return create(box, host);
    },
  };
})();
