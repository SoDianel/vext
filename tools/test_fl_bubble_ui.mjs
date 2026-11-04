// Run with node. The bubble runs in a vm sandbox with a fake DOM, a fake shell (IPC) and a fake index
// (window.VextFlLauncher): no window, no real FL, MIDI or preset is involved.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const read = (name) => fs.readFileSync(new URL(`../app/static/${name}`, import.meta.url), 'utf8');
const source = read('js/fl-bubble.js');
const settle = async () => { for (let i = 0; i < 50; i++) await Promise.resolve(); };
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};

// What fl_bubble_expand answers when nothing is queued: the circle's center, the panel to its left.
const GEOMETRY = { cx: 290, cy: 200, panel: { x: 90, y: 40, w: 168, h: 320 }, side: 'left' };
const INSIDE = { cx: 290, cy: 200 };
const OUTSIDE = { cx: -50, cy: -50 };
const allCalls = []; // every IPC call of every run: checked at the end

async function runtime({ preview = false } = {}) {
  const log = [];     // IPC, drags and panel changes, in order
  const calls = [];
  const errors = [];
  const resizes = []; // queued fl_bubble_expand answers: a promise, or a geometry
  const cursors = []; // queued fl_bubble_cursor answers
  const drags = [];   // queued startDragging answers
  let cursorAt = INSIDE;
  let panelHidden = true;
  const circle = {
    style: {}, listeners: {}, title: '',
    addEventListener(name, fn) { this.listeners[name] = fn; },
  };
  const panel = {
    style: {}, dataset: {},
    get hidden() { return panelHidden; },
    set hidden(value) { panelHidden = value; log.push(value ? 'panel hidden' : 'panel shown'); },
  };
  const root = { dataset: {} };
  const docListeners = {};
  const docEvent = (name, event = {}) => (docListeners[name] || []).map((fn) => fn(event));
  const timers = new Map();
  const intervals = new Map();
  let now = 1000;
  let id = 0;
  const index = {
    refreshes: 0, cancels: 0, dragging: false,
    refresh() { this.refreshes++; },
    cancelConfirm() { this.cancels++; },
    isDragging() { return this.dragging; },
  };
  const window = { innerWidth: 580, innerHeight: 400, VextFlLauncher: index };
  if (!preview) {
    window.__TAURI__ = {
      window: { getCurrentWindow: () => ({ startDragging: () => {
        log.push('startDragging');
        return drags.length ? drags.shift() : Promise.resolve();
      } }) },
      core: { invoke: async (name, args) => {
        calls.push({ name, args });
        allCalls.push({ name, args });
        if (name === 'fl_bubble_expand') {
          log.push(`resize ${args.expanded}${args.reflow ? ' reflow' : ''}`);
          return resizes.length ? resizes.shift() : GEOMETRY;
        }
        if (name === 'fl_bubble_cursor') return cursors.length ? cursors.shift() : cursorAt;
        throw new Error('unexpected IPC ' + name);
      } },
    };
  }
  const document = {
    documentElement: root,
    getElementById: (name) => (name === 'fb-panel' ? panel : circle),
    addEventListener(name, fn) { (docListeners[name] ||= []).push(fn); },
  };
  vm.runInNewContext(source, {
    window, document,
    setTimeout: (fn, ms = 0) => { timers.set(++id, { fn, at: now + ms }); return id; },
    clearTimeout: (key) => { timers.delete(key); },
    setInterval: (fn, ms) => { intervals.set(++id, { fn, ms }); return id; },
    clearInterval: (key) => { intervals.delete(key); },
    Date: { now: () => now },
    console: { error: (...args) => errors.push(args), warn() {} },
  });
  await settle();
  return {
    circle, panel, root, errors, log, index, calls,
    enter() { circle.listeners.mouseenter({ buttons: 0 }); },
    leave() { circle.listeners.mouseleave({}); },
    click() { circle.listeners.click({}); },
    key(name) { circle.listeners.keydown({ key: name, preventDefault() {} }); },
    docEvent,
    // Copied out of the vm realm, so deepStrictEqual compares plain objects.
    resizeCalls: () => calls.filter((c) => c.name === 'fl_bubble_expand').map((c) => JSON.parse(JSON.stringify(c.args))),
    queueResize(answer) { resizes.push(answer); },
    queueCursor(answer) { cursors.push(answer); },
    queueDrag(answer) { drags.push(answer); },
    setCursor(at) { cursorAt = at; },
    dragCount: () => log.filter((entry) => entry === 'startDragging').length,
    // Advances the clock in 20 ms steps: due timeouts fire, and the cursor watch polls every 120 ms.
    async elapse(ms) {
      for (let t = 20; t <= ms; t += 20) {
        now += 20;
        for (const [key, timer] of [...timers]) {
          if (timer.at <= now) { timers.delete(key); timer.fn(); }
        }
        if (t % 120 === 0) for (const { fn, ms: period } of [...intervals.values()]) if (period === 120) fn();
        await settle();
      }
    },
  };
}

