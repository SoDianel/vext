// The Analyzer section: one place to look at every sound VEXT plays. A bar picks the view
// (Spectrum, Spectrogram, Correlation, Imager), which signal (Before the plugin, After it, or
// Both) and the layout (both in one window, or Before | After side by side); a floating settings
// window (the Settings button) shows only what the view uses, Freq and Range as knobs
// (js/analyzer-knob.js); level meters sit on the right. Above the bar: the timeline with Play /
// Pause before it (js/analyzer-timeline.js).
//
// Frames come from VEXT.audioBus (the backend's analysis stream, or the Playground's in-page
// player); this file never fetches audio itself. It keeps a short history of each signal (so a
// new view starts with the last half second already in it) and feeds every view instance BOTH
// signals; the Signal selector only changes what is drawn.
//
// The views plug in through VEXT.AnalyzerViews (js/analyzer-dsp.js): Spectrum and Spectrogram
// are here, Correlation and Imager in js/analyzer-stereo.js. A view is created per pane:
//   create(pane, ctx) -> { feed(sig, frames, n, rate), draw(now), hover(x, y), reset(),
//                          snapshot(signals, w, h), destroy() }
//   pane: { el, canvas }  the shell sizes the canvas (device pixels) and clears it on a resize
//   ctx:  { dsp, params(), color(sig), signals(), dpr(), hasBefore(), theme() }
// The canvas's CSS `color` is the text colour; theme() is {version, font, line, grid, text, ...}.
// The shell draws the hover box, titles, badges and messages; a view draws inside its canvas.
//
// Drawing: requestAnimationFrame, throttled to the Refresh rate while sound arrives; stopped (and
// the picture frozen, "Frozen (stopped)") 300 ms after the frames stop; a manual Freeze ignores
// the frames altogether. A block that is silent on both signals is no frame at all (the sources
// keep sending zeros for a moment after the sound stops). Nothing is allocated per frame: typed arrays are reused, the
// spectrogram keeps its picture in a ring of columns on an offscreen canvas (one new column per
// hop, drawn in two pieces, so nothing is ever shifted).
(function () {
  const V = window.VEXT;
  if (!V || typeof V.registerSection !== 'function' || V.AnalyzerSection) return;
  const D = V.AnalyzerDSP;
  const Views = V.AnalyzerViews;
  const bus = V.audioBus;
  const K = V.AnalyzerKnob;
  if (!D || !Views || !bus || !K) { console.error('[analyzer] analyzer-dsp.js / audio-bus.js / analyzer-knob.js did not load'); return; }
  V.AnalyzerSection = true;

  const KEY = 'pa-analyzer';
  const SIGS = ['before', 'after'];
  const SIG_LABEL = { before: 'Before', after: 'After' };
  const SIG_BEFORE = ['before'];
  const SIG_AFTER = ['after'];
  const SIG_BOTH = ['before', 'after'];
  const SIG_NONE = [];
  const VIEW_ORDER = ['spectrum', 'spectrogram', 'correlation', 'imager'];
  const HAIRLINES = new Set(['spectrum', 'spectrogram']);
  const RING = 32768;               // frames each view keeps per signal (a power of two)
  const HIST_RING = 16384;          // frames the shell keeps to start a new view with
  const SLICE = 8192;               // the spectrogram takes new frames in pieces of at most this
  const MAX_COL_RATE = 60;          // spectrogram columns per second, at most (the picture is stretched)
  const STALL_MS = 300;             // no frames for this long = stopped
  const WATCH_MS = 100;
  const FLOOR = 1e-20;
  const MINUS = D.MINUS;
  const SETWIN_KEY = 'pa-analyzer-setwin';
  const SETWIN_EDGE = 8;            // px in from the views' top right corner (the default place)
  // The four knobs of the settings window: the parameter, its steps, the caption under it, its format.
  const KNOBS = [
    { key: 'freqLo', steps: K.STEPS.freqLo, label: 'Lo', aria: 'Frequency low (Hz)', fmt: K.fmtFreq },
    { key: 'freqHi', steps: K.STEPS.freqHi, label: 'Hi', aria: 'Frequency high (Hz)', fmt: K.fmtFreq },
    { key: 'rangeLo', steps: K.STEPS.range, label: 'Lo', aria: 'Range low (dB)', fmt: K.fmtDb },
    { key: 'rangeHi', steps: K.STEPS.range, label: 'Hi', aria: 'Range high (dB)', fmt: K.fmtDb },
  ];

  const clamp = (v, lo, hi) => (v < lo ? lo : v > hi ? hi : v);
  const esc = (v) => V.escHtml(v);
  const tauri = () => window.__TAURI__ || null;
  const errText = (e) => (e && e.message) || String(e);
  const winLabel = (id) => (D.WINDOWS.find((w) => w[0] === id) || D.WINDOWS[0])[1];

  // ======================================================================================
  // Spectrum
  // ======================================================================================
  // The newest `block` frames of each signal, windowed, FFT, power-averaged over the Avg time
  // (RMS meter) or held per bin and falling (Peak meter); a thin max-hold line per signal.
  // The FFT runs at the draw rate, on whatever the ring holds then, and only when new frames
  // arrived since the last one.

  const fminOf = (p) => Math.max(p.freqLo, 1);
  const fhiOf = (p) => Math.max(p.freqHi, fminOf(p) * 1.01);

  // Log frequency grid (1-2-5 per decade) and dB grid with labels, over a W x H device-pixel canvas.
  function drawSpectrumGrid(c2d, W, H, p, t, dpr) {
    const fmin = fminOf(p);
    const fhi = fhiOf(p);
    const line = Math.max(1, Math.round(dpr));
    const logR = Math.log(fhi / fmin);
    c2d.font = `${10.5 * dpr}px ${t.font}`;
    c2d.fillStyle = t.grid;
    const xs = D.freqTicks(fmin, fhi, p.freqLo).map((tk) => ({ ...tk, x: Math.round((Math.log(tk.f / fmin) / logR) * W) }));
    for (const tk of xs) c2d.fillRect(Math.min(W - line, tk.x), 0, line, H);
    const range = p.rangeHi - p.rangeLo;
    let step = 6;
    while (step < 48 && (H / dpr) * (step / range) < 14) step *= 2;
    const ys = [];
    for (let db = Math.ceil(p.rangeLo / step) * step; db <= p.rangeHi + 1e-9; db += step) {
      ys.push({ db, y: Math.round(((p.rangeHi - db) / range) * H) });
    }
    for (const tk of ys) c2d.fillRect(0, Math.min(H - line, tk.y), W, line);
    c2d.fillStyle = t.text;
    c2d.textBaseline = 'bottom';
    let right = -1e9;
    for (const tk of xs) {
      const w = c2d.measureText(tk.label).width;
      // A label that would run past the right edge (the 20k at the default top) ends at the edge instead.
      let x = tk.x + 3 * dpr;
      if (x + w > W) x = W - 2 * dpr - w;
      if (x < right + 6 * dpr) continue;   // still dropped when it would overlap the one before it
      c2d.fillText(tk.label, x, H - 3 * dpr);
      right = x + w;
    }
    for (const tk of ys) {
      if (tk.y > H - 16 * dpr) continue;
      if (tk.y < 12 * dpr) { c2d.textBaseline = 'top'; c2d.fillText(D.fmtDb(tk.db, 0), 4 * dpr, tk.y + 2 * dpr); }
      else { c2d.textBaseline = 'bottom'; c2d.fillText(D.fmtDb(tk.db, 0), 4 * dpr, tk.y - 2 * dpr); }
    }
  }

  // A small legend chip row ("Before ▬  After ▬") at the top left.
  function drawLegend(c2d, sigs, t, dpr) {
    c2d.font = `600 ${11.5 * dpr}px ${t.font}`;
    c2d.textBaseline = 'middle';
    let x = 40 * dpr;
    const y = 6 * dpr;
    const h = 19 * dpr;
    for (const sig of sigs) {
      const label = SIG_LABEL[sig];
      const w = c2d.measureText(label).width + 36 * dpr;
      c2d.fillStyle = t.chip;
      c2d.fillRect(x, y, w, h);
      c2d.fillStyle = t.line[sig];
      c2d.fillRect(x + 7 * dpr, y + h / 2 - dpr, 14 * dpr, 2 * dpr);
      c2d.fillText(label, x + 27 * dpr, y + h / 2 + 0.5 * dpr);
      x += w + 6 * dpr;
    }
  }

  function spectrumView(pane, ctx) {
    const c2d = pane.canvas.getContext('2d');
    const rings = { before: D.createRing(RING), after: D.createRing(RING) };
    let rate = 0;

    const newSpec = () => ({
      n: 0, rate: 0, fresh: true, have: false, last: 0, seen: -1, win: '', meter: '',
      pow: null, db: null, avg: null, pk: null, hold: null, curve: null, L: null, R: null, ch: null,
      map: null, mapW: 0, mapN: 0, mapRate: 0, mapFmin: 0, mapFhi: 0, px: null, hpx: null,
    });
    const st = { before: newSpec(), after: newSpec() };

    // The curve in dB for one signal's current state.
    function refreshCurve(s, p) {
      const h = s.curve.length;
      if (p.meter === 'rms') for (let k = 0; k < h; k++) s.curve[k] = 10 * Math.log10(s.avg[k] + FLOOR);
      else s.curve.set(s.pk);
    }

    // Takes the newest block of one signal into its averages (nothing happens without new frames).
    function update(sig, now) {
      const p = ctx.params();
      const r = rings[sig];
      const s = st[sig];
      if (!r.total || !rate) { s.have = false; return; }
      const n = p.block;
      const h = (n >> 1) + 1;
      if (s.n !== n) {
        s.n = n;
        s.pow = new Float64Array(h);
        s.db = new Float32Array(h);
        s.avg = new Float64Array(h);
        s.pk = new Float32Array(h);
        s.hold = new Float32Array(h);
        s.curve = new Float32Array(h);
        s.L = new Float32Array(n);
        s.R = new Float32Array(n);
        s.ch = [s.L, s.R];
        s.map = null;
        s.fresh = true;
        s.have = false;
      }
      if (s.rate !== rate) { s.rate = rate; s.map = null; s.fresh = true; }
      if (s.win !== p.window || s.meter !== p.meter) { s.win = p.window; s.meter = p.meter; s.fresh = true; }
      if (!s.fresh && s.have && s.seen === r.total) return;
      r.latest(n, s.L, s.R);
      D.power(s.ch, n, p.window, s.pow);
      const dt = Math.min(0.5, Math.max(0, (now - s.last) / 1000));
      s.last = now;
      if (s.fresh || !s.have) {
        for (let k = 0; k < h; k++) {
          const d = 10 * Math.log10(s.pow[k] + FLOOR);
          s.db[k] = d; s.avg[k] = s.pow[k]; s.pk[k] = d; s.hold[k] = d;
        }
        s.fresh = false;
      } else {
        const alpha = 1 - Math.exp(-dt / p.avgTime);
        const fall = ((p.rangeHi - p.rangeLo) / p.avgTime) * dt;
        for (let k = 0; k < h; k++) {
          const d = 10 * Math.log10(s.pow[k] + FLOOR);
          s.db[k] = d;
          s.avg[k] += (s.pow[k] - s.avg[k]) * alpha;
          const fallen = s.pk[k] - fall;
          s.pk[k] = d > fallen ? d : fallen;
          if (d > s.hold[k]) s.hold[k] = d;
        }
      }
      refreshCurve(s, p);
      s.seen = r.total;
      s.have = true;
    }

    // The curve and the hold line on the display columns of a canvas W wide: into `out`'s px / hpx.
    function fillPx(s, out, W, fmin, fhi) {
      if (!out.map || out.mapW !== W || out.mapN !== s.n || out.mapRate !== s.rate || out.mapFmin !== fmin || out.mapFhi !== fhi) {
        out.map = D.buildMap(W, s.n, s.rate, fmin, fhi);
        out.mapW = W; out.mapN = s.n; out.mapRate = s.rate; out.mapFmin = fmin; out.mapFhi = fhi;
      }
      if (!out.px || out.px.length !== W) { out.px = new Float32Array(W); out.hpx = new Float32Array(W); }
      D.applyMap(out.map, s.curve, out.px);
      D.applyMap(out.map, s.hold, out.hpx);
    }

    function drawTrace(g, sig, out, W, H, p, t, dpr) {
      const k = H / (p.rangeHi - p.rangeLo);
      const top = p.rangeHi;
      const px = out.px;
      const hpx = out.hpx;
      let first = -1;
      let last = -1;
      for (let x = 0; x < W; x++) if (!Number.isNaN(px[x])) { if (first < 0) first = x; last = x; }
      if (first < 0) return;
      g.lineJoin = 'round';
      // The max-hold trace, then the area, then the curve.
      g.strokeStyle = t.hold[sig];
      g.lineWidth = Math.max(1, dpr);
      g.beginPath();
      for (let x = first; x <= last; x++) {
        let y = (top - hpx[x]) * k;
        y = y < -1 ? -1 : y > H + 1 ? H + 1 : y;
        if (x === first) g.moveTo(x + 0.5, y); else g.lineTo(x + 0.5, y);
      }
      g.stroke();
      g.fillStyle = t.fill[sig];
      g.beginPath();
      g.moveTo(first + 0.5, H);
      for (let x = first; x <= last; x++) {
        let y = (top - px[x]) * k;
        y = y < -1 ? -1 : y > H + 1 ? H + 1 : y;
        g.lineTo(x + 0.5, y);
      }
      g.lineTo(last + 0.5, H);
      g.closePath();
      g.fill();
      g.strokeStyle = t.line[sig];
      g.lineWidth = 1.5 * dpr;
      g.beginPath();
      for (let x = first; x <= last; x++) {
        let y = (top - px[x]) * k;
        y = y < -1 ? -1 : y > H + 1 ? H + 1 : y;
        if (x === first) g.moveTo(x + 0.5, y); else g.lineTo(x + 0.5, y);
      }
      g.stroke();
    }

    // `outs`: where each signal's display columns live (the signal's own state when live, scratch for a snapshot).
    function paint(g, W, H, sigs, outs) {
      const p = ctx.params();
      const t = ctx.theme();
      const dpr = ctx.dpr();
      g.clearRect(0, 0, W, H);
      drawSpectrumGrid(g, W, H, p, t, dpr);
      const fmin = fminOf(p);
      const fhi = fhiOf(p);
      for (const sig of sigs) {
        const s = st[sig];
        if (!s.have) continue;
        fillPx(s, outs[sig], W, fmin, fhi);
        drawTrace(g, sig, outs[sig], W, H, p, t, dpr);
      }
      if (sigs.length > 1) drawLegend(g, sigs, t, dpr);
    }

    return {
      feed(sig, frames, n, r) {
        if (r !== rate) rate = r;
        rings[sig].push(frames, n);
      },
      draw(now) {
        const W = pane.canvas.width;
        const H = pane.canvas.height;
        if (!W || !H) return;
        const sigs = ctx.signals();
        for (const sig of sigs) update(sig, now);
        paint(c2d, W, H, sigs, st);
      },
      hover(x, y) {
        const w = pane.canvas.clientWidth;
        const h = pane.canvas.clientHeight;
        const sigs = ctx.signals();
        if (!w || !h || !rate || !sigs.length) return null;
        const p = ctx.params();
        const f = fminOf(p) * Math.pow(fhiOf(p) / fminOf(p), x / w);
        const note = D.noteOf(f);
        const lines = [note ? `${D.fmtHz(f)} · ${note}` : D.fmtHz(f)];
        lines.push(`cursor ${D.fmtDb(p.rangeHi - (y / h) * (p.rangeHi - p.rangeLo))} dB`);
        for (const sig of sigs) {
          const s = st[sig];
          let v = NaN;
          if (s.have && s.px) v = s.px[clamp(Math.floor((x / w) * s.px.length), 0, s.px.length - 1)];
          lines.push(`${SIG_LABEL[sig]} ${Number.isNaN(v) ? '—' : `${D.fmtDb(v)} dB`}`);
        }
        return lines;
      },
      reset() { for (const sig of SIGS) { st[sig].fresh = true; st[sig].have = false; } },
      snapshot(signals, w, h) {
        const c = document.createElement('canvas');
        c.width = w;
        c.height = h;
        const now = performance.now();
        const outs = {};
        for (const sig of signals) {
          outs[sig] = {};
          if (!st[sig].have) update(sig, now);   // never computed (it was not the one drawn): take its block now
        }
        paint(c.getContext('2d'), w, h, signals, outs);
        return c;
      },
      destroy() { rings.before = rings.after = null; },
    };
  }

  Views.register('spectrum', {
    label: 'Spectrum',
    settings: ['block', 'window', 'avgTime', 'meter', 'freq', 'range'],
    create: spectrumView,
  });

  // ======================================================================================
  // Spectrogram
  // ======================================================================================
  // Scrolling, newest at the right edge, `history` seconds across. Each hop of frames (history /
  // columns of audio) one column is made: the FFT of the block that ends there, mapped to rows on
  // the log frequency axis, kept as 0..255 (255 (dB - lo) / (hi - lo)) and as a pixel column of an
  // offscreen canvas that is a ring (the write position moves, nothing shifts). A draw puts the
  // ring on the pane in two pieces. Both signals are always computed (so Capture can use either).
  // A change that makes old columns wrong (block, window, frequency, range, history) starts over;
  // a resize keeps the picture, resampled.

  function spectrogramView(pane, ctx) {
    const c2d = pane.canvas.getContext('2d');
    const rings = { before: D.createRing(RING), after: D.createRing(RING) };
    let rate = 0;
    let themeVer = -1;
    const seen = { block: 0, window: '', freqLo: 0, freqHi: 0, rangeLo: 0, rangeHi: 0, history: 0 };
    const work = { n: 0, pow: null, db: null, L: null, R: null, ch: null };

    const newImg = () => ({
      cols: 0, rows: 0, u8: null, canvas: document.createElement('canvas'), c2d: null, head: 0, filled: 0,
      nextEnd: -1, colImg: null, col32: null, full: null, full32: null, rowv: null,
      map: null, mapBlock: 0, mapRows: 0, mapRate: 0, mapFmin: 0, mapFhi: 0,
    });
    const imgs = { before: newImg(), after: newImg() };

    function init(g, nc, nr) {
      g.cols = nc; g.rows = nr;
      g.u8 = new Uint8Array(nc * nr);
      g.canvas.width = nc;
      g.canvas.height = nr;
      g.c2d = g.canvas.getContext('2d');
      g.head = 0; g.filled = 0; g.nextEnd = -1;
      g.colImg = g.c2d.createImageData(1, nr);
      g.col32 = new Uint32Array(g.colImg.data.buffer);
      g.full = null; g.full32 = null;
      g.rowv = new Float32Array(nr);
      g.map = null;
    }

    // The whole picture again from the stored 0..255 values (a theme change, a resample).
    function repaint(g, lut) {
      if (!g.cols) return;
      if (!g.full || g.full.width !== g.cols || g.full.height !== g.rows) {
        g.full = g.c2d.createImageData(g.cols, g.rows);
        g.full32 = new Uint32Array(g.full.data.buffer);
      }
      const { cols, rows, u8, full32, head, filled } = g;
      for (let s = 0; s < cols; s++) {
        const valid = ((s - head + cols) % cols) >= cols - filled;
        const base = s * rows;
        for (let r = 0; r < rows; r++) full32[(rows - 1 - r) * cols + s] = valid ? lut[u8[base + r]] : 0;
      }
      g.c2d.putImageData(g.full, 0, 0);
    }

    // To a new size, keeping the picture: nearest column / row, in time order.
    function resample(g, nc, nr, lut) {
      const oc = g.cols;
      const or = g.rows;
      const old = g.u8;
      const oldHead = g.head;
      const oldFilled = g.filled;
      const nextEnd = g.nextEnd;
      init(g, nc, nr);
      g.nextEnd = nextEnd;
      let first = nc;
      for (let c = 0; c < nc; c++) {
        const oi = Math.min(oc - 1, Math.floor((c * oc) / nc));   // old linear index (0 = oldest slot)
        if (oi < oc - oldFilled) continue;
        if (c < first) first = c;
        const src = ((oldHead + oi) % oc) * or;
        const dst = c * nr;
        for (let r = 0; r < nr; r++) g.u8[dst + r] = old[src + Math.min(or - 1, Math.floor((r * or) / nr))];
      }
      g.filled = nc - first;
      g.head = 0;
      repaint(g, lut);
    }

    function clearAll() {
      for (const sig of SIGS) {
        const g = imgs[sig];
        if (!g.cols) continue;
        g.u8.fill(0);
        g.c2d.clearRect(0, 0, g.cols, g.rows);
        g.head = 0; g.filled = 0; g.nextEnd = -1;
      }
    }

    // Keeps the pictures matching the pane and the settings; false while the pane has no size yet.
    function ensureDims(p, t) {
      const W = pane.canvas.width;
      const H = pane.canvas.height;
      if (!W || !H) return false;
      if (seen.block !== p.block || seen.window !== p.window || seen.freqLo !== p.freqLo || seen.freqHi !== p.freqHi
        || seen.rangeLo !== p.rangeLo || seen.rangeHi !== p.rangeHi || seen.history !== p.history) {
        seen.block = p.block; seen.window = p.window; seen.freqLo = p.freqLo; seen.freqHi = p.freqHi;
        seen.rangeLo = p.rangeLo; seen.rangeHi = p.rangeHi; seen.history = p.history;
        clearAll();
      }
      const nc = clamp(Math.round(Math.min(W, p.history * MAX_COL_RATE)), 32, 4096);
      const nr = clamp(Math.round(H / Math.max(1, ctx.signals().length)), 16, 1024);
      for (const sig of SIGS) {
        const g = imgs[sig];
        if (g.cols === nc && g.rows === nr) continue;
        if (!g.cols) init(g, nc, nr);
        else resample(g, nc, nr, t.lut);
      }
      return true;
    }

    function ensureWork(n) {
      if (work.n === n) return;
      work.n = n;
      work.pow = new Float64Array((n >> 1) + 1);
      work.db = new Float32Array((n >> 1) + 1);
      work.L = new Float32Array(n);
      work.R = new Float32Array(n);
      work.ch = [work.L, work.R];
    }

    // One column: the block of `sig` that ends at frame `end`.
    function column(g, sig, end, p, lut) {
      const block = p.block;
      ensureWork(block);
      rings[sig].block(end, block, work.L, work.R);
      D.power(work.ch, block, p.window, work.pow);
      const h = (block >> 1) + 1;
      for (let k = 0; k < h; k++) work.db[k] = 10 * Math.log10(work.pow[k] + FLOOR);
      const fmin = fminOf(p);
      const fhi = fhiOf(p);
      if (!g.map || g.mapBlock !== block || g.mapRows !== g.rows || g.mapRate !== rate || g.mapFmin !== fmin || g.mapFhi !== fhi) {
        g.map = D.buildMap(g.rows, block, rate, fmin, fhi);
        g.mapBlock = block; g.mapRows = g.rows; g.mapRate = rate; g.mapFmin = fmin; g.mapFhi = fhi;
      }
      D.applyMap(g.map, work.db, g.rowv);
      const { rows, rowv, u8, col32 } = g;
      const lo = p.rangeLo;
      const k = 255 / (p.rangeHi - p.rangeLo);
      const slot = g.head;
      const base = slot * rows;
      for (let r = 0; r < rows; r++) {
        const v = rowv[r];
        let q = v !== v ? 0 : Math.round((v - lo) * k);   // above the Nyquist (NaN) and under the range = the floor
        q = q < 0 ? 0 : q > 255 ? 255 : q;
        u8[base + r] = q;
        col32[rows - 1 - r] = lut[q];
      }
      g.c2d.putImageData(g.colImg, slot, 0);
      g.head = slot + 1 === g.cols ? 0 : slot + 1;
      if (g.filled < g.cols) g.filled++;
    }

    const bandOf = (i, n, H) => {
      const y0 = Math.round((i * H) / n);
      return [y0, Math.round(((i + 1) * H) / n) - y0];
    };

    // One signal's picture into the band y0..y0+h of a canvas W wide, the quiet floor behind it.
    function drawBand(g2, g, W, y0, h, t) {
      g2.fillStyle = t.floor0;
      g2.fillRect(0, y0, W, h);
      if (!g.filled) return;
      const cols = g.cols;
      const f = g.filled;
      const s0 = (g.head - f + cols) % cols;        // slot of the oldest written column
      const a = Math.min(f, cols - s0);             // columns before the ring wraps
      const unit = W / cols;
      const xs = (cols - f) * unit;                 // where the oldest one lands
      g2.imageSmoothingEnabled = true;
      g2.drawImage(g.canvas, s0, 0, a, g.rows, xs, y0, a * unit, h);
      if (f > a) g2.drawImage(g.canvas, 0, 0, f - a, g.rows, xs + a * unit, y0, (f - a) * unit, h);
    }

    // Frequency lines and labels on the left edge of a band, and the band's name when there are two.
    function drawBandGrid(g2, W, y0, h, p, t, dpr, name) {
      const fmin = fminOf(p);
      const fhi = fhiOf(p);
      const logR = Math.log(fhi / fmin);
      const line = Math.max(1, Math.round(dpr));
      g2.font = `${10.5 * dpr}px ${t.font}`;
      g2.textBaseline = 'middle';
      let lastY = 1e9;
      for (const tk of D.freqTicks(fmin, fhi, p.freqLo)) {
        const y = y0 + Math.round(h - (Math.log(tk.f / fmin) / logR) * h);
        g2.fillStyle = t.grid;
        g2.fillRect(0, clamp(y, y0, y0 + h - line), W, line);
        if (Math.abs(lastY - y) < 14 * dpr) continue;
        lastY = y;
        // A label at the very top or bottom edge is moved inside the picture, not dropped.
        const ly = Math.min(y0 + h - 7 * dpr, Math.max(y0 + 7 * dpr, y));
        const w = g2.measureText(tk.label).width;
        g2.fillStyle = t.floor;
        g2.fillRect(2 * dpr, ly - 7 * dpr, w + 6 * dpr, 14 * dpr);
        g2.fillStyle = t.text;
        g2.fillText(tk.label, 5 * dpr, ly);
      }
      if (name) {
        g2.font = `600 ${11.5 * dpr}px ${t.font}`;
        const w = g2.measureText(name).width;
        g2.fillStyle = t.chip;
        g2.fillRect(40 * dpr, y0 + 5 * dpr, w + 36 * dpr, 19 * dpr);
        g2.fillStyle = t.line[name === SIG_LABEL.before ? 'before' : 'after'];
        g2.fillRect(47 * dpr, y0 + 14 * dpr, 14 * dpr, 2 * dpr);
        g2.fillText(name, 67 * dpr, y0 + 15 * dpr);
      }
    }

    function paintAll(g2, W, H, sigs) {
      const p = ctx.params();
      const t = ctx.theme();
      const dpr = ctx.dpr();
      g2.clearRect(0, 0, W, H);
      const n = Math.max(1, sigs.length);
      sigs.forEach((sig, i) => {
        const [y0, h] = bandOf(i, n, H);
        drawBand(g2, imgs[sig], W, y0, h, t);
        drawBandGrid(g2, W, y0, h, p, t, dpr, sigs.length > 1 ? SIG_LABEL[sig] : '');
      });
    }

    return {
      feed(sig, frames, n, r) {
        const p = ctx.params();
        const t = ctx.theme();
        if (r !== rate) { rate = r; clearAll(); }
        const g = imgs[sig];
        const ring = rings[sig];
        if (!ensureDims(p, t) || !g.cols) { ring.push(frames, n); return; }
        if (t.version !== themeVer) { themeVer = t.version; for (const s of SIGS) repaint(imgs[s], t.lut); }
        const hop = Math.max(64, (p.history * rate) / g.cols);
        if (g.nextEnd < 0 || g.nextEnd > ring.total + n + hop) g.nextEnd = ring.total + hop;
        for (let off = 0; off < n; off += SLICE) {
          const m = Math.min(SLICE, n - off);
          ring.push(frames, m, off);
          // Fell far behind (a stalled page): jump to the present instead of making hundreds of columns.
          if (ring.total - g.nextEnd > g.cols * hop) g.nextEnd = ring.total - hop;
          while (g.nextEnd <= ring.total) {
            column(g, sig, Math.floor(g.nextEnd), p, t.lut);
            g.nextEnd += hop;
          }
        }
      },
      draw() {
        const p = ctx.params();
        const t = ctx.theme();
        if (!ensureDims(p, t)) return;
        if (t.version !== themeVer) { themeVer = t.version; for (const s of SIGS) repaint(imgs[s], t.lut); }
        paintAll(c2d, pane.canvas.width, pane.canvas.height, ctx.signals());
      },
      hover(x, y) {
        const w = pane.canvas.clientWidth;
        const h = pane.canvas.clientHeight;
        const sigs = ctx.signals();
        if (!w || !h || !sigs.length || !rate) return null;
        const p = ctx.params();
        const n = sigs.length;
        const i = Math.min(n - 1, Math.floor((y / h) * n));
        const sig = sigs[i];
        const g = imgs[sig];
        const bandH = h / n;
        const fy = clamp(1 - (y - i * bandH) / bandH, 0, 1);
        const f = fminOf(p) * Math.pow(fhiOf(p) / fminOf(p), fy);
        const ago = (1 - x / w) * p.history;
        let text = `${ago < 0.05 ? 'now' : `${MINUS}${ago.toFixed(1)} s`} · ${D.fmtHz(f)}`;
        if (g.cols) {
          const ci = clamp(Math.floor((x / w) * g.cols), 0, g.cols - 1);   // 0 = the oldest column
          if (ci >= g.cols - g.filled) {
            const q = g.u8[((g.head + ci) % g.cols) * g.rows + clamp(Math.floor(fy * g.rows), 0, g.rows - 1)];
            text += q === 0 ? ` · ${D.fmtDb(p.rangeLo)} dB (floor)` : ` · ${D.fmtDb(p.rangeLo + (q / 255) * (p.rangeHi - p.rangeLo))} dB`;
          } else text += ' · no data yet';
        }
        return n > 1 ? [SIG_LABEL[sig], text] : [text];
      },
      reset() { clearAll(); },
      snapshot(signals, w, h) {
        const c = document.createElement('canvas');
        c.width = w;
        c.height = h;
        paintAll(c.getContext('2d'), w, h, signals);
        return c;
      },
      destroy() { rings.before = rings.after = null; imgs.before = imgs.after = null; },
    };
  }

  Views.register('spectrogram', {
    label: 'Spectrogram',
    settings: ['block', 'window', 'freq', 'range', 'history'],
    create: spectrogramView,
  });

  // ======================================================================================
  // The section
  // ======================================================================================

  let params = (() => {
    try { return D.normalize(JSON.parse(localStorage.getItem(KEY) || '{}')); } catch { return D.normalize({}); }
  })();
  const saveParams = () => { try { localStorage.setItem(KEY, JSON.stringify(params)); } catch { /* storage unavailable */ } };

  let shown = false;
  let rootEl = null;
  let els = null;
  let panes = [];
  let paneState = { view: '', split: null };
  let meterKey = '';
  let meters = [];                  // [{sig, el, canvas, c2d, val, clips}]
  let theme = null;
  let themeVersion = 0;
  let dpr = window.devicePixelRatio || 1;
  let manualFreeze = false;
  let autoFrozen = false;
  let everFed = false;
  let lastFrameAt = -1e9;
  let lastFlags = 1;
  let lastRate = 0;
  let lastRun = '';                 // the run key of the frames being shown (audio-bus: a new run = a new source)
  let pageSame = false;             // the page's Before and After are the same samples (no plugin between them)
  let lastSource = '';
  let raf = 0;
  let lastDraw = 0;
  let dirty = true;
  let watchTimer = 0;
  let hover = null;                 // {pane, x, y} css px in the pane
  let ro = null;
  let timeline = null;   // the strip on top (js/analyzer-timeline.js)
  let knobs = [];        // [{key, k}]: the Freq and Range knobs of the settings window
  // The settings window's state: open, and its place (x, y relative to the section; null = the default).
  let setwin = (() => {
    try {
      const s = JSON.parse(localStorage.getItem(SETWIN_KEY) || 'null');
      if (s && typeof s === 'object') return { open: !!s.open, x: Number.isFinite(s.x) ? s.x : null, y: Number.isFinite(s.y) ? s.y : null };
    } catch { /* nothing stored, or not readable */ }
    return { open: false, x: null, y: null };
  })();
  let setDrag = null;    // {id, dx, dy} while the header is dragged
  let themeObs = null;
  let mq = null;
  let unStatus = null;
  const hist = { before: D.createRing(HIST_RING), after: D.createRing(HIST_RING) };
  const tmp = { l: new Float32Array(HIST_RING), r: new Float32Array(HIST_RING), il: new Float32Array(HIST_RING * 2) };
  // The Peak meter's true peak, one stream per signal and channel (see analyzer-dsp.js truePeakStream).
  const tpOf = {
    before: [D.truePeakStream(), D.truePeakStream()],
    after: [D.truePeakStream(), D.truePeakStream()],
  };
  const resetTruePeak = () => { for (const sig of SIGS) for (const t of tpOf[sig]) t.reset(); };

  const frozen = () => manualFreeze || autoFrozen;
  const hasBefore = () => (lastFlags & 1) !== 0;
  const registeredViews = () => {
    const have = Views.list().map((e) => e.name);
    return [...VIEW_ORDER.filter((n) => have.includes(n)), ...have.filter((n) => !VIEW_ORDER.includes(n))];
  };
  // The view in use: the stored one when it is registered, else the first that is (Spectrum).
  const activeView = () => (Views.get(params.view) ? params.view : (registeredViews()[0] || 'spectrum'));
  const wantSplit = () => params.layout === 'split' && params.signal === 'both' && hasBefore();
  const drawnSignals = () => {
    if (params.signal === 'before') return hasBefore() ? SIG_BEFORE : SIG_NONE;
    if (params.signal === 'after') return SIG_AFTER;
    return hasBefore() ? SIG_BOTH : SIG_AFTER;
  };

  // ---------- colours ----------

  function readTheme() {
    const probe = (cls, prop) => {
      const el = els.probes.querySelector(cls);
      return D.rgbaOf(D.colorOf(getComputedStyle(el)[prop], 'rgba(128, 128, 128, 1)'));
    };
    const before = probe('.an-probe-before', 'color');
    const after = probe('.an-probe-after', 'color');
    const bg = probe('.an-probe-bg', 'color');
    const danger = probe('.an-probe-danger', 'color');
    const ok = probe('.an-probe-ok', 'color');
    const warn = probe('.an-probe-warn', 'color');
    const gridEl = getComputedStyle(els.probes.querySelector('.an-probe-grid'));
    const cs = getComputedStyle(els.root);
    const ramp = ['--an-c0', '--an-c1', '--an-c2', '--an-c3', '--an-c4'];
    const fb = ['rgba(242, 244, 243, 1)', 'rgba(223, 230, 245, 1)', 'rgba(138, 123, 216, 1)', 'rgba(224, 112, 43, 1)', 'rgba(58, 21, 0, 1)'];
    const stops = ramp.map((n, i) => D.rgbaOf(D.colorOf(cs.getPropertyValue(n).trim(), fb[i])));
    theme = {
      version: ++themeVersion,
      font: gridEl.fontFamily || 'sans-serif',
      line: { before: D.rgba(before, 1), after: D.rgba(after, 1) },
      fill: { before: D.rgba(before, 0.18), after: D.rgba(after, 0.18) },
      hold: { before: D.rgba(before, 0.55), after: D.rgba(after, 0.55) },
      solid: { before, after },
      grid: D.colorOf(gridEl.outlineColor, 'rgba(128, 128, 128, 0.25)'),
      text: D.colorOf(gridEl.columnRuleColor, 'rgba(128, 128, 128, 1)'),
      floor: D.rgba(stops[0], 0.62),
      floor0: D.rgba(stops[0], 1),
      chip: D.rgba(bg, 0.72),
      bg: D.rgba(bg, 1),
      danger: D.rgba(danger, 1),
      ok: D.rgba(ok, 1),
      warn: D.rgba(warn, 1),
      lut: D.buildLut(stops),
    };
    return theme;
  }
  const getTheme = () => theme || readTheme();

  // ---------- the DOM ----------

  const CHEV = '<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M4 6.2l4 4 4-4"/></svg>';
  const CAM = '<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M2.4 5.6c0-.6.4-1 1-1h1.9l1-1.6h3.4l1 1.6h1.9c.6 0 1 .4 1 1v6.4c0 .6-.4 1-1 1H3.4c-.6 0-1-.4-1-1z"/><circle cx="8" cy="8.6" r="2.3"/></svg>';
  const PLAY = '<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true"><path fill="currentColor" d="M5 2.9v10.2a.6.6 0 0 0 .92.5l8-5.1a.6.6 0 0 0 0-1l-8-5.1A.6.6 0 0 0 5 2.9z"/></svg>';
  const GEAR = '<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="8" r="2.1"/><path d="M8 1.8v1.6M8 12.6v1.6M1.8 8h1.6M12.6 8h1.6M3.6 3.6l1.15 1.15M11.25 11.25l1.15 1.15M3.6 12.4l1.15-1.15M11.25 4.75l1.15-1.15"/></svg>';
  const PAUSE = '<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true"><rect fill="currentColor" x="3.6" y="2.8" width="3" height="10.4" rx="1"/><rect fill="currentColor" x="9.4" y="2.8" width="3" height="10.4" rx="1"/></svg>';

  const segHtml = (key, label, items) => `
    <div class="segmented an-seg" role="group" aria-label="${label}" data-seg="${key}">${items.map(([val, text]) =>
    `<button type="button" data-val="${val}" aria-pressed="false">${text}</button>`).join('')}</div>`;

  function shellHtml() {
    return `
      <div class="an" id="an-root">
        <div class="an-timeline"></div>
        <div class="an-bar" role="toolbar" aria-label="Analyzer controls">
          <div class="an-seg-view" data-views></div>
          <div class="an-field"><span class="an-label">Signal</span>${segHtml('signal', 'Signal', [['before', 'Before'], ['after', 'After'], ['both', 'Both']])}</div>
          <div class="an-field"><span class="an-label">Layout</span>${segHtml('layout', 'Layout', [['overlay', 'One window'], ['split', 'Split']])}</div>
          <span class="an-spacer"></span>
          <button type="button" class="an-btn an-setbtn" data-act="settings" aria-pressed="false" aria-expanded="false" aria-controls="an-setwin"
            title="Show or hide the view's settings (block, window, averaging, frequency and level range…)">${GEAR}<span>Settings</span></button>
          <button type="button" class="an-btn" data-act="freeze" aria-pressed="false" title="Hold the picture as it is (Space still plays and pauses the Playground's sound)">Freeze</button>
          <button type="button" class="an-btn" data-act="reset" title="Clear the averages, the history, the hold lines and the meters' maxima">Reset</button>
          <div class="an-cap">
            <button type="button" class="an-btn an-capbtn" data-act="capture" aria-haspopup="menu" aria-expanded="false"
              title="Save a picture of what is shown">${CAM}<span>Capture</span>${CHEV}</button>
            <div class="an-capmenu" role="menu" aria-label="Capture and save" hidden>
              <button type="button" role="menuitem" data-act="capture-before">Before</button>
              <button type="button" role="menuitem" data-act="capture-after">After</button>
              <button type="button" role="menuitem" data-act="capture-both">Both</button>
            </div>
          </div>
        </div>
        <div class="an-source">
          <span class="an-dot" aria-hidden="true"></span>
          <span class="an-source-text"></span>
          <span class="an-source-note"></span>
          <span class="an-spacer"></span>
          <button type="button" class="an-btn an-transport" data-act="transport"></button>
        </div>
        <div class="an-main">
          <div class="an-vwrap"><div class="an-views"></div></div>
          <div class="an-meters" role="group" aria-label="Level meters"></div>
        </div>
        <div class="an-setwin" id="an-setwin" role="dialog" aria-modal="false" aria-labelledby="an-setwin-title" hidden>
          <div class="an-setwin-head">
            <span class="an-setwin-title" id="an-setwin-title">Analyzer settings</span>
            <span class="an-setwin-view"></span>
            <span class="an-spacer"></span>
            <button type="button" class="an-setwin-x" data-act="settings-close" aria-label="Close the settings" title="Close">&times;</button>
          </div>
          <div class="an-settings" role="group" aria-label="Analyzer settings"></div>
        </div>
        <div class="an-probes" aria-hidden="true">
          <i class="an-probe-before"></i><i class="an-probe-after"></i><i class="an-probe-bg"></i>
          <i class="an-probe-danger"></i><i class="an-probe-ok"></i><i class="an-probe-warn"></i><i class="an-probe-grid"></i>
        </div>
      </div>`;
  }

  // ---------- settings row ----------

  const field = (id, label, control) => `<div class="an-field"><label class="an-label" for="${id}">${label}</label>${control}</div>`;
  const selectHtml = (key, label, options, value) => `
    <select class="an-sel" id="an-set-${key}" data-key="${key}" aria-label="${label}">${options.map(([v, text]) =>
    `<option value="${v}"${String(v) === String(value) ? ' selected' : ''}>${text}</option>`).join('')}</select>`;
  // The view's fields, and its Freq and Range knobs in one row (a knob is mounted into its placeholder).
  function renderSettings() {
    destroyKnobs();
    const v = Views.get(activeView());
    const want = new Set((v && v.settings) || []);
    if (want.has('freqLo') || want.has('freqHi')) want.add('freq');
    els.setwinView.textContent = v ? `· ${v.label}` : '';
    const avgOpts = D.AVG_TIMES.includes(params.avgTime) ? D.AVG_TIMES : [...D.AVG_TIMES, params.avgTime].sort((a, b) => a - b);
    const parts = [];
    if (want.has('block')) parts.push(field('an-set-block', 'Block', selectHtml('block', 'Block size (samples)', D.BLOCKS.map((b) => [b, b]), params.block)));
    if (want.has('window')) parts.push(field('an-set-window', 'Window', selectHtml('window', 'Window function', D.WINDOWS, params.window)));
    if (want.has('avgTime')) parts.push(field('an-set-avgTime', 'Avg', selectHtml('avgTime', 'Averaging time', avgOpts.map((a) => [a, `${a} s`]), params.avgTime)));
    parts.push(`<div class="an-field"><span class="an-label">Meter</span>${segHtml('meter', 'Meter: RMS or peak', [['rms', 'RMS'], ['peak', 'Peak']])}</div>`);
    parts.push(field('an-set-fps', 'Refresh', selectHtml('fps', 'Refresh rate', D.FPS.map((f) => [f, `${f} fps`]), params.fps)));
    const groups = [];
    if (want.has('freq')) groups.push(['Freq · Hz', ['freqLo', 'freqHi']]);
    if (want.has('range')) groups.push(['Range · dB', ['rangeLo', 'rangeHi']]);
    if (groups.length) {
      parts.push(`<div class="an-knobrow">${groups.map(([cap, keys]) => `
        <div class="an-knobgrp" role="group" aria-label="${cap}"><span class="an-label">${cap}</span><div class="an-knobs">${keys.map((key) => `<span data-knob="${key}"></span>`).join('')}</div></div>`).join('')}</div>`);
    }
    if (want.has('history')) parts.push(field('an-set-history', 'History', selectHtml('history', 'History (seconds)', D.HISTORIES.map((h) => [h, `${h} s`]), params.history)));
    els.settings.innerHTML = parts.join('');
    for (const def of KNOBS) {
      const holder = els.settings.querySelector(`[data-knob="${def.key}"]`);
      if (!holder) continue;
      const k = K.create({
        label: def.label, ariaLabel: def.aria, values: def.steps, value: params[def.key],
        min: D.LIMITS[def.key][0], max: D.LIMITS[def.key][1], format: def.fmt,
        // setParam keeps the Lo / Hi gap; syncControls shows the value that was kept.
        onChange: (val) => setParam({ [def.key]: val }),
      });
      holder.replaceWith(k.el);
      knobs.push({ key: def.key, k });
    }
    syncControls();
  }

  function destroyKnobs() {
    for (const { k } of knobs) k.destroy();
    knobs = [];
  }

  // The controls show the (normalized) parameters.
  function syncControls() {
    const view = activeView();
    for (const seg of els.root.querySelectorAll('[data-seg]')) {
      const key = seg.dataset.seg;
      for (const b of seg.querySelectorAll('button')) {
        const on = b.dataset.val === params[key];
        b.setAttribute('aria-pressed', String(on));
        b.classList.toggle('active', on);
      }
    }
    for (const b of els.viewSeg.querySelectorAll('button')) {
      const on = b.dataset.val === view;
      b.setAttribute('aria-pressed', String(on));
      b.classList.toggle('active', on);
    }
    for (const c of els.settings.querySelectorAll('[data-key]')) {
      const val = String(params[c.dataset.key]);
      if (c.value !== val && document.activeElement !== c) c.value = val;
    }
    for (const { key, k } of knobs) k.set(params[key]);
    const split = els.root.querySelector('[data-seg="layout"]');
    const can = params.signal === 'both' && hasBefore();
    for (const b of split.querySelectorAll('button')) {
      b.disabled = !can && b.dataset.val === 'split';
      b.title = b.disabled ? (hasBefore() ? 'Split needs Signal = Both' : 'An instrument has no input: there is nothing to put beside the output') : '';
    }
    els.freeze.setAttribute('aria-pressed', String(manualFreeze));
    els.freeze.classList.toggle('is-on', manualFreeze);
  }

  function renderViewSwitch() {
    const names = registeredViews();
    els.viewSeg.innerHTML = `<div class="segmented an-seg" role="group" aria-label="View" data-seg="view">${names.map((n) => {
      const def = Views.get(n);
      return `<button type="button" data-val="${n}" aria-pressed="false">${esc((def && def.label) || n)}</button>`;
    }).join('')}</div>`;
  }

  // ---------- the settings window: a floating panel over the section ----------
  // Not modal (the views stay live behind it) and not closed by outside clicks: a tool window.

  // Where the panel sits when nothing is stored yet: the views' top right corner, a little in.
  function defaultSetwin() {
    const root = els.root;
    const rr = root.getBoundingClientRect();
    const vr = els.views.getBoundingClientRect();
    return {
      x: vr.right - rr.left + root.scrollLeft - els.setwin.offsetWidth - SETWIN_EDGE,
      y: vr.top - rr.top + root.scrollTop + SETWIN_EDGE,
    };
  }

  // Kept inside the section's box, as the section is scrolled and sized right now.
  function clampSetwin(x, y) {
    const root = els.root;
    const minX = root.scrollLeft;
    const minY = root.scrollTop;
    const maxX = Math.max(minX, root.scrollLeft + root.clientWidth - els.setwin.offsetWidth);
    const maxY = Math.max(minY, root.scrollTop + root.clientHeight - els.setwin.offsetHeight);
    return { x: clamp(x, minX, maxX), y: clamp(y, minY, maxY) };
  }

  function placeSetwin() {
    const at = setwin.x === null ? defaultSetwin() : setwin;
    const c = clampSetwin(at.x, at.y);
    els.setwin.style.left = `${c.x}px`;
    els.setwin.style.top = `${c.y}px`;
  }

  // Shows or hides the panel, and the Settings button that says so. Focus inside a panel that
  // closes goes back to the button.
  function showSetwin(open) {
    const inside = els.setwin.contains(document.activeElement);
    setwin.open = open;
    els.setwin.hidden = !open;
    els.setbtn.setAttribute('aria-pressed', String(open));
    els.setbtn.setAttribute('aria-expanded', String(open));
    if (open) placeSetwin();
    else if (inside) els.setbtn.focus();
  }

  function setSettingsOpen(open) {
    showSetwin(open);
    try { localStorage.setItem(SETWIN_KEY, JSON.stringify(setwin)); } catch { /* storage unavailable */ }
  }

  // The header drags the panel (a button in it keeps its own click).
  function onSetwinDown(e) {
    if (e.button !== 0 || e.target.closest('button')) return;
    const r = els.setwin.getBoundingClientRect();
    setDrag = { id: e.pointerId, dx: e.clientX - r.left, dy: e.clientY - r.top };
    try { els.setwinHead.setPointerCapture(e.pointerId); } catch { /* released already */ }
    e.preventDefault();
  }

  function onSetwinMove(e) {
    if (!setDrag || setDrag.id !== e.pointerId) return;
    const root = els.root;
    const rr = root.getBoundingClientRect();
    const c = clampSetwin(e.clientX - rr.left - setDrag.dx + root.scrollLeft, e.clientY - rr.top - setDrag.dy + root.scrollTop);
    setwin.x = c.x;
    setwin.y = c.y;
    placeSetwin();
  }

  function onSetwinUp(e) {
    if (!setDrag || setDrag.id !== e.pointerId) return;
    setDrag = null;
    try { localStorage.setItem(SETWIN_KEY, JSON.stringify(setwin)); } catch { /* storage unavailable */ }
  }

  // A window resize keeps the panel inside the section.
  function onResize() {
    if (setwin.open && els) placeSetwin();
  }

  function setParam(partial) {
    const prev = params;
    params = D.normalize(partial, prev);
    saveParams();
    if (prev.view !== params.view) renderSettings();
    else syncControls();
    // The meter rows follow the signal shown; their state stays. A new meter (RMS or Peak) measures
    // something else, so its maxima and lights start again.
    if (prev.signal !== params.signal) meterKey = '';
    if (prev.meter !== params.meter) { meterKey = ''; resetMeters(); resetTruePeak(); }
    dirty = true;
    renderSource();
    kick();
  }

  // ---------- panes (the views' homes) ----------

  function destroyPanes() {
    for (const p of panes) {
      try { if (p.view) p.view.destroy(); } catch (e) { console.error('[analyzer] view destroy failed:', e); }
    }
    panes = [];
    hover = null;
  }

  const paneSignals = (id) => (id === 'before' ? SIG_BEFORE : id === 'after' ? SIG_AFTER : drawnSignals());

  function makeCtx(id) {
    return {
      dsp: D,
      params: () => params,
      color: (sig) => getTheme().line[sig] || getTheme().line.after,
      signals: () => paneSignals(id),
      dpr: () => dpr,
      hasBefore,
      theme: getTheme,
    };
  }

  // Feeds a new view the last stretch of both signals, so it starts with something to show.
  function replay(view) {
    if (!lastRate) return;
    for (const sig of SIGS) {
      const r = hist[sig];
      const n = Math.min(r.total, HIST_RING);
      if (!n || (sig === 'before' && !hasBefore())) continue;
      r.latest(n, tmp.l, tmp.r);
      for (let i = 0, k = 0; i < n; i++, k += 2) { tmp.il[k] = tmp.l[i]; tmp.il[k + 1] = tmp.r[i]; }
      for (let off = 0; off < n; off += 4096) {
        const m = Math.min(4096, n - off);
        try { view.feed(sig, tmp.il.subarray(off * 2, (off + m) * 2), m, lastRate); } catch (e) { console.error('[analyzer] replay failed:', e); }
      }
    }
  }

  function ensurePanes() {
    const view = activeView();
    const split = wantSplit();
    if (view === paneState.view && split === paneState.split) return;
    paneState = { view, split };
    destroyPanes();
    const ids = split ? SIGS : ['main'];
    els.views.classList.toggle('is-split', split);
    els.views.innerHTML = ids.map((id) => `
      <div class="an-pane" data-pane="${id}" data-view="${view}">
        <canvas class="an-canvas" aria-hidden="true"></canvas>
        <div class="an-hx" hidden></div><div class="an-hy" hidden></div>
        <div class="an-side"><div class="an-title" hidden></div><div class="an-badge" hidden></div></div>
        <div class="an-hover" hidden></div>
        <div class="an-msg" hidden></div>
      </div>`).join('');
    const def = Views.get(view);
    panes = ids.map((id, i) => {
      const el = els.views.children[i];
      const p = {
        id, el, canvas: el.querySelector('canvas'), hx: el.querySelector('.an-hx'), hy: el.querySelector('.an-hy'),
        hoverEl: el.querySelector('.an-hover'), titleEl: el.querySelector('.an-title'), badgeEl: el.querySelector('.an-badge'),
        msgEl: el.querySelector('.an-msg'), view: null, hairlines: HAIRLINES.has(view), shown: { title: '', badge: '', msg: '', hover: '' },
      };
      el.addEventListener('pointermove', (e) => {
        const r = el.getBoundingClientRect();
        hover = { pane: p, x: e.clientX - r.left, y: e.clientY - r.top };
        updateHover(p);
      });
      el.addEventListener('pointerleave', () => { if (hover && hover.pane === p) { hover = null; hideHover(p); } });
      return p;
    });
    sizePanes();
    for (const p of panes) {
      try {
        p.view = def.create({ el: p.el, canvas: p.canvas }, makeCtx(p.id));
        replay(p.view);
      } catch (e) {
        console.error(`[analyzer] the ${view} view failed to start:`, e);
        p.view = null;
      }
    }
    dirty = true;
  }

  // Canvas pixels follow the pane's CSS size (resizing clears a canvas: the next draw is forced).
  function sizePanes() {
    dpr = window.devicePixelRatio || 1;
    let changed = false;
    for (const p of panes) {
      const w = Math.max(1, Math.round(p.el.clientWidth * dpr));
      const h = Math.max(1, Math.round(p.el.clientHeight * dpr));
      if (p.canvas.width !== w || p.canvas.height !== h) { p.canvas.width = w; p.canvas.height = h; changed = true; }
    }
    return changed;
  }

  // ---------- hover ----------

  function hideHover(p) {
    p.hx.hidden = true;
    p.hy.hidden = true;
    p.hoverEl.hidden = true;
  }

  function updateHover(p) {
    const h = hover;
    if (!h || h.pane !== p || !p.view) { hideHover(p); return; }
    let lines = null;
    try { lines = p.view.hover(h.x, h.y); } catch (e) { console.error('[analyzer] hover failed:', e); }
    if (!lines || !lines.length) { hideHover(p); return; }
    const text = lines.join('\n');
    p.hoverEl.hidden = false;
    p.hx.hidden = !p.hairlines;
    p.hy.hidden = !p.hairlines;
    if (p.hairlines) { p.hx.style.left = `${h.x}px`; p.hy.style.top = `${h.y}px`; }
    if (p.shown.hover !== text) { p.shown.hover = text; p.hoverEl.textContent = text; }
    const w = p.el.clientWidth;
    const hh = p.el.clientHeight;
    const bw = p.hoverEl.offsetWidth;
    const bh = p.hoverEl.offsetHeight;
    const left = h.x + 12 + bw > w ? h.x - 12 - bw : h.x + 12;
    const top = h.y + 12 + bh > hh ? h.y - 12 - bh : h.y + 12;
    p.hoverEl.style.left = `${Math.max(0, left)}px`;
    p.hoverEl.style.top = `${Math.max(0, top)}px`;
  }

  // ---------- titles, badges, messages ----------

  const badgeText = () => (manualFreeze ? 'Frozen' : autoFrozen ? 'Frozen (stopped)' : '');

  function paneMessage(p) {
    const sigs = paneSignals(p.id);
    if (!sigs.length) return 'An instrument has no input';
    if (!everFed) return 'Nothing is playing. Play the Audio tab\'s file, or the Playground\'s browser, and it shows here.';
    return '';
  }

  function decoratePanes() {
    const bt = badgeText();
    for (const p of panes) {
      const sigs = paneSignals(p.id);
      const title = (sigs.length === 1 && hasBefore()) || panes.length > 1 ? SIG_LABEL[sigs[0] || 'before'] : '';
      const shownTitle = sigs.length ? title : '';
      if (p.shown.title !== shownTitle) {
        p.shown.title = shownTitle;
        p.titleEl.textContent = shownTitle;
        p.titleEl.hidden = !shownTitle;
        p.titleEl.dataset.sig = sigs[0] || '';
      }
      if (p.shown.badge !== bt) { p.shown.badge = bt; p.badgeEl.textContent = bt; p.badgeEl.hidden = !bt; }
      const msg = paneMessage(p);
      if (p.shown.msg !== msg) { p.shown.msg = msg; p.msgEl.textContent = msg; p.msgEl.hidden = !msg; }
    }
  }

  // ---------- level meters ----------

  const MET = { LAB: 22, PAD: 4, GAP: 4, LO: -60, HI: 6, FALL: 40, HOLD_MS: 1500, HOLD_FALL: 20, FLOOR: -90 };
  const newMeter = () => ({ pk: [0, 0], tp: [0, 0], ms: [0, 0], bar: [MET.FLOOR, MET.FLOOR], hold: [MET.FLOOR, MET.FLOOR], holdAt: [0, 0], max: [MET.FLOOR, MET.FLOOR], clip: [false, false] });
  const meterState = { before: newMeter(), after: newMeter() };

  function resetMeters() {
    for (const sig of SIGS) meterState[sig] = newMeter();
    for (const m of meters) { m.val.textContent = ''; m.shownVal = ''; for (const c of m.clips) { c.classList.remove('is-on'); } }
  }

  // New frames: the sample peak and the true peak (Peak meter) since the last draw, the mean square
  // over the Avg time. Every fed sample is checked, whatever the draw rate.
  function meterFeed(sig, frames, n, rate) {
    const m = meterState[sig];
    let pl = 0;
    let pr = 0;
    let sl = 0;
    let sr = 0;
    for (let i = 0, k = 0; i < n; i++, k += 2) {
      const a = frames[k];
      const b = frames[k + 1];
      const aa = a < 0 ? -a : a;
      const bb = b < 0 ? -b : b;
      if (aa > pl) pl = aa;
      if (bb > pr) pr = bb;
      sl += a * a;
      sr += b * b;
    }
    if (pl > m.pk[0]) m.pk[0] = pl;
    if (pr > m.pk[1]) m.pk[1] = pr;
    if (params.meter === 'peak') {
      const tp = tpOf[sig];
      m.tp[0] = Math.max(m.tp[0], tp[0].push(frames, n, 0));
      m.tp[1] = Math.max(m.tp[1], tp[1].push(frames, n, 1));
    }
    const alpha = 1 - Math.exp(-(n / rate) / params.avgTime);
    m.ms[0] += (sl / n - m.ms[0]) * alpha;
    m.ms[1] += (sr / n - m.ms[1]) * alpha;
  }

  const toDb = (lin) => (lin > 3.2e-5 ? 20 * Math.log10(lin) : MET.FLOOR);

  // One draw's step of a signal's meter: new peak, fall, hold, maximum, clip.
  function meterStep(sig, now, dt, live) {
    if (!live) return;
    const m = meterState[sig];
    for (let c = 0; c < 2; c++) {
      let pdb = toDb(m.pk[c]);
      m.pk[c] = 0;
      if (params.meter === 'peak') pdb = Math.max(pdb, toDb(m.tp[c]));
      m.tp[c] = 0;
      m.bar[c] = Math.max(pdb, m.bar[c] - MET.FALL * dt);
      if (pdb >= m.hold[c]) { m.hold[c] = pdb; m.holdAt[c] = now; }
      else if (now - m.holdAt[c] > MET.HOLD_MS) m.hold[c] = Math.max(m.bar[c], m.hold[c] - MET.HOLD_FALL * dt);
      if (pdb > m.max[c]) m.max[c] = pdb;
      if (pdb > 0) m.clip[c] = true;
    }
  }

  function buildMeters() {
    const sigs = drawnSignals();
    const key = sigs.join(',');
    if (key === meterKey) return;
    meterKey = key;
    els.meters.innerHTML = sigs.map((sig) => `
      <div class="an-mgroup" data-sig="${sig}">
        ${sigs.length > 1 ? `<div class="an-mtitle">${SIG_LABEL[sig]}</div>` : '<div class="an-mtitle">Level</div>'}
        <div class="an-mrow">
          <span></span>
          <button type="button" class="an-clip" data-ch="0" aria-label="${SIG_LABEL[sig]} left channel clipped: click to clear" title="Clipped: click to clear">L</button>
          <button type="button" class="an-clip" data-ch="1" aria-label="${SIG_LABEL[sig]} right channel clipped: click to clear" title="Clipped: click to clear">R</button>
        </div>
        <canvas class="an-mcanvas" aria-hidden="true"></canvas>
        <div class="an-mrow an-mmax"><span></span><output class="an-mval" aria-label="${SIG_LABEL[sig]} maximum since Reset, dB" title="Highest level since Reset"></output></div>
      </div>`).join('');
    meters = [...els.meters.querySelectorAll('.an-mgroup')].map((el) => {
      const sig = el.dataset.sig;
      const m = { sig, el, canvas: el.querySelector('canvas'), val: el.querySelector('.an-mval'), clips: [...el.querySelectorAll('.an-clip')], shownVal: '' };
      m.c2d = m.canvas.getContext('2d');
      m.clips.forEach((b, c) => b.addEventListener('click', () => { meterState[sig].clip[c] = false; b.classList.remove('is-on'); }));
      return m;
    });
    dirty = true;
  }

  function drawMeter(m) {
    const t = getTheme();
    const st = meterState[m.sig];
    const cv = m.canvas;
    const W = Math.max(1, Math.round(cv.clientWidth * dpr));
    const H = Math.max(1, Math.round(cv.clientHeight * dpr));
    if (cv.width !== W || cv.height !== H) { cv.width = W; cv.height = H; }
    const g = m.c2d;
    g.clearRect(0, 0, W, H);
    const pad = MET.PAD * dpr;
    const gap = MET.GAP * dpr;
    const x0 = pad + MET.LAB * dpr + gap;
    const bw = (W - x0 - pad - gap) / 2;
    const top = 6 * dpr;
    const bot = H - 6 * dpr;
    const yOf = (db) => top + ((MET.HI - clamp(db, MET.LO, MET.HI)) / (MET.HI - MET.LO)) * (bot - top);
    // The scale: a faint line every 6 dB, a label every 6 or 12 dB, the part above 0 tinted.
    g.fillStyle = t.danger.replace(/[\d.]+\)$/, '0.12)');
    g.fillRect(x0, yOf(MET.HI), W - x0 - pad, yOf(0) - yOf(MET.HI));
    g.font = `${9.5 * dpr}px ${t.font}`;
    g.textBaseline = 'middle';
    g.textAlign = 'right';
    const every = (bot - top) / dpr / 11 >= 14 ? 6 : 12;
    for (let db = MET.HI; db >= MET.LO; db -= 6) {
      const y = Math.round(yOf(db));
      g.fillStyle = t.grid;
      g.fillRect(x0, y, W - x0 - pad, Math.max(1, Math.round(dpr * 0.5)));
      if ((db - MET.HI) % every === 0 || db === MET.LO) {
        g.fillStyle = t.text;
        g.fillText(D.fmtInt(db), pad + MET.LAB * dpr, y);
      }
    }
    g.textAlign = 'left';
    const col = t.solid[m.sig];
    for (let c = 0; c < 2; c++) {
      const x = x0 + c * (bw + gap);
      g.fillStyle = D.rgba(col, 0.1);
      g.fillRect(x, yOf(MET.HI), bw, bot - yOf(MET.HI));
      // The peak bar, the RMS bar inside it, the hold tick.
      const yp = yOf(st.bar[c]);
      g.fillStyle = D.rgba(col, 0.42);
      g.fillRect(x, yp, bw, bot - yp);
      const rms = st.ms[c] > 1e-9 ? 10 * Math.log10(st.ms[c]) : MET.FLOOR;
      const yr = yOf(rms);
      g.fillStyle = D.rgba(col, 0.95);
      g.fillRect(x + bw * 0.22, yr, bw * 0.56, bot - yr);
      if (st.hold[c] <= MET.LO) continue;
      g.fillStyle = t.line[m.sig];
      g.fillRect(x, Math.round(yOf(st.hold[c])) - dpr, bw, Math.max(2, Math.round(2 * dpr)));
    }
    // The text readouts change rarely: set them only when they do.
    const mx = Math.max(st.max[0], st.max[1]);
    const text = mx <= MET.FLOOR + 0.5 ? '' : D.fmtDb(mx);
    if (m.shownVal !== text) { m.shownVal = text; m.val.textContent = text; }
    for (let c = 0; c < 2; c++) m.clips[c].classList.toggle('is-on', st.clip[c]);
  }

  // ---------- the source line and the transport ----------

  const NO_PLUGIN = 'No plugin: Before and After are the same.';
  let lastWhat = '';
  function sourceInfo() {
    const s = bus.status();
    // The dry recording is played back without the plugin, whatever plugin the status names.
    const plugin = s.source === 'dry' ? null : s.plugin;
    const pg = V.playground;
    let what = '';
    let note = '';
    switch (s.source) {
      case 'file': what = `the Audio tab's file${plugin ? ` through ${plugin}` : ''}`; break;
      case 'dry': what = 'the Audio tab\'s recording (as recorded, no plugin)'; break;
      case 'browser': what = `the browser${plugin ? ` through ${plugin}` : ''}`; break;
      case 'instrument': what = plugin || 'an instrument'; break;
      case 'page': {
        let d = '';
        try { d = (pg && pg.describe && pg.describe()) || ''; } catch { /* the Playground is not ready */ }
        what = d || 'the Audio tab';
        break;
      }
      default: break;
    }
    if (what) {
      if (s.source === 'instrument') note = 'An instrument has no input.';
      else if (s.source === 'page') note = pageSame ? NO_PLUGIN : '';
      else if (!plugin) note = NO_PLUGIN;
      else if (s.bypass) note = 'The plugin is bypassed.';
    }
    return { what, note, playing: s.playing, source: s.source, plugin };
  }

  function renderSource() {
    if (!els) return;
    const info = sourceInfo();
    if (info.what) lastWhat = info.what;
    const live = info.playing && !!info.what;
    const text = live ? `Listening to ${info.what}` : everFed && lastWhat ? `Nothing is playing now (last: ${lastWhat})` : 'Nothing is playing';
    const note = live ? info.note : '';
    const key = `${text}|${note}|${live}`;
    if (key !== lastSource) {
      lastSource = key;
      els.sourceText.textContent = text;
      els.sourceNote.textContent = note;
      els.dot.classList.toggle('is-live', live);
    }
    renderTransport();
  }

  // Play / Pause of the Playground's tab: its Audio tab's file, or the browser's player.
  let transportKey = '';
  function renderTransport() {
    const pg = V.playground;
    let has = false;
    let playing = false;
    let tab = 'file';
    try { tab = pg.tab(); has = !!pg.canPlay(); playing = !!pg.playing(); } catch { /* not ready */ }
    const key = `${tab}|${has}|${playing}`;
    if (key === transportKey) return;
    transportKey = key;
    const what = tab === 'browser' ? 'the browser\'s video or song' : 'the Audio tab\'s file';
    els.transport.disabled = !has;
    els.transport.innerHTML = playing ? `${PAUSE}<span>Pause</span>` : `${PLAY}<span>Play</span>`;
    els.transport.setAttribute('aria-label', `${playing ? 'Pause' : 'Play'} ${what}`);
    els.transport.title = has ? `${playing ? 'Pause' : 'Play'} ${what} (Space)`
      : tab === 'browser' ? 'Start a video or a song in the Playground\'s browser to control it here'
        : 'Open an audio file in the Playground\'s Audio tab to play it here';
  }

  async function toggleTransport() {
    const pg = V.playground;
    if (!pg || !pg.canPlay()) return;
    try { await pg.playPause(); } catch (e) { V.toast(errText(e), true); }
    transportKey = '';
    renderTransport();
  }

  // ---------- feeding ----------

  // The views start over: averages, hold lines, history (and the replay buffer with clearHist).
  // The meters are left alone: their maxima and clip lights clear only on a click or Reset.
  function resetViews(clearHist) {
    for (const p of panes) { try { if (p.view) p.view.reset(); } catch (e) { console.error('[analyzer] reset failed:', e); } }
    if (clearHist) for (const sig of SIGS) hist[sig].clear();
    dirty = true;
  }

  // The Reset button: the views and the meters (maxima, hold lines, clip lights).
  function resetAll() {
    resetViews(false);
    resetMeters();
  }

  function onFrames(f) {
    // Silence on both signals is no frame: the sources keep sending zeros for a moment after the
    // sound stops, and those must not count as playing (the view freezes "Frozen (stopped)").
    if (f.quiet) return;
    if (timeline) timeline.feed(f); // the browser's waveform is drawn from what plays
    const t = performance.now();
    lastFrameAt = t;
    const first = !everFed;
    everFed = true;
    if (manualFreeze) return;
    // A new run (another source or stream, a new rate, a gap on the page side): start from nothing.
    if (f.run !== lastRun) {
      lastRun = f.run;
      lastRate = f.rate;
      resetTruePeak();
      resetViews(true);
    } else if (autoFrozen) {
      resetViews(false);   // sound came back after a stop: the views start again, the meters keep their state
    }
    autoFrozen = false;
    pageSame = !!f.same;
    const flags = f.before ? 1 : 0;
    if (flags !== (lastFlags & 1)) { lastFlags = flags; syncControls(); }
    lastFlags = f.flags;
    if (first) dirty = true;
    const n = f.n;
    for (const sig of SIGS) {
      const frames = sig === 'before' ? f.before : f.after;
      if (!frames) continue;
      hist[sig].push(frames, n);
      meterFeed(sig, frames, n, f.rate);
      for (const p of panes) {
        if (!p.view) continue;
        try { p.view.feed(sig, frames, n, f.rate); } catch (e) { console.error('[analyzer] feed failed:', e); }
      }
    }
    kick();
  }

  // ---------- the loop ----------

  function kick() {
    if (!raf && shown) raf = requestAnimationFrame(tick);
  }

  const receiving = (now) => everFed && now - lastFrameAt < STALL_MS;

  function tick(now) {
    raf = 0;
    if (!shown) return;
    const live = !frozen() && receiving(now);
    const due = now - lastDraw >= 1000 / params.fps - 1;
    if (live ? due : dirty) {
      const dt = live ? Math.min(0.5, (now - lastDraw) / 1000) : 0;
      lastDraw = now;
      dirty = false;
      drawAll(now, dt, live);
    }
    if (live) kick();
    else if (dirty) kick();
  }

  function drawAll(now, dt, live) {
    ensurePanes();
    buildMeters();
    sizePanes();
    for (const p of panes) {
      if (p.view) { try { p.view.draw(now); } catch (e) { console.error('[analyzer] draw failed:', e); } }
    }
    decoratePanes();
    if (hover && hover.pane.view) updateHover(hover.pane);
    for (const m of meters) { meterStep(m.sig, now, dt, live); drawMeter(m); }
  }

  // Stopped frames freeze the picture; the source line and the transport follow along.
  function watch() {
    const now = performance.now();
    if (everFed && !frozen() && now - lastFrameAt > STALL_MS) {
      autoFrozen = true;
      dirty = true;
      kick();
    }
    renderSource();
  }

  // ---------- Capture ----------

  function closeCapMenu(focus) {
    if (!els) return;
    els.capMenu.hidden = true;
    els.capBtn.setAttribute('aria-expanded', 'false');
    if (focus) els.capBtn.focus();
  }

  function openCapMenu() {
    const items = { before: els.capMenu.querySelector('[data-act="capture-before"]'), after: els.capMenu.querySelector('[data-act="capture-after"]'), both: els.capMenu.querySelector('[data-act="capture-both"]') };
    const hasA = hist.after.total > 0;
    const hasB = hist.before.total > 0 && hasBefore();
    items.before.disabled = !hasB;
    items.after.disabled = !hasA;
    items.both.disabled = !(hasA && hasB);
    items.before.title = hasB ? '' : (hasA ? 'There is no Before: an instrument has no input' : 'Nothing has played yet');
    items.after.title = hasA ? '' : 'Nothing has played yet';
    items.both.title = hasA && hasB ? '' : 'Needs both signals';
    els.capMenu.hidden = false;
    els.capBtn.setAttribute('aria-expanded', 'true');
    const first = els.capMenu.querySelector('button:not(:disabled)');
    if (first) first.focus();
  }

  // An opaque colour behind `el` (the first ancestor with a background), for a picture.
  function backdrop(el) {
    for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
      const c = getComputedStyle(n).backgroundColor;
      if (c && c !== 'transparent' && !/rgba\(.*,\s*0\)$/.test(c)) return c;
    }
    return getComputedStyle(document.body).backgroundColor || '#fff';
  }

  function paramLine() {
    const def = Views.get(activeView());
    const want = new Set((def && def.settings) || []);
    const parts = [];
    if (want.has('block')) parts.push(`Block ${params.block}`);
    if (want.has('window')) parts.push(winLabel(params.window));
    if (want.has('avgTime')) parts.push(`Avg ${params.avgTime} s`);
    if (want.has('meter')) parts.push(params.meter === 'rms' ? 'RMS' : 'Peak');
    if (want.has('freq')) parts.push(`${D.fmtHzShort(params.freqLo)} – ${D.fmtHzShort(params.freqHi)}`);
    if (want.has('range')) parts.push(`${D.fmtInt(params.rangeLo)} … ${D.fmtInt(params.rangeHi)} dB`);
    if (want.has('history')) parts.push(`${params.history} s`);
    return parts.join(' · ');
  }

  // A short name for the source, for a file name: the plugin, else what plays.
  function sourceShort() {
    const info = sourceInfo();
    if (info.plugin) return info.plugin;
    const w = info.what || lastWhat || 'nothing';
    return w.replace(/^the /, '');
  }

  async function capture(which) {
    closeCapMenu(true);
    const view = panes.find((p) => p.view);
    if (!view) return;
    const list = which === 'both' ? SIGS : [which];
    const t = getTheme();
    dpr = window.devicePixelRatio || 1;
    const pw = which === 'both' ? Math.max(Math.round(360 * dpr), Math.round((els.views.clientWidth * dpr) / 2)) : Math.max(Math.round(360 * dpr), Math.round(els.views.clientWidth * dpr));
    const ph = Math.max(Math.round(240 * dpr), Math.round(els.views.clientHeight * dpr) / (panes.length > 1 && els.views.classList.contains('is-split') && els.views.clientWidth < 700 ? panes.length : 1));
    const headH = Math.round(40 * dpr);
    const out = document.createElement('canvas');
    out.width = pw * list.length;
    out.height = headH + Math.round(ph);
    const g = out.getContext('2d');
    g.fillStyle = backdrop(els.root);
    g.fillRect(0, 0, out.width, out.height);
    const viewLabel = (Views.get(activeView()) || {}).label || activeView();
    const source = sourceInfo().what || lastWhat || 'nothing';
    const line = paramLine();
    for (let i = 0; i < list.length; i++) {
      const sig = list[i];
      const x = i * pw;
      let snap = null;
      try { snap = view.view.snapshot([sig], pw, Math.round(ph)); } catch (e) { console.error('[analyzer] snapshot failed:', e); }
      if (snap) g.drawImage(snap, x, headH);
      if (i > 0) { g.fillStyle = t.grid; g.fillRect(x, 0, Math.max(1, Math.round(dpr)), out.height); }
      g.textBaseline = 'middle';
      g.textAlign = 'left';
      g.font = `600 ${12 * dpr}px ${t.font}`;
      g.fillStyle = t.line[sig];
      g.fillText(`${viewLabel} · ${SIG_LABEL[sig]}`, x + 10 * dpr, headH * 0.32);
      g.font = `${10.5 * dpr}px ${t.font}`;
      g.fillStyle = t.text;
      const sub = [line, source].filter(Boolean).join(' · ');
      g.fillText(sub, x + 10 * dpr, headH * 0.72, pw - 20 * dpr);
    }
    const blob = await new Promise((resolve) => out.toBlob(resolve, 'image/png'));
    if (!blob) { V.toast('Couldn\'t capture the picture', true); return; }
    await savePicture(blob, `Analyzer - ${viewLabel} - ${which === 'both' ? 'Before and After' : SIG_LABEL[which]} (${sourceShort()})`);
  }

  // Saves a picture: Windows' save dialog (the name suggested), then the bytes go to the backend,
  // which writes them (Pictures\VEXT when there is no dialog).
  async function savePicture(blob, baseName) {
    const name = baseName.replace(/[<>:"/\\|?*]+/g, ' ');
    let path = null;
    const dialog = tauri() && tauri().dialog;
    if (dialog && typeof dialog.save === 'function') {
      try {
        path = await dialog.save({ title: 'Save the picture', defaultPath: `${name}.png`, filters: [{ name: 'PNG picture', extensions: ['png'] }] });
        if (!path) return; // cancelled
      } catch {
        path = null; // this build can't show a save dialog: it goes to Pictures\VEXT
      }
    }
    const q = new URLSearchParams({ name });
    if (path) q.set('path', path);
    try {
      const r = await V.api(`/api/playground/audio/picture?${q}`, { method: 'POST', headers: { 'Content-Type': 'image/png' }, body: blob });
      V.toast(`Saved: ${r.path}`);
    } catch (err) {
      V.toast(errText(err), true);
    }
  }

  // ---------- events ----------

  function onClick(e) {
    const seg = e.target.closest('[data-seg] button');
    if (seg && els.root.contains(seg)) {
      if (seg.disabled) return;
      setParam({ [seg.closest('[data-seg]').dataset.seg]: seg.dataset.val });
      return;
    }
    const btn = e.target.closest('[data-act]');
    if (btn) {
      switch (btn.dataset.act) {
        case 'freeze':
          manualFreeze = !manualFreeze;
          if (manualFreeze) autoFrozen = false;
          syncControls();
          dirty = true;
          kick();
          break;
        case 'reset': resetAll(); kick(); break;
        case 'settings': setSettingsOpen(!setwin.open); break;
        case 'settings-close': setSettingsOpen(false); break;
        case 'capture': if (els.capMenu.hidden) openCapMenu(); else closeCapMenu(); break;
        case 'capture-before': capture('before'); break;
        case 'capture-after': capture('after'); break;
        case 'capture-both': capture('both'); break;
        case 'transport': toggleTransport(); break;
        default: break;
      }
      return;
    }
    if (!e.target.closest('.an-cap')) closeCapMenu();
  }

  function onChange(e) {
    const c = e.target.closest('[data-key]');
    if (!c || !els.settings.contains(c)) return;
    setParam({ [c.dataset.key]: c.value });
  }

  // Space plays and pauses the Playground's sound wherever the focus is in the section, a button included
  // (a clicked button kept the focus, and Space then did nothing, or pressed that button again), except
  // where Space is for typing or choosing, and the Capture menu.
  function spaceIsOurs(e) {
    if (!shown || (e.key !== ' ' && e.code !== 'Space') || e.ctrlKey || e.metaKey || e.altKey) return false;
    const t = e.target instanceof Element ? e.target : null;
    const tag = t && t.tagName;
    if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA' || (t && t.isContentEditable)) return false;
    if (t && t.closest('a, .an-capmenu')) return false;
    return !(V.overlayOpen && V.overlayOpen());
  }

  function onKeyDown(e) {
    if (!shown) return;
    if (e.key === 'Escape' && !els.capMenu.hidden) { closeCapMenu(true); e.preventDefault(); return; }
    // Escape closes the settings window only when the focus is in it (a knob's typing box takes Escape first).
    if (e.key === 'Escape' && setwin.open && els.setwin.contains(document.activeElement)) { setSettingsOpen(false); e.preventDefault(); return; }
    if (e.defaultPrevented || !spaceIsOurs(e)) return;
    const pg = V.playground;
    if (!pg || !pg.canPlay()) return;
    e.preventDefault();
    // A held key repeats: only the first press toggles (the Playground's Space does the same).
    if (!e.repeat) toggleTransport();
  }
  // A focused button clicks when Space comes back up: that must not happen either.
  function onKeyUp(e) {
    if (spaceIsOurs(e)) e.preventDefault();
  }
  // A button the mouse pressed doesn't keep the focus (the keyboard's Tab still moves it as usual).
  function onMouseUp(e) {
    const b = e.target instanceof Element ? e.target.closest('button') : null;
    if (b && els && els.root.contains(b) && !b.closest('.an-capmenu')) b.blur();
  }

  function onDocPointer(e) {
    if (els && !els.capMenu.hidden && !e.target.closest('.an-cap')) closeCapMenu();
  }

  function recolor() {
    theme = null;
    if (timeline) timeline.recolor();
    dirty = true;
    kick();
  }

  // ---------- lifecycle ----------

  function build(el) {
    rootEl = el;
    el.innerHTML = shellHtml();
    const q = (s) => el.querySelector(s);
    els = {
      root: q('#an-root'), viewSeg: q('[data-views]'), settings: q('.an-settings'), views: q('.an-views'), meters: q('.an-meters'),
      freeze: q('[data-act="freeze"]'), capBtn: q('.an-capbtn'), capMenu: q('.an-capmenu'), probes: q('.an-probes'),
      dot: q('.an-dot'), sourceText: q('.an-source-text'), sourceNote: q('.an-source-note'), transport: q('.an-transport'),
      setbtn: q('.an-setbtn'), setwin: q('.an-setwin'), setwinHead: q('.an-setwin-head'), setwinView: q('.an-setwin-view'),
    };
    timeline = V.AnalyzerTimeline ? V.AnalyzerTimeline.create(q('.an-timeline')) : null;
    // Play / Pause sits before the waveform (it stays on the source line without a timeline).
    if (timeline && timeline.lead) timeline.lead.appendChild(els.transport);
    theme = null;
    paneState = { view: '', split: null };
    meterKey = '';
    meters = [];
    lastSource = '';
    transportKey = '';
    manualFreeze = false;
    autoFrozen = false;
    renderViewSwitch();
    renderSettings();
    showSetwin(setwin.open);   // the panel comes back as it was left (open or closed, and where)
    els.setwinHead.addEventListener('pointerdown', onSetwinDown);
    els.setwinHead.addEventListener('pointermove', onSetwinMove);
    els.setwinHead.addEventListener('pointerup', onSetwinUp);
    els.setwinHead.addEventListener('pointercancel', onSetwinUp);
    els.setwinHead.addEventListener('lostpointercapture', onSetwinUp);
    window.addEventListener('resize', onResize);
    el.addEventListener('click', onClick);
    el.addEventListener('change', onChange);
    ro = typeof ResizeObserver === 'function' ? new ResizeObserver(() => { if (shown) { dirty = true; kick(); } }) : null;
    if (ro) ro.observe(els.views);
    themeObs = new MutationObserver(recolor);
    themeObs.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme', 'class'] });
    if (window.matchMedia) {
      mq = window.matchMedia('(prefers-color-scheme: dark)');
      if (mq.addEventListener) mq.addEventListener('change', recolor);
    }
    document.addEventListener('keydown', onKeyDown);
    document.addEventListener('keyup', onKeyUp, true);
    el.addEventListener('mouseup', onMouseUp);
    document.addEventListener('pointerdown', onDocPointer, true);
  }

  function teardown() {
    shown = false;
    cancelAnimationFrame(raf);
    raf = 0;
    clearInterval(watchTimer);
    watchTimer = 0;
    bus.unsubscribe(onFrames);
    if (unStatus) { unStatus(); unStatus = null; }
    if (ro) { ro.disconnect(); ro = null; }
    if (themeObs) { themeObs.disconnect(); themeObs = null; }
    if (mq && mq.removeEventListener) mq.removeEventListener('change', recolor);
    mq = null;
    document.removeEventListener('keydown', onKeyDown);
    document.removeEventListener('keyup', onKeyUp, true);
    document.removeEventListener('pointerdown', onDocPointer, true);
    if (timeline) { timeline.destroy(); timeline = null; }
    destroyPanes();
    destroyKnobs();
    setDrag = null;
    window.removeEventListener('resize', onResize);
    if (els) {
      els.setwinHead.removeEventListener('pointerdown', onSetwinDown);
      els.setwinHead.removeEventListener('pointermove', onSetwinMove);
      els.setwinHead.removeEventListener('pointerup', onSetwinUp);
      els.setwinHead.removeEventListener('pointercancel', onSetwinUp);
      els.setwinHead.removeEventListener('lostpointercapture', onSetwinUp);
    }
    if (rootEl) {
      rootEl.removeEventListener('click', onClick);
      rootEl.removeEventListener('mouseup', onMouseUp);
      rootEl.removeEventListener('change', onChange);
    }
  }

  function load(el) {
    if (shown && el === rootEl) return;   // a refresh in place: nothing to redo
    if (shown) teardown();
    build(el);
    shown = true;
    everFed = false;
    lastFrameAt = -1e9;
    lastRun = '';
    lastRate = 0;
    lastFlags = 1;
    pageSame = false;
    for (const sig of SIGS) hist[sig].clear();
    resetMeters();
    resetTruePeak();
    dirty = true;
    ensurePanes();
    buildMeters();
    renderSource();
    bus.subscribe(onFrames);
    unStatus = bus.onStatus(renderSource);
    watchTimer = setInterval(watch, WATCH_MS);
    kick();
  }

  V.registerSection('analyzer', {
    load,
    // `next` is the section being opened (the Playground keeps playing when it is this one).
    unload() { teardown(); },
  });
})();
