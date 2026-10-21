// The plugin frame's page (desktop app only): the title bar above an open
// plugin's editor and the control bar below it. The shell
// (desktop/src-tauri/src/plugin_frame.rs) owns the window and keeps the
// editor, a window of the plugin's worker process, on this page's body,
// between the two bars. This page handles the bars:
//  - dragging either bar moves the frame (plugin_frame_move), kept inside
//    VEXT's window
//  - title bar: Capture ▾, a native menu, so the editor can't cover it: set as
//    the plugin's picture, save a PNG to Pictures\VEXT, copy the image
//  - title bar: minimize / full screen (plugin_frame_toggle); a double-click on
//    the title bar also toggles full screen, Escape leaves it
//  - title bar: close: closes the plugin on the backend, then the frame
//  - control bar: IN / OUT knobs and Auto-Balance (POST
//    /api/plugins/{id}/host/gain), and Bypass (POST /host/bypass; the only
//    bypass control in VEXT; plugin_frame_bypass lays the dark overlay over
//    the editor)
//  - scroll bars when the editor is bigger than the room there is
//    (plugin_frame_scroll)
//
// The shell gives the page window.__VEXT__ = { apiBase, token } (the backend's
// address and the per-launch API token every non-GET request must carry), and
// ?id=&name= so the title shows on the first paint; plugin_frame_info and the
// 'plugin-frame://info' event keep it current.
(function () {
  'use strict';

  const T = window.__TAURI__ || null;
  const CFG = window.__VEXT__ || {};
  const $ = (id) => document.getElementById(id);
  const params = new URLSearchParams(location.search);

  const svg = (body) => `<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" fill="none" ` +
    `stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">${body}</svg>`;
  const ICON = {
    minimize: svg('<path d="M4 8.5h8"/>'),
    expand: svg('<path d="M4.5 6.5L8 10l3.5-3.5"/>'),
    fullscreen: svg('<path d="M3 6.5V3h3.5M9.5 3H13v3.5M13 9.5V13H9.5M6.5 13H3V9.5"/>'),
    exitFullscreen: svg('<path d="M6.5 3v3.5H3M13 6.5H9.5V3M9.5 13V9.5H13M3 9.5h3.5V13"/>'),
  };
  const CAPTURE = {
    picture: { busy: 'Taking the picture…', done: 'Set as the plugin picture' },
    file: { busy: 'Saving…', done: 'Saved to Pictures\\VEXT' },
    clipboard: { busy: 'Copying…', done: 'Copied to the clipboard' },
  };

  let pluginId = Number(params.get('id')) || null;
  let mode = 'normal';
  let closing = false;

  // ---------- title, mode, notes ----------

  function setName(name) {
    const text = name || 'Plugin';
    $('pf-name').textContent = text;
    $('pf-name').title = text;
    document.title = `${text} - VEXT`;
  }

  function setMode(next) {
    mode = next || 'normal';
    document.body.dataset.mode = mode;
    const min = $('pf-min');
    const full = $('pf-full');
    min.innerHTML = mode === 'minimize' ? ICON.expand : ICON.minimize;
    min.title = mode === 'minimize' ? 'Show the plugin' : 'Minimize';
    min.setAttribute('aria-label', min.title);
    full.innerHTML = mode === 'fullscreen' ? ICON.exitFullscreen : ICON.fullscreen;
    full.title = mode === 'fullscreen' ? 'Exit full screen' : 'Full screen';
    full.setAttribute('aria-label', full.title);
    // A hidden editor can't be captured (the backend answers 409).
    const cap = $('pf-capture');
    cap.disabled = closing || mode === 'minimize';
    cap.title = mode === 'minimize' ? 'Show the plugin to capture it' : "Take a picture of the plugin's window";
  }

  let noteTimer = 0;
  let standing = ''; // shown when no other note is (a plugin without audio output says so)
  function note(text, kind, title) {
    const el = $('pf-note');
    clearTimeout(noteTimer);
    if (!text) text = standing;
    el.textContent = text || '';
    el.className = 'pf-note' + (kind ? ` ${kind}` : '');
    el.title = title || text || '';
    if (kind) noteTimer = setTimeout(() => note(''), kind === 'err' ? 6000 : 2500);
  }

  function applyInfo(info) {
    if (!info) return;
    if (info.pluginId != null) pluginId = Number(info.pluginId);
    setName(info.name);
    setMode(info.mode);
    const root = document.documentElement.style;
    if (info.titleHeight) root.setProperty('--pf-title-h', `${info.titleHeight}px`);
    if (info.controlsHeight) root.setProperty('--pf-controls-h', `${info.controlsHeight}px`);
    applyView(info.view || null);
  }

  // ---------- scroll bars (an editor bigger than the room there is) ----------
  // The shell sends the viewport ({contentW, contentH, viewW, viewH, x, y,
  // barX, barY}, physical px; null: the editor fits). The page shows native
  // scroll bars for it; scrolling them (drag, wheel over them, Shift+wheel for
  // sideways) moves the editor window behind the viewport (plugin_frame_scroll).
  // The wheel over the editor itself still goes to the plugin.

  let view = null;
  let scrollSent = null; // {x, y} last sent (physical px)
  let scrollRaf = 0;

  function applyView(v) {
    view = v;
    const box = $('pf-scroll');
    const inner = $('pf-scroll-content');
    box.classList.toggle('has-x', !!(v && v.barX));
    box.classList.toggle('has-y', !!(v && v.barY));
    if (!v) { inner.style.width = inner.style.height = '1px'; scrollSent = null; return; }
    const dpr = window.devicePixelRatio || 1;
    inner.style.width = v.barX ? `${v.contentW / dpr}px` : '1px';
    inner.style.height = v.barY ? `${v.contentH / dpr}px` : '1px';
    // Where the shell has it (it keeps the position in range, e.g. after a resize).
    scrollSent = { x: v.x, y: v.y };
    if (Math.abs(box.scrollLeft * dpr - v.x) > 1) box.scrollLeft = v.x / dpr;
    if (Math.abs(box.scrollTop * dpr - v.y) > 1) box.scrollTop = v.y / dpr;
  }

  function applyTheme() {
    let t = null;
    try { t = localStorage.getItem('pa-theme'); } catch { /* storage unavailable */ }
    if (t === 'light' || t === 'dark') document.documentElement.dataset.theme = t;
    else delete document.documentElement.dataset.theme;
  }

  setName(params.get('name'));
  setMode('normal');
  // The main window changed the appearance (localStorage is shared: same origin).
  window.addEventListener('storage', (e) => { if (e.key === 'pa-theme' || e.key === null) applyTheme(); });
  // No browser context menu (Reload, Inspect…) on a title bar.
  document.addEventListener('contextmenu', (e) => e.preventDefault());

  if (!T || !T.core || typeof T.core.invoke !== 'function') {
    $('pf-hint').textContent = 'This page is part of the VEXT desktop app.';
    document.querySelectorAll('button').forEach((b) => { b.disabled = true; });
    return;
  }
  const invoke = T.core.invoke;
  $('pf-hint').textContent = '';

  invoke('plugin_frame_info').then(applyInfo).catch(() => { /* the frame is closing */ });
  if (T.event && typeof T.event.listen === 'function') {
    T.event.listen('plugin-frame://info', (e) => applyInfo(e.payload));
    T.event.listen('plugin-frame://view', (e) => applyView(e.payload || null));
  }

  // At most one scroll move per animation frame, and none the shell already has.
  $('pf-scroll').addEventListener('scroll', () => {
    if (!view || scrollRaf) return;
    scrollRaf = requestAnimationFrame(() => {
      scrollRaf = 0;
      if (!view) return;
      const box = $('pf-scroll');
      const dpr = window.devicePixelRatio || 1;
      const at = { x: Math.round(box.scrollLeft * dpr), y: Math.round(box.scrollTop * dpr) };
      if (scrollSent && Math.abs(at.x - scrollSent.x) <= 1 && Math.abs(at.y - scrollSent.y) <= 1) return;
      scrollSent = at;
      invoke('plugin_frame_scroll', at).catch(() => { /* frame closing */ });
    });
  }, { passive: true });

  // ---------- backend ----------

  async function api(path, method, body) {
    const headers = { 'X-VEXT-Token': CFG.token || '' };
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    const res = await fetch(`${CFG.apiBase || ''}${path}`, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    let data = null;
    try { data = await res.json(); } catch { /* no body */ }
    if (!res.ok) {
      const detail = data && (typeof data.detail === 'string' ? data.detail : data.error);
      throw new Error(detail || `the backend answered ${res.status}`);
    }
    return data;
  }

  const errText = (err) => (err && err.message) || String(err);

  // ---------- Capture ▾ ----------

  async function capture(target) {
    if (!pluginId || closing) return;
    const words = CAPTURE[target];
    note(words.busy);
    try {
      const res = await api(`/api/plugins/${pluginId}/host/capture`, 'POST', { target });
      note(words.done, 'ok', res && res.path ? res.path : '');
      if (target === 'picture' && T.event && typeof T.event.emitTo === 'function') {
        // The main window shows the new picture.
        T.event.emitTo('main', 'plugin-frame://captured', { pluginId }).catch(() => { /* not listening */ });
      }
    } catch (err) {
      note(`Capture failed: ${errText(err)}`, 'err');
    }
  }

  // The entries are MenuItem objects made first and kept here. Written inline
  // (Menu.new({items: [{text, action}]})), an entry never runs its action:
  // Tauri builds the inline item, appends it and drops it right away, and
  // dropping an item unregisters its action, so the menu opened and nothing
  // happened. No fixed ids either: actions are registered by item id, and a
  // closed frame's items being dropped would unregister a new frame's ones.
  let menu = null;
  let menuItems = null; // referenced so they live as long as the page
  async function captureMenu() {
    if (menu) return menu;
    const { Menu, MenuItem } = T.menu;
    menuItems = await Promise.all([
      MenuItem.new({ text: 'Set as plugin picture', action: () => capture('picture') }),
      MenuItem.new({ text: 'Save PNG to Pictures\\VEXT', action: () => capture('file') }),
      MenuItem.new({ text: 'Copy image', action: () => capture('clipboard') }),
    ]);
    menu = await Menu.new({ items: menuItems });
    return menu;
  }

  async function showCaptureMenu() {
    const btn = $('pf-capture');
    try {
      await captureMenu();
      const r = btn.getBoundingClientRect();
      btn.setAttribute('aria-expanded', 'true');
      await menu.popup(new T.dpi.LogicalPosition(Math.round(r.left), Math.round(r.bottom + 2)));
    } catch (err) {
      note(`The menu couldn't open: ${errText(err)}`, 'err');
    } finally {
      btn.setAttribute('aria-expanded', 'false');
    }
  }
  $('pf-capture').addEventListener('click', showCaptureMenu);

  // ---------- minimize / full screen / close ----------

  async function toggle(which) {
    try {
      const res = await invoke('plugin_frame_toggle', { mode: which });
      setMode(res && res.mode);
    } catch (err) {
      note(errText(err), 'err');
    }
  }
  $('pf-min').addEventListener('click', () => toggle('minimize'));
  $('pf-full').addEventListener('click', () => toggle('fullscreen'));
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && mode === 'fullscreen') toggle('fullscreen');
  });
  // Space is VEXT's Play / Stop here too (it would press the focused button): the main page plays.
  document.addEventListener('keydown', (e) => {
    if (e.code !== 'Space' || e.ctrlKey || e.metaKey || e.altKey) return;
    e.preventDefault();
    if (e.repeat || !T.event || typeof T.event.emitTo !== 'function') return;
    T.event.emitTo('main', 'plugin-frame://space', {}).catch(() => { /* not listening */ });
  });
  document.addEventListener('keyup', (e) => { if (e.code === 'Space') e.preventDefault(); });

  $('pf-close').addEventListener('click', async () => {
    if (closing) return;
    closing = true;
    document.querySelectorAll('button').forEach((b) => { b.disabled = true; });
    drawControls();
    note('Closing…');
    if (pluginId) {
      // A plugin left open without its frame is closed by the main window
      // anyway (it hears 'plugin-frame://closed'), so a failure here only
      // means it closes from there.
      try { await api(`/api/plugins/${pluginId}/host`, 'DELETE'); } catch { /* see above */ }
    }
    try { await invoke('plugin_frame_close'); } catch { /* already gone */ }
  });

  // ---------- control bar: IN / OUT knobs, Auto-Balance, Bypass ----------
  // The worker scales what goes into the plugin (IN) and what comes out (OUT),
  // -60 dB (silence) .. +12 dB; Auto-Balance keeps the output about as loud as
  // the input (POST /host/gain; kept per plugin by the backend). Bypass is the
  // host's (POST /host/bypass): dry out, the
  // plugin keeps running unheard. The host state is read every second: the
  // gains, what Auto-Balance applies, bypass (the shell's dark overlay over the
  // editor follows it) and whether the plugin is an instrument (no bypass).

  const GAIN_MIN = -60;
  const GAIN_MAX = 12;
  const SWEEP = 270; // degrees from the minimum to the maximum
  // After a bypass change made here, a poll still on its
  // way may carry the old value: polled values that disagree are ignored this long.
  const BYPASS_HOLD_MS = 1500;
  let gain = null; // {in_db, out_db, auto_balance, balance_db}
  let hostOpen = false;
  let instrument = false;
  let bypass = false;
  let bypassShown = null; // what the shell's overlay was last told
  let bypassHold = 0; // performance.now() until which polls don't overrule `bypass`
  const sending = { busy: false, pending: null };

  const clampDb = (v) => Math.max(GAIN_MIN, Math.min(GAIN_MAX, Math.round(v * 10) / 10));
  const dbText = (v) => (v <= GAIN_MIN ? '−∞' : `${v > 0 ? '+' : v < 0 ? '−' : ''}${Math.abs(v).toFixed(1)}`);

  function arc(fromDeg, toDeg) {
    const r = 9;
    const pt = (deg) => {
      const a = (deg - 90) * Math.PI / 180;
      return `${(12 + r * Math.cos(a)).toFixed(2)} ${(12 + r * Math.sin(a)).toFixed(2)}`;
    };
    if (toDeg - fromDeg < 0.5) return '';
    return `M ${pt(fromDeg)} A ${r} ${r} 0 ${toDeg - fromDeg > 180 ? 1 : 0} 1 ${pt(toDeg)}`;
  }

  function drawKnob(el, db) {
    const angle = -SWEEP / 2 + ((db - GAIN_MIN) / (GAIN_MAX - GAIN_MIN)) * SWEEP;
    el.querySelector('.pf-knob-track').setAttribute('d', arc(-SWEEP / 2, SWEEP / 2));
    el.querySelector('.pf-knob-arc').setAttribute('d', arc(-SWEEP / 2, angle));
    el.querySelector('.pf-knob-tick').setAttribute('transform', `rotate(${angle.toFixed(1)} 12 12)`);
    el.querySelector('.pf-knob-value').textContent = dbText(db);
    el.setAttribute('aria-valuenow', String(db));
    el.setAttribute('aria-valuetext', db <= GAIN_MIN ? 'silent' : `${dbText(db)} dB`);
  }

  const AUTO_TITLE = 'Auto-Balance: keeps what comes out of the plugin about as loud as what goes in, and peaks under -1 dBFS';

  // The whole bar from `gain`, `hostOpen`, `instrument` and `bypass`. Shown
  // from the first paint (no jump when the state arrives), inert until then.
  function drawControls() {
    const live = !!gain && !closing;
    $('pf-controls').classList.toggle('is-idle', !live);
    for (const el of [$('pf-in'), $('pf-out')]) {
      el.tabIndex = live ? 0 : -1;
      el.setAttribute('aria-disabled', String(!live));
      if (gain && (!knobDrag || knobDrag.el !== el)) drawKnob(el, gain[el.dataset.key]);
    }
    const auto = $('pf-auto');
    auto.disabled = !live;
    auto.setAttribute('aria-pressed', String(!!(gain && gain.auto_balance)));
    const b = gain ? Number(gain.balance_db) || 0 : 0;
    const applied = gain && gain.auto_balance && Math.abs(b) >= 0.5 ? `${dbText(b)} dB` : '';
    $('pf-auto-db').textContent = applied ? dbText(b) : '';
    // The compact [A] has no room for the readout: the tooltip carries it.
    auto.title = applied ? `${AUTO_TITLE}. Applying ${applied} now.` : AUTO_TITLE;

    const by = $('pf-bypass');
    const canBypass = hostOpen && !instrument && !closing;
    by.disabled = !canBypass;
    by.setAttribute('aria-pressed', String(hostOpen && bypass));
    by.title = instrument
      ? 'An instrument has no input to compare with: it has no bypass'
      : bypass ? 'Bypassed: you hear the sound without the plugin. Click to hear the plugin again'
        : 'Bypass: hear the sound without the plugin, to compare';
  }

  async function sendGain(fields) {
    if (!pluginId) return;
    if (sending.busy) { sending.pending = { ...(sending.pending || {}), ...fields }; return; }
    sending.busy = true;
    try {
      const st = await api(`/api/plugins/${pluginId}/host/gain`, 'POST', fields);
      if (st && st.gain && !knobDrag) { gain = st.gain; drawControls(); }
    } catch (err) {
      note(`Couldn't change the level: ${errText(err)}`, 'err');
    } finally {
      sending.busy = false;
      if (sending.pending) { const next = sending.pending; sending.pending = null; sendGain(next); }
    }
  }

  function setKnob(el, db) {
    if (!gain || closing) return;
    const v = clampDb(db);
    gain = { ...gain, [el.dataset.key]: v };
    drawKnob(el, v);
    sendGain({ [el.dataset.key]: v });
  }

  let knobDrag = null;
  for (const el of [$('pf-in'), $('pf-out')]) {
    el.addEventListener('pointerdown', (e) => {
      if (e.button !== 0 || !gain || closing) return;
      e.preventDefault();
      el.focus({ preventScroll: true });
      try { el.setPointerCapture(e.pointerId); } catch { /* released already */ }
      knobDrag = { el, id: e.pointerId, y: e.clientY, start: gain[el.dataset.key] };
      el.classList.add('is-active'); // the compact knob shows its value while dragged
    });
    el.addEventListener('pointermove', (e) => {
      if (!knobDrag || knobDrag.id !== e.pointerId) return;
      const step = e.shiftKey ? 0.05 : 0.25; // dB per pixel
      setKnob(el, knobDrag.start + (knobDrag.y - e.clientY) * step);
    });
    const end = (e) => {
      if (knobDrag && knobDrag.id === e.pointerId) knobDrag = null;
      el.classList.remove('is-active');
    };
    el.addEventListener('pointerup', end);
    el.addEventListener('pointercancel', end);
    el.addEventListener('lostpointercapture', end);
    el.addEventListener('wheel', (e) => {
      if (!gain) return;
      e.preventDefault();
      setKnob(el, gain[el.dataset.key] + (e.deltaY < 0 ? 1 : -1) * (e.shiftKey ? 0.2 : 1));
    }, { passive: false });
    el.addEventListener('dblclick', (e) => { e.stopPropagation(); setKnob(el, 0); });
    el.addEventListener('keydown', (e) => {
      if (!gain) return;
      const v = gain[el.dataset.key];
      const keys = { ArrowUp: v + 0.5, ArrowRight: v + 0.5, ArrowDown: v - 0.5, ArrowLeft: v - 0.5,
        PageUp: v + 6, PageDown: v - 6, Home: GAIN_MIN, End: GAIN_MAX, Delete: 0, Backspace: 0 };
      if (!(e.key in keys)) return;
      e.preventDefault();
      setKnob(el, keys[e.key]);
    });
  }
  $('pf-auto').addEventListener('click', () => {
    if (!gain || closing) return;
    gain = { ...gain, auto_balance: !gain.auto_balance };
    drawControls();
    sendGain({ auto_balance: gain.auto_balance });
  });

  // The shell's overlay follows `on`.
  function showBypass(on) {
    bypassShown = on;
    invoke('plugin_frame_bypass', { on }).catch(() => { /* frame closing */ });
  }

  async function setBypass(on) {
    const id = pluginId;
    if (!id || !hostOpen || instrument || closing) return;
    bypass = on;
    bypassHold = performance.now() + BYPASS_HOLD_MS;
    drawControls();
    try {
      await api(`/api/plugins/${id}/host/bypass`, 'POST', { on });
      // After the host has it: the overlay never shows a state the host doesn't have.
      if (id === pluginId) showBypass(on);
    } catch (err) {
      if (id !== pluginId) return;
      bypass = !on;
      bypassHold = 0;
      drawControls();
      note(`Couldn't ${on ? 'bypass' : 'un-bypass'} the plugin: ${errText(err)}`, 'err');
    }
  }
  $('pf-bypass').addEventListener('click', () => setBypass(!bypass));

  let polledFor = null;
  async function poll() {
    const id = pluginId;
    if (id && !closing && !document.hidden) {
      try {
        const st = await api(`/api/plugins/${id}/host`, 'GET');
        if (id === pluginId && !closing) {
          if (polledFor !== id) { polledFor = id; gain = null; bypassShown = null; bypassHold = 0; }
          const open = !!(st && st.state === 'open');
          hostOpen = open;
          instrument = open && !!st.is_instrument;
          if (!knobDrag && !sending.busy) gain = open ? st.gain || null : null;
          const keep = (open && st.no_output_note) || '';
          if (keep !== standing) { standing = keep; if ($('pf-note').className === 'pf-note') note(''); }
          const by = open && !!st.bypass;
          if (by === bypass || performance.now() >= bypassHold) {
            bypass = by;
            bypassHold = 0;
            if (by !== bypassShown) showBypass(by);
          }
          drawControls();
        }
      } catch { /* the backend restarts or the plugin closes: next time */ }
    }
    setTimeout(poll, 1000);
  }
  drawControls();
  poll();

  // ---------- dragging the frame by either bar ----------
  // screenX/Y deltas (CSS px, times devicePixelRatio = physical px) from where
  // the frame was when the drag started; one move in flight at a time, at most
  // one per animation frame. The shell keeps the frame inside VEXT's window.
  // Buttons and knobs don't start a drag; the bars' empty parts do.

  const bar = $('pf-bar');
  let drag = null;

  function target(d) {
    return { x: Math.round(d.x0 + d.dx * d.dpr), y: Math.round(d.y0 + d.dy * d.dpr) };
  }

  function schedule() {
    if (drag && !drag.raf) drag.raf = requestAnimationFrame(step);
  }

  async function step() {
    const d = drag;
    if (!d) return;
    d.raf = 0;
    if (d.busy || d.x0 == null || !d.dirty) return;
    d.dirty = false;
    d.busy = true;
    try { await invoke('plugin_frame_move', target(d)); } catch { /* frame closing */ }
    d.busy = false;
    if (d.dirty) schedule();
  }

  function endDrag(e) {
    const d = drag;
    if (!d || e.pointerId !== d.id) return;
    drag = null;
    if (d.raf) cancelAnimationFrame(d.raf);
    // The last bit of movement, if it hadn't gone out yet.
    if (d.dirty && d.x0 != null) invoke('plugin_frame_move', target(d)).catch(() => { /* frame closing */ });
  }

  for (const handle of [bar, $('pf-controls')]) {
    handle.addEventListener('pointerdown', async (e) => {
      if (e.button !== 0 || e.target.closest('button, .pf-knob') || mode === 'fullscreen' || closing) return;
      try { handle.setPointerCapture(e.pointerId); } catch { /* released already */ }
      const d = drag = {
        id: e.pointerId, sx: e.screenX, sy: e.screenY, dpr: window.devicePixelRatio || 1,
        x0: null, y0: null, dx: 0, dy: 0, dirty: false, busy: false, raf: 0,
      };
      try {
        const info = await invoke('plugin_frame_info');
        if (info && drag === d) { d.x0 = info.x; d.y0 = info.y; schedule(); }
      } catch { /* frame closing */ }
    });
    handle.addEventListener('pointermove', (e) => {
      if (!drag || e.pointerId !== drag.id) return;
      drag.dx = e.screenX - drag.sx;
      drag.dy = e.screenY - drag.sy;
      drag.dirty = true;
      schedule();
    });
    handle.addEventListener('pointerup', endDrag);
    handle.addEventListener('pointercancel', endDrag);
    handle.addEventListener('lostpointercapture', endDrag);
  }

  bar.addEventListener('dblclick', (e) => {
    if (!e.target.closest('button, .pf-knob') && !closing) toggle('fullscreen');
  });
})();
