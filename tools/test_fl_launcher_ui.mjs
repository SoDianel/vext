// Run with node. The launcher runs in a vm sandbox with a fake DOM and a fake backend:
// no browser, no real FL, MIDI or Tauri window is involved.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const read = (name) => fs.readFileSync(new URL(`../app/static/${name}`, import.meta.url), 'utf8');
const presetSource = read('js/fl-preset.js');
const launcherSource = read('js/fl-launcher.js');

const pick = { key: 'Installed/Generators/VST3/Synth.fst', name: 'Synth', kind: 'Generators' };
const listing = {
  title: 'FabFilter',
  plugins: [{ id: 7, name: 'Pro-Q 3', vendor: 'FabFilter', plugin_type: 'Effect', format: 'VST3', has_image: false, categories: [['Equalizer', 'equalizer']] }],
};
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

class FakeCustomEvent {
  constructor(type, init = {}) { this.type = type; this.detail = init.detail ?? null; }
}

function element(id, hidden = false) {
  const listeners = {};
  let hiddenValue = hidden;
  const el = {
    id, listeners, value: '', textContent: '', innerHTML: '', checked: false, dataset: {}, tagName: 'DIV',
    shows: 0, // how many times the element was shown
    get hidden() { return hiddenValue; },
    set hidden(value) { if (!value) el.shows++; hiddenValue = value; },
    classList: { toggle() {}, add() {}, remove() {} },
    addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
    setAttribute() {},
    remove() {},
    focus() {},
    closest() { return null; },
    querySelectorAll() { return []; },
    querySelector() { return element('child'); },
  };
  return el;
}

// Answers the backend routes the launcher uses; `overrides` replaces single routes.
function backend({ connected = true } = {}) {
  return (url) => {
    if (url.endsWith('/connection')) return { running: true, connected, message: connected ? 'Connected to FL Studio' : 'FL Studio running, controller disconnected' };
    if (url.includes('/presets/')) return { presets: [pick] };
    if (url.includes('/api/fl-launch/preset?')) return pick;
    if (url.includes('/api/fl-launch/menu?')) return { configured: true, ...listing };
    if (url.endsWith('/load')) return { state: 'sent', message: 'Handed to FL' };
    if (url.endsWith('/status')) return { state: 'sent', message: 'Handed to FL' };
    return {};
  };
}

function runtime({ connected = true, answer = backend({ connected }), focusAllowed = true, snapshot = null } = {}) {
  const log = [];
  const requests = [];
  const invokes = [];
  const elements = new Map();
  const documentListeners = {};
  const documentEvents = []; // every document.dispatchEvent type, in order
  const storage = new Map();
  if (snapshot) storage.set('fl-launcher-snapshot', JSON.stringify(snapshot));
  const window = {
    __VEXT__: { apiBase: "", token: "launch-token" },
    __TAURI__: {
      core: {
        invoke: async (name, args) => {
          log.push('invoke ' + name);
          invokes.push({ name, args });
          if (name === 'fl_focus' && !focusAllowed) throw new Error('Windows did not give FL Studio focus.');
          return null;
        },
      },
    },
  };
  const context = {
    window,
    CustomEvent: FakeCustomEvent,
    document: {
      title: '',
      body: { appendChild() {} },
      getElementById(id) {
        // The confirm bar starts hidden, as in the page.
        if (!elements.has(id)) elements.set(id, element(id, id === 'fln-confirm'));
        return elements.get(id);
      },
      createElement() { return element('dialog'); },
      addEventListener(type, fn) { (documentListeners[type] ||= []).push(fn); },
      dispatchEvent(event) {
        documentEvents.push(event.type);
        return (documentListeners[event.type] || []).map((fn) => fn(event));
      },
    },
    fetch: async (url, options = {}) => {
      const method = String(options.method || 'GET').toUpperCase();
      requests.push({ url, method, body: options.body });
      if (method === 'POST' && url.endsWith('/load')) log.push('POST /api/fl-launch/load');
      let data;
      try {
        data = await answer(url, options);
      } catch (error) {
        return { ok: false, status: 409, json: async () => ({ detail: error.message }) };
      }
      return { ok: true, status: 200, json: async () => data };
    },
    localStorage: {
      getItem: (key) => (storage.has(key) ? storage.get(key) : null),
      setItem: (key, value) => storage.set(key, String(value)),
    },
    setInterval() {},
    setTimeout,
    clearTimeout,
    console,
    Date, Math, Number, String, Promise, Error, Object, Array, JSON, Map, Set,
  };
  vm.createContext(context);
  vm.runInContext(presetSource, context);
  vm.runInContext(launcherSource, context);
  return {
    api: window.VextFlLauncher, log, requests, invokes, elements, documentListeners, documentEvents, storage,
    fire(id, type, event = {}) {
      return (elements.get(id).listeners[type] || []).map((fn) => fn(event));
    },
    docEvent(type, event) {
      return (documentListeners[type] || []).map((fn) => fn(event));
    },
  };
}

