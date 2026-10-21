// VEXT.audioBus: the one source of audio frames for the Analyzer section.
//
// Two things feed it. The backend's analysis stream (WS /api/analysis/stream: every sound VEXT
// plays through its own audio path, binary "VXAN" frames, see app/analysis_tap.py) and the page
// itself (the Playground's in-page player calls pushLocal while an <audio> element plays). The
// stream is only opened while somebody is subscribed, and it reconnects after a second when it
// drops. While local frames keep arriving the stream's frames are ignored: only one thing plays
// at a time, and the page knows best what it plays.
//
// Frame message (little-endian): "VXAN", u32 rate, u32 mode, u32 flags, u32 gen, u64 end_frame,
// u32 n, then float32 before[n][2] and float32 after[n][2]. flags: bit 0 = has a Before (an
// instrument has none: `before` is null), bit 1 = bypassed. mode: 0 idle, 1 file, 2 live (the
// browser through the plugin), 3 instrument, 4 browser (no plugin), 5 dry (the Audio tab's
// recording played back). Local frames carry mode 'page'.
//
// A subscriber gets fn({rate, mode, source, flags, gen, run, quiet, same, before, after, n});
// `before` / `after` are interleaved stereo Float32Arrays that are only valid during the call (the
// stream's are views into the message; copy what you keep). `run` names one continuous run of one
// producer ("stream:<plugin>:<mode>:<gen>:<rate>" or "local:page:<gen>:<rate>"): a change of it
// means another source, so the views start over. `quiet`: both signals are silent (the sources
// keep sending zeros for a moment after the sound stops); a quiet block is not "playing".
// `same`: the page's Before and After are the same samples (no plugin in between).
(function () {
  const V = window.VEXT;
  if (!V || V.audioBus) return;

  const LOCAL_WINS_MS = 300;       // local frames this recent make the stream's frames ignored
  const PLAYING_MS = 300;          // non-quiet frames this recent = playing
  const RECONNECT_MS = 1000;
  const WATCH_MS = 100;            // how often "playing" is re-evaluated while subscribed
  const MODE_SOURCE = { 1: 'file', 2: 'browser', 3: 'instrument', 4: 'browser', 5: 'dry' };
  const MAGIC = 0x4e415856;        // "VXAN" read as a little-endian u32
  const HEAD = 32;
  const SILENT = 1e-6;             // a sample under this (in magnitude) is silence

  // True when the first `len` values of a block are silence (a null block counts as silent).
  function quietOf(a, len) {
    if (!a) return true;
    for (let i = 0; i < len; i++) {
      const v = a[i];
      if (v >= SILENT || v <= -SILENT) return false;
    }
    return true;
  }

  // Whether two blocks hold the same samples.
  function sameBlock(a, b) {
    if (a === b) return true;
    if (a.length !== b.length) return false;
    for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
    return true;
  }

  const subs = new Set();
  const statusSubs = new Set();
  let ws = null;
  let retry = 0;
  let watch = 0;
  let lastLocalAt = -1e9;
  let lastFrameAt = -1e9;
  let localGen = 1;
  let localRate = 0;
  let server = { source: null, plugin: null, rate: 0, has_before: true, bypass: false };
  let lastKey = '';
  let frame0 = null;               // what the newest stream frame said: {source, has_before, bypass}

  const now = () => performance.now();
  const isPlaying = () => now() - lastFrameAt < PLAYING_MS;
  const localActive = () => now() - lastLocalAt < LOCAL_WINS_MS;

  function status() {
    const local = localActive();
    if (local) {
      return { source: 'page', plugin: null, rate: localRate, has_before: true, bypass: false, playing: isPlaying() };
    }
    // While frames flow, what they carry (mode, flags) is newer than the last status message.
    const live = isPlaying() && frame0;
    return {
      source: (live && frame0.source) || server.source || null, plugin: server.plugin || null, rate: server.rate || 0,
      has_before: live ? frame0.has_before : server.has_before !== false,
      bypass: live ? frame0.bypass : !!server.bypass, playing: isPlaying(),
    };
  }

  // Tells the status listeners when anything they could show has changed.
  function notifyStatus() {
    const s = status();
    const key = `${s.source}|${s.plugin}|${s.rate}|${s.has_before}|${s.bypass}|${s.playing}`;
    if (key === lastKey) return;
    lastKey = key;
    for (const fn of [...statusSubs]) {
      try { fn(s); } catch (e) { console.error('[audioBus] status listener failed:', e); }
    }
  }

  function emit(frame) {
    for (const fn of [...subs]) {
      try { fn(frame); } catch (e) { console.error('[audioBus] subscriber failed:', e); }
    }
  }

  // ---------- the backend stream ----------

  function onMessage(ev) {
    if (typeof ev.data === 'string') {
      let msg = null;
      try { msg = JSON.parse(ev.data); } catch { /* not ours */ }
      if (msg && msg.status && typeof msg.status === 'object') {
        server = { ...server, ...msg.status };
        notifyStatus();
      }
      return;
    }
    const buf = ev.data;
    if (!(buf instanceof ArrayBuffer) || buf.byteLength < HEAD) return;
    const head = new DataView(buf);
    if (head.getUint32(0, true) !== MAGIC) return;
    const n = head.getUint32(28, true);
    if (!n || buf.byteLength < HEAD + n * 16) return;
    if (localActive()) return;   // the page's own player wins
    const rate = head.getUint32(4, true);
    const mode = head.getUint32(8, true);
    const flags = head.getUint32(12, true);
    const gen = head.getUint32(16, true);
    const before = flags & 1 ? new Float32Array(buf, HEAD, n * 2) : null;
    const after = new Float32Array(buf, HEAD + n * 8, n * 2);
    const quiet = quietOf(before, n * 2) && quietOf(after, n * 2);
    const frame = {
      rate, mode, source: MODE_SOURCE[mode] || null, flags, gen, n, before, after, quiet,
      // The plugin is the ring's name (null for the browser's ring): two rings can both be at gen 1.
      run: `stream:${server.plugin || ''}:${mode}:${gen}:${rate}`, same: false,
    };
    if (!quiet) {
      lastFrameAt = now();
      frame0 = { source: frame.source, has_before: (flags & 1) !== 0, bypass: (flags & 2) !== 0 };
    }
    emit(frame);
    notifyStatus();
  }

  function connect() {
    if (ws || !subs.size) return;
    const base = String(V.API_BASE || location.origin).replace(/^http/i, 'ws');
    let sock;
    try {
      sock = new WebSocket(`${base}/api/analysis/stream?token=${encodeURIComponent(V.token ? V.token() : '')}`);
    } catch {
      retry = setTimeout(() => { retry = 0; connect(); }, RECONNECT_MS);
      return;
    }
    ws = sock;
    sock.binaryType = 'arraybuffer';
    sock.onmessage = onMessage;
    sock.onclose = () => {
      if (ws !== sock) return;
      ws = null;
      if (subs.size && !retry) retry = setTimeout(() => { retry = 0; connect(); }, RECONNECT_MS);
    };
    sock.onerror = () => { try { sock.close(); } catch { /* already gone */ } };
  }

  function disconnect() {
    clearTimeout(retry);
    retry = 0;
    if (ws) {
      const sock = ws;
      ws = null;
      sock.onclose = null;
      try { sock.close(); } catch { /* already gone */ }
    }
    server = { source: null, plugin: null, rate: 0, has_before: true, bypass: false };
    frame0 = null;
  }

  // ---------- the page's own frames ----------

  // The in-page player's frames: {rate, before, after} (interleaved stereo Float32Arrays, the same
  // length; `before` defaults to `after`). Only worth calling while wanted().
  function pushLocal(f) {
    if (!f || !f.after || !subs.size) return;
    const t = now();
    const rate = f.rate | 0;
    // A gap, or a new rate, is a new run for the views (the run key changes with the gen).
    if (t - lastLocalAt > LOCAL_WINS_MS || rate !== localRate) localGen++;
    localRate = rate;
    lastLocalAt = t;
    const after = f.after;
    const len = after.length;
    const before = f.before || after;
    const quiet = quietOf(after, len) && quietOf(before, len);
    if (!quiet) lastFrameAt = t;
    emit({
      rate, mode: 'page', source: 'page', flags: 1, gen: localGen, n: len >> 1,
      run: `local:page:${localGen}:${rate}`, quiet, same: sameBlock(before, after), before, after,
    });
    notifyStatus();
  }

  // ---------- the API ----------

  function subscribe(fn) {
    if (typeof fn !== 'function') return;
    const first = !subs.size;
    subs.add(fn);
    if (first) {
      connect();
      watch = setInterval(notifyStatus, WATCH_MS);
    }
  }

  function unsubscribe(fn) {
    if (!subs.delete(fn) || subs.size) return;
    clearInterval(watch);
    watch = 0;
    disconnect();
  }

  function onStatus(fn) {
    if (typeof fn !== 'function') return () => {};
    statusSubs.add(fn);
    return () => statusSubs.delete(fn);
  }

  V.audioBus = { subscribe, unsubscribe, pushLocal, status, onStatus, wanted: () => subs.size > 0 };
})();