// Hover opens after 200 ms, and the panel is placed from the rect the shell returned.
{
  const rt = await runtime();
  assert.equal(rt.panel.hidden, true, 'The panel starts folded');
  assert.equal(rt.root.dataset.open, undefined);
  assert.equal(rt.circle.style.left, '262px', 'The circle sits on the returned center');
  rt.enter();
  await rt.elapse(180);
  assert.equal(rt.resizeCalls().some((a) => a.expanded), false, 'Hover waits 200 ms');
  await rt.elapse(40);
  assert.deepEqual(rt.resizeCalls().at(-1), { expanded: true, reflow: false });
  assert.equal(rt.panel.hidden, false);
  assert.deepEqual(rt.panel.style, { left: '90px', top: '40px', width: '168px', height: '320px' });
  assert.equal(rt.panel.dataset.side, 'left');
  assert.equal(rt.root.dataset.open, 'true');
  assert.equal(rt.index.refreshes, 1, 'Each open refreshes the listing');
}

// Leaving before 200 ms cancels the hover.
{
  const rt = await runtime();
  rt.enter();
  await rt.elapse(100);
  rt.leave();
  await rt.elapse(300);
  assert.equal(rt.panel.hidden, true, 'A hover that leaves early does not open the panel');
  assert.equal(rt.resizeCalls().some((a) => a.expanded), false);
}

// A click opens at once; a click on the open circle never folds it.
{
  const rt = await runtime();
  rt.click();
  await settle();
  assert.equal(rt.panel.hidden, false, 'A click opens the panel');
  rt.click();
  await settle();
  assert.equal(rt.resizeCalls().filter((a) => a.expanded).length, 1, 'A click on the open circle does nothing');
  assert.equal(rt.panel.hidden, false);
}

// Enter and Space open the panel like a click.
for (const key of ['Enter', ' ']) {
  const rt = await runtime();
  rt.key(key);
  await settle();
  assert.equal(rt.panel.hidden, false, `${JSON.stringify(key)} opens the panel`);
}

// A drag of the circle never opens the panel: not on release, and not for the hover or click right after it.
{
  const rt = await runtime();
  const drag = deferred();
  rt.queueDrag(drag.promise);
  rt.circle.listeners.pointerdown({ button: 0, clientX: 30, clientY: 30 });
  const moved = Promise.all(rt.docEvent('pointermove', { buttons: 1, clientX: 45, clientY: 30 }));
  await settle();
  assert.equal(rt.dragCount(), 1, 'Moving the circle past 6 px starts a native drag');
  drag.resolve();
  rt.docEvent('pointerup', {});
  await moved;
  rt.enter();
  rt.click();
  await rt.elapse(250);
  assert.equal(rt.panel.hidden, true, 'A drag release never opens the panel');
  await rt.elapse(400);
  rt.enter();
  await rt.elapse(220);
  assert.equal(rt.panel.hidden, false, 'Hover opens the panel again once the drag has settled');
}