const loads = (rt) => rt.requests.filter((r) => r.method === 'POST' && r.url.endsWith('/load'));

async function started(options) {
  const rt = runtime(options);
  await settle();
  await settle();
  return rt;
}

// Focus first, then the load: FL must be in front before the controller's MIDI arrives.
{
  const rt = await started();
  const found = rt.requests.find((r) => r.url.includes('/api/fl-launch/menu?'));
  assert.ok(found, 'The launcher lists the plugins for its query');
  assert.match(found.url, /include_plugins=true/);
  assert.equal(rt.elements.get('fln-title').textContent, 'FL plugin index');

  await rt.api.load(7);
  assert.equal(rt.elements.get('fln-confirm').hidden, false, 'A click shows the inline confirm bar first');
  assert.equal(loads(rt).length, 0, 'Nothing is sent before the confirm');
  await Promise.all(rt.fire('fln-send', 'click'));
  assert.ok(rt.log.indexOf('invoke fl_focus') >= 0, 'fl_focus is invoked');
  assert.ok(rt.log.indexOf('invoke fl_focus') < rt.log.indexOf('POST /api/fl-launch/load'),
    'Click-to-load must hand focus to FL before the load POST');
  assert.equal(loads(rt).length, 1);
  assert.deepEqual(JSON.parse(loads(rt)[0].body), { plugin_id: 7, key: pick.key });
  assert.equal(rt.elements.get('fln-result').textContent, 'Handed to FL');
}

// Cancel = no load.
{
  const rt = await started();
  await rt.api.load(7);
  await Promise.all(rt.fire('fln-cancel', 'click'));
  assert.equal(rt.elements.get('fln-confirm').hidden, true);
  assert.equal(loads(rt).length, 0, 'Cancel must not send a load');
  assert.equal(rt.log.some((entry) => entry === 'invoke fl_focus'), false, 'Cancel must not move focus');
}

// Don't ask again skips the confirm bar for the rest of this window's session.
{
  const rt = await started();
  await rt.api.load(7);
  rt.elements.get('fln-dont-ask').checked = true;
  await Promise.all(rt.fire('fln-send', 'click'));
  assert.equal(loads(rt).length, 1);
  assert.equal(rt.storage.get('fl-launcher-skip-confirm'), 'launch-token', 'The flag is stamped with this launch, so it ends when VEXT closes');
  const shownBefore = rt.elements.get('fln-confirm').shows;
  await rt.api.load(7);
  assert.equal(loads(rt).length, 2, 'With don\'t ask again, a click sends at once');
  assert.equal(rt.elements.get('fln-confirm').shows, shownBefore, 'The confirm bar is not shown again');
}

// Disconnected: a click never sends a load.
{
  const rt = await started({ connected: false });
  await rt.api.load(7);
  assert.equal(loads(rt).length, 0, 'Disconnected clicks must never send a load');
  assert.equal(rt.elements.get('fln-result').textContent, 'FL Studio running, controller disconnected');
}

