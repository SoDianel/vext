// Run with node. No backend, windows, settings or real plugin data are used.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const read = name => fs.readFileSync(new URL('../../app/static/' + name, import.meta.url), 'utf8');
const source = read('js/bubble-mode.js');
function runtime({ desktop = true, invoke = async () => {} } = {}) {
  const calls = [], errors = [];
  const button = { hidden: true, disabled: false, addEventListener(name, fn) { this[name] = fn; } };
  const window = { VEXT: { toast: (...args) => errors.push(args) } };
  if (desktop) window.__TAURI__ = { core: { invoke: async name => { calls.push(name); return invoke(); } } };
  vm.runInNewContext(source, { window, document: { getElementById: () => button } });
  return { button, calls, errors };
}

assert.equal(runtime({ desktop: false }).button.hidden, true);
const rt = runtime();
assert.equal(rt.button.hidden, false, 'Bubble mode works without querying FL or backend setup');
await rt.button.click();
assert.deepEqual(rt.calls, ['bubble_enter']);
assert.equal(rt.button.disabled, false);

let reject;
const pending = runtime({ invoke: () => new Promise((_, no) => { reject = no; }) });
const entering = pending.button.click();
assert.equal(pending.button.disabled, true);
await pending.button.click();
assert.equal(pending.calls.length, 1, 'Ignore duplicate entry while the window transition is pending');
reject(new Error('Window unavailable'));
await entering;
assert.equal(pending.button.disabled, false);
assert.match(pending.errors[0][0], /Window unavailable/);

const html = read('index.html');
assert.match(html, /<header class="toolbar">[\s\S]*id="bubble-enter"/);
assert.doesNotMatch(html, /fl-bubble-switch|fl-bubble-reason/);
assert.doesNotMatch(read('js/fl-launch.js'), /syncBubble|bubble_set|fl_bubble_enabled/);
const bubble = read('bubble.html');
assert.match(bubble, /<header class="fln-bar">[\s\S]*id="bubble-restore"[\s\S]*<\/header>/);
console.log('Bubble mode: global entry, browser fallback, duplicate clicks and failed transition recovery passed.');
