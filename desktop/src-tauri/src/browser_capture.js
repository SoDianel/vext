// Injected into every page (all frames) of the Playground's browser webview.
// Takes the audio of any <video>/<audio> that starts playing and, while VEXT
// routes it (a plugin is open), streams it to the backend instead of the
// speakers, so it is only heard after the plugin has processed it.
// The backend also asks for the stream when it wants to see the sound (the
// Analyzer, a recording of the browser: "tap"): the page then sends its frames
// and stays audible, as it plays them itself.
// Media with DRM (mediaKeys set) is left alone: the browser would only give
// silence for it, so it keeps playing normally instead.
//
// The backend says its sample rate on connect ({"route": bool, "rate": n,
// "tap": bool}, and again whenever route or tap changes). The audio context is created at that rate, so
// nothing needs converting; media that starts playing before the rate is known
// is taken over once it arrives. A media element stays bound to the context it
// was first connected to, so if the rate changes later the context can't be
// replaced: the tap then resamples to the new rate instead.
//
// The capture runs in blocks of CHUNK frames (a ScriptProcessorNode, on the
// page's main thread). A page too busy to run a callback loses that block: the
// sound has a gap. The script counts the lost blocks from the callbacks' own
// clock (playbackTime) and tells the backend ({"gaps": n}, the total so far, at
// most once a second while it grows), so the Log can say what happened.
//
// The player: the media element that played last is this frame's player. Its
// state goes to the backend ({"media": {playing, t, d, rate, loop, title}}, on
// every play / pause / seek and 4 times a second while it plays), which shows
// it as the Analyzer's timeline and plays / pauses / seeks / loops it from
// there ({"media": {"action": "play"|"pause"|"toggle"|"seek", "t": s}},
// {"loop": [a, b] | "all" | null}). A loop is kept by this script: past its
// end the player goes back to its start.
(() => {
  if (window.__vextCapture) return;
  window.__vextCapture = true;
  const AUDIO_URL = '__VEXT_AUDIO_URL__';
  // Frames per capture block. Must match host_worker.BROWSER_CHUNK (the worker
  // sizes its buffers from it).
  const CHUNK = 2048;
  let ctx = null, dry = null, tap = null, ws = null, routed = false, tapped = false;
  let rate = 0;                 // the backend's rate; 0 = not known yet
  let gaps = 0, reportedGaps = 0, reportedAt = 0;  // lost blocks; the count last sent
  let lastAt = -1;              // playbackTime of the last live block; -1 = none yet
  let player = null;            // the media element that played last (the timeline's player)
  let loop = null;              // [a, b] (seconds) or 'all': kept by checkLoop()
  let mediaTimer = 0, loopTimer = 0;
  const hooked = new WeakSet();
  const waiting = new Set();    // media that played before the rate was known
  // Resampler state (only used when the context's rate isn't the backend's).
  let pos = 0, lastL = 0, lastR = 0;

  function connect() {
    if (ws) return;
    ws = new WebSocket(AUDIO_URL);
    ws.binaryType = 'arraybuffer';
    reportedGaps = 0;  // the backend counts per connection: send the total again
    ws.onopen = () => sendMedia();
    ws.onmessage = (e) => {
      let m;
      try { m = JSON.parse(e.data); } catch { return; /* not for us */ }
      if (typeof m.rate === 'number' && m.rate > 0 && m.rate !== rate) {
        rate = m.rate;
        pos = 0;
        for (const el of waiting) hook(el);
        waiting.clear();
      }
      if (typeof m.tap === 'boolean') tapped = m.tap;
      if (typeof m.route === 'boolean') setRoute(m.route);
      // The backend asks for a pause (a browser recording ended and the Audio tab opened).
      if (m.pause === true) {
        for (const el of document.querySelectorAll('video, audio')) {
          try { el.pause(); } catch { /* not a playable element */ }
        }
        for (const el of waiting) {
          try { el.pause(); } catch { /* gone */ }
        }
      }
      if (m.media && typeof m.media === 'object') control(m.media);
      if ('loop' in m) setLoop(m.loop);
    };
    ws.onclose = () => {
      ws = null;
      tapped = false;
      setRoute(false);
      setTimeout(() => { if (ctx || waiting.size || player) connect(); }, 2000);
    };
  }

  function setRoute(on) {
    routed = on;
    if (dry) dry.gain.setTargetAtTime(on ? 0 : 1, ctx.currentTime, 0.01);
  }

  // Interleaved stereo at the backend's rate (linear interpolation when the
  // context runs at another rate; the position carries over between blocks).
  function frames(l, r) {
    const n = l.length;
    if (ctx.sampleRate === rate) {
      const out = new Float32Array(n * 2);
      for (let i = 0; i < n; i++) { out[2 * i] = l[i]; out[2 * i + 1] = r[i]; }
      return out;
    }
    const step = ctx.sampleRate / rate;
    const out = new Float32Array(Math.ceil((n - pos) / step) * 2 + 2);
    let t = pos, k = 0;
    while (t <= n - 1) {
      const i = Math.floor(t), f = t - i;
      const l0 = i < 0 ? lastL : l[i], r0 = i < 0 ? lastR : r[i];
      const l1 = f ? l[i + 1] : l0, r1 = f ? r[i + 1] : r0;
      out[k++] = l0 + (l1 - l0) * f;
      out[k++] = r0 + (r1 - r0) * f;
      t += step;
    }
    pos = t - n;
    lastL = l[n - 1];
    lastR = r[n - 1];
    return out.subarray(0, k);
  }

  function ensureContext() {
    if (ctx) return ctx;
    // 'playback': a larger output buffer than 'interactive', so a busy page
    // (or a busy plugin's CPU) costs less sound.
    ctx = new AudioContext({ sampleRate: rate, latencyHint: 'playback' });
    dry = ctx.createGain();
    dry.gain.value = routed ? 0 : 1;
    dry.connect(ctx.destination);
    // ScriptProcessor rather than an AudioWorklet: a worklet module would have
    // to be loaded from a URL, which a page's CSP / Trusted Types can refuse.
    tap = ctx.createScriptProcessor(CHUNK, 2, 2);
    tap.onaudioprocess = (e) => {
      const live = routed || tapped;
      const at = e.playbackTime, dur = CHUNK / ctx.sampleRate;
      // A block that came later than the previous one plus its own length was lost.
      if (live && lastAt >= 0 && at - lastAt - dur > dur / 2) {
        gaps += Math.round((at - lastAt - dur) / dur);
      }
      lastAt = live ? at : -1;
      if (!live) return;
      if (gaps !== reportedGaps && Date.now() - reportedAt >= 1000 && ws && ws.readyState === 1) {
        reportedGaps = gaps;
        reportedAt = Date.now();
        ws.send(JSON.stringify({ gaps }));
      }
      if (!ws || ws.readyState !== 1) return;
      const out = frames(e.inputBuffer.getChannelData(0), e.inputBuffer.getChannelData(1));
      if (out.length) ws.send(out); // a view: only its own bytes go out
    };
    const mute = ctx.createGain();
    mute.gain.value = 0;  // the tap only runs while connected to the output
    tap.connect(mute);
    mute.connect(ctx.destination);
    return ctx;
  }

  function hook(el) {
    if (hooked.has(el) || el.mediaKeys) return;
    if (!rate) {
      // Plays normally until the backend has said its rate.
      waiting.add(el);
      connect();
      return;
    }
    hooked.add(el);
    const c = ensureContext();
    try {
      const src = c.createMediaElementSource(el);
      src.connect(dry);
      src.connect(tap);
    } catch { /* already owned by the page's own audio graph */ }
    if (c.state === 'suspended') c.resume();
  }

  // ---- the player ----

  function mediaState() {
    const el = player;
    if (!el) return null;
    const d = isFinite(el.duration) ? el.duration : 0;
    return { playing: !el.paused && !el.ended, t: el.currentTime || 0, d, rate: el.playbackRate || 1,
      loop, title: document.title || '' };
  }

  function sendMedia() {
    const m = mediaState();
    if (m && ws && ws.readyState === 1) ws.send(JSON.stringify({ media: m }));
  }

  function control(c) {
    const el = player;
    if (!el) return;
    const play = () => { const p = el.play(); if (p && p.catch) p.catch(() => { /* refused: stays paused */ }); };
    if (c.action === 'play') play();
    else if (c.action === 'pause') el.pause();
    else if (c.action === 'toggle') { if (el.paused || el.ended) play(); else el.pause(); }
    else if (c.action === 'seek' && typeof c.t === 'number' && isFinite(c.t)) el.currentTime = Math.max(0, c.t);
    sendMedia();
  }

  // Past the loop's end (or, for 'all', the media's end, before the site moves on to something else),
  // back to its start. Checked every 20 ms while a loop is set.
  function checkLoop() {
    const el = player;
    if (!el || !loop || el.paused) return;
    const d = isFinite(el.duration) ? el.duration : 0;
    const [a, b] = loop === 'all' ? [0, d - 0.12] : loop;
    if (b > a && el.currentTime >= b) {
      el.currentTime = a;
      sendMedia();
    }
  }

  function setLoop(value) {
    loop = value === 'all' || (Array.isArray(value) && value.length === 2 && value[1] > value[0]) ? value : null;
    clearInterval(loopTimer);
    loopTimer = loop ? setInterval(checkLoop, 20) : 0;
    sendMedia();
  }

  function onMediaEvent(e) {
    const el = e.target;
    if (!(el instanceof HTMLMediaElement)) return;
    if (e.type === 'play' || !player) player = el;
    if (el !== player) return;
    if (e.type === 'ended' && loop) {  // looping: the end goes back to the start (and plays on)
      el.currentTime = loop === 'all' ? 0 : loop[0];
      const p = el.play();
      if (p && p.catch) p.catch(() => { /* refused */ });
    }
    if (!mediaTimer) mediaTimer = setInterval(() => { if (player && !player.paused) sendMedia(); }, 250);
    if (!ws) connect();
    sendMedia();
  }

  document.addEventListener('play', (e) => {
    if (e.target instanceof HTMLMediaElement) hook(e.target);
  }, true);
  for (const type of ['play', 'pause', 'seeked', 'ended', 'durationchange', 'ratechange', 'emptied']) {
    document.addEventListener(type, onMediaEvent, true);
  }
})();