// A denied focus handoff never sends MIDI.
{
  const rt = await started({ focusAllowed: false });
  await rt.api.load(7);
  await Promise.all(rt.fire('fln-send', 'click'));
  assert.equal(loads(rt).length, 0, 'A denied focus handoff must never send a load');
}

// Drag: only while the left button stays held.
const card = { dataset: { pluginId: '7' } };
const cardTarget = { closest: (selector) => (selector === '[data-plugin-id]' ? card : null) };
{
  const rt = await started();
  rt.docEvent('pointerdown', { target: cardTarget, button: 0, clientX: 0, clientY: 0 });
  await Promise.all(rt.docEvent('pointermove', { target: cardTarget, buttons: 1, clientX: 20, clientY: 0 }));
  const drag = rt.invokes.find((call) => call.name === 'fl_drag_preset');
  assert.ok(drag, 'A held drag past 8 px starts the native drag');
  assert.equal(drag.args.pluginId, 7);
  assert.equal(drag.args.key, pick.key);
}
{
  let releasePresets;
  const rt = await started({
    answer: (url, options) => {
      if (url.includes('/presets/')) return new Promise((resolve) => { releasePresets = () => resolve({ presets: [pick] }); });
      return backend()(url, options);
    },
  });
  rt.docEvent('pointerdown', { target: cardTarget, button: 0, clientX: 0, clientY: 0 });
  const moved = Promise.all(rt.docEvent('pointermove', { target: cardTarget, buttons: 1, clientX: 20, clientY: 0 }));
  await settle();
  rt.docEvent('pointerup', {});
  releasePresets();
  await moved;
  assert.equal(rt.invokes.some((call) => call.name === 'fl_drag_preset'), false,
    'A late preset answer must not start a drag after the button was released');
}
{
  // A drag shorter than 8 px is a click, not a drag.
  const rt = await started();
  rt.docEvent('pointerdown', { target: cardTarget, button: 0, clientX: 0, clientY: 0 });
  await Promise.all(rt.docEvent('pointermove', { target: cardTarget, buttons: 1, clientX: 3, clientY: 2 }));
  assert.equal(rt.invokes.some((call) => call.name === 'fl_drag_preset'), false);
}

console.log('FL launcher UI: confirm, cancel, don\'t-ask-again, focus order and drag checks passed.');

// An FL-only FST has no catalog ID: click and drag must carry its exact key instead.
{
  const rt = await started();
  await rt.api.load(null, pick.key);
  assert.equal(loads(rt).length, 0);
  await Promise.all(rt.fire('fln-send', 'click'));
  assert.deepEqual(JSON.parse(loads(rt)[0].body), { plugin_id: null, key: pick.key });
  assert.ok(rt.log.indexOf('invoke fl_focus') < rt.log.indexOf('POST /api/fl-launch/load'));
}
{
  const rt = await started();
  const target = { closest: (selector) => selector === '[data-plugin-id]' ?
    { dataset: { pluginId: '', presetKey: pick.key } } : null };
  rt.docEvent('pointerdown', { target, button: 0, clientX: 0, clientY: 0 });
  await Promise.all(rt.docEvent('pointermove', { target, buttons: 1, clientX: 20, clientY: 0 }));
  const drag = rt.invokes.find((call) => call.name === 'fl_drag_preset');
  assert.equal(drag.args.pluginId, null);
  assert.equal(drag.args.key, pick.key);
}
{
  let reply;
  const rt = await started({ answer: (url) => url.includes('/api/fl-launch/preset?') ?
    new Promise((resolve) => { reply = () => resolve(pick); }) : backend()(url) });
  const target = { closest: (selector) => selector === '[data-plugin-id]' ?
    { dataset: { pluginId: '', presetKey: pick.key } } : null };
  rt.docEvent('pointerdown', { target, button: 0, clientX: 0, clientY: 0 });
  const moved = Promise.all(rt.docEvent('pointermove', { target, buttons: 1, clientX: 20, clientY: 0 }));
  await settle();
  rt.docEvent('pointerup', {});
  reply();
  await moved;
  assert.equal(rt.invokes.some((call) => call.name === 'fl_drag_preset'), false);
}