// Outside for more than 400 ms folds the panel, and the native region shrinks before the panel is hidden.
{
  const rt = await runtime();
  rt.enter();
  await rt.elapse(200);
  rt.setCursor(OUTSIDE);
  await rt.elapse(120); // the first outside sample
  assert.equal(rt.panel.hidden, false, 'One outside sample is not enough');
  await rt.elapse(240);
  assert.equal(rt.panel.hidden, false, 'Still inside the 400 ms grace');
  await rt.elapse(240);
  assert.equal(rt.panel.hidden, true, 'Outside for more than 400 ms folds the panel');
  const shrink = rt.log.indexOf('resize false');
  assert.ok(shrink > -1 && shrink < rt.log.indexOf('panel hidden'), 'The native region shrinks before the panel is hidden');
  assert.equal(rt.root.dataset.open, 'false');
  assert.equal(rt.index.cancels, 1, 'Folding cancels a pending confirm');
  rt.setCursor(INSIDE);
  rt.enter();
  await rt.elapse(200);
  assert.equal(rt.panel.hidden, false, 'Hover reopens the folded panel');
  assert.equal(rt.index.refreshes, 2);
}

// The index's own preset drag keeps the panel open, even with the pointer outside.
{
  const rt = await runtime();
  rt.enter();
  await rt.elapse(200);
  rt.index.dragging = true;
  rt.setCursor(OUTSIDE);
  await rt.elapse(1000);
  assert.equal(rt.panel.hidden, false, 'No collapse while a preset drag into FL is running');
  rt.index.dragging = false;
  await rt.elapse(720);
  assert.equal(rt.panel.hidden, true, 'The panel folds once the drag has ended and the pointer stays out');
}

// Dragging the bubble with the pointer outside keeps the panel open; after the drop it folds normally.
{
  const rt = await runtime();
  rt.enter();
  await rt.elapse(200);
  const drag = deferred();
  rt.queueDrag(drag.promise);
  rt.setCursor(OUTSIDE);
  rt.circle.listeners.pointerdown({ button: 0, clientX: 30, clientY: 30 });
  const moved = Promise.all(rt.docEvent('pointermove', { buttons: 1, clientX: 45, clientY: 30 }));
  await rt.elapse(1000);
  assert.equal(rt.panel.hidden, false, 'No collapse while the bubble itself is being dragged');
  drag.resolve();
  rt.docEvent('pointerup', {});
  await moved;
  await rt.elapse(720);
  assert.equal(rt.panel.hidden, true, 'After the drop, the outside pointer folds the panel');
}

// The index's close button and Escape both send fl-index:close; a folded panel ignores it.
{
  const rt = await runtime();
  rt.enter();
  await rt.elapse(200);
  rt.docEvent('fl-index:close');
  await settle();
  assert.equal(rt.panel.hidden, true, 'fl-index:close folds the panel');
  assert.equal(rt.root.dataset.open, 'false');
  assert.equal(rt.index.cancels, 1);
  const shrinks = rt.resizeCalls().length;
  rt.docEvent('fl-index:close');
  await settle();
  assert.equal(rt.resizeCalls().length, shrinks, 'A folded panel ignores a further close');
}

// A failed expand keeps the circle and a folded panel; the next hover retries.
{
  const rt = await runtime();
  const failed = deferred();
  rt.queueResize(failed.promise);
  rt.enter();
  await rt.elapse(200);
  failed.reject(new Error('resize denied'));
  await settle();
  assert.equal(rt.panel.hidden, true, 'A failed expand leaves the panel folded');
  assert.equal(rt.root.dataset.open, undefined, 'The open state is not set');
  assert.equal(rt.index.refreshes, 0);
  assert.equal(rt.circle.style.left, '262px', 'The circle stays where it was');
  assert.equal(rt.errors.length, 1, 'The failure is reported');
  rt.enter();
  await rt.elapse(200);
  assert.equal(rt.panel.hidden, false, 'A later hover retries');
}

// A failed collapse leaves the panel open and usable; the next outside interval retries.
{
  const rt = await runtime();
  rt.enter();
  await rt.elapse(200);
  const failed = deferred();
  rt.queueResize(failed.promise);
  rt.setCursor(OUTSIDE);
  await rt.elapse(600); // the collapse starts; the shell has not answered yet
  failed.reject(new Error('collapse denied'));
  await settle();
  assert.equal(rt.panel.hidden, false, 'A failed collapse keeps the panel open');
  assert.equal(rt.root.dataset.open, 'true');
  assert.equal(rt.index.cancels, 0, 'The confirm bar is untouched');
  assert.equal(rt.errors.length, 1);
  await rt.elapse(720);
  assert.equal(rt.panel.hidden, true, 'The next outside interval folds the panel');
}

