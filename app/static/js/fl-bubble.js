// The FL bubble (desktop app only: the fl-bubble window, desktop/src-tauri/src/fl_bubble.rs). The VEXT circle is
// the drag handle. Hovering or clicking it opens the FL plugin index panel (fl-launcher.js) inside this window,
// beside the circle; the panel folds back when the pointer leaves both. Outside the desktop app the panel is
// drawn once, so the layout can be previewed in a browser.
(function () {
  'use strict';

  const T = window.__TAURI__ || null;
  const invoke = T && T.core && typeof T.core.invoke === 'function' ? T.core.invoke : null;
  const root = document.documentElement;
  const circle = document.getElementById('fb-collapsed');
  const panel = document.getElementById('fb-panel');

  const RING = 36; // the circle's outer ring, from its center
  let center = { cx: 36, cy: 36 };
  let panelBox = { x: 0, y: 0, w: 0, h: 0 };
  let expanded = false;
  let transitioning = false;
  let cursorPending = false;
  let dragging = false;
  let dragDown = null;
  let moveTimer = 0;
  let generation = 0; // bumped on every collapse, so a late answer is dropped
  let windowQueue = Promise.resolve(); // window size changes run one at a time, in order
  let openTimer = 0, suppressClickUntil = 0;
  let watchTimer = 0, outsideSince = 0;

  // ---------- geometry ----------

  // The circle is 56 px and sits on the bubble's center, wherever the shell clamped it in the window.
  function placeCircle() {
    circle.style.left = (center.cx - 28) + 'px';
    circle.style.top = (center.cy - 28) + 'px';
  }

  // fl_bubble_expand answers with the circle's center, the panel rect (CSS px in the window) and its side.
  function applyGeometry(g) {
    center = { cx: g.cx, cy: g.cy };
    panelBox = g.panel;
    placeCircle();
    panel.style.left = g.panel.x + 'px';
    panel.style.top = g.panel.y + 'px';
    panel.style.width = g.panel.w + 'px';
    panel.style.height = g.panel.h + 'px';
    panel.dataset.side = g.side;
  }

  // Inside: within the circle's ring, or within the panel grown by 12 px (the gap between them counts).
  function inside(at) {
    if (Math.hypot(at.cx - center.cx, at.cy - center.cy) <= RING) return true;
    const p = panelBox;
    return at.cx >= p.x - 12 && at.cx <= p.x + p.w + 12 && at.cy >= p.y - 12 && at.cy <= p.y + p.h + 12;
  }

  function resize(open, reflow = false) {
    const next = windowQueue.then(() => invoke('fl_bubble_expand', { expanded: open, reflow }));
    windowQueue = next.catch(() => {});
    return next;
  }

  function report(operation, error) {
    console.error('FL bubble ' + operation, error);
    circle.title = 'FL plugin index could not ' + operation + '. Move off the bubble and try again.';
  }

  // Opening waits for the pointer to settle: never during a drag, and not right after one.
  function canOpen() {
    return !expanded && !transitioning && !dragging && Date.now() >= suppressClickUntil;
  }

  // ---------- open and fold ----------

  async function expand() {
    if (expanded || transitioning) return;
    transitioning = true;
    clearTimeout(openTimer);
    const token = ++generation;
    try {
      // The circle stays painted until the shell answers with the real geometry.
      const g = await resize(true);
      if (token !== generation) return;
      applyGeometry(g);
      panel.hidden = false;
      expanded = true;
      root.dataset.open = 'true';
      window.VextFlLauncher.refresh();
      watch();
    } catch (error) {
      report('open', error);
    } finally {
      transitioning = false;
    }
  }

  // The shell shrinks the native region first, so the panel is never hidden inside a larger window.
  async function collapse() {
    if (!expanded || transitioning) return;
    transitioning = true;
    try {
      const g = await resize(false);
      generation++;
      expanded = false;
      clearInterval(watchTimer);
      panel.hidden = true;
      root.dataset.open = 'false';
      window.VextFlLauncher.cancelConfirm();
      applyGeometry(g);
    } catch (error) {
      // A failed resize leaves the panel as it was, and usable; the watch retries after a new outside interval.
      report('close', error);
    } finally {
      transitioning = false;
      outsideSince = 0;
    }
  }

  // Polls are serialized: a late cursor reply must not close a newer panel.
  function watch() {
    clearInterval(watchTimer);
    outsideSince = 0;
    watchTimer = setInterval(async () => {
      // A native drag (of this bubble, or of a preset into FL) keeps the panel open whatever the pointer does.
      if (!expanded || transitioning || cursorPending || dragging || dragDown || window.VextFlLauncher.isDragging()) return;
      cursorPending = true;
      const token = generation;
      try {
        const at = await invoke('fl_bubble_cursor');
        if (!expanded || transitioning || token !== generation) return;
        if (inside(at)) { outsideSince = 0; return; }
        if (!outsideSince) outsideSince = Date.now();
        else if (Date.now() - outsideSince >= 400) collapse();
      } catch (error) {
        report('read the pointer', error);
      } finally {
        cursorPending = false;
      }
    }, 120);
  }

  // ---------- events ----------

  circle.addEventListener('mouseenter', (event) => {
    clearTimeout(openTimer);
    if (!event.buttons && canOpen()) {
      openTimer = setTimeout(() => { if (canOpen()) expand(); }, 200);
    }
  });
  circle.addEventListener('mouseleave', () => clearTimeout(openTimer));
  circle.addEventListener('click', () => { if (canOpen()) expand(); });
  circle.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    event.preventDefault();
    if (canOpen()) expand();
  });

  async function reflow() {
    if (transitioning) return;
    transitioning = true;
    try {
      applyGeometry(await resize(expanded, true));
    } catch (error) { report('place the bubble', error); }
    finally { transitioning = false; outsideSince = 0; }
  }

  function beginDrag(event) {
    if (event.button !== 0) return;
    clearTimeout(openTimer);
    dragDown = { x: event.clientX, y: event.clientY };
    if (circle.setPointerCapture && event.pointerId !== undefined) circle.setPointerCapture(event.pointerId);
    outsideSince = 0;
  }
  circle.addEventListener('pointerdown', beginDrag);
  document.addEventListener('pointerup', () => { dragDown = null; });
  document.addEventListener('pointercancel', () => { dragDown = null; });
  document.addEventListener('pointermove', async (event) => {
    if (!dragDown || dragging || !(event.buttons & 1) || Math.hypot(event.clientX - dragDown.x, event.clientY - dragDown.y) < 6) return;
    dragDown = null;
    if (!T?.window?.getCurrentWindow) return;
    dragging = true;
    suppressClickUntil = Date.now() + 1000;
    try { await T.window.getCurrentWindow().startDragging(); }
    catch (error) { report('move the bubble', error); }
    finally { dragging = false; suppressClickUntil = Date.now() + 300; outsideSince = 0; await reflow(); }
  });
  if (T?.event?.listen) {
    T.event.listen('fl-bubble:moved', () => {
      clearTimeout(moveTimer);
      moveTimer = setTimeout(() => { if (!dragging) reflow(); }, 120);
    });
  }

  // The index's close button and Escape (fl-launcher.js) fold the panel back into the circle.
  document.addEventListener('fl-index:close', () => { collapse(); });
  document.addEventListener('contextmenu', (event) => event.preventDefault());

  async function start() {
    if (!invoke) {
      // Browser preview: the index panel, drawn once to the left of the circle.
      const w = window.innerWidth, h = window.innerHeight;
      expanded = true;
      applyGeometry({ cx: w - 44, cy: h / 2, panel: { x: 8, y: 8, w: w - 98, h: h - 16 }, side: 'left' });
      panel.hidden = false;
      root.dataset.open = 'true';
      return;
    }
    try {
      applyGeometry(await resize(false)); // The saved position may clamp the initial center too.
    } catch (error) { report('place the bubble', error); }
  }

  start();
})();