// The first frame can show the menu's authenticated snapshot while HTTP is still pending.
{
  let finish;
  const rt = await started({
    snapshot: { token: 'launch-token', at: Date.now(), plugins: listing.plugins },
    answer: (url) => url.includes('/api/fl-launch/menu?') ?
      new Promise((resolve) => { finish = () => resolve(listing); }) : backend()(url),
  });
  assert.match(rt.elements.get('fln-grid').innerHTML, /Pro-Q 3/);
  assert.equal(rt.elements.get('fln-empty').hidden, true);
  finish();
  await settle();
}
{
  const rt = await started({
    snapshot: { token: 'old-launch', at: Date.now(), plugins: listing.plugins },
    answer: (url) => url.includes('/api/fl-launch/menu?') ? new Promise(() => {}) : backend()(url),
  });
  assert.doesNotMatch(rt.elements.get('fln-grid').innerHTML, /Pro-Q 3/);
  assert.match(rt.elements.get('fln-empty').textContent, /Loading/);
}


// All FSTs are indexed first; sidebar rows and search then filter the local snapshot.
{
  const all = [
    ...listing.plugins,
    { id: null, key: 'Generators/Keys.fst', name: 'Soft Keys', vendor: 'Other Vendor', plugin_type: 'Instrument', format: 'FST', has_image: false, categories: [['Piano / Keys', 'keys']] },
    { id: null, key: 'Effects/Room.fst', name: 'Room', vendor: 'Other Vendor', plugin_type: 'Effect', format: 'FST', has_image: false, categories: [['Reverb', 'reverb']] },
  ];
  const rt = await started({ answer: (url) => url.includes('/api/fl-launch/menu?') ? { plugins: all } : backend()(url) });
  const html = () => rt.elements.get('fln-grid').innerHTML;
  assert.match(html(), /Soft Keys/);
  assert.match(html(), /Pro-Q 3/);
  assert.match(rt.elements.get('fln-groups').innerHTML, /Piano \/ Keys/);
  assert.equal(rt.elements.get('fln-count').textContent, '3 / 3 presets');
  const click = (id, dataset) => Promise.all(rt.fire(id, 'click', { target: { closest: () => ({ dataset }) } }));
  await click('fln-types', { type: 'instrument' });
  assert.match(html(), /Soft Keys/);
  assert.doesNotMatch(html(), /Pro-Q 3/);
  await click('fln-types', { type: 'all' });
  await click('fln-group-modes', { mode: 'vendor' });
  assert.match(rt.elements.get('fln-groups').innerHTML, /Other Vendor/);
  await click('fln-groups', { group: 'Other Vendor', name: 'Other Vendor' });
  assert.match(html(), /Soft Keys/);
  assert.doesNotMatch(html(), /Pro-Q 3/);
  rt.elements.get('fln-filter').value = 'room fst';
  await Promise.all(rt.fire('fln-filter', 'input'));
  assert.match(html(), /Room/);
  assert.doesNotMatch(html(), /Soft Keys/);
  await click('fln-views', { view: 'list' });
  assert.equal(rt.elements.get('fln-grid').dataset.view, 'list');
  await click('fln-views', { view: 'compact' });
  assert.equal(rt.storage.get('fl-launcher-view'), 'compact');
  assert.doesNotMatch(html(), /fln-pic/);
  await Promise.all(rt.fire('fln-reset', 'click'));
  assert.match(html(), /Soft Keys/);
  assert.match(html(), /Pro-Q 3/);
  await click('fln-group-modes', { mode: 'category' });
  await click('fln-groups', { group: 'equalizer', name: 'Equalizer' });
  assert.match(html(), /Pro-Q 3/);
  assert.doesNotMatch(html(), /Soft Keys/);
  // Browsing groups does not need another HTTP listing request.
  assert.equal(rt.requests.filter((r) => r.url.includes('/api/fl-launch/menu?')).length, 1);
}

