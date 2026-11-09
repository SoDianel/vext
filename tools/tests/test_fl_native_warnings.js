// Exercise the real status renderer without a browser or native plugin.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../../app/static/js/playground.js'), 'utf8');
const start = source.indexOf('  function nativeLimitsHtml(h) {');
const end = source.indexOf('\n  // variant', start);
assert(start >= 0 && end > start);
const context = vm.createContext({
  esc: value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;'),
  els: { status: {} }, phase: () => 'playing', closedError: '', selectedId: 1,
  notice: '', frameError: '', source: 'file', file: {}, playBlocker: () => '',
  hostNote: () => '', overloadHtml: () => '',
  setHtml: (_key, element, html) => { element.html = html; },
  host: { state: 'open', playing: true, native_limitations: [
    { status: 'out_of_scope', message: 'No mixer destinations <script>unsafe()</script>' },
    { status: 'pending', message: 'Playback needs validation.' },
  ] },
});
vm.runInContext(source.slice(start, end), context);
context.renderStatus();
assert.equal(context.els.status.hidden, false);
assert.match(context.els.status.html, /Outside VEXT beta scope/);
assert.match(context.els.status.html, /Pending integration/);
assert.match(context.els.status.html, /&lt;script&gt;/);
assert.doesNotMatch(context.els.status.html, /<script>/);
// A transient error may add a message but must not hide compatibility limits.
context.host.error = 'error';
context.audible = () => false;
context.renderStatus();
assert.match(context.els.status.html, /Plugin limitations/);
context.host = { state: 'closed', native_limitations: context.host.native_limitations };
context.renderStatus();
assert.equal(context.els.status.hidden, true);
context.host = { state: 'open' };
context.renderStatus();
assert.equal(context.els.status.hidden, true);
context.host = { state: 'open', native_audition: true, native_limitations: [] };
context.renderStatus();
assert.match(context.els.status.html, /Monitor on/);
assert.match(context.els.status.html, /aria-pressed="false"/);
context.host.source_kind = 'editor';
context.renderStatus();
assert.match(context.els.status.html, /Monitor off/);
assert.match(context.els.status.html, /aria-pressed="true"/);
console.log('PASS: native warnings survive playback/errors, escape markup, and clear on close');
