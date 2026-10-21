// The Scan hero's background: a dimmed wall of the owner's own plugin pictures.
//
// Rows of pictures (each whole, never cropped: a row's pictures share its height and keep
// their aspect ratio), tilted a little, toned with the palette (Dusty Taupe) and dimmed
// toward the page colour, drawn ONCE into a canvas per size and theme. The canvas then
// just sits there: no animation loop of its own, so with visual effects off it never moves;
// with them on, js/fx-scan3d.js shifts it with the pointer and lets it sway while a scan
// runs (a transform, nothing redrawn). style.css lays a veil over it (a calm middle for the
// button, the top and bottom fading into the page) and a plate under the text.
//
// Cheap on purpose: the pictures are the backend's thumbnail variant (<= 480x300 JPEG,
// cached as immutable, the same URLs Browse uses), at most MAX_PICS of them, drawn at 1x
// (a dimmed background doesn't need more); about 100 tiles take ~10-30 ms once. The canvas
// is released when the Scan section is left and drawn again on return. Fewer than MIN_PICS
// pictures: no wall, the palette gradient and dot grid under it (style.css) show alone.
// Classic script, loaded after js/fx.js; uses only window.VEXT and the DOM.
(function () {
  const V = window.VEXT;
  const canvas = document.getElementById('scan-wall-canvas');
  const hero = document.getElementById('scan-hero');
  if (!V || !canvas || !hero) return;

  const MAX_PICS = 36;
  const MIN_PICS = 5;
  const ANGLE = (-8 * Math.PI) / 180;
  const INDEX_TTL = 5 * 60 * 1000;   // the picture index is fetched again after this
  const LOAD_TIMEOUT = 8000;

  let index = null;                  // {plugin id: picture stamp}
  let indexAt = 0;
  let pics = [];                     // [{img, aspect}] in wall order
  let picsKey = '';                  // the ids and stamps `pics` was loaded for
  let loading = null;
  let drawnKey = '';                 // size + theme + pictures the canvas shows now
  let shown = false;
  let renderMs = 0;
  let resizeTimer = 0;
  // A new arrangement each time VEXT starts, the same one within a session.
  const seed = (Date.now() ^ (Math.random() * 0x7fffffff)) >>> 0;

  // ---------- colours from the stylesheet ----------
  const clamp01 = (v) => (v < 0 ? 0 : v > 1 ? 1 : v);
  function parseColor(s) {
    const nums = (String(s).match(/-?[\d.]+(?:e-?\d+)?/g) || []).map(Number);
    if (/^color\(srgb/i.test(s) && nums.length >= 3) return nums.slice(0, 3).map(v => Math.round(clamp01(v) * 255));
    if (/^rgba?\(/i.test(s) && nums.length >= 3) return nums.slice(0, 3).map(Math.round);
    return null;
  }
  function readColors() {
    const probe = document.createElement('i');
    probe.style.cssText = 'position:absolute;width:0;height:0;overflow:hidden;visibility:hidden;';
    hero.appendChild(probe);
    const read = (expr, fallback) => {
      probe.style.color = '';
      probe.style.color = expr;
      return parseColor(getComputedStyle(probe).color) || fallback;
    };
    const bg = read('var(--bg-content)', [242, 244, 243]);
    const tone = read('var(--vext-taupe)', [169, 146, 125]);
    probe.remove();
    const dark = (0.2126 * bg[0] + 0.7152 * bg[1] + 0.0722 * bg[2]) / 255 < 0.35;
    return { bg, tone, dark };
  }
  const rgba = ([r, g, b], a = 1) => `rgba(${r},${g},${b},${a})`;

  // ---------- the pictures ----------
  function rand(s) {
    let x = s >>> 0;
    return () => { x = (x * 1664525 + 1013904223) >>> 0; return x / 4294967296; };
  }
  const thumbUrl = (id, stamp) =>
    `${V.API_BASE}/api/plugins/${encodeURIComponent(id)}/image?variant=thumb&t=${encodeURIComponent(stamp)}`;

  // Loaded and decoded off the main thread (decode()), so drawing the wall is quick.
  function loadImage(src) {
    return new Promise((resolve) => {
      const img = new Image();
      let done = false;
      const finish = (ok) => { if (!done) { done = true; resolve(ok ? img : null); } };
      img.decoding = 'async';
      img.src = src;
      img.decode().then(() => finish(img.naturalWidth > 0), () => finish(false));
      setTimeout(() => finish(false), LOAD_TIMEOUT);
    });
  }

  async function ensurePictures() {
    if (!index || Date.now() - indexAt > INDEX_TTL) {
      try {
        index = await V.api('/api/pictures/index');
        indexAt = Date.now();
      } catch {
        index = index || {};
      }
    }
    const r = rand(seed);
    const ids = Object.keys(index || {});
    for (let i = ids.length - 1; i > 0; i--) { // seeded shuffle: same order for the same index
      const j = Math.floor(r() * (i + 1));
      [ids[i], ids[j]] = [ids[j], ids[i]];
    }
    const chosen = ids.slice(0, MAX_PICS);
    const key = chosen.map(id => `${id}:${index[id]}`).join(',');
    if (key === picsKey) return;
    const imgs = await Promise.all(chosen.map(id => loadImage(thumbUrl(id, index[id]))));
    pics = imgs.filter(Boolean).map(img => ({ img, aspect: img.naturalWidth / img.naturalHeight }));
    picsKey = key;
    drawnKey = '';
  }

  // ---------- drawing ----------
  function render() {
    if (!shown) return;
    const w = Math.round(canvas.clientWidth);
    const h = Math.round(canvas.clientHeight);
    if (!w || !h) return;
    const c = readColors();
    const key = `${w}x${h}|${c.bg}|${c.dark}|${picsKey}`;
    if (key === drawnKey) return;
    drawnKey = key;
    if (pics.length < MIN_PICS) {
      canvas.classList.remove('is-ready');
      canvas.width = canvas.height = 0;
      return;
    }
    const t0 = performance.now();
    // 1x: a dimmed background doesn't need more (and stays light in memory).
    canvas.width = w;
    canvas.height = h;
    const ctx = canvas.getContext('2d', { alpha: false });
    ctx.globalCompositeOperation = 'source-over';
    ctx.globalAlpha = 1;
    ctx.fillStyle = rgba(c.bg);
    ctx.fillRect(0, 0, w, h);

    // Rows of whole pictures, tilted; each row starts further along the list (and shifted),
    // so neighbours differ. Tiles outside the canvas are skipped.
    const rowH = Math.max(84, Math.min(150, Math.round(h / 6.5)));
    const gap = Math.round(rowH * 0.1);
    const radius = Math.max(6, Math.round(rowH * 0.07));
    const half = Math.hypot(w, h) / 2;
    const cos = Math.cos(ANGLE), sin = Math.sin(ANGLE);
    const n = pics.length;
    ctx.save();
    ctx.translate(w / 2, h / 2);
    ctx.rotate(ANGLE);
    let row = 0;
    for (let y = -half - rowH / 2; y < half; y += rowH + gap, row++) {
      let x = -half - (row % 3) * rowH * 0.45;
      let i = (row * 11) % n;
      while (x < half) {
        const p = pics[i % n];
        i++;
        const tw = Math.round(rowH * Math.max(0.5, Math.min(3.2, p.aspect)));
        // On screen? The tile's centre back in canvas coordinates, with its half diagonal.
        const cx = x + tw / 2, cy = y + rowH / 2;
        const sx = w / 2 + cx * cos - cy * sin;
        const sy = h / 2 + cx * sin + cy * cos;
        const reach = Math.hypot(tw, rowH) / 2;
        if (sx + reach > 0 && sx - reach < w && sy + reach > 0 && sy - reach < h) {
          // Whole: an aspect outside the clamp is fitted inside its tile, not stretched.
          let dw = tw, dh = rowH;
          if (p.aspect > tw / rowH) dh = tw / p.aspect; else dw = rowH * p.aspect;
          ctx.save();
          ctx.beginPath();
          ctx.roundRect(x, y, tw, rowH, radius);
          ctx.clip();
          ctx.fillStyle = rgba(c.dark ? [0, 0, 0] : [255, 255, 255], 0.35);
          ctx.fillRect(x, y, tw, rowH);
          ctx.drawImage(p.img, x + (tw - dw) / 2, y + (rowH - dh) / 2, dw, dh);
          ctx.restore();
        }
        x += tw + gap;
      }
    }
    ctx.restore();

    // Toned with the palette: the pictures keep their light and shade, take the taupe's hue.
    ctx.globalCompositeOperation = 'color';
    ctx.globalAlpha = c.dark ? 0.82 : 0.7;
    ctx.fillStyle = rgba(c.tone);
    ctx.fillRect(0, 0, w, h);
    // Dimmed toward the page colour.
    ctx.globalCompositeOperation = 'source-over';
    ctx.globalAlpha = c.dark ? 0.72 : 0.8;
    ctx.fillStyle = rgba(c.bg);
    ctx.fillRect(0, 0, w, h);
    ctx.globalAlpha = 1;
    renderMs = Math.round((performance.now() - t0) * 10) / 10;
    canvas.classList.add('is-ready');
  }

  // ---------- lifecycle ----------
  async function show() {
    shown = true;
    if (!loading) {
      loading = ensurePictures().catch(() => {}).finally(() => { loading = null; });
    }
    await loading;
    if (!shown) return;
    // After the section's first paint: the wall fades in under the content.
    requestAnimationFrame(() => setTimeout(render, 0));
  }

  function hide() {
    shown = false;
    drawnKey = '';
    canvas.classList.remove('is-ready');
    canvas.width = canvas.height = 0; // release the backing store
  }

  // Drawn only while the hero shows: Scan section, Scan tab (app.js showScanTab hides it for Plug-ins).
  document.addEventListener('vext:sectionchange', (e) => {
    const section = e && e.detail && e.detail.section;
    if (section === 'scan' && !hero.hidden) show(); else if (shown) hide();
  });
  document.addEventListener('vext:scantab', (e) => {
    const tab = e && e.detail && e.detail.tab;
    if (tab === 'scan' && V.currentSection && V.currentSection() === 'scan') show(); else if (shown) hide();
  });
  const redraw = () => { if (shown) { drawnKey = ''; render(); } };
  document.addEventListener('vext:themechange', redraw);
  const darkQuery = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;
  if (darkQuery && darkQuery.addEventListener) darkQuery.addEventListener('change', redraw);
  // A new picture shows on the next visit; the size follows the window.
  if (typeof ResizeObserver === 'function') {
    new ResizeObserver(() => {
      if (!shown) return;
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(render, 160);
    }).observe(hero);
  }
  canvas.addEventListener('contextrestored', redraw);

  V.scanWall = {
    // For checks: pictures loaded, last draw time, canvas size.
    stats: () => ({ shown, pictures: pics.length, renderMs, size: `${canvas.width}x${canvas.height}`, ready: canvas.classList.contains('is-ready') }),
    refresh: () => { indexAt = 0; if (shown) show(); },
  };

  if (typeof V.currentSection === 'function' && V.currentSection() === 'scan') show();
})();