// The view switch is icon-only: each button needs an accessible name and an SVG, and no visible text.
{
  const bubbleHtml = read('fl-bubble.html');
  const switchBlock = bubbleHtml.match(/<div class="fln-view-switch" id="fln-views"[^>]*>([\s\S]*?)<\/div>/);
  assert.ok(switchBlock, 'The view switch is in fl-bubble.html');
  for (const view of ['cards', 'list', 'compact']) {
    const button = switchBlock[1].match(new RegExp(`<button\\b[^>]*data-view="${view}"[^>]*>([\\s\\S]*?)</button>`));
    assert.ok(button, `The ${view} view has a button`);
    assert.match(button[0], /aria-label="[^"]+"/, `The ${view} view button has an aria-label`);
    assert.match(button[1], /<svg/, `The ${view} view button contains an SVG icon`);
    assert.equal(button[1].replace(/<[^>]*>/g, '').trim(), '', `The ${view} view button shows no visible text`);
  }
}

// The panel lives in the bubble: no URL query, no show event, and no window command for closing.
assert.doesNotMatch(launcherSource, /fl-launcher:show|location\.search|fl_launcher_close/, 'The index does not read a URL or close a window of its own');

// The bubble drives the panel through this object; each open refreshes the listing.
{
  const rt = await started();
  for (const name of ['load', 'setQuery', 'refresh', 'cancelConfirm', 'isDragging']) {
    assert.equal(typeof rt.api[name], 'function', `VextFlLauncher.${name} is exposed`);
  }
  const menuRequests = () => rt.requests.filter((r) => r.url.includes('/api/fl-launch/menu?')).length;
  const before = menuRequests();
  await rt.api.refresh();
  assert.equal(menuRequests(), before + 1, 'refresh() lists the presets again');
}

// The status pill follows the connection: Connected, Waiting (FL running, controller not connected) or FL closed.
{
  const connected = await started();
  const status = connected.elements.get('fln-status');
  assert.equal(status.dataset.state, 'connected');
  assert.equal(connected.elements.get('fln-status-text').textContent, 'Connected');
  assert.equal(status.title, 'Connected to FL Studio');
  assert.equal(connected.elements.get('fln-conn').textContent, 'Connected to FL Studio');
}
{
  const waiting = await started({ connected: false });
  assert.equal(waiting.elements.get('fln-status').dataset.state, 'running');
  assert.equal(waiting.elements.get('fln-status-text').textContent, 'Waiting');
  assert.equal(waiting.elements.get('fln-status').title, 'FL Studio running, controller disconnected');
}
{
  const closed = await started({
    answer: (url, options) => (url.endsWith('/connection')
      ? { running: false, connected: false, message: 'FL Studio is not running' } : backend()(url, options)),
  });
  assert.equal(closed.elements.get('fln-status').dataset.state, 'closed');
  assert.equal(closed.elements.get('fln-status-text').textContent, 'FL closed');
  assert.equal(closed.elements.get('fln-status').title, 'FL Studio is not running');
}

// The close button asks the bubble to fold the panel; it never invokes a window command.
{
  const rt = await started();
  await Promise.all(rt.fire('fln-close', 'click'));
  assert.deepEqual(rt.documentEvents, ['fl-index:close']);
  assert.equal(rt.invokes.some((call) => call.name === 'fl_launcher_close'), false, 'The removed window command is never invoked');
}

// Escape cancels an open confirm bar first; with nothing pending it closes the index.
{
  const rt = await started();
  await rt.api.load(7);
  assert.equal(rt.elements.get('fln-confirm').hidden, false);
  rt.docEvent('keydown', { key: 'Escape' });
  assert.equal(rt.elements.get('fln-confirm').hidden, true, 'Escape cancels the confirm bar');
  assert.deepEqual(rt.documentEvents, [], 'That Escape does not close the index');
  rt.docEvent('keydown', { key: 'Escape' });
  assert.deepEqual(rt.documentEvents, ['fl-index:close'], 'Escape closes the index when nothing is pending');
  assert.equal(loads(rt).length, 0, 'Escape never sends a load');
}

