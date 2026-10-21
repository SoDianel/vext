// The Analyzer's timeline (the small strip at the top of the section): the sound of the Playground's tab
// as a play line. The Audio tab's file: its waveform (the backend's peaks), the playhead, the selection
// and the loop, the very ones the Playground's waveform has (VEXT.playground keeps them). The browser:
// its player's timeline (a YouTube video, a song); its waveform can't be known before it plays, so it is
// drawn from what the Analyzer receives as it plays.
// Click = play from there (a selection goes), drag = select (making one loops it), Loop: No loop ·
// Selection · All. The playhead waits while the Plugin buffer refills (host `buffering`).
// Layout: the title, the times and the Loop control on top; under them the lead (the Analyzer puts the
// Play button there) and the strip, which takes the rest of the row.
(function () {
  const V = window.VEXT;
  const BROWSER_COLS = 1200;   // the browser's waveform: one level per 1/1200 of the media
  const DRAG_PX = 3;           // a press that moves less than this is a click

  const fmt = (s) => {
    if (s == null || !isFinite(s)) return '–:––';
    const t = Math.max(0, s);
    const h = Math.floor(t / 3600);
    const m = Math.floor((t % 3600) / 60);
    const sec = Math.floor(t % 60);
    return h ? `${h}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}` : `${m}:${String(sec).padStart(2, '0')}`;
  };

  // VXPK (GET /api/playground/audio/peaks): uint32 magic, kind (1 samples, 2 min/max pairs), channels, count,
  // then float32. Each column's level: the largest magnitude over its channels.
  function levels(buf) {
    if (!buf || buf.byteLength < 16) return null;
    const head = new DataView(buf, 0, 16);
    const kind = head.getUint32(4, true);
    const channels = Math.max(1, head.getUint32(8, true));
    const count = head.getUint32(12, true);
    const arr = new Float32Array(buf, 16, (buf.byteLength - 16) >> 2);
    const per = kind === 2 ? channels * 2 : channels;
    const out = new Float32Array(count);
    for (let i = 0; i < count; i++) {
      let m = 0;
      for (let k = 0; k < per; k++) {
        const v = Math.abs(arr[i * per + k] || 0);
        if (v > m) m = v;
      }
      out[i] = Math.min(1, m);
    }
    return out;
  }

  function create(box) {
    box.innerHTML = `
      <div class="an-tl" role="group" aria-label="Timeline of what the Playground plays">
        <div class="an-tl-top">
          <span class="an-tl-title"></span>
          <span class="an-tl-time an-tl-cur">–:––</span>
          <span class="an-tl-sep" aria-hidden="true">/</span>
          <span class="an-tl-time an-tl-dur">–:––</span>
          <span class="an-spacer"></span>
          <div class="an-field"><span class="an-label">Loop</span>
            <div class="segmented an-seg an-tl-loop" role="group" aria-label="Loop">
              <button type="button" data-loop="off" aria-pressed="false">Off</button>
              <button type="button" data-loop="sel" aria-pressed="false" title="Loop the selection (without one, the whole timeline)">Selection</button>
              <button type="button" data-loop="all" aria-pressed="false" title="Loop the whole timeline, even with a selection">All</button>
            </div>
          </div>
        </div>
        <div class="an-tl-row">
          <div class="an-tl-lead"></div>
          <div class="an-tl-track" title="Click: play from there · Drag: select a part (it loops)">
            <canvas class="an-tl-canvas" aria-hidden="true"></canvas>
            <div class="an-tl-sel" hidden></div>
            <div class="an-tl-head" hidden></div>
            <div class="an-tl-msg"></div>
          </div>
        </div>
      </div>`;
    const q = (s) => box.querySelector(s);
    const els = {
      title: q('.an-tl-title'), cur: q('.an-tl-cur'), dur: q('.an-tl-dur'), lead: q('.an-tl-lead'), track: q('.an-tl-track'),
      canvas: q('.an-tl-canvas'), sel: q('.an-tl-sel'), head: q('.an-tl-head'), msg: q('.an-tl-msg'),
      loops: [...box.querySelectorAll('[data-loop]')],
    };
    let alive = true;
    let raf = 0;
    let st = null;           // the last VEXT.playground.timeline()
    let drag = null;         // {id, x0, t0, moved, a, b} while the pointer is down
    let drawnKey = '';       // what the canvas shows (redrawn only when it changes)
    let file = { key: '', cols: null, busy: false };          // the file's levels
    let web = { key: '', cols: new Float32Array(BROWSER_COLS), version: 0, drawnAt: 0 }; // the browser's
    let last = {};           // text / style last written, to skip identical writes

    const pg = () => V.playground;
    const set = (key, value, write) => { if (last[key] !== value) { last[key] = value; write(value); } };

    function readState() {
      try { return pg() && pg().timeline ? pg().timeline() : null; } catch { return null; }
    }

    async function fetchFile(track, cols) {
      const key = `${track.version}:${track.frames}:${cols}`;
      if (file.key === key || file.busy) return;
      file.busy = true;
      try {
        const q2 = new URLSearchParams({ which: 'source', start: 0, end: track.frames, width: cols });
        const res = await fetch(`${V.API_BASE || ''}/api/playground/audio/peaks?${q2}`);
        if (!res.ok) throw new Error(res.statusText);
        const got = levels(await res.arrayBuffer());
        if (alive && got) file = { key, cols: got, busy: false };
      } catch { /* backend busy: asked again on the next frame */ }
      finally { file.busy = false; }
    }

    function paint(cols, W, H) {
      const c = els.canvas;
      const dpr = window.devicePixelRatio || 1;
      const w = Math.max(1, Math.round(W * dpr));
      const h = Math.max(1, Math.round(H * dpr));
      if (c.width !== w || c.height !== h) { c.width = w; c.height = h; }
      const g = c.getContext('2d');
      g.clearRect(0, 0, w, h);
      const style = getComputedStyle(c);
      g.fillStyle = style.outlineColor || style.color;
      g.fillRect(0, Math.round(h / 2), w, Math.max(1, Math.round(dpr)));   // the centre line
      if (!cols || !cols.length) return;
      g.fillStyle = style.color;
      const n = cols.length;
      // An overview: scaled to its loudest part, so a quiet file shows its shape too.
      let top = 0;
      for (let i = 0; i < n; i++) if (cols[i] > top) top = cols[i];
      const scale = top > 1e-5 ? 1 / top : 1;
      for (let x = 0; x < w; x++) {
        // The loudest of the columns under this pixel.
        const i0 = Math.floor((x / w) * n);
        const i1 = Math.max(i0 + 1, Math.floor(((x + 1) / w) * n));
        let m = 0;
        for (let i = i0; i < i1 && i < n; i++) if (cols[i] > m) m = cols[i];
        if (m <= 0) continue;
        const bar = Math.max(1, m * scale * h * 0.92);
        g.fillRect(x, (h - bar) / 2, 1, bar);
      }
    }

    function draw() {
      const W = els.track.clientWidth;
      const H = els.track.clientHeight;
      const has = !!(st && st.has && st.duration > 0);
      const d = has ? st.duration : 0;
      const msg = !st ? '' : has ? '' : st.kind === 'browser'
        ? 'Play something in the Playground\'s browser: its timeline shows here.'
        : 'Open an audio file in the Playground\'s Audio tab: its waveform shows here.';
      set('msg', msg, (v) => { els.msg.textContent = v; els.msg.hidden = !v; });
      set('title', has ? st.title : '', (v) => { els.title.textContent = v; els.title.title = v; });
      els.track.classList.toggle('is-empty', !has);
      for (const b of els.loops) {
        const on = !!st && st.loop === b.dataset.loop;
        b.setAttribute('aria-pressed', String(on));
        b.disabled = !has;
      }
      set('dur', has ? fmt(d) : '–:––', (v) => { els.dur.textContent = v; });
      const pos = has ? Math.min(Math.max(0, st.position), d) : 0;
      set('cur', has ? fmt(pos) : '–:––', (v) => { els.cur.textContent = v; });

      // The waveform: the file's levels, or the browser's as heard so far.
      let cols = null;
      let key = `${W}x${H}`;
      if (has && st.kind === 'file' && st.track && st.track.frames) {
        const want = Math.max(2, Math.min(4000, Math.round(W * (window.devicePixelRatio || 1))));
        fetchFile(st.track, want);
        cols = file.cols;
        key += `|file|${file.key}`;
      } else if (has && st.kind === 'browser') {
        const id = `${st.title}|${Math.round(d)}`;
        if (web.key !== id) { web = { key: id, cols: new Float32Array(BROWSER_COLS), version: 0, drawnAt: 0 }; }
        cols = web.cols;
        // Grows as it plays: redrawn at most 8 times a second.
        const now = performance.now();
        if (now - web.drawnAt > 125) web.drawnAt = now;
        key += `|web|${web.key}|${web.drawnAt}`;
      }
      if (key !== drawnKey) {
        drawnKey = key;
        paint(cols, W, H);
      }

      // The selection (the one being dragged wins) and the playhead.
      const s = drag && drag.moved ? { a: drag.a, b: drag.b } : has ? st.sel : null;
      const selCss = s && d ? `${(s.a / d) * 100}%|${((s.b - s.a) / d) * 100}%` : '';
      set('sel', selCss, (v) => {
        els.sel.hidden = !v;
        if (v) { const [l, w] = v.split('|'); els.sel.style.left = l; els.sel.style.width = w; }
      });
      const head = has ? `${(pos / d) * 100}%` : '';
      set('head', head, (v) => { els.head.hidden = !v; if (v) els.head.style.left = v; });
      els.head.classList.toggle('is-waiting', !!(st && st.buffering));
      els.head.classList.toggle('is-playing', !!(st && st.playing));
    }

    function frame() {
      raf = 0;
      if (!alive) return;
      st = readState();
      draw();
      raf = requestAnimationFrame(frame);
    }

    const timeAt = (e) => {
      const r = els.track.getBoundingClientRect();
      const x = Math.min(Math.max(0, e.clientX - r.left), r.width);
      return st && st.duration > 0 && r.width ? (x / r.width) * st.duration : 0;
    };

    function onDown(e) {
      if (e.button !== 0 || !st || !st.has || !(st.duration > 0)) return;
      els.track.setPointerCapture(e.pointerId);
      const t = timeAt(e);
      drag = { id: e.pointerId, x0: e.clientX, t0: t, moved: false, a: t, b: t };
      e.preventDefault();
    }
    function onMove(e) {
      if (!drag || e.pointerId !== drag.id) return;
      if (!drag.moved && Math.abs(e.clientX - drag.x0) < DRAG_PX) return;
      drag.moved = true;
      const t = timeAt(e);
      drag.a = Math.min(drag.t0, t);
      drag.b = Math.max(drag.t0, t);
    }
    function onUp(e) {
      if (!drag || e.pointerId !== drag.id) return;
      const d = drag;
      drag = null;
      const p = pg();
      if (!p) return;
      try {
        if (d.moved && d.b - d.a >= 0.01) p.select({ a: d.a, b: d.b });
        else p.seek(d.t0);
      } catch (err) { V.toast(err.message || String(err), true); }
    }
    function onCancel() { drag = null; }
    function onLoop(e) {
      const b = e.target.closest('[data-loop]');
      if (!b || b.disabled || !pg()) return;
      try { pg().setLoop(b.dataset.loop); } catch (err) { V.toast(err.message || String(err), true); }
    }

    els.track.addEventListener('pointerdown', onDown);
    els.track.addEventListener('pointermove', onMove);
    els.track.addEventListener('pointerup', onUp);
    els.track.addEventListener('pointercancel', onCancel);
    box.addEventListener('click', onLoop);
    raf = requestAnimationFrame(frame);

    return {
      // The Play button goes here (before the strip); the Analyzer appends it.
      lead: els.lead,
      // A block the Analyzer received (VEXT.audioBus): while the browser plays, its level goes into the
      // browser's waveform at the player's position.
      feed(f) {
        if (!st || st.kind !== 'browser' || !st.playing || !(st.duration > 0)) return;
        const frames = f.after || f.before;
        if (!frames) return;
        let m = 0;
        for (let i = 0; i < frames.length; i++) {
          const v = frames[i] < 0 ? -frames[i] : frames[i];
          if (v > m) m = v;
        }
        const i = Math.min(BROWSER_COLS - 1, Math.floor((st.position / st.duration) * BROWSER_COLS));
        if (i >= 0 && m > web.cols[i]) web.cols[i] = Math.min(1, m);
      },
      // The theme changed: the canvas reads its colours again.
      recolor() { drawnKey = ''; },
      destroy() {
        alive = false;
        cancelAnimationFrame(raf);
        box.removeEventListener('click', onLoop);
        box.innerHTML = '';
      },
    };
  }

  V.AnalyzerTimeline = { create };
})();