// Cursor reads never overlap: a slow reply holds the next poll back.
{
  const rt = await runtime();
  rt.enter();
  await rt.elapse(200);
  const slow = deferred();
  rt.queueCursor(slow.promise);
  await rt.elapse(240); // two polls are due, one read is in flight
  assert.equal(rt.calls.filter((c) => c.name === 'fl_bubble_cursor').length, 1);
  slow.reject(new Error('cursor denied'));
  await settle();
  assert.equal(rt.errors.length, 1);
  assert.equal(rt.panel.hidden, false, 'A failed read keeps the panel open');
}

// Dropping the bubble reflows: the canvas and the panel are refitted from the shell's answer.
{
  const rt = await runtime();
  rt.enter();
  await rt.elapse(200);
  const drag = deferred(), refit = deferred();
  rt.queueDrag(drag.promise);
  rt.queueResize(refit.promise);
  rt.circle.listeners.pointerdown({ button: 0, clientX: 30, clientY: 30 });
  const moved = Promise.all(rt.docEvent('pointermove', { buttons: 1, clientX: 45, clientY: 30 }));
  drag.resolve();
  await settle();
  assert.deepEqual(rt.resizeCalls().at(-1), { expanded: true, reflow: true }, 'Drop refits the canvas');
  refit.resolve({ cx: 550, cy: 380, panel: { x: 12, y: 40, w: 520, h: 320 }, side: 'left' });
  await moved;
  assert.equal(rt.circle.style.left, '522px', 'An edge anchor stays where the shell put it');
  assert.equal(rt.circle.style.top, '352px');
  assert.deepEqual(rt.panel.style, { left: '12px', top: '40px', width: '520px', height: '320px' });
}

// Browser preview (no IPC): the panel is drawn once, to the left of the circle at the right edge.
{
  const rt = await runtime({ preview: true });
  assert.equal(rt.panel.hidden, false);
  assert.equal(rt.root.dataset.open, 'true');
  assert.deepEqual(rt.panel.style, { left: '8px', top: '8px', width: '482px', height: '384px' });
  assert.equal(rt.panel.dataset.side, 'left');
  assert.equal(rt.circle.style.left, '508px');
  assert.equal(rt.circle.style.top, '172px');
  assert.equal(rt.calls.length, 0, 'The preview makes no IPC call');
}

// Static checks: the page stacks the circle above the panel, the status SVG and the old window commands are gone.
{
  const html = read('fl-bubble.html');
  assert.doesNotMatch(html, /fb-svg|data-tauri-drag-region|fln-light|fl-launcher\.html/, 'The status SVG, drag regions, footer light and old page are gone');
  assert.match(html, /<section class="fb-panel fln-panel" id="fb-panel" aria-label="FL plugin index" hidden>/);
  assert.match(html, /<span class="fln-status" id="fln-status" data-state="closed"/);
  assert.ok(html.indexOf('id="fb-panel"') < html.indexOf('id="fb-collapsed"'), 'The circle comes after the panel, so it draws above it');
  assert.match(html, /style\.css[\s\S]*fl-launch\.css[\s\S]*fl-launcher\.css[\s\S]*fl-bubble\.css/, 'The bubble stylesheet loads last');
  assert.match(html, /fl-preset\.js[\s\S]*fl-launcher\.js[\s\S]*fl-bubble\.js/, 'The scripts load in dependency order');
  const css = read('css/fl-bubble.css');
  assert.doesNotMatch(css, /fb-svg|fb-status/);
  assert.match(css, /\.fb-collapsed \{[^}]*z-index: 2/);
  assert.doesNotMatch(read('css/fl-launcher.css'), /^(html|body)\s*[,{]/m, 'The panel CSS never restyles the transparent page');
  assert.doesNotMatch(source, /fl_launcher_open|fl_launcher_close|fb-svg|loadMenu|pollConnection/);
}

// No run ever asked the shell for the removed launcher window; only the two bubble commands were used.
assert.equal(allCalls.some((c) => c.name === 'fl_launcher_open' || c.name === 'fl_launcher_close'), false);
assert.deepEqual([...new Set(allCalls.map((c) => c.name))].sort(), ['fl_bubble_cursor', 'fl_bubble_expand']);

console.log('FL bubble UI: hover/click open, placement, drag and reflow, outside and drag holds, close events, failed resize and cursor checks passed.');