// isDragging() is true from the 8 px threshold until the native preset drag settles.
{
  let releasePresets;
  const rt = await started({
    answer: (url, options) => {
      if (url.includes('/presets/')) return new Promise((resolve) => { releasePresets = () => resolve({ presets: [pick] }); });
      return backend()(url, options);
    },
  });
  assert.equal(rt.api.isDragging(), false, 'No drag before the pointer moves');
  rt.docEvent('pointerdown', { target: cardTarget, button: 0, clientX: 0, clientY: 0 });
  const moved = Promise.all(rt.docEvent('pointermove', { target: cardTarget, buttons: 1, clientX: 20, clientY: 0 }));
  assert.equal(rt.api.isDragging(), true, 'A drag past 8 px is in progress');
  releasePresets();
  await moved;
  assert.equal(rt.api.isDragging(), false, 'The drag is over once its native call settles');
  assert.equal(rt.invokes.filter((call) => call.name === 'fl_drag_preset').length, 1);
}

// A card's picture: own preset picture, then catalog picture, then the filed copy's picture, then the placeholder.
{
  const base = { vendor: 'Vendor', plugin_type: 'Effect', format: 'VST3', categories: [] };
  const all = [
    { ...base, id: 11, key: 'Installed/Effects/VST3/Filed.fst', name: 'Filed only', has_image: false, has_preset_image: false, has_filed_image: true },
    { ...base, id: 12, key: 'Installed/Effects/VST3/Catalog.fst', name: 'Catalog wins', has_image: true, has_preset_image: false, has_filed_image: true },
    { ...base, id: 13, key: 'Installed/Effects/VST3/Bare.fst', name: 'Bare', has_image: false, has_preset_image: false, has_filed_image: false },
    { ...base, id: 14, key: 'Installed/Effects/VST3/Own.fst', name: 'Own picture', has_image: true, has_preset_image: true, has_filed_image: true },
  ];
  const rt = await started({ answer: (url, options) => (url.includes('/api/fl-launch/menu?') ? { plugins: all } : backend()(url, options)) });
  const html = rt.elements.get('fln-grid').innerHTML;
  const card = (name) => html.split('<div class="fln-card"').find((chunk) => chunk.includes(`>${name}</div>`));
  const presetUrl = (key) => `/api/fl-launch/preset-image?key=${encodeURIComponent(key)}`;

  const filed = card('Filed only');
  assert.match(filed, new RegExp(`<img src="${presetUrl(all[0].key).replace(/[.?]/g, '\\$&')}"`), 'A filed copy alone uses the preset-image route');
  assert.doesNotMatch(filed, /is-empty/, 'A filed picture is not an empty card');

  const catalog = card('Catalog wins');
  assert.match(catalog, /<img src="\/api\/plugins\/12\/image"/, 'The catalog picture wins over the filed copy');
  assert.doesNotMatch(catalog, /preset-image/);

  const own = card('Own picture');
  assert.match(own, new RegExp(`<img src="${presetUrl(all[3].key).replace(/[.?]/g, '\\$&')}"`), 'The own preset picture is first');
  assert.doesNotMatch(own, /\/api\/plugins\/14\/image/);

  const bare = card('Bare');
  assert.match(bare, /class="logo-mark"/, 'Without any picture the card shows the placeholder');
  assert.doesNotMatch(bare, /<img/);
  assert.match(bare, /class="fln-pic is-empty"/, 'Only a card with no picture of any kind is is-empty');
}

console.log('FL launcher panel: status pill, close and Escape events, bubble API and drag state checks passed.');
console.log('FL launcher index pictures: own, catalog, filed and placeholder order checks passed.');
