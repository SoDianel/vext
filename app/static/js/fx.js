// Visual effects: the one switch behind every animation in VEXT (Configuration > General >
// Visual effects) and the small motion helpers the shell and modules share.
//
//   setting  visual_effects = auto | on | off (backend setting, mirrored in localStorage
//            'pa-fx' so the page knows before first paint, see index.html)
//   auto     on, unless Windows asks for less motion (prefers-reduced-motion)
//   on       on, even with reduced motion
//   off      no canvas, no animation loops, instant UI
// The 3D Scan button (js/fx-scan3d.js) is a 2D canvas that runs on the CPU alone, so it
// needs no GPU check.
//
// <html data-fx="on|off"> is the effective state (css/fx.css gates on it) and
// data-fx-pref the choice. Modules opt in through window.VEXT.fx (see the API at the
// bottom); every helper does the plain thing at once when effects are off.
// Classic script, loaded right after app.js.
(function () {
  const V = window.VEXT;
  if (!V || V.fx) return;

  const root = document.documentElement;
  const SCRIPT_URL = (document.currentScript && document.currentScript.src) || new URL('/static/js/fx.js', location.href).href;
  const MODES = ['auto', 'on', 'off'];
  const STORE_KEY = 'pa-fx';
  // Mirrors the tokens in css/fx.css.
  const T = { fast: 120, base: 200, slow: 300, reveal: 700 };
  const EASE = {
    out: 'cubic-bezier(0.22, 1, 0.36, 1)',
    in: 'cubic-bezier(0.55, 0, 1, 0.45)',
    inOut: 'cubic-bezier(0.65, 0, 0.35, 1)',
    spring: 'cubic-bezier(0.34, 1.4, 0.64, 1)',
  };

  const reduceQuery = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
  let mode = MODES.includes(root.dataset.fxPref) ? root.dataset.fxPref : 'auto';
  const listeners = new Set();

  const reducedMotion = () => !!(reduceQuery && reduceQuery.matches);
  const enabled = () => mode === 'on' || (mode === 'auto' && !reducedMotion());

  // ---------- applying the choice ----------
  function apply() {
    const before = root.dataset.fx;
    root.dataset.fxPref = mode;
    root.dataset.fx = enabled() ? 'on' : 'off';
    renderControl();
    if (before !== root.dataset.fx) {
      const detail = { enabled: enabled(), mode };
      listeners.forEach(fn => { try { fn(detail); } catch (e) { console.error('[fx] listener failed:', e); } });
      document.dispatchEvent(new CustomEvent('vext:fxchange', { detail }));
      sync3d();
    }
  }

  function remember(m) {
    try { localStorage.setItem(STORE_KEY, m); } catch { /* storage unavailable */ }
  }

  async function setMode(next) {
    if (!MODES.includes(next)) throw new Error('visual effects must be auto, on or off');
    const previous = mode;
    mode = next;
    remember(next);
    apply();
    renderControl();
    try {
      await V.api('/api/settings/visual_effects', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ value: next }),
      });
    } catch (e) {
      // An older backend without the setting: the local choice still holds.
      if (e && e.status === 404) return;
      mode = previous;
      remember(previous);
      apply();
      throw e;
    }
  }

  if (reduceQuery) {
    const onReduce = () => apply();
    if (reduceQuery.addEventListener) reduceQuery.addEventListener('change', onReduce);
    else if (reduceQuery.addListener) reduceQuery.addListener(onReduce);
  }

  // The backend is the source of truth; localStorage only paints the first frame right.
  function loadSetting() {
    if (typeof V.settings !== 'function') return;
    V.settings().then((s) => {
      const v = s && s.visual_effects;
      if (!MODES.includes(v) || v === mode) return;
      mode = v;
      remember(v);
      apply();
    }).catch(() => { /* backend not up yet: keep the local choice */ });
  }

  // ---------- Configuration > General > Visual effects ----------
  function renderControl() {
    document.querySelectorAll('[data-fx-choice]').forEach((b) => {
      const on = b.dataset.fxChoice === mode;
      b.setAttribute('aria-checked', String(on));
      b.classList.toggle('active', on);
    });
    const status = document.getElementById('fx-status');
    if (!status) return;
    status.textContent = enabled()
      ? 'Effects are on.'
      : mode === 'off' ? 'Effects are off.' : 'Effects are off because Windows asks for less motion (Animation effects).';
  }
  document.querySelectorAll('[data-fx-choice]').forEach((b) => {
    b.addEventListener('click', () => {
      const next = b.dataset.fxChoice;
      if (next === mode) return;
      setMode(next).then(() => {
        V.toast(next === 'off' ? 'Visual effects are off.' : next === 'on' ? 'Visual effects are on.' : 'Visual effects follow Windows.');
      }).catch((e) => V.toast('Could not save the visual effects setting: ' + ((e && e.message) || e), true));
    });
  });

  // ---------- motion helpers ----------
  const motion = () => enabled() && !document.hidden;
  const keyOf = (el) => el.getAttribute('data-fx-key') || el.getAttribute('data-plugin-id') || el.getAttribute('data-id') || '';
  // The part of the window an element can be seen in: the viewport cut by every scrolling
  // or clipping ancestor (a list in its own scroll box, main).
  function clipOf(el) {
    let top = 0, left = 0, bottom = window.innerHeight, right = window.innerWidth;
    for (let a = el && el.parentElement; a && a !== document.body; a = a.parentElement) {
      const cs = getComputedStyle(a);
      if (cs.overflowX === 'visible' && cs.overflowY === 'visible') continue;
      const r = a.getBoundingClientRect();
      top = Math.max(top, r.top); left = Math.max(left, r.left);
      bottom = Math.min(bottom, r.bottom); right = Math.min(right, r.right);
    }
    return { top, left, bottom, right };
  }
  const inView = (r, c) => r.width > 0 && r.bottom > c.top && r.top < c.bottom && r.right > c.left && r.left < c.right;
  const marked = new Set();

  // Runs update() as a View Transition (a crossfade of the page) when effects are on.
  // `morph`: a container whose keyed items (data-fx-key / data-plugin-id / data-id)
  // in view morph from their old box to their new one (list <-> tiles).
  function swap(update, opts = {}) {
    if (!motion() || typeof document.startViewTransition !== 'function') {
      update();
      return Promise.resolve();
    }
    const named = [];
    const nameOf = new Map();
    const tag = (el, name) => {
      el.style.viewTransitionName = name;
      el.style.viewTransitionClass = 'fx-item';
      named.push(el);
    };
    const container = opts.morph;
    const pick = () => (container ? [...(opts.items ? opts.items(container) : container.children)] : []);
    if (container) {
      let n = 0;
      const clip = clipOf(container);
      for (const el of pick()) {
        if (n >= 48) break;
        const k = keyOf(el);
        if (!k || nameOf.has(k) || !inView(el.getBoundingClientRect(), clip)) continue;
        const name = `fx-k${n++}`;
        nameOf.set(k, name);
        tag(el, name);
      }
    }
    let t;
    try {
      t = document.startViewTransition(() => {
        update();
        // A module may have re-rendered its items: give the new element of each key the same name.
        if (!container || !nameOf.size) return;
        const seen = new Set();
        for (const el of pick()) {
          const k = keyOf(el);
          const name = nameOf.get(k);
          if (!name || seen.has(k)) continue;
          seen.add(k);
          if (el.style.viewTransitionName !== name) tag(el, name);
        }
        // Old elements that left the DOM keep nothing; ones still in it but not chosen must not clash.
        named.forEach((el) => { if (el.isConnected && !seen.has(keyOf(el))) el.style.viewTransitionName = ''; });
      });
    } catch (e) {
      named.forEach((el) => { el.style.viewTransitionName = ''; el.style.viewTransitionClass = ''; });
      update();
      return Promise.resolve();
    }
    const clear = () => named.forEach((el) => { el.style.viewTransitionName = ''; el.style.viewTransitionClass = ''; });
    t.finished.then(clear, clear);
    return t.updateCallbackDone.catch(() => {});
  }

  // FLIP: rewrites a list with mutate() and lets the items in view glide from where they
  // were to where they land; new ones rise in, a few at a time. Items are matched by key.
  // A long list (the whole catalog) isn't measured: reading 600+ table rows forces two full
  // layouts (~150 ms). Its first rows just rise in (class fx-fresh, css/fx.css).
  const LONG_LIST = 150;
  function flip(container, mutate, opts = {}) {
    if (!container || !motion() || !container.getClientRects().length) {
      if (container) container.classList.remove('fx-fresh');
      mutate();
      flashMarked(container);
      return;
    }
    const items = () => [...(opts.items ? opts.items(container) : container.children)];
    const before = new Map();
    let clip = null;
    if (container.childElementCount <= LONG_LIST) {
      clip = clipOf(container);
      for (const el of items()) {
        const k = keyOf(el);
        if (!k || before.has(k)) continue;
        const r = el.getBoundingClientRect();
        if (inView(r, clip)) before.set(k, r);
      }
    }
    const hadItems = before.size > 0;
    if (container.childElementCount > LONG_LIST) {
      container.classList.add('fx-fresh');
      mutate();
      flashMarked(container);
      return;
    }
    container.classList.remove('fx-fresh');
    mutate();
    if (container.childElementCount > LONG_LIST) {
      container.classList.add('fx-fresh');
      flashMarked(container);
      return;
    }
    clip = clipOf(container);
    const max = opts.max || 60;
    let animated = 0;
    let entered = 0;
    const done = new Set();
    for (const el of items()) {
      if (animated >= max) break;
      const k = keyOf(el);
      if (k && done.has(k)) continue;
      if (k) done.add(k);
      const r = el.getBoundingClientRect();
      if (!inView(r, clip)) continue;
      const old = k && before.get(k);
      if (old) {
        const dx = old.left - r.left;
        const dy = old.top - r.top;
        if (Math.abs(dx) < 1 && Math.abs(dy) < 1) continue;
        el.animate([{ transform: `translate(${dx}px, ${dy}px)` }, { transform: 'none' }],
          { duration: T.slow, easing: EASE.out });
        animated++;
      } else if (opts.enter !== false && (k || !hadItems)) {
        el.animate([{ opacity: 0, transform: 'translateY(6px)' }, { opacity: 1, transform: 'none' }],
          { duration: T.base + 40, easing: EASE.out, delay: Math.min(entered++, 14) * 16, fill: 'backwards' });
        animated++;
      }
    }
    flashMarked(container);
  }

  // An element leaves before the list is reloaded: slides out and folds away (rows), or
  // fades and shrinks (tiles). Resolves when it's gone from view; the caller removes it.
  function leave(el) {
    if (!el || !el.isConnected || !motion()) return Promise.resolve();
    const parent = el.parentElement;
    const grid = parent && /grid/.test(getComputedStyle(parent).display);
    el.style.pointerEvents = 'none';
    let anim;
    if (grid) {
      anim = el.animate([{ opacity: 1, transform: 'none' }, { opacity: 0, transform: 'scale(0.94)' }],
        { duration: T.base, easing: EASE.in, fill: 'forwards' });
    } else {
      const cs = getComputedStyle(el);
      const h = el.getBoundingClientRect().height;
      const full = { height: h + 'px', paddingTop: cs.paddingTop, paddingBottom: cs.paddingBottom };
      el.style.overflow = 'hidden';
      el.style.boxSizing = 'border-box';
      anim = el.animate([
        { opacity: 1, transform: 'none', ...full, offset: 0 },
        { opacity: 0, transform: 'translateX(24px)', ...full, offset: 0.5 },
        { opacity: 0, transform: 'translateX(24px)', height: '0px', paddingTop: '0px', paddingBottom: '0px', borderBottomWidth: '0px', offset: 1 },
      ], { duration: T.slow + 60, easing: EASE.inOut, fill: 'forwards' });
    }
    return anim.finished.then(() => {}, () => {});
  }

  // Removes an element, after a short exit when it has one in css/fx.css (class fx-out).
  function dismiss(el) {
    if (!el || !el.isConnected) return;
    if (!motion()) { el.remove(); return; }
    let gone = false;
    const done = () => { if (!gone) { gone = true; el.remove(); } };
    el.classList.add('fx-out');
    el.addEventListener('animationend', (e) => { if (e.target === el) done(); });
    setTimeout(done, T.slow + 200);
  }

  // Items rise in one after another (first render of a list or a block).
  function enter(target, opts = {}) {
    if (!motion() || !target) return;
    const els = target instanceof Element ? [...target.children] : [...target];
    const stagger = opts.stagger == null ? 28 : opts.stagger;
    const clip = clipOf(els[0]);
    let i = 0;
    for (const el of els) {
      if (i > 24) break;
      const r = el.getBoundingClientRect();
      if (!inView(r, clip)) continue;
      el.animate([{ opacity: 0, transform: 'translateY(8px)' }, { opacity: 1, transform: 'none' }],
        { duration: T.slow + 60, easing: EASE.out, delay: (opts.delay || 0) + i++ * stagger, fill: 'backwards' });
    }
  }

  // A number counts from `from` to `to`. The element's text ends exactly on format(to).
  function countUp(el, to, opts = {}) {
    if (!el) return;
    const format = opts.format || ((n) => Math.round(n).toLocaleString('en-US'));
    const from = opts.from == null ? 0 : opts.from;
    if (el._fxRaf) cancelAnimationFrame(el._fxRaf);
    el._fxRaf = 0;
    if (!motion() || from === to || !isFinite(to)) { el.textContent = format(to); return; }
    const duration = opts.duration || T.reveal + 200;
    const delay = opts.delay || 0;
    const t0 = performance.now() + delay;
    el.textContent = format(from);
    const step = (now) => {
      const k = Math.max(0, Math.min(1, (now - t0) / duration));
      const e = 1 - Math.pow(1 - k, 4);
      el.textContent = format(from + (to - from) * e);
      if (k < 1 && el.isConnected) el._fxRaf = requestAnimationFrame(step);
      else { el._fxRaf = 0; el.textContent = format(to); }
    };
    el._fxRaf = requestAnimationFrame(step);
  }

  // A count that just changed gives a small bump.
  function pulse(el) {
    if (!el || !motion() || !el.getClientRects().length) return;
    el.animate([{ transform: 'scale(1)' }, { transform: 'scale(1.25)', offset: 0.35 }, { transform: 'scale(1)' }],
      { duration: T.slow + 80, easing: EASE.out });
  }

  // A row or card that was just acted on glows once.
  function flash(el) {
    if (!el || !motion()) return;
    el.classList.remove('fx-flash');
    void el.offsetWidth; // restart the animation
    el.classList.add('fx-flash');
    el.addEventListener('animationend', () => el.classList.remove('fx-flash'), { once: true });
  }
  // Flash the item with this key after the next flip() that renders it (e.g. a proposed row).
  function mark(key) { if (key != null) marked.add(String(key)); }
  function flashMarked(container) {
    if (!marked.size || !container) return;
    for (const el of container.children) {
      const k = keyOf(el);
      if (k && marked.has(k)) { marked.delete(k); flash(el); }
    }
  }

  // ---------- modals: closing leaves a fading copy ----------
  const ghostLayer = document.createElement('div');
  ghostLayer.className = 'fx-ghosts';
  ghostLayer.setAttribute('aria-hidden', 'true');
  ghostLayer.inert = true;
  document.body.appendChild(ghostLayer);

  function ghost(node) {
    if (!(node instanceof Element) || !node.classList.contains('modal-backdrop')) return;
    if (node.id) node.removeAttribute('id');
    node.querySelectorAll('[id]').forEach((n) => n.removeAttribute('id'));
    node.classList.add('fx-leaving');
    ghostLayer.appendChild(node);
    let gone = false;
    const done = () => { if (!gone) { gone = true; node.remove(); } };
    node.addEventListener('animationend', (e) => { if (e.target === node) done(); });
    setTimeout(done, T.base + 250);
  }

  const overlayRoots = ['modal-root', 'confirm-root'].map((id) => document.getElementById(id)).filter(Boolean);
  const overlayWatch = new MutationObserver((records) => {
    if (!motion()) return;
    for (const host of overlayRoots) {
      const mine = records.filter((r) => r.target === host);
      if (!mine.length) continue;
      const removed = mine.flatMap((r) => [...r.removedNodes]);
      const added = mine.flatMap((r) => [...r.addedNodes]).filter((n) => n instanceof Element && n.isConnected);
      if (!host.childElementCount) removed.forEach(ghost);
      else if (removed.length && added.length) added.forEach((n) => n.classList.add('fx-no-enter'));
    }
  });
  overlayRoots.forEach((r) => overlayWatch.observe(r, { childList: true }));

  // ---------- the 3D Scan button (js/fx-scan3d.js, a 2D canvas) ----------
  // Loaded on first use, mounted only while effects are on and the Scan section is shown,
  // destroyed (canvas and its memory released) as soon as either stops being true.
  const s3d = { ctrl: null, loading: false, token: 0 };
  let scanState = { running: false, startedAt: 0, expectedMs: 0 };

  // Only while the Scan tab of the section shows (app.js showScanTab: Plug-ins hides the hero).
  const want3d = () => {
    const hero = document.getElementById('scan-hero');
    return enabled() && V.currentSection() === 'scan' && !(hero && hero.hidden);
  };

  function sync3d() {
    if (want3d()) mount3d();
    else unmount3d();
  }

  async function mount3d() {
    if (s3d.ctrl || s3d.loading) return;
    const stage = document.getElementById('scan-stage');
    if (!stage) return;
    const token = ++s3d.token;
    s3d.loading = true;
    stage.classList.add('fx-3d');
    try {
      const mod = await import(new URL('fx-scan3d.js', SCRIPT_URL).href);
      if (token !== s3d.token || !want3d()) {
        if (token === s3d.token) stage.classList.remove('fx-3d');
        return;
      }
      s3d.ctrl = mod.mount(stage, {
        button: document.getElementById('rescan-btn'),
        wall: document.getElementById('scan-wall-canvas'), // js/scan-wall.js: parallax and the sway while scanning
        scan: { ...scanState },
      });
    } catch (e) {
      console.warn('[fx] the 3D Scan button is off:', e);
      if (token === s3d.token) stage.classList.remove('fx-3d');
    } finally {
      if (token === s3d.token) s3d.loading = false;
    }
  }

  function unmount3d() {
    s3d.token++;
    s3d.loading = false;
    if (s3d.ctrl) {
      try { s3d.ctrl.destroy(); } catch (e) { console.error('[fx] 3D cleanup failed:', e); }
      s3d.ctrl = null;
    }
    const stage = document.getElementById('scan-stage');
    if (stage) stage.classList.remove('fx-3d');
  }

  document.addEventListener('vext:sectionchange', () => sync3d());
  document.addEventListener('vext:scantab', () => sync3d());
  // app.js: {phase: 'start', at, expectedMs} then {phase: 'end', ok, found, flagged, message}.
  document.addEventListener('vext:scan', (e) => {
    const d = (e && e.detail) || {};
    if (d.phase === 'start') scanState = { running: true, startedAt: d.at || Date.now(), expectedMs: d.expectedMs || 0 };
    else if (d.phase === 'end') scanState = { running: false, startedAt: 0, expectedMs: 0 };
    if (s3d.ctrl) s3d.ctrl.scan(d);
  });

  // ---------- public API ----------
  V.fx = {
    enabled,                 // effects on right now (the setting and, in Auto, the OS)
    mode: () => mode,        // 'auto' | 'on' | 'off'
    setMode,                 // persists (PUT /api/settings/visual_effects); returns a Promise
    reducedMotion,           // the OS asks for less motion
    onChange(fn) { listeners.add(fn); return () => listeners.delete(fn); }, // also 'vext:fxchange' on document
    T, EASE,                 // durations (ms) and easings, same as the css/fx.css tokens
    swap, flip, leave, dismiss, enter, countUp, pulse, flash, mark,
    // For checks and tests: is the 3D scene mounted, and its frame counter.
    stats: () => ({ enabled: enabled(), mode, scan3d: s3d.ctrl ? s3d.ctrl.stats() : (s3d.loading ? 'loading' : null) }),
  };

  apply();
  loadSetting();
})();
