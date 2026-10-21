// Playground › Signal tab: the test-signal designer (VEXT.PlaygroundSignal), drawn into the Signal
// tab's panel by js/playground.js. The designer's state is a patch (the schema of app/signal_gen.py):
// POST /api/playground/signal renders it to a WAV that opens as the Playground's file, so the signal
// plays through the plugin, loops and records like any file. Live renders ~300 ms after the last change
// (a knob drag sends many); one render runs at a time, and a change made during one queues one more.
// The patch is kept in localStorage. The scope (js/signal-scope.js) draws the rendered preview.
// No control takes Space: it is the Playground's play / pause key, so it passes through.
(function () {
  const V = window.VEXT;
  if (!V || V.PlaygroundSignal || !V.AnalyzerKnob) return;
  const K = V.AnalyzerKnob;

  const KEY = 'pa-pg-signal';
  const LIVE_KEY = 'pa-pg-signal-live';
  const REC_OK_KEY = 'pa-pg-signal-rec-ok';
  const LIVE_MS = 300;             // Live: render this long after the last change
  const REC_POLL_MS = 300;         // a render waiting for Record to stop
  const MINUS = '−';

  const LENGTHS = [1, 2, 5, 10, 20, 30, 60];
  const RATES = [[44100, '44.1 kHz'], [48000, '48 kHz'], [88200, '88.2 kHz'], [96000, '96 kHz']];
  const SEAMS = [[0, 'Off'], [10, '10 ms'], [20, '20 ms'], [50, '50 ms'], [100, '100 ms']];
  const CHANNELS = [['stereo', 'Stereo'], ['mono', 'Mono'], ['left', 'L'], ['right', 'R'], ['invert', 'Inverted']];
  const WAVES = [['sine', 'Sine'], ['triangle', 'Tri'], ['saw', 'Saw'], ['square', 'Square'], ['pulse', 'Pulse'],
    ['wavetable', 'Table']];
  const COLORS = [['white', 'White'], ['pink', 'Pink'], ['brown', 'Brown'], ['blue', 'Blue'], ['violet', 'Violet']];
  const STEREO = [['same', 'Same'], ['independent', 'Independent']];
  const POLARITY = [['positive', '+'], ['negative', MINUS], ['alternate', '±']];
  const LAWS = [['log', 'Log'], ['linear', 'Linear']];
  const BUILTIN_TABLES = [
    { name: 'basic', label: 'Basic', user: false }, { name: 'harmonics', label: 'Harmonics', user: false },
    { name: 'pwm', label: 'PWM', user: false }, { name: 'formant', label: 'Formant', user: false },
    { name: 'sync', label: 'Sync', user: false }, { name: 'fm', label: 'FM', user: false },
    { name: 'bitcrush', label: 'Bitcrush', user: false },
  ];

  // Knob steps (ascending lists; the knob draws a value between two steps where it falls).
  function range(a, b, step) {
    const out = [];
    for (let v = a; v <= b + 1e-9; v += step) out.push(Math.round(v * 1000) / 1000);
    return out;
  }
  const DB = range(-60, 0, 1);
  const DB_FULL = range(-80, 0, 1);
  const PERCENT = range(0, 100, 1);
  const PERCENT_5 = range(0, 100, 5);
  const WIDTH = range(1, 99, 1);
  const PAN = range(-100, 100, 5);
  const SEMI = range(-48, 48, 1);
  const DETUNE = range(-100, 100, 1);
  const PHASE = range(0, 360, 15);
  const VOICES = range(1, 8, 1);
  const SPREAD = [0, 1, 2, 3, 5, 7, 10, 15, 20, 25, 30, 40, 50, 75, 100];
  const LFO_HZ = [0, 0.1, 0.2, 0.5, 1, 2, 3, 4, 5, 6, 8, 10, 15, 20];
  const LFO_CENTS = [0, 5, 10, 20, 50, 100, 200, 1200];
  const MS = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000];
  const EVERY = [10, 20, 50, 100, 200, 250, 500, 1000, 2000, 5000];
  const STEP_DB = [1, 2, 3, 6, 10];
  const TONE = K.STEPS.tone;

  const sgn = (v) => (v > 0 ? `+${v}` : v < 0 ? `${MINUS}${-v}` : '0');
  const pct = (v) => `${v}%`;
  const panText = (v) => (v < 0 ? `L${-v}` : v > 0 ? `R${v}` : 'C');
  const msText = (v) => (v >= 1000 ? `${v / 1000} s` : `${v} ms`);
  const hzText = (v) => `${v} Hz`;
  const ctText = (v) => `${v} ct`;
  const degText = (v) => `${v}°`;
  const dbText = (v) => (v == null ? `${MINUS}∞` : `${v < 0 ? MINUS : ''}${Math.abs(v).toFixed(1)}`);

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
      on: true, wave: 'sine', table: 'basic', position: 0, width: 0.5, freq: 1000, semitones: 0, detune: 0,
      phase: 0, unison: 1, unison_detune: 15, unison_spread: 0.5, random_phase: false, level_db: 0, pan: 0,
      invert: false, sweep: { on: false, to: 20000, law: 'log' }, vibrato: { rate: 0, depth: 0 },
      tremolo: { rate: 0, depth: 0 },
    };
  }
  function base() {
    return {
      seconds: 10, rate: 48000, level_db: -18, normalize: true, channels: 'stereo', seam_ms: 20,
      oscillators: [osc()],
      noise: { on: false, color: 'pink', level_db: 0, stereo: 'same' },
      impulse: { on: false, every_ms: 1000, level_db: 0, polarity: 'positive' },
      gate: { on: false, on_ms: 200, off_ms: 800, attack_ms: 1, release_ms: 1 },
      steps: { on: false, from_db: -60, to_db: 0, step_db: 6, step_ms: 500 },
      dc: 0,
    };
  }
  const clone = (p) => JSON.parse(JSON.stringify(p));

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
    p.oscillators = Array.isArray(src && src.oscillators)
      ? src.oscillators.slice(0, 4).map((o) => merge(osc(), o))
      : base().oscillators;
    return p;
  }
  function loadPatch() {
    try {
      const saved = JSON.parse(localStorage.getItem(KEY) || 'null');
      if (saved && typeof saved === 'object') return fill(saved);
    } catch { /* a damaged value: the default signal */ }
    return base();
  }

  // The presets: each one a whole patch (the defaults plus its own changes).
  const sweep = (p, law) => {
    Object.assign(p.oscillators[0], { freq: 20, sweep: { on: true, to: 20000, law } });
  };
  const noise = (p, color) => {
    p.oscillators = [];
    Object.assign(p.noise, { on: true, color });
  };
  const PRESETS = [
    { group: 'Tones', name: 'Sine 1 kHz, −18 dBFS', edit: () => {} },
    { group: 'Tones', name: 'Sine 997 Hz, −1 dBFS (THD)', edit: (p) => { p.level_db = -1; p.oscillators[0].freq = 997; } },
    { group: 'Tones', name: 'Sine 100 Hz', edit: (p) => { p.oscillators[0].freq = 100; } },
    { group: 'Tones', name: 'Square 100 Hz', edit: (p) => { Object.assign(p.oscillators[0], { wave: 'square', freq: 100 }); } },
    { group: 'Tones', name: 'Saw 110 Hz', edit: (p) => { Object.assign(p.oscillators[0], { wave: 'saw', freq: 110 }); } },
    { group: 'Tones', name: 'Supersaw (saw 110 Hz, 7 voices)', edit: (p) => {
      Object.assign(p.oscillators[0], { wave: 'saw', freq: 110, unison: 7, unison_detune: 25, unison_spread: 0.8 });
    } },
    { group: 'Tones', name: 'Wavetable sweep', edit: (p) => {
      Object.assign(p.oscillators[0], { wave: 'wavetable', table: 'basic', position: 0.5, freq: 110 });
    } },
    { group: 'Sweeps', name: 'Log sweep 20 Hz–20 kHz (10 s)', edit: (p) => sweep(p, 'log') },
    { group: 'Sweeps', name: 'Linear sweep 20 Hz–20 kHz (10 s)', edit: (p) => sweep(p, 'linear') },
    { group: 'Noise', name: 'Pink noise', edit: (p) => noise(p, 'pink') },
    { group: 'Noise', name: 'White noise', edit: (p) => noise(p, 'white') },
    { group: 'Noise', name: 'Brown noise', edit: (p) => noise(p, 'brown') },
    { group: 'Distortion', name: 'SMPTE IMD (60 Hz + 7 kHz at −12 dB, 4:1)', edit: (p) => {
      p.oscillators = [{ ...osc(), freq: 60 }, { ...osc(), freq: 7000, level_db: -12 }];
    } },
    { group: 'Distortion', name: 'CCIF IMD (19 kHz + 20 kHz)', edit: (p) => {
      p.oscillators = [{ ...osc(), freq: 19000 }, { ...osc(), freq: 20000 }];
    } },
    { group: 'Timing', name: 'Tone burst 1 kHz (200 ms on, 800 ms off)', edit: (p) => { p.gate.on = true; } },
    { group: 'Timing', name: 'Level staircase 1 kHz (−60 → 0 dB, 6 dB / 500 ms)', edit: (p) => { p.steps.on = true; } },
    { group: 'Timing', name: 'Impulse train (1 per second)', edit: (p) => { p.oscillators = []; p.impulse.on = true; } },
    { group: 'Stereo', name: 'Stereo phase test (sine 1 kHz, inverted)', edit: (p) => { p.channels = 'invert'; } },
    { group: 'Stereo', name: 'Beating (440 Hz + 441 Hz)', edit: (p) => {
      p.oscillators = [{ ...osc(), freq: 440 }, { ...osc(), freq: 441 }];
    } },
    { group: 'Other', name: 'Silence (noise-floor test)', edit: (p) => { p.oscillators = []; } },
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
    let live = localStorage.getItem(LIVE_KEY) !== '0';
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
    let oscCount = -1;          // the oscillator cards on screen
    let tables = BUILTIN_TABLES;
    let menuOpen = false;

    let presetBtn = null;
    let presetWrap = null;
    let menu = null;
    let renderBtn = null;
    let infoEl = null;
    let oscGrid = null;
    let addBtn = null;
    let emptyHint = null;
    let scope = null;
    const scopeBox = make('div', 'pgs-scope');

    function save() {
      try { localStorage.setItem(KEY, JSON.stringify(patch)); } catch { /* blocked: the patch lasts this session */ }
    }

    // A change: keep it, bring every control in step with the patch, and plan a Live render.
    function changed(structural) {
      save();
      syncAll(structural);
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
    function syncAll(structural) {
      if (structural || oscCount !== patch.oscillators.length) buildOscs();
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

    // Record is running: the render waits for it to stop (no render while recording).
    function waitRecording() {
      recPending = true;
      setInfo('Recording: changes apply when it stops.');
      if (shown) startRecPoll();
    }
    function startRecPoll() {
      if (recTimer) return;
      recTimer = setInterval(() => {
        if (host.recording()) return;
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
      if (host.recording()) { waitRecording(); return; }
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
      syncAll(true);
      render(true);
    }

    // ---------- tables (wavetables) ----------
    function fillTables(sel) {
      sel.textContent = '';
      for (const t of tables) sel.add(new Option(t.user ? `${t.label} (yours)` : t.label, t.name));
    }
    function refillTables() {
      for (const sel of box.querySelectorAll('.pgs-table')) fillTables(sel);
      syncAll(false);
    }
    async function loadTables() {
      try {
        const r = await V.api('/api/playground/signal/tables');
        if (r && Array.isArray(r.tables) && r.tables.length) {
          tables = r.tables.map((t) => ({ name: t.name, label: t.label || t.name, user: !!t.user }));
        }
      } catch { /* offline or an older backend: the built-in tables stay */ }
      if (!destroyed) refillTables();
    }
    async function importTable(i) {
      let path = '';
      try {
        const dlg = window.__TAURI__ && window.__TAURI__.dialog;
        if (dlg && typeof dlg.open === 'function') {
          const picked = await dlg.open({ filters: [{ name: 'WAV', extensions: ['wav'] }] });
          path = Array.isArray(picked) ? picked[0] : picked;
        } else {
          path = window.prompt('Path of a WAV file to use as a wavetable:', '');
        }
      } catch (err) {
        V.toast((err && err.message) || String(err), true);
        return;
      }
      path = String(path || '').trim();
      if (!path) return;
      const before = new Set(tables.map((t) => t.name));
      try {
        await V.api('/api/playground/signal/wavetable', {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ path }),
        });
      } catch (err) {
        V.toast(err.message, true);
        return;
      }
      await loadTables();
      const added = tables.find((t) => t.user && !before.has(t.name));
      if (added && patch.oscillators[i]) {
        patch.oscillators[i].table = added.name;
        changed();
      } else {
        V.toast('Wavetable imported. Pick it in the Table list.');
      }
    }

    // ---------- controls ----------
    // A knob bound to one patch field: get() reads its value, put(v) writes what the knob gives.
    // show() (optional) hides it while it doesn't apply.
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
        onChange: (v) => { shownValue = v; o.put(v); changed(); },
      });
      tag(k.el, () => {
        const v = o.get();
        if (v !== shownValue) { shownValue = v; k.set(v); }
        if (o.show) k.el.hidden = !o.show();
      });
      return k.el;
    }

    // Segmented buttons for one patch field; items are [value, text].
    function seg(name, items, get, put) {
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
      return group;
    }

    // A dropdown for one patch field; options are [value, text]. The value is handed over as a string.
    function select(name, options, get, put) {
      const sel = make('select', 'pgs-select');
      sel.setAttribute('aria-label', name);
      for (const [v, text] of options) sel.add(new Option(text, String(v)));
      sel.addEventListener('change', () => { put(sel.value); changed(); });
      tag(sel, () => { sel.value = String(get()); });
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

    // A card of a noise / impulse / burst / staircase: its title, an On switch, the controls (dimmed while Off).
    function fxCard(title, get, put, parts) {
      const c = make('section', 'pgs-card');
      c.setAttribute('aria-label', title);
      const head = make('header', 'pgs-card-head');
      head.append(make('span', 'pgs-card-title', title), toggle('On', get, put));
      const body = make('div', 'pgs-card-body');
      body.append(...parts);
      c.append(head, body);
      tag(c, () => c.classList.toggle('is-off', !get()));
      return c;
    }

    function oscCard(i) {
      const o = () => patch.oscillators[i];
      const kn = (spec) => knob({ ...spec, aria: `Osc ${i + 1} ${spec.label}` });
      const card = make('section', 'pgs-card pgs-osc');
      card.setAttribute('aria-label', `Oscillator ${i + 1}`);

      const head = make('header', 'pgs-card-head');
      const x = btn('×', 'pgs-x');
      x.title = 'Remove this oscillator';
      x.setAttribute('aria-label', `Remove Osc ${i + 1}`);
      x.addEventListener('click', () => { patch.oscillators.splice(i, 1); changed(true); });
      head.append(
        make('span', 'pgs-card-title', `Osc ${i + 1}`),
        toggle('On', () => o().on, (v) => { o().on = v; }),
        make('span', 'pgs-spacer'),
        x,
      );

      const wave = seg('Wave', WAVES, () => o().wave, (v) => { o().wave = v; });

      const tableSel = make('select', 'pgs-select pgs-table');
      tableSel.setAttribute('aria-label', `Osc ${i + 1} table`);
      fillTables(tableSel);
      tableSel.addEventListener('change', () => { o().table = tableSel.value; changed(); });
      tag(tableSel, () => {
        const v = o().table;
        if (![...tableSel.options].some((opt) => opt.value === v)) tableSel.add(new Option(v, v));
        tableSel.value = v;
      });
      const importBtn = btn('Import…', '');
      importBtn.title = 'Use a WAV file as a wavetable: one cycle per 2048 samples';
      importBtn.addEventListener('click', () => importTable(i));
      const tableRow = row(lab('Table'), tableSel, importBtn);
      tag(tableRow, () => { tableRow.hidden = o().wave !== 'wavetable'; });

      const pitch = knobRow(
        kn({ label: 'Freq', values: TONE, min: 1, max: 24000, format: K.fmtFreq, parse: parseHz,
          get: () => o().freq, put: (v) => { o().freq = v; } }),
        kn({ label: 'Semi', values: SEMI, min: -48, max: 48, format: sgn,
          get: () => o().semitones, put: (v) => { o().semitones = v; } }),
        kn({ label: 'Detune', values: DETUNE, min: -100, max: 100, format: (v) => `${sgn(v)} ct`,
          get: () => o().detune, put: (v) => { o().detune = v; } }),
        kn({ label: 'Phase', values: PHASE, min: 0, max: 360, format: degText,
          get: () => o().phase, put: (v) => { o().phase = v; } }),
        kn({ label: 'Level', values: DB, min: -60, max: 0, format: K.fmtDb,
          get: () => o().level_db, put: (v) => { o().level_db = v; } }),
        kn({ label: 'Pan', values: PAN, min: -100, max: 100, format: panText,
          get: () => Math.round(o().pan * 100), put: (v) => { o().pan = v / 100; } }),
        kn({ label: 'Position', values: PERCENT, min: 0, max: 100, format: pct,
          get: () => Math.round(o().position * 100), put: (v) => { o().position = v / 100; },
          show: () => o().wave === 'wavetable' }),
        kn({ label: 'Pulse', values: WIDTH, min: 1, max: 99, format: pct,
          get: () => Math.round(o().width * 100), put: (v) => { o().width = v / 100; },
          show: () => o().wave === 'pulse' }),
      );

      const flags = row(
        toggle('Invert', () => o().invert, (v) => { o().invert = v; }),
        toggle('Random phase', () => o().random_phase, (v) => { o().random_phase = v; }),
      );
      const unison = row(
        lab('Unison'),
        knobRow(
          kn({ label: 'Voices', values: VOICES, min: 1, max: 8, format: String,
            get: () => o().unison, put: (v) => { o().unison = v; } }),
          kn({ label: 'Spread det.', values: SPREAD, min: 0, max: 100, format: ctText,
            get: () => o().unison_detune, put: (v) => { o().unison_detune = v; } }),
          kn({ label: 'Width', values: PERCENT_5, min: 0, max: 100, format: pct,
            get: () => Math.round(o().unison_spread * 100), put: (v) => { o().unison_spread = v / 100; } }),
        ),
      );

      // Modulation: a row that opens by itself when something in it is on.
      const modOn = () => o().sweep.on || o().vibrato.depth > 0 || o().tremolo.depth > 0;
      const mod = make('div', 'pgs-mod');
      mod.hidden = !modOn();
      const modBtn = btn('Modulation', 'pgs-mod-btn');
      modBtn.setAttribute('aria-expanded', String(!mod.hidden));
      modBtn.addEventListener('click', () => {
        mod.hidden = !mod.hidden;
        modBtn.setAttribute('aria-expanded', String(!mod.hidden));
      });
      const sweepRow = row(
        toggle('Sweep', () => o().sweep.on, (v) => { o().sweep.on = v; }),
        kn({ label: 'To', values: TONE, min: 20, max: 20000, format: K.fmtFreq, parse: parseHz,
          get: () => o().sweep.to, put: (v) => { o().sweep.to = v; } }),
        seg('Sweep law', LAWS, () => o().sweep.law, (v) => { o().sweep.law = v; }),
      );
      const vibRow = row(
        lab('Vibrato'),
        knobRow(
          kn({ label: 'Rate', values: LFO_HZ, min: 0, max: 20, format: hzText,
            get: () => o().vibrato.rate, put: (v) => { o().vibrato.rate = v; } }),
          kn({ label: 'Depth', values: LFO_CENTS, min: 0, max: 1200, format: ctText,
            get: () => o().vibrato.depth, put: (v) => { o().vibrato.depth = v; } }),
        ),
      );
      const tremRow = row(
        lab('Tremolo'),
        knobRow(
          kn({ label: 'Rate', values: LFO_HZ, min: 0, max: 20, format: hzText,
            get: () => o().tremolo.rate, put: (v) => { o().tremolo.rate = v; } }),
          kn({ label: 'Depth', values: PERCENT_5, min: 0, max: 100, format: pct,
            get: () => Math.round(o().tremolo.depth * 100), put: (v) => { o().tremolo.depth = v / 100; } }),
        ),
      );
      mod.append(sweepRow, vibRow, tremRow);

      const body = make('div', 'pgs-card-body');
      body.append(row(lab('Wave'), wave), tableRow, pitch, flags, unison, modBtn, mod);
      card.append(head, body);
      tag(card, () => card.classList.toggle('is-off', !o().on));
      return card;
    }

    // The oscillator cards, rebuilt when their number or the patch's objects change.
    function buildOscs() {
      oscGrid.textContent = '';
      patch.oscillators.forEach((_, i) => oscGrid.append(oscCard(i)));
      oscCount = patch.oscillators.length;
      emptyHint.hidden = oscCount > 0;
      addBtn.disabled = oscCount >= 4;
    }

    // ---------- the page ----------
    function build() {
      box.textContent = '';
      box.classList.add('pgs-root');

      // Top bar: presets, the file's length, rate, level, channels, seam, Live and Render.
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
        get: () => patch.level_db, put: (v) => { patch.level_db = v; } });
      const channels = seg('Channels', CHANNELS, () => patch.channels, (v) => { patch.channels = v; });
      const spacer = make('span', 'pgs-spacer');

      // Live on with a change not rendered yet renders it; turning it on alone does not.
      const liveToggle = toggle('Live', () => live, (v) => {
        live = v;
        try { localStorage.setItem(LIVE_KEY, v ? '1' : '0'); } catch { /* the default next time */ }
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
          () => patch.seconds, (v) => { patch.seconds = Number(v); })),
        field('Rate', select('Rate', RATES, () => patch.rate, (v) => { patch.rate = Number(v); })),
        levelKnob,
        toggle('Normalize', () => patch.normalize, (v) => { patch.normalize = v; }),
        channels,
        field('Seam', select('Seam', SEAMS, () => patch.seam_ms, (v) => { patch.seam_ms = Number(v); })),
        spacer,
        liveToggle,
        renderBtn,
      );
      infoEl = make('div', 'pgs-info');
      infoEl.setAttribute('role', 'status');
      infoEl.setAttribute('aria-live', 'polite');
      setInfo('Not rendered yet. Press Render, or change a setting with Live on.');

      // Sources (left): oscillators, then the noise / impulse / burst / staircase cards.
      const oscHead = make('div', 'pgs-head');
      addBtn = btn('+ Oscillator', 'pgs-add');
      addBtn.addEventListener('click', () => {
        if (patch.oscillators.length < 4) {
          patch.oscillators.push(osc());
          changed(true);
        }
      });
      oscHead.append(make('span', 'pgs-section', 'Oscillators'), addBtn);
      oscGrid = make('div', 'pgs-osc-grid');
      emptyHint = make('p', 'pgs-empty', 'No oscillators: noise, impulses and staircases only.');

      const noiseCard = fxCard('Noise', () => patch.noise.on, (v) => { patch.noise.on = v; }, [
        row(lab('Color'), seg('Noise color', COLORS, () => patch.noise.color, (v) => { patch.noise.color = v; })),
        knobRow(knob({ label: 'Level', values: DB, min: -60, max: 0, format: K.fmtDb,
          get: () => patch.noise.level_db, put: (v) => { patch.noise.level_db = v; } })),
        row(lab('Stereo'), seg('Noise stereo', STEREO, () => patch.noise.stereo, (v) => { patch.noise.stereo = v; })),
      ]);
      const impulseCard = fxCard('Impulse', () => patch.impulse.on, (v) => { patch.impulse.on = v; }, [
        knobRow(
          knob({ label: 'Every', values: EVERY, min: 10, max: 5000, format: msText,
            get: () => patch.impulse.every_ms, put: (v) => { patch.impulse.every_ms = v; } }),
          knob({ label: 'Level', values: DB, min: -60, max: 0, format: K.fmtDb,
            get: () => patch.impulse.level_db, put: (v) => { patch.impulse.level_db = v; } }),
        ),
        row(lab('Polarity'), seg('Impulse polarity', POLARITY,
          () => patch.impulse.polarity, (v) => { patch.impulse.polarity = v; })),
      ]);
      const burstCard = fxCard('Burst', () => patch.gate.on, (v) => { patch.gate.on = v; }, [
        knobRow(
          knob({ label: 'On', values: MS, min: 1, max: 5000, format: msText,
            get: () => patch.gate.on_ms, put: (v) => { patch.gate.on_ms = v; } }),
          knob({ label: 'Off', values: MS, min: 1, max: 5000, format: msText,
            get: () => patch.gate.off_ms, put: (v) => { patch.gate.off_ms = v; } }),
          knob({ label: 'Attack', values: MS, min: 1, max: 5000, format: msText,
            get: () => patch.gate.attack_ms, put: (v) => { patch.gate.attack_ms = v; } }),
          knob({ label: 'Release', values: MS, min: 1, max: 5000, format: msText,
            get: () => patch.gate.release_ms, put: (v) => { patch.gate.release_ms = v; } }),
        ),
      ]);
      const stairCard = fxCard('Staircase', () => patch.steps.on, (v) => { patch.steps.on = v; }, [
        knobRow(
          knob({ label: 'From', values: DB_FULL, min: -80, max: 0, format: K.fmtDb,
            get: () => patch.steps.from_db, put: (v) => { patch.steps.from_db = v; } }),
          knob({ label: 'To', values: DB_FULL, min: -80, max: 0, format: K.fmtDb,
            get: () => patch.steps.to_db, put: (v) => { patch.steps.to_db = v; } }),
          knob({ label: 'Step', values: STEP_DB, min: 1, max: 10, format: (v) => `${v} dB`,
            get: () => patch.steps.step_db, put: (v) => { patch.steps.step_db = v; } }),
          knob({ label: 'Every', values: MS, min: 1, max: 5000, format: msText,
            get: () => patch.steps.step_ms, put: (v) => { patch.steps.step_ms = v; } }),
        ),
      ]);
      const fx = make('div', 'pgs-fx-grid');
      fx.append(noiseCard, impulseCard, burstCard, stairCard);

      const sources = make('div', 'pgs-sources');
      sources.append(oscHead, oscGrid, emptyHint, fx);
      const bodyEl = make('div', 'pgs-body');
      bodyEl.append(sources, scopeBox);

      box.append(bar, infoEl, bodyEl);

      // The scope is optional: without it the sources take the whole width.
      scope = null;
      if (V.SignalScope) {
        try { scope = V.SignalScope.create(scopeBox); } catch { scope = null; }
      }
      bodyEl.classList.toggle('no-scope', !scope);
      syncAll(true);
      loadTables();
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
