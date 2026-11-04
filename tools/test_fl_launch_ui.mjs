// Run with node. No browser automation or real MIDI/FL state is involved.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../app/static/js/fl-launch.js', import.meta.url), 'utf8');
// The shared preset helper (picker and drag) is js/fl-preset.js. It runs in the same context first.
const presetUrl = new URL('../app/static/js/fl-preset.js', import.meta.url);
const presetSource = fs.existsSync(presetUrl) ? fs.readFileSync(presetUrl, 'utf8') : null;

// Click-to-load and the toolbar light moved out of the main window: no reference may remain.
assert.doesNotMatch(source, /fl-connection|fl-launch-root|flLaunch\.load/, 'Removed FL UI hooks must not be referenced');

function runtime(api) {
  const events = new Map();
  const invokes = [];
  const context = {
    window: {
      VEXT: { api: (path, options) => path.endsWith('/connection') ? Promise.resolve({ connected: true, running: true, message: 'FL Studio running, controller disconnected' }) : api(path, options), escHtml: String, isTauri: () => true, toast() {}, navigate() {}, confirmDialog: async () => true },
      __TAURI__: { core: { invoke: async (...args) => { invokes.push(args); } } },
    },
    document: { getElementById() { return null; }, querySelectorAll() { return []; }, addEventListener(name, fn) { events.set(name, fn); } },
    setInterval() {}, setTimeout, Date, Map, Set, Math, Number, String, Promise, Error,
  };
  if (presetSource) vm.runInNewContext(presetSource, context);
  vm.runInNewContext(source, context);
  return { bridge: context.window.VEXT.flLaunch, events, invokes };
}

const pick = { key: 'Installed/Generators/VST3/Synth.fst', name: 'Synth', kind: 'Generators' };
const bridge = runtime(async () => ({})).bridge;
assert.equal(typeof bridge.mount, 'function');
assert.equal(typeof bridge.refreshConnection, 'function');
assert.equal(typeof bridge.disabledReason, 'function');
assert.equal(bridge.load, undefined, 'Click-to-load moved to the launcher and must not be on the main window');
console.log('FL launch UI: click-to-load is gone from the main window.');

if (!presetSource) {
  console.log('SKIP drag checks: app/static/js/fl-preset.js does not exist yet (another worker creates it).');
} else {
  const card = { dataset: { pluginId: '7' } };
  const target = { closest: selector => selector === '[data-plugin-id]:not(.row-menu-btn)' ? card : null };

  // A late preset answer after the button was released must not start OLE dragging.
  let resolvePresets;
  const late = runtime(() => new Promise(resolve => { resolvePresets = resolve; }));
  late.events.get('pointerdown')({ target, button: 0, clientX: 0, clientY: 0 });
  const movement = late.events.get('pointermove')({ target, buttons: 1, clientX: 20, clientY: 0 });
  late.events.get('pointerup')({});
  resolvePresets({ presets: [pick] });
  await movement;
  assert.equal(late.invokes.length, 0, 'A delayed resolver must not start OLE dragging after button release');

  const held = runtime(async () => ({ presets: [pick] }));
  held.events.get('pointerdown')({ target, button: 0, clientX: 0, clientY: 0 });
  await held.events.get('pointermove')({ target, buttons: 1, clientX: 20, clientY: 0 });
  assert.equal(held.invokes[0][0], 'fl_drag_preset');
  assert.equal(held.invokes[0][1].key, pick.key);

  // Movement under the 8 px threshold is a click, not a drag.
  const small = runtime(async () => ({ presets: [pick] }));
  small.events.get('pointerdown')({ target, button: 0, clientX: 0, clientY: 0 });
  await small.events.get('pointermove')({ target, buttons: 1, clientX: 3, clientY: 0 });
  assert.equal(small.invokes.length, 0, 'Moves under 8 px must not start a drag');

  // The real Playground card uses a button for its entire content. It must remain draggable.
  const playground = fs.readFileSync(new URL('../app/static/js/playground.js', import.meta.url), 'utf8');
  assert.match(playground, /button type="button" class="pg-plugin-pick"/);
  const pickButton = { className: 'pg-plugin-pick' };
  const playgroundTarget = { closest(selector) {
    if (selector === '[data-plugin-id]:not(.row-menu-btn)') return card;
    if (selector === 'button, a, input, select, textarea, [contenteditable]') return pickButton;
    return null;
  } };
  const playgroundDrag = runtime(async () => ({ presets: [pick] }));
  playgroundDrag.events.get('pointerdown')({ target: playgroundTarget, button: 0, clientX: 0, clientY: 0 });
  await playgroundDrag.events.get('pointermove')({ target: playgroundTarget, buttons: 1, clientX: 20, clientY: 0 });
  assert.equal(playgroundDrag.invokes.length, 1, 'The Playground pick button must allow native dragging');
  console.log('FL launch UI: drag timing and threshold checks passed.');
}
