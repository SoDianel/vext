// Render the two setup panes against fake APIs; no browser or actual MIDI devices.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const escape = value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;').replaceAll("'", '&#39;');
const flSource = fs.readFileSync(new URL('../app/static/js/fl-launch.js', import.meta.url), 'utf8');
const audioSource = fs.readFileSync(new URL('../app/static/js/audio-settings.js', import.meta.url), 'utf8');
const name = 'Port <&"\'>';
const states = ['missing', 'ambiguous', 'available', 'unavailable'];
for (const state of states) {
  const elements = Object.fromEntries(['[data-fl-port]', '[data-fl-test]', '[data-fl-install]', '[data-fl-loopback]', '[data-fl-managed]', '[data-fl-message]'].map(s => [s, {disabled: false, value: name, textContent: ''}]));
  const panel = { innerHTML: '', querySelector: s => elements[s] };
  let mode = 'ok', putResolve;
  const messages = [];
  const V = {escHtml: escape, settings: async () => ({fl_studio_installed: true}), isTauri: () => false, toast: m => messages.push(m), navigate() {}, confirmDialog: async () => true,
    api: async (path, options) => {
      if (path === '/api/fl-launch/setup') return {port: name, outputs: [name], loopback: {state, message: `${state} <status>`, managed: {state: state === 'unavailable' ? 'unavailable' : 'active', owned: state !== 'unavailable', message: state === 'unavailable' ? 'Driver missing <external fallback>' : 'Owned <VEXT>'}}, installed: true, update_available: false, script_path: '<script>', result: {message: ''}};
      if (path === '/api/settings/fl_launch_port') { if (mode === 'savefail') throw Error('write failed'); return new Promise(resolve => { putResolve = resolve; }); }
      if (path === '/api/midi/loopback') { if (mode === 'fail') throw Error('offline'); return {port: 'Next', message: 'Next available'}; }
      return {};
    }};
  const doc = {getElementById: id => id === 'fl-controller-root' ? panel : null, addEventListener() {}};
  vm.runInNewContext(flSource, {window: {VEXT: V}, document: doc, setInterval() {}, setTimeout, Date, Map, Set, Math, Number, String, Promise, Error});
  await V.flLaunch.mount();
  assert.ok(panel.innerHTML.includes(escape(name))); assert.ok(!panel.innerHTML.includes(name));
  assert.ok(panel.innerHTML.includes(`${state} &lt;status&gt;`)); assert.ok(panel.innerHTML.includes(state === 'unavailable' ? 'Driver missing &lt;external fallback&gt;' : 'Owned &lt;VEXT&gt;')); assert.ok(panel.innerHTML.includes('&lt;script&gt;'));
  const select = elements['[data-fl-port]']; select.value = 'Next';
  const pending = select.onchange({target: select});
  assert.equal(select.disabled, true); assert.equal(elements['[data-fl-test]'].disabled, true); assert.equal(elements['[data-fl-install]'].disabled, true);
  putResolve(); await pending;
  assert.equal(elements['[data-fl-loopback]'].textContent, 'Next available'); assert.equal(select.value, 'Next'); assert.equal(select.disabled, false);
  mode = 'fail'; select.value = 'Other'; const failedRefresh = select.onchange({target: select}); putResolve(); await failedRefresh;
  assert.match(elements['[data-fl-loopback]'].textContent, /saved.*could not be refreshed/i);
  assert.match(messages.at(-1), /saved.*could not be refreshed/i);
  mode = 'savefail'; select.value = 'Rejected'; await select.onchange({target: select});
  assert.equal(select.value, 'Other'); assert.match(messages.at(-1), /could not save/i); assert.equal(select.disabled, false);
}

let registration, current = {port: name, state: 'missing', message: 'Missing <port>', input_matches: 0, output_matches: 0, fl_connection: {message: 'Closed'}};
const nodes = {'.aud': {}, '.aud-loopback': {innerHTML: ''}, '.aud-midi': {innerHTML: ''}, '.aud-midi-status': {innerHTML: ''}};
const listeners = {};
const root = {innerHTML: '', querySelector: s => nodes[s] || null, querySelectorAll: () => [], addEventListener: (event, fn) => {listeners[event] = fn;}, contains: () => true};
let navigated;
const V = {escHtml: escape, registerSection: (name, section) => {registration = section;}, settings: async () => ({}), toast() {}, navigate: (...args) => {navigated = args;}, api: async path => path === '/api/midi/loopback' ? current : {inputs: [], open: null}};
vm.runInNewContext(audioSource, {window: {VEXT: V}, Element: class {}, WeakSet, Number, String, JSON, Math, Promise, Error});
await registration.load(root, {part: 'midi'});
assert.match(root.innerHTML, /Virtual MIDI/); assert.ok(nodes['.aud-loopback'].innerHTML.includes(escape(name))); assert.ok(nodes['.aud-loopback'].innerHTML.includes('Missing &lt;port&gt;'));
current = {port: 'Updated', message: 'Available', input_matches: 1, output_matches: 1, fl_connection: {message: 'Connected'}};
const button = {dataset: {act: 'refresh-midi'}, disabled: false};
await listeners.click({target: {closest: () => button}});
assert.match(nodes['.aud-loopback'].innerHTML, /Updated: Available/);
button.dataset.act = 'fl-setup'; await listeners.click({target: {closest: () => button}});
assert.equal(navigated[0], 'config'); assert.equal(navigated[1].pane, 'daws');
let pendingLookups = [];
V.api = path => path === '/api/midi/loopback' ? new Promise(resolve => pendingLookups.push(resolve)) : Promise.resolve({inputs: [], open: null});
const first = {dataset: {act: 'refresh-midi'}, disabled: false}, second = {dataset: {act: 'refresh-midi'}, disabled: false};
const older = listeners.click({target: {closest: () => first}}), newer = listeners.click({target: {closest: () => second}});
pendingLookups[1]({port: 'Newest', message: 'Available'}); await newer;
pendingLookups[0]({port: 'Old', message: 'Missing'}); await older;
assert.match(nodes['.aud-loopback'].innerHTML, /Newest: Available/);
console.log('MIDI/FL setup rendered states, escaping, save/refresh, races, controls and navigation passed');
