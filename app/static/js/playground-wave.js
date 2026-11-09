// The Playground's Audio tab waveform (VEXT.WaveView; js/playground.js drives it):
// two panes of equal height on one time axis, the loaded file on top and the
// recording after the plugin below, a time ruler and a scroll bar.
//
// Peaks come from the backend per view (GET /api/playground/audio/peaks: frames
// [start, end) in as many columns as the canvas has device pixels; binary, see
// app/playground_audio.py). Zoomed out they are min/max per pixel from the peak
// pyramid; zoomed in past one sample per pixel they are the samples themselves,
// drawn as a line with dots. While a new view's peaks are on their way, the last
// ones are stretched onto it, so zooming and scrolling never wait.
//
// Mouse: click = move the playhead (onSeek), drag = select (onSelect), Shift+click
// extends the selection, double-click selects everything. Wheel = zoom at the
// pointer (a touchpad pinch too), Shift+wheel or a sideways wheel = scroll.
// Keys (when the waveform has focus): ← → scroll, + − zoom, Home / End, Esc
// drops the selection.
//
// Colours come from the theme (css/playground.css sets them on the canvases);
// a theme switch redraws. Visual effects off: zoom buttons jump instead of glide.
(function () {
  const V = window.VEXT;
  if (!V || V.WaveView) return;

  const PANES = ['source', 'recording'];
  const MAX_PX_PER_SAMPLE = 24;    // the deepest zoom: one sample every 24 px
  const DOT_PX = 4;                // samples at least this far apart get a dot
  const MAX_COLUMNS = 8192;
  const KIND = { EMPTY: 0, SAMPLES: 1, PEAKS: 2 };
  const ZOOM_MS = 140;
  const RULER_STEPS = [0.0001, 0.0002, 0.0005, 0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5,
    1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600];

  const fxOn = () => !!(V.fx && typeof V.fx.enabled === 'function' && V.fx.enabled());

  function fmtRuler(t, step) {
    const neg = t < 0 ? '-' : '';
    const decimals = step >= 1 ? 0 : step >= 0.1 ? 1 : step >= 0.01 ? 2 : step >= 0.001 ? 3 : 4;
    // Rounded first: 299.99999 is 5:00.0000, not 4:60.0000.
    const units = Math.round(Math.abs(t) * 10 ** decimals);
    const per = 60 * 10 ** decimals;
    const m = Math.floor(units / per);
    const sec = ((units % per) / 10 ** decimals).toFixed(decimals);
    const [whole, frac] = sec.split('.');
    return `${neg}${m}:${whole.padStart(2, '0')}${frac ? `.${frac}` : ''}`;
  }

  // Any CSS colour (color-mix included) as something a canvas takes: drawn once into a 1x1 canvas.
  const probe = document.createElement('canvas');
  probe.width = probe.height = 1;
  const probeCtx = probe.getContext('2d', { willReadFrequently: true });
  function canvasColor(css, fallback) {
    if (!css) return fallback;
    probeCtx.clearRect(0, 0, 1, 1);
    probeCtx.fillStyle = fallback;
    probeCtx.fillStyle = css;
    probeCtx.fillRect(0, 0, 1, 1);
    const [r, g, b, a] = probeCtx.getImageData(0, 0, 1, 1).data;
    return `rgba(${r}, ${g}, ${b}, ${(a / 255).toFixed(3)})`;
  }

  function parsePeaks(buf) {
    if (!buf || buf.byteLength < 16) return null;
    const head = new DataView(buf, 0, 16);
    const kind = head.getUint32(4, true);
    const channels = head.getUint32(8, true);
    const count = head.getUint32(12, true);
    const arr = new Float32Array(buf, 16, (buf.byteLength - 16) >> 2);
    return { kind, channels, count, arr };
  }

  function create(box, hooks = {}) {
    box.innerHTML = `
      <div class="pg-wv" tabindex="0" aria-label="Waveform: click to move the playhead, drag to select, wheel to zoom">
        <div class="pg-wv-ruler"><canvas aria-hidden="true"></canvas></div>
        ${PANES.map(p => `
          <section class="pg-wv-pane" data-pane="${p}">
            <div class="pg-wv-head">
              <span class="pg-wv-label"></span>
              <span class="pg-wv-info"></span>
              <span class="pg-wv-tools"></span>
            </div>
            <div class="pg-wv-body">
              <canvas class="pg-wv-canvas" aria-hidden="true"></canvas>
              <div class="pg-wv-sel" hidden></div>
              <div class="pg-wv-cursor" hidden></div>
              <div class="pg-wv-msg" hidden></div>
            </div>
          </section>`).join('')}
        <div class="pg-wv-scroll" aria-hidden="true"><div class="pg-wv-thumb"></div></div>
      </div>`;
    const root = box.querySelector('.pg-wv');
    const rulerBox = root.querySelector('.pg-wv-ruler');
    const ruler = rulerBox.querySelector('canvas');
    const scroll = root.querySelector('.pg-wv-scroll');
    const thumb = root.querySelector('.pg-wv-thumb');
    const pane = {};
    for (const p of PANES) {
      const el = root.querySelector(`.pg-wv-pane[data-pane="${p}"]`);
      pane[p] = {
        el, body: el.querySelector('.pg-wv-body'), canvas: el.querySelector('canvas'),
        sel: el.querySelector('.pg-wv-sel'), cursor: el.querySelector('.pg-wv-cursor'), msg: el.querySelector('.pg-wv-msg'),
        label: el.querySelector('.pg-wv-label'), info: el.querySelector('.pg-wv-info'), tools: el.querySelector('.pg-wv-tools'),
      };
    }

    const view = { start: 0, span: 0 };      // seconds
    let duration = 0;                        // the time axis (the loaded file's length)
    const tracks = { source: null, recording: null };   // {rate, channels, frames, version}
    const data = { source: null, recording: null };     // the last peaks: {key, start, end, rate, kind, channels, count, arr}
    const busy = { source: false, recording: false };
    const again = { source: false, recording: false };
    let sel = null;                          // {a, b} seconds
    let playhead = null;
    let looping = false;
    let colors = null;
    let drawRaf = 0;
    let anim = 0;
    let dpr = window.devicePixelRatio || 1;

    // ---------- geometry ----------

    const cssWidth = () => pane.source.body.clientWidth || pane.recording.body.clientWidth || 0;
    const rateMax = () => Math.max(1, ...PANES.map(p => (tracks[p] ? tracks[p].rate : 0)));
    const minSpan = () => Math.max(1e-4, cssWidth() / (rateMax() * MAX_PX_PER_SAMPLE));
    const maxSpan = () => Math.max(minSpan(), duration || 1);

    function clampView(v = view) {
      v.span = Math.min(maxSpan(), Math.max(minSpan(), v.span || maxSpan()));
      v.start = Math.max(0, Math.min(v.start, Math.max(0, duration - v.span)));
      return v;
    }

    const timeAt = (clientX) => {
      const r = pane.source.body.getBoundingClientRect();
      const x = Math.max(0, Math.min(r.width, clientX - r.left));
      return Math.max(0, Math.min(duration, view.start + (x / (r.width || 1)) * view.span));
    };
    const pct = (t) => ((t - view.start) / (view.span || 1)) * 100;

    // ---------- colours ----------

    function readColors() {
      const src = getComputedStyle(pane.source.canvas);
      const rec = getComputedStyle(pane.recording.canvas);
      const rul = getComputedStyle(ruler);
      colors = {
        source: canvasColor(src.color, '#888'),
        recording: canvasColor(rec.color, '#a9927d'),
        mid: canvasColor(src.outlineColor, 'rgba(128,128,128,0.3)'),
        lane: canvasColor(src.columnRuleColor, 'rgba(128,128,128,0.2)'),
        tick: canvasColor(rul.outlineColor, 'rgba(128,128,128,0.5)'),
        text: canvasColor(rul.color, '#888'),
      };
    }

    // ---------- fetching ----------

    async function fetchPane(p) {
      const tr = tracks[p];
      if (!tr || !tr.frames || !view.span) { data[p] = null; scheduleDraw(); return; }
      const width = Math.min(MAX_COLUMNS, Math.round(pane[p].body.clientWidth * dpr));
      if (width < 2) return;
      const start = Math.max(0, Math.floor(view.start * tr.rate));
      const end = Math.min(tr.frames, Math.ceil((view.start + view.span) * tr.rate) + 1);
      const key = `${tr.version}:${start}:${end}:${width}`;
      if (data[p] && data[p].key === key) return;
      if (busy[p]) { again[p] = true; return; }
      busy[p] = true;
      try {
        const q = new URLSearchParams({ which: p, start, end, width });
        const res = await fetch(`${V.API_BASE || ''}/api/playground/audio/peaks?${q}`);
        if (!res.ok) throw new Error(res.statusText);
        const got = parsePeaks(await res.arrayBuffer());
        if (got && tracks[p] === tr) {
          data[p] = { key, start, end, rate: tr.rate, ...got };
          scheduleDraw();
        }
      } catch { /* backend busy: the next view change asks again */ }
      finally {
        busy[p] = false;
        if (again[p]) { again[p] = false; fetchPane(p); }
      }
    }

    function fetchAll() { for (const p of PANES) fetchPane(p); }

    // ---------- drawing ----------

    function sizeCanvas(c, el) {
      const w = Math.max(1, Math.round(el.clientWidth * dpr));
      const h = Math.max(1, Math.round(el.clientHeight * dpr));
      if (c.width !== w || c.height !== h) { c.width = w; c.height = h; }
      return [w, h];
    }

    function drawPane(p) {
      const { canvas, body } = pane[p];
      const [W, H] = sizeCanvas(canvas, body);
      const ctx = canvas.getContext('2d');
      ctx.clearRect(0, 0, W, H);
      const d = data[p];
      const tr = tracks[p];
      if (!tr || !view.span) return;
      const channels = (d && d.channels) || tr.channels || 1;
      const laneH = H / channels;
      // Lanes and their centre lines.
      ctx.fillStyle = colors.mid;
      for (let lane = 0; lane < channels; lane++) ctx.fillRect(0, Math.round(laneH * (lane + 0.5)), W, Math.max(1, Math.round(dpr)));
      ctx.fillStyle = colors.lane;
      for (let lane = 1; lane < channels; lane++) ctx.fillRect(0, Math.round(laneH * lane), W, Math.max(1, Math.round(dpr)));
      // Past the end of the track (a recording at another rate, rounding): nothing to draw there.
      if (!d || !d.count || d.kind === KIND.EMPTY) return;
      const toX = (frame) => ((frame / d.rate - view.start) / view.span) * W;
      ctx.fillStyle = colors[p];
      ctx.strokeStyle = colors[p];
      const amp = (laneH / 2) * 0.92;
      const clamp = (v) => (v > 1 ? 1 : v < -1 ? -1 : v);
      if (d.kind === KIND.PEAKS) {
        const per = (d.end - d.start) / d.count;
        for (let lane = 0; lane < d.channels; lane++) {
          const cy = laneH * (lane + 0.5);
          for (let i = 0; i < d.count; i++) {
            const x0 = toX(d.start + i * per);
            const x1 = toX(d.start + (i + 1) * per);
            if (x1 < 0 || x0 > W) continue;
            const k = (i * d.channels + lane) * 2;
            const top = cy - clamp(d.arr[k + 1]) * amp;
            const bottom = cy - clamp(d.arr[k]) * amp;
            const x = Math.floor(x0);
            ctx.fillRect(x, top, Math.max(1, Math.ceil(x1) - x), Math.max(1, bottom - top));
          }
        }
      } else {
        const step = W / ((view.span * d.rate) || 1);   // device px per sample
        const dots = step >= DOT_PX * dpr;
        ctx.lineWidth = Math.max(1, dpr);
        for (let lane = 0; lane < d.channels; lane++) {
          const cy = laneH * (lane + 0.5);
          ctx.beginPath();
          for (let i = 0; i < d.count; i++) {
            const x = toX(d.start + i);
            const y = cy - clamp(d.arr[i * d.channels + lane]) * amp;
            if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y);
          }
          ctx.stroke();
          if (dots) {
            const r = Math.min(3.5, step / 4) * 1;
            for (let i = 0; i < d.count; i++) {
              const x = toX(d.start + i);
              if (x < -r || x > W + r) continue;
              ctx.beginPath();
              ctx.arc(x, cy - clamp(d.arr[i * d.channels + lane]) * amp, Math.max(1.5 * dpr, r), 0, Math.PI * 2);
              ctx.fill();
            }
          }
        }
      }
    }

    function drawRuler() {
      const [W, H] = sizeCanvas(ruler, rulerBox);
      const ctx = ruler.getContext('2d');
      ctx.clearRect(0, 0, W, H);
      if (!duration || !view.span) return;
      const cssW = W / dpr;
      const want = view.span * (80 / Math.max(1, cssW));   // a label every ~80 px
      const step = RULER_STEPS.find(s => s >= want) || RULER_STEPS[RULER_STEPS.length - 1];
      const minor = step / 5;
      const toX = (t) => ((t - view.start) / view.span) * W;
      ctx.fillStyle = colors.tick;
      const first = Math.floor(view.start / minor) * minor;
      for (let t = first, i = 0; t <= view.start + view.span + minor && i < 2000; t += minor, i++) {
        const major = Math.abs(t / step - Math.round(t / step)) < 1e-6;
        const x = Math.round(toX(t));
        ctx.fillRect(x, major ? H * 0.45 : H * 0.72, Math.max(1, Math.round(dpr)), H);
      }
      ctx.fillStyle = colors.text;
      ctx.font = `${10.5 * dpr}px ${getComputedStyle(ruler).fontFamily || 'sans-serif'}`;
      ctx.textBaseline = 'top';
      const firstMajor = Math.floor(view.start / step) * step;
      for (let t = firstMajor, i = 0; t <= view.start + view.span && i < 400; t += step, i++) {
        if (t < 0 || toX(t) < -1) continue;
        ctx.fillText(fmtRuler(t, step), Math.round(toX(t)) + 3 * dpr, 1 * dpr);
      }
    }

    function placeOverlays() {
      for (const p of PANES) {
        const { sel: s, cursor } = pane[p];
        const show = !!tracks.source && duration > 0;
        if (sel && show && sel.b > view.start && sel.a < view.start + view.span) {
          const l = Math.max(0, pct(sel.a));
          const r = Math.min(100, pct(sel.b));
          s.hidden = false;
          s.style.left = `${l}%`;
          s.style.width = `${Math.max(0, r - l)}%`;
          s.classList.toggle('is-loop', looping);
        } else {
          s.hidden = true;
        }
        if (playhead != null && show && playhead >= view.start && playhead <= view.start + view.span) {
          cursor.hidden = false;
          cursor.style.left = `${pct(playhead)}%`;
        } else {
          cursor.hidden = true;
        }
      }
      // The scroll bar: the part of the file shown.
      if (duration > 0 && view.span < duration - 1e-9) {
        scroll.classList.remove('is-idle');
        thumb.style.left = `${(view.start / duration) * 100}%`;
        thumb.style.width = `${Math.max(2, (view.span / duration) * 100)}%`;
      } else {
        scroll.classList.add('is-idle');
        thumb.style.left = '0';
        thumb.style.width = '100%';
      }
    }

    function draw() {
      drawRaf = 0;
      dpr = window.devicePixelRatio || 1;
      if (!colors) readColors();
      drawRuler();
      for (const p of PANES) drawPane(p);
      placeOverlays();
    }

    function scheduleDraw() { if (!drawRaf) drawRaf = requestAnimationFrame(draw); }

    function viewChanged() {
      clampView();
      scheduleDraw();
      fetchAll();
      if (hooks.onView) hooks.onView({ ...view, duration });
    }

    // The panes changed size (the observer): clamp, redraw, ask for peaks again.
    function resized() {
      clampView();
      scheduleDraw();
      fetchAll();
      if (hooks.onView) hooks.onView({ ...view, duration });
    }

    // ---------- zoom and scroll ----------

    function setView(start, span, animate = false) {
      const target = clampView({ start, span });
      cancelAnimationFrame(anim);
      anim = 0;
      if (!animate || !fxOn() || !view.span) {
        view.start = target.start;
        view.span = target.span;
        viewChanged();
        return;
      }
      const from = { ...view };
      const t0 = performance.now();
      const ease = (x) => 1 - Math.pow(1 - x, 3);
      // Glide in log-span (zoom feels even) and keep the point under the centre steady.
      const step = (now) => {
        const k = ease(Math.min(1, (now - t0) / ZOOM_MS));
        view.span = Math.exp(Math.log(from.span) + (Math.log(target.span) - Math.log(from.span)) * k);
        view.start = from.start + (target.start - from.start) * k;
        clampView();
        scheduleDraw();
        if (k < 1) {
          if (hooks.onView) hooks.onView({ ...view, duration });
          anim = requestAnimationFrame(step);
        } else { anim = 0; viewChanged(); }
      };
      anim = requestAnimationFrame(step);
    }

    function zoomAt(t, factor, animate) {
      const span = Math.min(maxSpan(), Math.max(minSpan(), view.span * factor));
      const frac = view.span ? (t - view.start) / view.span : 0.5;
      setView(t - frac * span, span, animate);
    }

    function zoom(how) {
      if (!duration) return;
      if (how === 'fit') { setView(0, maxSpan(), true); return; }
      const center = playhead != null && playhead >= view.start && playhead <= view.start + view.span
        ? playhead : view.start + view.span / 2;
      zoomAt(center, how === 'in' ? 0.5 : 2, true);
    }

    function scrollBy(seconds) { setView(view.start + seconds, view.span); }

    // ---------- pointer ----------

    let drag = null;   // {x, t, moved, pointerId}
    for (const p of PANES) {
      const body = pane[p].body;
      body.addEventListener('pointerdown', (e) => {
        if (e.button !== 0 || !duration) return;
        root.focus({ preventScroll: true, focusVisible: false });
        e.preventDefault();
        try { body.setPointerCapture(e.pointerId); } catch { /* gone */ }
        const t = timeAt(e.clientX);
        if (e.shiftKey) {
          const anchor = sel ? (Math.abs(t - sel.a) < Math.abs(t - sel.b) ? sel.b : sel.a) : (playhead || 0);
          drag = { x: e.clientX, t: anchor, moved: true, pointerId: e.pointerId };
          setSelection({ a: Math.min(anchor, t), b: Math.max(anchor, t) }, true);
          return;
        }
        drag = { x: e.clientX, t, moved: false, pointerId: e.pointerId };
      });
      body.addEventListener('pointermove', (e) => {
        if (!drag || e.pointerId !== drag.pointerId) return;
        if (!drag.moved && Math.abs(e.clientX - drag.x) < 4) return;
        drag.moved = true;
        const t = timeAt(e.clientX);
        setSelection({ a: Math.min(drag.t, t), b: Math.max(drag.t, t) }, false);
        // Dragging past an edge scrolls.
        const r = body.getBoundingClientRect();
        if (e.clientX < r.left + 6) scrollBy(-view.span * 0.02);
        else if (e.clientX > r.right - 6) scrollBy(view.span * 0.02);
      });
      const up = (e) => {
        if (!drag || e.pointerId !== drag.pointerId) return;
        const d = drag;
        drag = null;
        if (d.moved) {
          if (sel && sel.b - sel.a < 0.0005) setSelection(null, true);
          else if (hooks.onSelect) hooks.onSelect(sel ? { ...sel } : null);
          return;
        }
        setSelection(null, true);
        if (hooks.onSeek) hooks.onSeek(d.t);
      };
      body.addEventListener('pointerup', up);
      body.addEventListener('pointercancel', up);
      body.addEventListener('dblclick', () => { if (duration) setSelection({ a: 0, b: duration }, true); });
      body.addEventListener('wheel', (e) => {
        if (!duration) return;
        e.preventDefault();
        const dx = e.deltaMode === 1 ? e.deltaX * 16 : e.deltaX;
        const dy = e.deltaMode === 1 ? e.deltaY * 16 : e.deltaY;
        const w = body.clientWidth || 1;
        if (e.shiftKey || Math.abs(dx) > Math.abs(dy)) {
          scrollBy(((e.shiftKey ? dy || dx : dx) / w) * view.span);
          return;
        }
        // Pinch (ctrlKey on a touchpad) is finer-grained: a stronger factor per unit.
        const factor = Math.exp(dy * (e.ctrlKey ? 0.01 : 0.0018));
        zoomAt(timeAt(e.clientX), factor, false);
      }, { passive: false });
    }

    // The scroll bar: drag the thumb, or click beside it to page.
    let thumbDrag = null;
    scroll.addEventListener('pointerdown', (e) => {
      if (e.button !== 0 || !duration || scroll.classList.contains('is-idle')) return;
      e.preventDefault();
      const r = scroll.getBoundingClientRect();
      if (e.target === thumb) {
        thumbDrag = { x: e.clientX, start: view.start, width: r.width, id: e.pointerId };
        try { scroll.setPointerCapture(e.pointerId); } catch { /* gone */ }
        return;
      }
      const t = ((e.clientX - r.left) / (r.width || 1)) * duration;
      setView(t < view.start ? view.start - view.span : view.start + view.span, view.span, true);
    });
    scroll.addEventListener('pointermove', (e) => {
      if (!thumbDrag || e.pointerId !== thumbDrag.id) return;
      setView(thumbDrag.start + ((e.clientX - thumbDrag.x) / (thumbDrag.width || 1)) * duration, view.span);
    });
    const thumbUp = (e) => { if (thumbDrag && e.pointerId === thumbDrag.id) thumbDrag = null; };
    scroll.addEventListener('pointerup', thumbUp);
    scroll.addEventListener('pointercancel', thumbUp);

    root.addEventListener('keydown', (e) => {
      if (!duration || e.ctrlKey || e.metaKey || e.altKey) return;
      const k = e.key;
      if (k === 'ArrowLeft' || k === 'ArrowRight') scrollBy((k === 'ArrowLeft' ? -1 : 1) * view.span * 0.1);
      else if (k === '+' || k === '=') zoom('in');
      else if (k === '-' || k === '_') zoom('out');
      else if (k === 'Home') setView(0, view.span, true);
      else if (k === 'End') setView(duration - view.span, view.span, true);
      else if (k === 'Escape' && sel) setSelection(null, true);
      else return;
      e.preventDefault();
      e.stopPropagation();
    });

    // ---------- size, theme ----------

    const ro = typeof ResizeObserver === 'function' ? new ResizeObserver(() => {
      if (!root.isConnected || !root.clientWidth) return;
      resized();
    }) : null;
    if (ro) for (const p of PANES) ro.observe(pane[p].body);
    const recolor = () => { colors = null; scheduleDraw(); };
    new MutationObserver(recolor).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme', 'class'] });
    if (window.matchMedia) {
      const mq = window.matchMedia('(prefers-color-scheme: dark)');
      if (mq.addEventListener) mq.addEventListener('change', recolor);
    }

    // ---------- the API ----------

    function setSelection(next, notify) {
      sel = next && next.b > next.a ? { a: Math.max(0, next.a), b: Math.min(duration, next.b) } : null;
      placeOverlays();
      if (notify && hooks.onSelect) hooks.onSelect(sel ? { ...sel } : null);
    }

    function setTrack(p, info) {
      const prev = tracks[p];
      tracks[p] = info && info.frames ? {
        rate: info.rate, channels: info.channels || 1, frames: info.frames, version: info.version || 0,
      } : null;
      if (p === 'source') {
        const dur = tracks.source ? tracks.source.frames / tracks.source.rate : 0;
        const fresh = !prev || !tracks.source || Math.abs(dur - duration) > 1e-6 || prev.rate !== tracks.source.rate;
        duration = dur;
        if (fresh) {
          view.start = 0;
          view.span = dur;
          sel = null;
          for (const q of PANES) data[q] = null;
        }
      }
      if (!tracks[p]) data[p] = null;
      if (!prev || !tracks[p] || prev.version !== tracks[p].version || prev.frames !== tracks[p].frames) viewChanged();
      else scheduleDraw();
    }

    // An opaque colour behind `el` (the first ancestor with a background), for a picture.
    function backdrop(el) {
      for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
        const c = getComputedStyle(n).backgroundColor;
        if (c && c !== 'transparent' && !/rgba\(.*,\s*0\)$/.test(c)) return c;
      }
      return getComputedStyle(document.body).backgroundColor || '#fff';
    }

    // The view as a picture (PNG blob): the ruler, then each pane of `which` ('source',
    // 'recording' or 'both') with its title and info line, the selection shaded. Same zoom
    // and size as on screen (device pixels).
    function snapshot(which) {
      draw(); // current, not a frame behind
      const panes = which === 'both' ? PANES : [which];
      const W = ruler.width;
      const headH = Math.round(20 * dpr);
      const heights = panes.map(p => pane[p].canvas.height);
      const H = ruler.height + heights.reduce((sum, h) => sum + headH + h, 0);
      const out = document.createElement('canvas');
      out.width = W;
      out.height = H;
      const ctx = out.getContext('2d');
      ctx.fillStyle = backdrop(root);
      ctx.fillRect(0, 0, W, H);
      ctx.drawImage(ruler, 0, 0);
      let y = ruler.height;
      const font = getComputedStyle(root).fontFamily || 'sans-serif';
      panes.forEach((p, i) => {
        const { label, info, canvas, sel: selEl } = pane[p];
        ctx.fillStyle = colors.lane;
        ctx.fillRect(0, y, W, Math.max(1, Math.round(dpr)));
        ctx.textBaseline = 'middle';
        ctx.fillStyle = colors.text;
        ctx.font = `600 ${11.5 * dpr}px ${font}`;
        const title = label.textContent.trim();
        ctx.fillText(title, 8 * dpr, y + headH / 2);
        ctx.font = `${10.5 * dpr}px ${font}`;
        ctx.fillText(info.textContent.trim(), 8 * dpr + ctx.measureText(title).width + 24 * dpr, y + headH / 2);
        y += headH;
        if (sel && view.span) {
          const x0 = ((sel.a - view.start) / view.span) * W;
          const x1 = ((sel.b - view.start) / view.span) * W;
          ctx.fillStyle = getComputedStyle(selEl).backgroundColor || 'rgba(128,128,128,0.2)';
          ctx.fillRect(Math.max(0, x0), y, Math.min(W, x1) - Math.max(0, x0), heights[i]);
        }
        ctx.drawImage(canvas, 0, y);
        y += heights[i];
      });
      return new Promise((resolve, reject) => out.toBlob(b => (b ? resolve(b) : reject(new Error('the picture could not be made'))), 'image/png'));
    }

    return {
      el: root,
      snapshot,
      head: (p) => ({ label: pane[p].label, info: pane[p].info, tools: pane[p].tools }),
      setSource: (info) => setTrack('source', info),
      setRecording: (info) => setTrack('recording', info),
      setPlayhead(t, follow) {
        playhead = t == null || !isFinite(t) ? null : Math.max(0, Math.min(duration, t));
        if (follow && playhead != null && view.span && !drag && !thumbDrag && !anim
          && (playhead < view.start || playhead > view.start + view.span)) {
          setView(playhead - view.span * 0.02, view.span);   // turn the page
          return;
        }
        placeOverlays();
      },
      setSelection: (s) => setSelection(s, false),
      getSelection: () => (sel ? { ...sel } : null),
      setLoop(on) { looping = !!on; placeOverlays(); },
      zoomAtTime: (t, factor) => zoomAt(t, factor, false),
      scrollBy,
      timeAt,
      message(p, html) {
        const m = pane[p].msg;
        if (m.dataset.html === (html || '')) return;
        m.dataset.html = html || '';
        m.innerHTML = html || '';
        m.hidden = !html;
      },
      zoom,
      view: () => ({ ...view, duration }),
      redraw() { colors = null; clampView(); scheduleDraw(); fetchAll(); },
    };
  }

  V.WaveView = { create, fmtRuler, canvasColor };
})();
