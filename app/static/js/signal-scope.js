// The scope on the Playground's Signal tab (VEXT.SignalScope.create). Signal draws the last rendered
// preview. Live draws the plugin's Before and After from the audio bus (VEXT.audioBus), overlaid, but
// only while the scope is shown, Live is picked and the picture isn't frozen: otherwise nothing is
// subscribed and nothing is drawn. L/R draws amplitude against time, triggered on a rising zero crossing
// so a steady tone stands still. X-Y draws one channel against the other (a Lissajous curve; in Live,
// the Before's mid against the After's mid).
(function () {
  const V = window.VEXT;
  if (!V || V.SignalScope) return;

  const KEY = 'pa-pg-scope';
  const SPANS = [1, 2, 5, 10, 20, 50, 100];   // ms across the picture
  const RING = 16384;              // frames kept for Live (about 340 ms at 48 kHz)
  const MAX_VIEW = RING - 2;       // the most frames one picture can show
  const FRAME_MS = 1000 / 30;      // Live redraws about 30 times a second
  const DEFAULT_RATE = 48000;
  const PAD = { l: 36, r: 10, t: 10, b: 20 };   // room for the amplitude labels and the time line
  const XY_PAD = 14;
  const FADE = 0.3;                // X-Y persistence: how much of the old picture is wiped each frame
  const LIMIT = 1.5;               // values beyond this are drawn at the edge, not off the canvas
  const MINUS = '−';

  // Saved choices (the picture itself is not saved).
  function loadPrefs() {
    let p = null;
    try { p = JSON.parse(localStorage.getItem(KEY) || 'null'); } catch { /* unreadable: defaults */ }
    p = p || {};
    return {
      mode: p.mode === 'live' ? 'live' : 'signal',
      view: p.view === 'xy' ? 'xy' : 'lr',
      ms: SPANS.includes(+p.ms) ? +p.ms : 10,
    };
  }
  const savePrefs = (p) => { try { localStorage.setItem(KEY, JSON.stringify(p)); } catch { /* storage unavailable */ } };

  // A ring of stereo frames: the newest `have` frames end just before index `end`. `cap` is a power of two,
  // so an index wraps with a mask and nothing is copied when the ring moves on.
  const makeRing = (cap) => ({ cap, mask: cap - 1, l: new Float32Array(cap), r: new Float32Array(cap), end: 0, have: 0 });

  // Adds the first `n` interleaved stereo frames of src; when they don't all fit, the newest are kept.
  function pushFrames(ring, src, n) {
    const from = Math.max(0, n - ring.cap);
    let e = ring.end;
    for (let i = from; i < n; i++) {
      ring.l[e] = src[2 * i];
      ring.r[e] = src[2 * i + 1];
      e = (e + 1) & ring.mask;
    }
    ring.end = e;
    ring.have = Math.min(ring.cap, ring.have + n - from);
  }

  // Copies the window of N frames that ends `shift` frames before the newest one into `out`, time running
  // left to right. ch: 0 = left, 1 = right, 2 = mid. Returns the largest magnitude copied.
  function grab(ring, N, shift, ch, out) {
    const m = ring.mask;
    const e = ring.end;
    const L = ring.l;
    const R = ring.r;
    let peak = 0;
    for (let t = 0; t < N; t++) {
      const p = (e - (shift + N - t)) & m;
      const v = ch === 0 ? L[p] : ch === 1 ? R[p] : (L[p] + R[p]) * 0.5;
      out[t] = v;
      const a = v < 0 ? -v : v;
      if (a > peak) peak = a;
    }
    return peak;
  }

  // How far back the window starts so that it begins on a rising zero crossing of the left channel: the
  // newest crossing that still leaves a whole window, so the picture moves as little as it can. 0 when
  // there is none (the newest frames are shown as they are).
  function triggerShift(ring, N, have) {
    const m = ring.mask;
    const e = ring.end;
    const L = ring.l;
    for (let a = N; a < have; a++) {
      const newer = L[(e - a) & m];
      const older = L[(e - a - 1) & m];
      if (older < 0 && newer >= 0) return a - N;
    }
    return 0;
  }

  const fmtDb = (peak) => (peak > 0 ? (20 * Math.log10(peak)).toFixed(1).replace('-', MINUS) : MINUS + '∞');
  const clampV = (v) => (v > LIMIT ? LIMIT : v < -LIMIT ? -LIMIT : v);

  const MARKUP = `
    <div class="ss">
      <div class="ss-bar">
        <span class="ss-title">Scope</span>
        <div class="segmented ss-seg" role="group" aria-label="Source" data-seg="mode">
          <button type="button" data-val="signal" aria-pressed="false">Signal</button>
          <button type="button" data-val="live" aria-pressed="false" title="What the plugin does to the sound, as it plays">Live</button>
        </div>
        <div class="segmented ss-seg" role="group" aria-label="View" data-seg="view">
          <button type="button" data-val="lr" aria-pressed="false">L/R</button>
          <button type="button" data-val="xy" aria-pressed="false">X-Y</button>
        </div>
        <label class="ss-field"><span class="ss-label">Time</span>
          <select class="ss-sel" data-key="ms" aria-label="Time across the scope">${SPANS.map((ms) => `<option value="${ms}">${ms} ms</option>`).join('')}</select>
        </label>
        <span class="ss-spacer"></span>
        <button type="button" class="ss-btn" data-act="freeze" aria-pressed="false" title="Hold the picture (Live only)">Freeze</button>
      </div>
      <div class="ss-plot">
        <canvas class="ss-canvas" aria-label="Scope: the shape of the sound over time"></canvas>
        <div class="ss-readout"></div>
        <div class="ss-probes" aria-hidden="true"><i class="ss-p-fg3"></i><i class="ss-p-accent"></i><i class="ss-p-fg2"></i><i class="ss-p-bg"></i><i class="ss-p-grid"></i><i class="ss-p-axis"></i></div>
      </div>
    </div>`;

  function create(container) {
    let dead = !container;
    const root = document.createElement('div');
    root.innerHTML = MARKUP;
    const box = root.firstElementChild;
    if (container) container.appendChild(box);

    const q = (s) => box.querySelector(s);
    const els = {
      plot: q('.ss-plot'), canvas: q('.ss-canvas'), readout: q('.ss-readout'), probes: q('.ss-probes'),
      freeze: q('[data-act="freeze"]'), select: q('select[data-key="ms"]'),
    };
    const ctx = els.canvas.getContext('2d');
    const probeCtx = document.createElement('canvas').getContext('2d');

    const prefs = loadPrefs();
    let mode = prefs.mode;
    let view = prefs.view;
    let ms = prefs.ms;
    let frozen = false;
    let shown = false;
    let subscribed = false;
    let raf = 0;
    let dirty = false;               // something on screen is stale
    let liveDirty = false;           // new Live frames came in since the last draw
    let lastDraw = 0;
    let scale = 1;                   // device pixels per CSS pixel
    let cssW = 0;
    let cssH = 0;
    let theme = null;                // colours as canvas strings; read again when the theme changes
    let sig = null;                  // the last render (a ring holding its left and right channel)
    let sigRate = DEFAULT_RATE;
    const before = makeRing(RING);   // Live: the plugin's input
    const after = makeRing(RING);    // Live: the plugin's output
    let hasBefore = false;
    let liveRate = DEFAULT_RATE;
    let liveRun = '';
    const scratchA = new Float32Array(RING);
    const scratchB = new Float32Array(RING);
    let readoutText = '';
    let ro = null;
    let mo = null;
    let mq = null;

    const spanFrames = (rate) => Math.max(2, Math.min(MAX_VIEW, Math.round(ms * rate / 1000)));
    const wantBus = () => shown && mode === 'live' && !frozen;

    // Frames the picture can use now (0 when there are none).
    function dataFrames(live) {
      if (live) {
        if (!after.have) return 0;
        return hasBefore ? Math.min(after.have, before.have) : after.have;
      }
      return sig ? sig.have : 0;
    }

    // ---------- colours ----------

    // A CSS colour as the canvas reads it (the canvas turns color-mix() and friends into plain colours).
    function canvasColour(css) {
      probeCtx.fillStyle = 'rgb(0, 0, 0)';
      probeCtx.fillStyle = css;
      return probeCtx.fillStyle;
    }
    const probeColour = (cls) => canvasColour(getComputedStyle(els.probes.querySelector(cls)).color);

    function readTheme() {
      theme = {
        before: probeColour('.ss-p-fg3'),
        accent: probeColour('.ss-p-accent'),
        fg2: probeColour('.ss-p-fg2'),
        bg: probeColour('.ss-p-bg'),
        grid: probeColour('.ss-p-grid'),
        axis: probeColour('.ss-p-axis'),
        font: getComputedStyle(box).fontFamily || 'sans-serif',
      };
    }

    // ---------- sizing and drawing ----------

    // Sizes the canvas to its box in device pixels; a box that is hidden has no size and draws nothing.
    function sizeCanvas() {
      scale = window.devicePixelRatio || 1;
      cssW = els.plot.clientWidth;
      cssH = els.plot.clientHeight;
      const w = Math.max(1, Math.round(cssW * scale));
      const h = Math.max(1, Math.round(cssH * scale));
      if (els.canvas.width !== w || els.canvas.height !== h) {
        els.canvas.width = w;
        els.canvas.height = h;
      }
    }

    function setReadout(text) {
      if (text === readoutText) return;
      readoutText = text;
      els.readout.textContent = text;
    }

    function plotRect(W, H) {
      return { x: PAD.l, y: PAD.t, w: Math.max(1, W - PAD.l - PAD.r), h: Math.max(1, H - PAD.t - PAD.b) };
    }

    function gridLR(p) {
      const y0 = p.y + p.h / 2;
      ctx.lineWidth = 1;
      ctx.strokeStyle = theme.grid;
      ctx.beginPath();
      for (let i = 1; i < 10; i++) {
        const x = Math.round(p.x + (p.w * i) / 10) + 0.5;
        ctx.moveTo(x, p.y);
        ctx.lineTo(x, p.y + p.h);
      }
      for (const v of [-0.5, 0.5]) {
        const y = Math.round(y0 - v * (p.h / 2)) + 0.5;
        ctx.moveTo(p.x, y);
        ctx.lineTo(p.x + p.w, y);
      }
      ctx.stroke();
      ctx.strokeStyle = theme.axis;
      ctx.strokeRect(p.x + 0.5, p.y + 0.5, p.w - 1, p.h - 1);
      ctx.beginPath();
      ctx.moveTo(p.x, Math.round(y0) + 0.5);
      ctx.lineTo(p.x + p.w, Math.round(y0) + 0.5);
      ctx.stroke();
      ctx.font = `10.5px ${theme.font}`;
      ctx.fillStyle = theme.before;
      ctx.textAlign = 'left';
      ctx.textBaseline = 'middle';
      ctx.fillText('+1', 6, p.y);
      ctx.fillText('0', 6, y0);
      ctx.fillText(MINUS + '1', 6, p.y + p.h);
      ctx.fillText('0', p.x, p.y + p.h + 13);
      ctx.textAlign = 'right';
      ctx.fillText(ms + ' ms', p.x + p.w, p.y + p.h + 13);
      ctx.textAlign = 'start';
    }

    function gridXY(p) {
      const cx = p.x + p.w / 2;
      const cy = p.y + p.h / 2;
      ctx.lineWidth = 1;
      ctx.strokeStyle = theme.grid;
      ctx.strokeRect(p.x + p.w / 4, p.y + p.h / 4, p.w / 2, p.h / 2);
      ctx.strokeStyle = theme.axis;
      ctx.strokeRect(p.x + 0.5, p.y + 0.5, p.w - 1, p.h - 1);
      ctx.beginPath();
      ctx.moveTo(cx, p.y);
      ctx.lineTo(cx, p.y + p.h);
      ctx.moveTo(p.x, cy);
      ctx.lineTo(p.x + p.w, cy);
      ctx.stroke();
    }

    // One channel's window as a line; out[0..N) holds the values.
    function strokeWave(arr, N, p, color) {
      const mid = p.y + p.h / 2;
      const half = p.h / 2;
      const step = N > 1 ? p.w / (N - 1) : 0;
      ctx.beginPath();
      for (let t = 0; t < N; t++) {
        const x = p.x + t * step;
        const y = mid - clampV(arr[t]) * half;
        if (t) ctx.lineTo(x, y);
        else ctx.moveTo(x, y);
      }
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.25;
      ctx.stroke();
    }

    function drawLR(p, N, have, live) {
      gridLR(p);
      if (N < 2) return 0;
      const shift = triggerShift(live ? after : sig, N, have);
      if (live) {
        let peak = 0;
        if (hasBefore) {
          peak = grab(before, N, shift, 0, scratchA);
          strokeWave(scratchA, N, p, theme.before);
        }
        peak = Math.max(peak, grab(after, N, shift, 0, scratchB));
        strokeWave(scratchB, N, p, theme.accent);
        return peak;
      }
      // The right channel first, so the left (the accent) is on top.
      const pr = grab(sig, N, shift, 1, scratchA);
      strokeWave(scratchA, N, p, theme.fg2);
      const pl = grab(sig, N, shift, 0, scratchB);
      strokeWave(scratchB, N, p, theme.accent);
      return Math.max(pr, pl);
    }

    function drawXY(W, H, N, live) {
      const side = Math.max(20, Math.min(W, H) - XY_PAD * 2);
      const p = { x: (W - side) / 2, y: (H - side) / 2, w: side, h: side };
      gridXY(p);
      if (N < 2) return 0;
      let peak;
      if (live && hasBefore) {
        peak = grab(before, N, 0, 2, scratchA);
        peak = Math.max(peak, grab(after, N, 0, 2, scratchB));
      } else {
        const src = live ? after : sig;
        peak = grab(src, N, 0, 0, scratchA);
        peak = Math.max(peak, grab(src, N, 0, 1, scratchB));
      }
      const cx = p.x + p.w / 2;
      const cy = p.y + p.h / 2;
      const hw = p.w / 2;
      const hh = p.h / 2;
      ctx.beginPath();
      for (let t = 0; t < N; t++) {
        const px = cx + clampV(scratchA[t]) * hw;
        const py = cy - clampV(scratchB[t]) * hh;
        if (t) ctx.lineTo(px, py);
        else ctx.moveTo(px, py);
      }
      ctx.strokeStyle = theme.accent;
      ctx.lineWidth = 1.25;
      ctx.stroke();
      return peak;
    }

    // Redraws the picture. fade: Live X-Y keeps a faint trail of the older frames.
    function draw(fade) {
      if (!shown || dead) return;
      sizeCanvas();
      if (!(cssW > 0 && cssH > 0)) return;
      if (!theme) readTheme();
      ctx.setTransform(scale, 0, 0, scale, 0, 0);
      const live = mode === 'live';
      const have = dataFrames(live);
      const N = Math.min(spanFrames(live ? liveRate : sigRate), have);
      if (fade && view === 'xy') {
        ctx.globalAlpha = FADE;
        ctx.fillStyle = theme.bg;
        ctx.fillRect(0, 0, cssW, cssH);
        ctx.globalAlpha = 1;
      } else {
        ctx.fillStyle = theme.bg;
        ctx.fillRect(0, 0, cssW, cssH);
      }
      let peak = 0;
      if (view === 'xy') peak = drawXY(cssW, cssH, N, live);
      else peak = drawLR(plotRect(cssW, cssH), N, have, live);
      if (N < 2) {
        ctx.font = `12px ${theme.font}`;
        ctx.fillStyle = theme.before;
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText(live ? 'Play the Playground to see it here.' : 'Render a signal to see it here.', cssW / 2, cssH / 2);
        ctx.textAlign = 'start';
        setReadout('');
      } else {
        setReadout(`Peak ${fmtDb(peak)} dBFS`);
      }
    }

    // ---------- scheduling ----------

    // One frame loop, only while shown: it draws what changed, and keeps going while Live runs.
    function tick() {
      raf = 0;
      if (!shown) return;
      const live = wantBus();
      const t = performance.now();
      if ((dirty || liveDirty) && (!live || t - lastDraw >= FRAME_MS)) {
        const fade = live;
        dirty = false;
        liveDirty = false;
        lastDraw = t;
        draw(fade);
      }
      if (live) raf = requestAnimationFrame(tick);
    }

    function requestDraw() {
      dirty = true;
      if (shown && !raf) raf = requestAnimationFrame(tick);
    }

    // ---------- the audio bus ----------

    function resetLive() {
      before.end = 0;
      before.have = 0;
      after.end = 0;
      after.have = 0;
    }

    // One block of frames from the bus. The views are copies: the bus's arrays are only valid during the call.
    function onFrame(f) {
      if (!f || !f.after) return;
      const n = Math.min(f.n | 0, f.after.length >> 1);
      if (n <= 0) return;
      // Another source (or a new run of the same one) starts the picture over.
      if (f.run !== liveRun) {
        liveRun = f.run;
        resetLive();
      }
      liveRate = f.rate > 0 ? f.rate : DEFAULT_RATE;
      pushFrames(after, f.after, n);
      hasBefore = !!f.before;
      if (hasBefore) pushFrames(before, f.before, n);
      else before.have = 0;
      liveDirty = true;
    }

    function syncBus() {
      const want = wantBus();
      const bus = V.audioBus;
      if (bus && want !== subscribed) {
        subscribed = want;
        if (want) {
          // A fresh subscription starts with an empty picture (what was there is old).
          resetLive();
          liveRun = '';
          bus.subscribe(onFrame);
        } else {
          bus.unsubscribe(onFrame);
        }
      }
      requestDraw();
    }

    // ---------- the controls ----------

    function paint() {
      for (const b of box.querySelectorAll('[data-seg="mode"] button')) b.setAttribute('aria-pressed', String(b.dataset.val === mode));
      for (const b of box.querySelectorAll('[data-seg="view"] button')) b.setAttribute('aria-pressed', String(b.dataset.val === view));
      els.freeze.setAttribute('aria-pressed', String(frozen));
      els.freeze.disabled = mode !== 'live';
      els.select.value = String(ms);
    }

    function save() { savePrefs({ mode, view, ms }); }

    function setMode(m) {
      if ((m !== 'signal' && m !== 'live') || m === mode) return;
      mode = m;
      if (mode !== 'live') frozen = false;   // freezing is for Live only
      save();
      paint();
      syncBus();
    }

    function setView(v) {
      if ((v !== 'lr' && v !== 'xy') || v === view) return;
      view = v;
      save();
      paint();
      requestDraw();
    }

    function onClick(e) {
      const b = e.target.closest('button');
      if (!b || !box.contains(b)) return;
      if (b.dataset.act === 'freeze') {
        if (mode !== 'live') return;
        frozen = !frozen;
        paint();
        syncBus();
        return;
      }
      const group = b.parentElement && b.parentElement.dataset.seg;
      if (group === 'mode') setMode(b.dataset.val);
      else if (group === 'view') setView(b.dataset.val);
    }

    function onChange(e) {
      if (e.target !== els.select) return;
      const v = +els.select.value;
      if (!SPANS.includes(v) || v === ms) return;
      ms = v;
      save();
      requestDraw();
    }

    // Theme changed (the app's theme, or the system's when the app follows it): read the colours again.
    function onTheme() {
      theme = null;
      requestDraw();
    }

    // ---------- the API ----------

    // A render's preview: { rate, l, r } (arrays of floats). Missing or short arrays are fine (zeros fill).
    function setPreview(p) {
      if (dead) return;
      const l = p && p.l;
      const r = p && p.r;
      const nl = l && l.length > 0 ? l.length : 0;
      const nr = r && r.length > 0 ? r.length : 0;
      const len = Math.min(1 << 20, Math.max(nl, nr));
      let cap = 2;
      while (cap < len) cap *= 2;
      if (!sig || sig.cap < cap) sig = makeRing(cap);
      const clean = (arr, i) => {
        const v = arr && i < arr.length ? +arr[i] : 0;
        return Number.isFinite(v) ? v : 0;
      };
      for (let i = 0; i < len; i++) {
        sig.l[i] = clean(l, i);
        sig.r[i] = clean(r, i);
      }
      sig.end = len & sig.mask;
      sig.have = len;
      sigRate = +(p && p.rate) > 0 ? +p.rate : DEFAULT_RATE;
      requestDraw();
    }

    function show(on) {
      if (dead) return;
      const want = !!on;
      if (want === shown) return;
      shown = want;
      if (!shown) {
        cancelAnimationFrame(raf);
        raf = 0;
      }
      if (shown) dirty = true;
      syncBus();
    }

    function destroy() {
      if (dead) return;
      dead = true;
      shown = false;
      cancelAnimationFrame(raf);
      raf = 0;
      syncBus();
      if (ro) ro.disconnect();
      if (mo) mo.disconnect();
      if (mq && mq.removeEventListener) mq.removeEventListener('change', onTheme);
      box.removeEventListener('click', onClick);
      box.removeEventListener('change', onChange);
      box.remove();
    }

    if (!dead) {
      box.addEventListener('click', onClick);
      box.addEventListener('change', onChange);
      ro = typeof ResizeObserver === 'function' ? new ResizeObserver(requestDraw) : null;
      if (ro) ro.observe(els.plot);
      mo = new MutationObserver(onTheme);
      mo.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme', 'class'] });
      if (window.matchMedia) {
        mq = window.matchMedia('(prefers-color-scheme: dark)');
        if (mq.addEventListener) mq.addEventListener('change', onTheme);
      }
      paint();
    }

    return { setPreview, show, destroy };
  }

  V.SignalScope = { create };
})();
