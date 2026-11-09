// Run the page's seek handler without a browser or an audio device.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../../app/static/js/playground.js'), 'utf8');
const start = source.indexOf('  async function seekTo(t) {');
const end = source.indexOf('\n  // The playhead and the time', start);
assert(start >= 0 && end > start);

async function seek({ playing = false, worker = false, takesFile = false, player = true }, seconds) {
  const events = [];
  const context = vm.createContext({
    cursor: 0,
    recEl: player ? { currentTime: 2 } : null,
    cmpEl: { paused: false, currentTime: 2 },
    duration: () => 12,
    recElPlaying: () => playing,
    audioActive: () => worker,
    pluginTakesFile: () => takesFile,
    round3: t => Math.round(t * 1000) / 1000,
    command: async (route, body) => events.push(['worker', route, body.seconds]),
    reportPageTransport: (on, position) => events.push(['page', on, position]),
    renderDeck: () => {},
  });
  vm.runInContext(source.slice(start, end), context);
  await context.seekTo(seconds);
  return { context, events };
}

async function main() {
  // A paused page player (including Big Clock) must relocate without resuming.
  let result = await seek({}, 10);
  assert.equal(result.context.recEl.currentTime, 10);
  assert.deepEqual(result.events, [['page', false, 10]]);
  result = await seek({ player: false }, 10);
  assert.equal(result.context.cursor, 10);
  assert.deepEqual(result.events, [['page', false, 10]]);
  result = await seek({}, 20);
  assert.equal(result.context.recEl.currentTime, 11.999);
  assert.deepEqual(result.events, [['page', false, 11.999]]);

  // Playing page media supplies transport events itself; comparisons stay aligned.
  result = await seek({ playing: true }, 5);
  assert.equal(result.context.recEl.currentTime, 5);
  assert.equal(result.context.cmpEl.currentTime, 5);
  assert.deepEqual(result.events, []);

  // A processing plugin owns its clock, including while it holds a paused file.
  result = await seek({ worker: true, takesFile: true }, 5);
  assert.deepEqual(result.events, [['worker', '/seek', 5]]);
  result = await seek({ takesFile: true }, 5);
  assert.deepEqual(result.events, []);
  console.log('PASS: paused/idle page seeks, bounds, comparison alignment and worker clock ownership');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
