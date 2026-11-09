// VEXT image browser (imgsearch.rs): injected into the top frame of every page
// shown there. A "Use this image" button appears over the image under the
// mouse; pressing it sends the image's address (and the page it is on) to VEXT
// through the one command this webview may call, imgsearch_pick. On Google
// Images a thumbnail is resolved to the original image: the result is opened
// in Google's preview panel, which loads the full-size original, and that
// address is sent (the encrypted thumbnail only if the original doesn't show
// up in time). No data is read from the page beyond that.
(() => {
  if (window.top !== window || window.__vextPick) return;
  window.__vextPick = true;

  const MIN = 64;          // smaller images are icons, not candidates
  const WAIT_MS = 5000;    // how long the preview may take to show the original
  const isGoogle = () => /(^|\.)google\.[a-z.]+$/i.test(location.hostname) && location.pathname === '/search';
  const isThumb = (src) => /^data:/i.test(src) || /(^|\.)gstatic\.com$/i.test(hostOf(src));
  function hostOf(src) { try { return new URL(src, location.href).hostname; } catch { return ''; } }
  const srcOf = (img) => img.currentSrc || img.src || '';
  const usable = (src) => /^https?:/i.test(src) || /^data:image\//i.test(src);
  const external = (href) => {
    try {
      const u = new URL(href, location.href);
      return /^https?:$/.test(u.protocol) && !/(^|\.)(google\.[a-z.]+|gstatic\.com|googleusercontent\.com)$/i.test(u.hostname);
    } catch { return false; }
  };

  let host = null, btn = null, note = null, current = null, busy = false, raf = 0, lastXY = null;

  function build() {
    host = document.createElement('vext-pick');
    host.style.cssText = 'position:fixed;left:0;top:0;width:0;height:0;z-index:2147483647;pointer-events:none;';
    const root = host.attachShadow({ mode: 'closed' });
    root.innerHTML = `<style>
      .b{position:fixed;display:none;pointer-events:auto;font:600 13px/1 "Segoe UI",system-ui,sans-serif;
        color:#fff;background:#d14b2a;border:0;border-radius:6px;padding:8px 12px;cursor:pointer;
        box-shadow:0 2px 10px rgba(0,0,0,.45);white-space:nowrap}
      .b:hover{background:#b83c1f}.b[disabled]{opacity:.8;cursor:progress}
      .n{position:fixed;left:50%;bottom:18px;transform:translateX(-50%);display:none;max-width:80vw;
        font:500 13px/1.35 "Segoe UI",system-ui,sans-serif;color:#fff;background:rgba(20,20,20,.92);
        padding:9px 14px;border-radius:6px;box-shadow:0 2px 12px rgba(0,0,0,.5)}
    </style><button class="b" type="button">Use this image</button><div class="n" role="status"></div>`;
    btn = root.querySelector('.b');
    note = root.querySelector('.n');
    btn.addEventListener('mousedown', (e) => { e.preventDefault(); e.stopPropagation(); }, true);
    btn.addEventListener('click', (e) => { e.preventDefault(); e.stopPropagation(); use(); }, true);
    (document.body || document.documentElement).appendChild(host);
  }

  let noteTimer = 0;
  function say(text, ms = 3500) {
    if (!host) build();
    note.textContent = text;
    note.style.display = 'block';
    clearTimeout(noteTimer);
    if (ms) noteTimer = setTimeout(() => { note.style.display = 'none'; }, ms);
  }

  // The image under the point, even when the page lays a transparent element over it.
  function imageAt(x, y) {
    for (const el of document.elementsFromPoint(x, y)) {
      if (el === host) return current;
      if (el.tagName !== 'IMG') continue;
      const r = el.getBoundingClientRect();
      if (r.width >= MIN && r.height >= MIN && usable(srcOf(el))) return el;
    }
    return null;
  }

  // The part of the image actually on screen: its box cut by the ancestors that clip it
  // (cropped thumbnails, sliders) and by the window.
  function shown(img) {
    const b = img.getBoundingClientRect();
    let left = Math.max(b.left, 0), top = Math.max(b.top, 0);
    let right = Math.min(b.right, innerWidth), bottom = Math.min(b.bottom, innerHeight);
    for (let el = img.parentElement, i = 0; el && i < 15 && el !== document.body; el = el.parentElement, i++) {
      const cs = getComputedStyle(el);
      if (cs.overflowX === 'visible' && cs.overflowY === 'visible') continue;
      const c = el.getBoundingClientRect();
      left = Math.max(left, c.left); top = Math.max(top, c.top);
      right = Math.min(right, c.right); bottom = Math.min(bottom, c.bottom);
    }
    return right - left >= 40 && bottom - top >= 30 ? { left, top, right, bottom, width: right - left } : b;
  }

  function place() {
    raf = 0;
    if (!lastXY || busy) return;
    const img = imageAt(lastXY[0], lastXY[1]);
    if (!img) { if (btn) btn.style.display = 'none'; current = null; return; }
    if (!host || !host.isConnected) build();
    current = img;
    const r = shown(img);
    btn.style.display = 'block';
    const bw = btn.offsetWidth, bh = btn.offsetHeight;
    btn.style.left = `${Math.round(Math.max(r.left + 6, Math.min(r.right - bw - 6, r.left + (r.width - bw) / 2)))}px`;
    btn.style.top = `${Math.round(Math.max(r.top + 6, Math.min(r.bottom - bh - 6, window.innerHeight - bh - 6)))}px`;
  }

  document.addEventListener('mousemove', (e) => {
    lastXY = [e.clientX, e.clientY];
    if (!raf) raf = requestAnimationFrame(place);
  }, { capture: true, passive: true });
  window.addEventListener('scroll', () => { if (!raf) raf = requestAnimationFrame(place); }, { capture: true, passive: true });
  window.addEventListener('mouseout', (e) => {
    if (e.relatedTarget || busy) return;  // left the page
    lastXY = null;
    if (btn) btn.style.display = 'none';
    current = null;
  }, true);

  // Google's tracking parameter on the result links.
  function clean(href) {
    try { const u = new URL(href); u.searchParams.delete('srsltid'); return u.href; } catch { return href; }
  }

  // The page the image belongs to: the nearest outside link around it (Google's preview
  // panel links the page it found the image on), else this page.
  function pageOf(img) {
    if (!isGoogle()) return location.href;
    for (let el = img, i = 0; el && i < 10; el = el.parentElement, i++) {
      const a = el.tagName === 'A' ? el : null;
      if (a && external(a.href)) return clean(a.href);
      const inner = el.querySelector && [...el.querySelectorAll('a[href]')].find(x => external(x.href));
      if (inner) return clean(inner.href);
    }
    return null;
  }

  // A loaded original (not Google's own thumbnail) shown big enough, whose center lies in
  // `area` if given; the largest such one.
  function originalIn(area, except) {
    let best = null, size = 0;
    for (const img of document.images) {
      const src = srcOf(img);
      if (!/^https?:/i.test(src) || isThumb(src) || (except && except.has(src)) || !img.complete || !img.naturalWidth) continue;
      if (/(^|\.)(google\.[a-z.]+|googleusercontent\.com)$/i.test(hostOf(src))) continue;
      const r = img.getBoundingClientRect();
      if (r.width < 160 || r.height < 100 || r.bottom < 0 || r.top > innerHeight) continue;
      if (area) {
        const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
        if (cx < area.left || cx > area.right || cy < area.top || cy > area.bottom) continue;
      }
      if (r.width * r.height > size) { size = r.width * r.height; best = img; }
    }
    return best;
  }

  // Google's preview panel shows a big copy of the chosen thumbnail (same address) and lays
  // the original from the site over it once loaded: the original over that copy.
  function originalOf(thumbSrc, thumbEl) {
    for (const copy of document.images) {
      if (copy === thumbEl || srcOf(copy) !== thumbSrc) continue;
      const r = copy.getBoundingClientRect();
      if (r.width < 160 || r.height < 60) continue;
      const big = originalIn(r, null);
      if (big) return big;
    }
    return null;
  }

  async function resolve(img) {
    const src = srcOf(img);
    if (!isGoogle() || !isThumb(src)) return { image: src, page: pageOf(img), thumb: false };
    const found = (big) => ({ image: srcOf(big), page: pageOf(big), thumb: false });
    // Already open in the preview panel?
    let big = originalOf(src, img);
    if (big) return found(big);
    // The panel's own big copy links to the site: clicking it would leave Google. Only a
    // result in the grid is clicked, which opens it in the preview panel.
    const link = img.closest('a[href]');
    if (!(link && external(link.href))) {
      const before = new Set([...document.images].map(srcOf));
      const target = img.closest('a, [role="button"], [jsaction]') || img;
      target.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
      const until = Date.now() + WAIT_MS;
      while (Date.now() < until) {
        await new Promise(r => setTimeout(r, 150));
        // Over the copy of this thumbnail; else (a data: thumbnail has no copy with the same
        // address) a new original that showed up since the click.
        big = originalOf(src, img) || (/^data:/i.test(src) ? originalIn(null, before) : null);
        if (big) return found(big);
      }
    }
    // The thumbnail, with the page from the panel's copy when there is one.
    const copy = [...document.images].find(c => c !== img && srcOf(c) === src && c.getBoundingClientRect().width >= 160);
    return { image: src, page: pageOf(copy || img), thumb: true };
  }

  async function use() {
    const img = current;
    if (!img || busy) return;
    const ipc = window.__TAURI_INTERNALS__;
    if (!ipc || typeof ipc.invoke !== 'function') { say('This page can\'t reach VEXT.'); return; }
    busy = true;
    btn.disabled = true;
    btn.textContent = 'Getting the full image…';
    try {
      const got = await resolve(img);
      btn.textContent = 'Sending to VEXT…';
      await ipc.invoke('imgsearch_pick', { imageUrl: got.image, pageUrl: got.page || null, thumbnail: got.thumb });
      say(got.thumb ? 'Sent to VEXT (only the thumbnail was available).' : 'Sent to VEXT.');
    } catch (e) {
      say(`Couldn't send it: ${e && e.message ? e.message : e}`);
    } finally {
      busy = false;
      btn.disabled = false;
      btn.textContent = 'Use this image';
      btn.style.display = 'none';
      current = null;
      if (lastXY && !raf) raf = requestAnimationFrame(place);
    }
  }
})();
