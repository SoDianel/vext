// Playground: try a plugin on real sound. Two tabs at the top of the same card,
// each the sound source while it's shown (source 'browser' | 'file'):
//  - Browser (the default): a full web browser inside VEXT. It is a native
//    webview that the desktop shell lays over the rectangle this page reserves
//    for it (browser_place). Whatever plays on the page is captured by the
//    shell and streamed to the backend, which plays it through the open plugin.
//    The webview is hidden while the Audio tab is shown (a native view covers HTML).
//  - Audio: one audio file (any format FFmpeg reads; system dialog, drag and
//    drop, or a typed path), decoded by the backend (app/playground_audio.py)
//    and played through the plugin with the transport below (play/pause, stop,
//    loop the selection). Two waveforms of equal height on one time axis
//    (js/playground-wave.js): the file on top, with Fade in / Fade out /
//    Normalize / Undo (non-destructive: the worker applies them as a gain
//    envelope); below it the recording of what comes out of the plugin (Record),
//    which plays back without the plugin, saves as a WAV, or clears.
//    The Browser tab can also record the browser's sound into the Audio tab (the
//    Record button in its deck row, backend app/browser_record.py).
//  The analyzers live in their own section (Analyzer, js/analyzer.js). This module
//  feeds it what plays in the page (the in-page tap below) and exports V.playground
//  (hasFile / isPlaying / toggle / stop / describe) so the Analyzer can play and
//  stop the Audio tab's file; going to the Analyzer keeps everything playing.
//
// The plugin runs out of process (app/host_worker.py). Its editor shows in
// VEXT's plugin frame, a small window owned by the main one (desktop/src-tauri,
// plugin-frame.html): this page opens the plugin on the backend, waits until
// the editor window exists (editor_hwnd in the host state), then hands it to the
// shell (plugin_frame_open). One plugin at a time: the backend closes the open
// one when another opens, and the frame's own close button closes the plugin.
//
// A native view covers any HTML drawn where it sits, so the browser is hidden
// whenever this section isn't shown and while an overlay covers it (a modal, a
// confirmation, the context menu where it sits over the browser: app.js announces
// them as 'vext:overlay'). The
// page itself never scrolls, so the browser never has to follow a scroll.
//
// In a plain browser tab (no desktop shell) there is no web browser and no
// frame: the plugin list still works, a plugin opens in its own window, and a
// file can be played by typing its path.
//
// Classic script in an IIFE; talks to the rest of the app only via window.VEXT.
(function () {
  const V = window.VEXT;
  if (!V || typeof V.registerSection !== 'function') return;

  const POLL_BUSY_MS = 250;        // plugin loading, its window not there yet, or a file playing
  const POLL_OPENING_MS = 60;      // opening: the editor goes into the frame the moment it exists (cheap local GET)
  const POLL_IDLE_MS = 1500;       // plugin open, nothing else happening: notices it being closed
  const EDITOR_WAIT_MS = 20000;    // an open plugin without editor_hwnd after this: its window stays on its own
  const PLACE_CHECK_MS = 1000;
  const MEDIA_ASK_MS = 200;        // the browser's player is asked at most this often (askMedia)     // safety net for layout changes no observer reports (position only)
  const SEARCH_URL = 'https://duckduckgo.com/?q=';
  const LAYOUT_KEY = 'pa-pg-layout';
  const TAB_KEY = 'pa-pg-tab';
  // The file dialog's filter (GET /api/playground/decoding has the backend's list; FFmpeg reads more).
  let audioExtensions = ['wav', 'mp3', 'flac', 'ogg', 'oga', 'opus', 'm4a', 'aac', 'aif', 'aiff', 'wma', 'wv', 'ape',
    'caf', 'w64', 'mka', 'webm', 'ac3', 'mp2', 'mp4', 'mov', 'mkv'];
  const AUDIO_POLL_MS = 300;       // the Audio tab: decoding progress, the recording growing
  const FADE_LENGTHS = [0.1, 0.5, 1, 2, 5, 10];
  const FADE_KEY = 'pa-pg-fade';
  const OLD_ANALYZER_KEY = 'pa-pg-analyzer';   // the Audio tab's own analyzers (removed): its saved settings are dropped once
  const BREC_HINT_KEY = 'pa-pg-browser-rec-hint';   // 'off' once "Don't show again" was ticked and confirmed
  const BREC_POLL_MS = 300;        // a browser recording runs: its time and level
  const TAP_WORKLET = '/static/js/analyzer-tap-worklet.js';
  const TAP_SYNC_MS = 250;         // how often the page notices the Analyzer started / stopped listening
  const TAP_IDLE_MS = 1000;        // nothing plays in the page this long: the page's AudioContext suspends
  const HOLD_WATCH_MS = 500;       // gone to the Analyzer: how often the page checks the next section is not another one
  const HOST_FRESH_MS = 1500;      // away from the section and without the hold, the host's last report counts this long
  const KICK_MS = 1000;            // the Analyzer's questions ask the host again at most this often
  const QUIET_HOLD_MS = 2000;      // the hold ends once nothing has played this long (when the section is not known)
  const SOUND_HOLD_MS = 4000;      // the page counts as playing for this long after sound last arrived
  const SOUND_PEAK = 0.0005;       // input peak above this is sound, below is silence
  // Host messages the page already says in its own words. A note after "Playing …."
  // (the device or the sample rate had to fall back) is still shown.
  const QUIET_MESSAGES = /^(Paused|Finished|Loading the plugin…?)$/;
  const BROWSER_PLAYING = 'Playing the browser\'s audio through the plugin.';
  const CANT_OPEN = 'Can\'t open this plugin: ';
  const CHECK_POLL_MS = 2000;
  const POLL_MIDI_MS = 300;        // a plugin that takes MIDI is open: its MIDI light and level

  const svg = (body, size = 16) => `<svg viewBox="0 0 16 16" width="${size}" height="${size}" aria-hidden="true" fill="none" ` +
    `stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">${body}</svg>`;
  const ICON = {
    back: svg('<path d="M10 3.5L5.5 8l4.5 4.5"/>'),
    forward: svg('<path d="M6 3.5L10.5 8 6 12.5"/>'),
    reload: svg('<path d="M12.6 8.2a4.6 4.6 0 1 1-1.4-3.5"/><path d="M12.4 2.6v2.6H9.8"/>'),
    stopLoad: svg('<path d="M4.5 4.5l7 7M11.5 4.5l-7 7"/>'),
    home: svg('<path d="M2.6 7.6L8 3.2l5.4 4.4"/><path d="M4.2 6.4v6.3h7.6V6.4"/>'),
    side: svg('<rect x="1.8" y="2.8" width="12.4" height="10.4" rx="1.6"/><path d="M10.1 2.8v10.4"/>'),
    stacked: svg('<rect x="1.8" y="2.8" width="12.4" height="10.4" rx="1.6"/><path d="M1.8 9.1h12.4"/>'),
    close: svg('<path d="M4.5 4.5l7 7M11.5 4.5l-7 7"/>', 12),
    play: '<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><path fill="currentColor" d="M5 2.9v10.2a.6.6 0 0 0 .92.5l8-5.1a.6.6 0 0 0 0-1l-8-5.1A.6.6 0 0 0 5 2.9z"/></svg>',
    pause: '<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><rect fill="currentColor" x="3.5" y="2.5" width="3.2" height="11" rx="0.8"/><rect fill="currentColor" x="9.3" y="2.5" width="3.2" height="11" rx="0.8"/></svg>',
    stop: '<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true"><rect fill="currentColor" x="3" y="3" width="10" height="10" rx="1.6"/></svg>',
    globe: svg('<circle cx="8" cy="8" r="5.8"/><path d="M2.4 8h11.2M8 2.2c1.7 1.6 2.5 3.6 2.5 5.8S9.7 12.2 8 13.8C6.3 12.2 5.5 10.2 5.5 8S6.3 3.8 8 2.2z"/>', 14),
    wave: svg('<path d="M1.8 8h1.4M4.6 5.6v4.8M7 3.2v9.6M9.4 5v6M11.8 6.6v2.8M14.2 8h-.8"/>', 14),
    signal: svg('<path d="M1.6 8c1.1-4.4 2.1-4.4 3.2 0s2.1 4.4 3.2 0 2.1-4.4 3.2 0 2.1 4.4 3.2 0"/>', 14),
    open: svg('<path d="M2.2 4.6v7.6c0 .6.4 1 1 1h9.6c.6 0 1-.4 1-1V6.4c0-.6-.4-1-1-1H8L6.6 3.6H3.2c-.6 0-1 .4-1 1z"/>', 14),
    fadeIn: svg('<path d="M2.2 12.8L13.8 3.2v9.6z"/>'),
    fadeOut: svg('<path d="M2.2 3.2l11.6 9.6H2.2z"/>'),
    normalize: svg('<path d="M2 8h1.6M4.6 4.6v6.8M7 2.6v10.8M9.4 4.6v6.8M11.8 6v4M14 2.4v11.2" /><path d="M13 2.4h2M13 13.6h2"/>'),
    undo: svg('<path d="M5.4 3.6L2.6 6.4l2.8 2.8"/><path d="M2.8 6.4h6.6a3.6 3.6 0 0 1 0 7.2H6.6"/>'),
    zoomIn: svg('<circle cx="7" cy="7" r="4.4"/><path d="M10.3 10.3l3.3 3.3M5 7h4M7 5v4"/>'),
    zoomOut: svg('<circle cx="7" cy="7" r="4.4"/><path d="M10.3 10.3l3.3 3.3M5 7h4"/>'),
    zoomFit: svg('<path d="M2.4 5.4V2.4h3M10.6 2.4h3v3M13.6 10.6v3h-3M5.4 13.6h-3v-3"/><path d="M4.6 8h6.8"/>'),
    loop: svg('<path d="M3 7.2V6.6a2.2 2.2 0 0 1 2.2-2.2h7.4"/><path d="M10.8 2.6l1.8 1.8-1.8 1.8"/><path d="M13 8.8v.6a2.2 2.2 0 0 1-2.2 2.2H3.4"/><path d="M5.2 13.4l-1.8-1.8 1.8-1.8"/>', 15),
    record: '<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true"><circle fill="currentColor" cx="8" cy="8" r="5"/></svg>',
    save: svg('<path d="M8 2.4v7.4M5.2 7.2L8 10l2.8-2.8"/><path d="M2.8 10.6v1.6c0 .7.5 1.2 1.2 1.2h8c.7 0 1.2-.5 1.2-1.2v-1.6"/>', 14),
    capture: svg('<path d="M2.4 5.6c0-.6.4-1 1-1h1.9l1-1.6h3.4l1 1.6h1.9c.6 0 1 .4 1 1v6.4c0 .6-.4 1-1 1H3.4c-.6 0-1-.4-1-1z"/><circle cx="8" cy="8.6" r="2.3"/>', 14),
    trash: svg('<path d="M3 4.6h10M6.4 4.6V3.2h3.2v1.4M4.4 4.6l.6 8.2c0 .5.4.8.9.8h4.2c.5 0 .9-.3.9-.8l.6-8.2"/>', 14),
  };

  const esc = (v) => V.escHtml(v);
  const num = (v) => (typeof v === 'number' && isFinite(v) ? v : null);
  const errText = (err) => (err && err.message) || String(err);
  const round3 = (t) => Math.round(t * 1000) / 1000;

  // 3:07, or 1:02:07 past an hour. Unknown reads as a dash, never "NaN".
  function fmtTime(s) {
    if (s == null || !isFinite(s) || s < 0) return '–:––';
    s = Math.floor(s);
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = String(s % 60).padStart(2, '0');
    return h ? `${h}:${String(m).padStart(2, '0')}:${sec}` : `${m}:${sec}`;
  }

  // 1:02.345: the Audio tab's playhead and selection, to the millisecond.
  function fmtTimeMs(s) {
    if (s == null || !isFinite(s) || s < 0) return '–:––.–––';
    const ms = Math.round(s * 1000);
    return `${fmtTime(Math.floor(ms / 1000))}.${String(ms % 1000).padStart(3, '0')}`;
  }

  // ---------- the desktop shell (Tauri) ----------

  const tauri = () => window.__TAURI__ || null;
  const hasShell = () => {
    const t = tauri();
    return !!(t && t.core && typeof t.core.invoke === 'function');
  };
  const invoke = (cmd, args) => tauri().core.invoke(cmd, args);
  function onShellEvent(name, fn) {
    const t = tauri();
    if (!t || !t.event || typeof t.event.listen !== 'function') return;
    Promise.resolve()
      .then(() => t.event.listen(name, (e) => {
        try { fn(e && e.payload); } catch (err) { console.error(`[playground] ${name}:`, err); }
      }))
      .catch(() => { /* not available: the page works without it */ });
  }

  // ---------- state ----------
  // Kept for the whole app session: leaving the section and coming back finds
  // the same page, plugin and file.

  let root = null;
  let els = null;
  let active = false;              // the section is on screen
  let keepOn = false;              // gone to the Analyzer: what plays goes on playing (and being polled), see unload()
  let holdTimer = null;
  let quietAt = 0;                 // the hold: since when nothing has played here (0: something does)
  let kickAt = 0;                  // performance.now() when the host was last asked a question of its own

  let layout = (() => {
    try { return localStorage.getItem(LAYOUT_KEY) === 'stacked' ? 'stacked' : 'side'; } catch { return 'side'; }
  })();
  // The tab shown = what plays through the plugin: 'browser' | 'file' (the Audio tab, or the Signal tab:
  // a signal is rendered to a file and played like one, js/playground-signal.js).
  const storedTab = (() => { try { return localStorage.getItem(TAB_KEY) || ''; } catch { return ''; } })();
  let source = !hasShell() || storedTab === 'file' || storedTab === 'signal' ? 'file' : 'browser';
  let fileView = storedTab === 'signal' ? 'signal' : 'audio';   // which of the file's tabs shows
  let signal = null;               // the Signal tab's designer (V.PlaygroundSignal), made when first shown
  let resumeAt = 0;                // a new file that plays when decoded starts here (a signal rendered again while playing)
  let pendingLoop = null;          // a loop mode asked for before the new file was decoded

  // The web browser.
  const nav = { url: '', title: '', can_go_back: false, can_go_forward: false, loading: false };
  let addressDirty = false;        // the address field holds what's being typed: state updates leave it alone
  let placeRaf = 0;
  let placeTimer = null;
  let placeBusy = false;
  let placeWanted = null;          // the newest rectangle not sent yet
  let placeSent = '';              // the last one the shell accepted
  let lastRect = null;             // last visible rectangle (a hidden browser keeps it)
  let showAsked = false;           // a show has been sent: from then on every hide must be sent too
  let browserShown = false;        // the browser has been on screen at least once
  let browserError = '';

  // Plugins.
  let plugins = [];
  let pluginsState = 'idle';       // idle | loading | ready | error
  let pluginsError = '';
  let pluginsSeq = 0;
  let unavailable = [];            // GET /api/playground/unavailable: the others, each with host_issue
  let unavailOpen = false;         // the "Can't open here" group is expanded
  let check = null;                // GET /api/playground/check while (or after) a background check runs
  let checkTimer = null;
  let filterText = '';
  const imgStamp = {};             // plugin id -> cache-buster after a new picture

  let selectedId = null;
  let host = null;                 // last host state of selectedId (see normHost)
  let loadKey = '';                // the last audio report seen (noteLoad)
  let overloads = 0;               // reports in a row with the plugin's CPU load above 100 %
  let hostAt = 0;                  // performance.now() when `host` arrived
  let hostLag = 0;                 // half the round trip of the last poll, in seconds
  let epoch = 0;                   // bumped per command: answers to older requests are stale
  let pollTimer = null;
  let pollToken = 0;
  let openedAt = 0;                // performance.now() when the backend accepted the open
  let framed = null;               // { id, hwnd }: the editor handed to the plugin frame
  let routed = null;               // plugin id told to take the browser's sound
  let soundAt = 0;                 // performance.now() when sound from the page last arrived
  let notice = '';                 // why the last request failed, or why Play can't start
  let closedError = '';            // the error the plugin closed with (failed to load, crashed)
  let closedRetry = true;          // trying again may help (not a format, 32-bit, licensing… issue)
  let relisted = null;             // plugin id the lists were reloaded for once it opened
  let frameError = '';             // why the editor couldn't go into VEXT's frame (kept until the next plugin)

  // File playback (the Audio tab).
  let file = null;                 // { path, name, duration } once decoded (from `audio`)
  let audio = null;                // GET /api/playground/audio: the file, its edits, the recording
  let audioTimer = null;
  let audioSeq = 0;
  let audioWanted = false;         // play the file as soon as it's decoded (it was just picked)
  let wv = null;                   // the waveforms (VEXT.WaveView)
  let sel = null;                  // the selection {a, b} (seconds)
  let loopOn = false;              // Loop: the selection (or the whole file) repeats
  let loopAll = false;             // Loop All: the whole file repeats, even with a selection (loopOn is on too)
  let fadeLen = (() => { try { const v = Number(localStorage.getItem(FADE_KEY)); return FADE_LENGTHS.includes(v) ? v : 1; } catch { return 1; } })();
  let recEl = null;                // <audio> playing in the page when no plugin is open
  let recElWhich = null;           // what recEl plays: 'source' (the file, edits applied) or 'recording'
  // Compare: with a recording (and not recording), the transport plays both tracks in the page,
  // in sync; M on each pane mutes it (hear the original, the recording, or both).
  let cmpEl = null;                // <audio> playing the recording alongside recEl while comparing
  let muteSrc = false;
  let muteRec = false;
  let editBusy = false;
  let cursor = 0;                  // where Play starts while nothing plays
  let playFrom = 0;                // the start point of this play: Play/Space stops back there
  let endStopping = false;         // the selection's end was reached: its stop is on the way
  let pageHold = null;             // the page's players were paused: {which, compare}; Play goes on from `cursor`
  let loopAuto = false;            // Loop was switched on by making a selection (clearing it switches it off again)
  let autoEditsPending = false;    // a new file or browser recording: the automatic edits go on once it's decoded
  let wantPlay = false;            // keep listening when the plugin changes
  let playsInFlight = 0;
  let ending = false;              // we are stopping or closing it ourselves
  let tickRaf = 0;                 // the playhead follows the sound (requestAnimationFrame)
  let spaceSeq = null;             // the last Space press from the plugin's editor acted on (null: none seen yet)

  // The browser's player (the media element that played last in a page: GET /api/browser/media), for the
  // Analyzer's timeline and Play / Pause. Asked for on demand (askMedia), at most every MEDIA_ASK_MS.
  let media = null;                // {has, playing, t, d, loop, title}
  let mediaAt = 0;                 // performance.now() when it arrived
  let mediaAskAt = 0;
  let bsel = null;                 // the timeline's selection on the browser's player {a, b} (seconds)
  let bLoop = 'off';               // its loop: 'off' | 'sel' | 'all'
  let bLoopAuto = false;           // switched on by making a selection (clearing it switches it off again)

  // Recording the browser into the Audio tab (the Browser tab's Record button).
  const brec = { on: false, busy: false, stopping: false, seconds: 0, peak: 0, through: null, timer: null };

  // The in-page tap for the Analyzer: recEl and cmpEl go through one AudioContext (their M mutes are
  // its gains) and an AudioWorklet posts what plays to VEXT.audioBus while the Analyzer listens.
  const tap = {
    ctx: null, pending: null, node: null, sync: null,
    srcNode: null, cmpNode: null,    // the elements' source nodes (an element can be routed only once)
    gSrc: null, gRec: null,          // the M mutes: the file / the recording
    afterSrc: null, afterCmp: null,  // what the tap hears as "after": the file alone, or the recording while comparing
    idle: null,                      // the timer that suspends the context once nothing plays in the page
    on: false,                       // the worklet is told to post blocks
  };

  const last = {};                 // innerHTML last written per region, to skip identical writes

  // ---------- host state ----------

  function normHost(st) {
    const s = st && typeof st === 'object' ? st : {};
    return {
      state: s.state === 'open' || s.state === 'starting' ? s.state : 'closed',
      is_instrument: !!s.is_instrument,
      playing: !!s.playing, paused: !!s.paused,
      position_s: num(s.position_s) || 0, duration_s: num(s.duration_s),
      source: s.source || '', message: s.message || '', error: s.error || '',
      // Loading waits for the user to answer a window the plugin opened (license, registration).
      dialog: !!s.dialog,
      // Why it couldn't open ({code, reason}), once loading failed.
      issue: s.host_issue && typeof s.host_issue === 'object' ? s.host_issue : null,
      editor_hwnd: num(s.editor_hwnd) || null,
      editor_width: num(s.editor_width), editor_height: num(s.editor_height),
      source_kind: ['browser', 'file', 'instrument'].includes(s.source_kind) ? s.source_kind : null,
      // The Audio tab: the recording plays back (no plugin), a loop, the recording is armed.
      dry: !!s.dry, loop: Array.isArray(s.loop) ? s.loop : null, recording: !!s.recording,
      // The Plugin buffer refills (a start, a seek, a new loop): nothing is heard yet, the playhead waits.
      buffering: !!s.buffering,
      // Space presses in the plugin's editor (the worker keeps them from the plugin).
      space_seq: num(s.space_seq) || 0,
      // The recording plays along with the file, both into the plugin.
      mix: !!s.mix,
      // While the page's sound plays through the plugin: the loudest input since the last report.
      live_peak: s.live && typeof s.live === 'object' ? num(s.live.peak_in) : null,
      // MIDI (see the MIDI section below): it takes MIDI; the output stream; inputs and activity;
      // why an instrument's sound is off (it crashed the last time it played).
      accepts_midi: !!s.accepts_midi,
      audio: s.audio && typeof s.audio === 'object' ? s.audio : null,
      midi: s.midi && typeof s.midi === 'object' ? s.midi : null,
      sound_off: s.sound_off || '',
    };
  }

  // The plugin's load per audio report (not per redraw): the warning waits for two reports in a row above 100 %.
  function noteLoad(a) {
    const key = a ? JSON.stringify(a) : '';
    if (key === loadKey) return;
    loadKey = key;
    overloads = a && Number(a.load_pct) > 100 ? overloads + 1 : 0;
  }

  // What the host says that the page doesn't already: a fallback note, a status line.
  function hostNote(h) {
    const m = (h && h.message) || '';
    if (!m || QUIET_MESSAGES.test(m)) return '';
    if (m.startsWith(BROWSER_PLAYING)) return m.slice(BROWSER_PLAYING.length).trim();
    if (m.startsWith('Playing ')) {
      // "Playing <file name>. <note>": the Audio tab shows the file already.
      const name = h.source ? String(h.source).split(/[\\/]/).pop() : '';
      const head = `Playing ${name}.`;
      return name && m.startsWith(head) ? m.slice(head.length).trim() : '';
    }
    return m;
  }

  // closed | starting | stopped | paused | playing (of the file; the browser's sound is "stopped":
  // `playing` stays false while the browser is the source)
  function phase(h = host) {
    if (!h || h.state === 'closed') return 'closed';
    if (h.state === 'starting') return 'starting';
    if (h.source_kind === 'browser') return 'stopped';
    if (h.paused) return 'paused';
    return h.playing ? 'playing' : 'stopped';
  }
  // A file is playing for this session (heard or paused).
  const audioActive = (p = phase()) => p === 'playing' || p === 'paused';
  // The file is being heard (not paused).
  const running = () => (recElPlaying() && recElWhich === 'source') || phase() === 'playing';
  const audible = () => phase() === 'playing';
  // Paused, not stopped: the host holds the file, or the page's players were paused (pageHold).
  const isPaused = () => (phase() === 'paused' && !recElPlaying()) || !!pageHold;
  const isOpen = () => !!host && host.state === 'open';
  // Open, but the editor window isn't known yet: it's about to go into the frame.
  const awaitingEditor = () => hasShell() && isOpen() && !host.editor_hwnd
    && performance.now() - openedAt < EDITOR_WAIT_MS;

  // The file's length: from the host while it plays, remembered once known (so the
  // seek bar can pick a starting point after Stop too).
  const duration = () => (file && file.duration) || (host && audioActive() && host.duration_s) || 0;
  // The recording plays in the page (no plugin open).
  const recElPlaying = () => !!recEl && !recEl.paused && !recEl.ended;
  // What plays now in the Audio tab: 'source' (through the plugin), 'recording' (as recorded) or null.
  function playingWhich() {
    if (recElPlaying()) return recElWhich || 'recording';
    if (!host || !audioActive()) return null;
    return host.dry ? 'recording' : 'source';
  }

  // Seconds into the file that are being heard right now.
  function position() {
    if (recElPlaying()) return recEl.currentTime;
    if (!audioActive()) return cursor;
    let t = host.position_s;
    // Guessed on from the last report while the sound goes on; not while the buffer refills (nothing is
    // heard then: the worker says when it plays again).
    if (audible() && !host.buffering) t += (performance.now() - hostAt) / 1000 + hostLag;
    // Looping: the time guessed since the last report wraps like the sound does.
    // (Only a guess past the end wraps: a reported position past it is the old buffer still playing out.)
    const lp = host.loop;
    if (lp && lp[1] > lp[0] && t > lp[1] && host.position_s <= lp[1]) t = lp[0] + ((t - lp[0]) % (lp[1] - lp[0]));
    const d = duration();
    return d ? Math.min(t, d) : t;
  }

  function playBlocker() {
    if (!file) return audio && audio.status === 'decoding' ? 'The file is still being decoded…' : 'Open an audio file first.';
    // No plugin (or an instrument, which takes no audio): the file plays as it is, in the page.
    if (selectedId && phase() === 'starting') return 'Waiting for the plugin to open…';
    return '';
  }

  const pluginById = (id) => plugins.find(p => p.id === id) || null;
  const unavailableById = (id) => unavailable.find(p => p.id === id) || null;
  const pluginName = (id) => (pluginById(id) || unavailableById(id) || {}).name || `Plugin #${id}`;
  // host_issue.retry: opening it anyway is allowed (the cause may be gone); else it can't open.
  const canRetry = (p) => !!(p && p.host_issue && p.host_issue.retry);

  // ---------- talking to the host ----------

  function hostRequest(id, path = '', method = 'GET', body) {
    if (method === 'GET') return V.api(`/api/plugins/${id}/host${path}`);
    const opts = { method };
    if (body !== undefined) {
      opts.headers = { 'Content-Type': 'application/json' };
      opts.body = JSON.stringify(body);
    }
    return V.api(`/api/plugins/${id}/host${path}`, opts);
  }

  // A command for the selected plugin. Shows the state it answers with, or why it failed.
  async function command(path, body, method = 'POST') {
    const id = selectedId;
    if (!id) return null;
    const e = ++epoch;
    try {
      const st = await hostRequest(id, path, method, body);
      if (id === selectedId && e === epoch) {
        notice = '';
        if (st && typeof st === 'object' && 'state' in st) applyHost(st);
      }
      return st || {};
    } catch (err) {
      if (id === selectedId) { notice = errText(err); renderDeck(); }
      return null;
    } finally {
      if (id === selectedId) schedulePoll(POLL_BUSY_MS);
    }
  }

  function applyHost(st) {
    const prev = host;
    const prevPhase = phase(prev);
    host = normHost(st);
    noteLoad(host.audio);
    hostAt = performance.now();
    onEditorSpace(host.state === 'open' ? host.space_seq : null, false);
    const p = phase();

    if (host.state === 'closed') {
      // The backend keeps the last error until something succeeds, so an older
      // error isn't why the plugin went away: only a new one is.
      const err = host.error && (!prev || host.error !== prev.error) ? host.error : '';
      if (prev && prev.state !== 'closed' && !err) {
        // Closed from its frame (or its own window): nothing is selected any more.
        deselect();
        return;
      }
      if (err) {
        closedError = err;
        closedRetry = !host.issue || !!host.issue.retry;
        // It failed to load: the backend recorded why, so it now belongs under "Can't open here".
        if (host.issue) loadPlugins();
      }
      routed = null;
      dropFrame();
    } else {
      closedError = '';
    }
    // A plugin opened from "Can't open here" (Try again) is listed with the others again.
    if (isOpen() && selectedId !== relisted && !pluginById(selectedId)) {
      relisted = selectedId;
      loadPlugins();
    }

    // The file ended by itself: it finished, failed, or the plugin closed.
    if (audioActive(prevPhase) && !audioActive(p)) {
      wantPlay = false;
      if (!ending) {
        const d = (prev && prev.duration_s) || 0;
        const at = host.position_s || prev.position_s;
        // The file's end, or the end of the selection it played: back to the start point.
        const selEnd = sel && playFrom >= sel.a - 0.001 && playFrom < sel.b && at >= sel.b - 0.05;
        cursor = (d && at >= d - 1) || selEnd ? playFrom : at;
      }
    }
    if (host.is_instrument) wantPlay = false;

    if (host.source_kind === 'browser' && host.live_peak != null && host.live_peak > SOUND_PEAK) soundAt = performance.now();
    // The recording grows while it's armed and the file plays: its waveform follows.
    if (prev && prev.recording && !host.recording) loadAudio(); // its last part, and Record's button
    else if (host.recording || audioActive(p)) scheduleAudioPoll();
    if (audioActive(p) && !audioActive(prevPhase)) startTick();

    if (isOpen()) {
      if (host.source_kind === 'browser') routed = selectedId;
      showFrame();
      if (source === 'browser') routeBrowser();
      // A plugin picked while the file played takes over from the same spot once it's open.
      else if (p === 'stopped' && wantPlay && file && !playsInFlight && !ending) startPlayback(cursor);
    }

    updateListMarks();
    renderDeck();
  }

  // ---------- polling ----------

  function pollDelay() {
    const p = phase();
    if (p === 'closed') return null; // nothing changes by itself
    if (awaitingEditor()) return POLL_OPENING_MS;
    if (p === 'starting' && openedAt && performance.now() - openedAt < 2000) return POLL_OPENING_MS;
    if (p === 'starting' || p === 'playing' || wantPlay) return POLL_BUSY_MS;
    if (midiShown() || host.recording) return POLL_MIDI_MS; // lights and levels follow the sound
    return POLL_IDLE_MS;
  }

  function stopPolling() {
    clearTimeout(pollTimer);
    pollTimer = null;
    pollToken++;
  }

  function schedulePoll(delay) {
    stopPolling();
    const token = pollToken;
    if (!selectedId) return;
    // Away from the section, only a plugin still opening is followed (its frame must still appear), and
    // while the Analyzer is shown whatever plays (it shows and controls it).
    if (!active && !keepOn && !(phase() === 'starting' || awaitingEditor())) return;
    const d = delay != null ? delay : pollDelay();
    if (d == null) return;
    pollTimer = setTimeout(() => poll(token), d);
  }

  async function poll(token) {
    const id = selectedId;
    const e = epoch;
    const sent = performance.now();
    let st = null;
    try { st = await hostRequest(id); } catch { /* backend busy or restarting: next round */ }
    if (token !== pollToken) return;
    if (st && id === selectedId && e === epoch) {
      hostLag = Math.min(0.25, (performance.now() - sent) / 2000);
      applyHost(st);
    }
    schedulePoll();
  }

  // ---------- the plugin frame ----------

  // The editor window exists: it goes into VEXT's frame (once per window).
  function showFrame() {
    if (!hasShell() || !isOpen() || !host.editor_hwnd) return;
    const id = selectedId;
    if (framed && framed.id === id && framed.hwnd === host.editor_hwnd) return;
    framed = { id, hwnd: host.editor_hwnd };
    invoke('plugin_frame_open', {
      pluginId: id, name: pluginName(id), hwnd: host.editor_hwnd,
      width: Math.round(host.editor_width || 0), height: Math.round(host.editor_height || 0),
    }).catch((err) => {
      // Kept as "framed" so it isn't retried on every poll: the editor stays in its own window.
      if (selectedId !== id) return;
      frameError = `The plugin's window couldn't be placed inside VEXT (${errText(err)}). It stays in its own window.`;
      hostRequest(id, '/reveal', 'POST').catch(() => { /* it shows by itself after a few seconds */ });
      renderDeck();
    });
  }

  // Only after the backend closed the plugin: the shell then closes a frame with nothing in it.
  function dropFrame() {
    if (!framed) return;
    framed = null;
    if (hasShell()) invoke('plugin_frame_close').catch(() => { /* already gone */ });
  }

  // The frame went away (its close button, or the editor disappeared).
  async function onFrameClosed(payload) {
    const id = Number(payload && (payload.pluginId != null ? payload.pluginId : payload.plugin_id));
    if (!id || id !== selectedId) return; // an older plugin's frame, replaced by this one
    framed = null;
    let st = null;
    try { st = normHost(await hostRequest(id)); } catch { /* treated as closed */ }
    if (id !== selectedId || (st && st.state === 'starting')) return;
    if (st && st.state === 'open') {
      // No frame but still running: a plugin nobody can see must not keep playing.
      ending = true;
      try { await hostRequest(id, '', 'DELETE'); } catch { /* it closes with the worker anyway */ }
      finally { ending = false; }
      if (id === selectedId) deselect();
      return;
    }
    if (st && st.error && (!host || st.error !== host.error)) { applyHost(st); return; } // crashed: say why
    deselect();
  }

  onShellEvent('plugin-frame://closed', onFrameClosed);
  // A new picture taken from the frame's Capture menu (if the shell announces it).
  onShellEvent('plugin-frame://captured', (payload) => {
    const id = Number(payload && (payload.pluginId != null ? payload.pluginId : payload.plugin_id));
    if (id) pictureChanged(id);
  });

  // ---------- plugins ----------

  async function selectPlugin(id) {
    id = Number(id);
    if (!id) return;
    if (id === selectedId) {
      if (phase() === 'closed') return openSelected();
      return;
    }
    const prevId = selectedId;
    const prevOpen = !!prevId && !!host && host.state !== 'closed';
    midiReset(); // the previous plugin closes: its notes end with it
    if (prevId && audioActive()) {
      // The next plugin picks the file up from the same spot once it has opened.
      wantPlay = running();
      cursor = position();
    }
    selectedId = id;
    host = normHost({ state: 'starting' });
    routed = null;
    notice = '';
    closedError = '';
    frameError = '';
    epoch++;
    stopPolling();
    renderDeck();
    if (prevOpen) {
      // One plugin at a time. The backend would close it too when the next one
      // opens; closing it first means its frame is gone before the next one's comes.
      ending = true;
      try { await hostRequest(prevId, '', 'DELETE'); } catch { /* closes with its worker anyway */ }
      finally { ending = false; }
      dropFrame();
      pictureChanged(prevId);
      if (selectedId !== id) return;
    }
    await openSelected();
  }

  async function openSelected() {
    const id = selectedId;
    if (!id) return;
    if (recElWhich === 'source') stopRecEl(); // the file played in the page: the plugin takes over
    const e = ++epoch;
    stopPolling(); // a poll racing the open could report "closed" before the session exists
    host = normHost({ state: 'starting' });
    hostAt = performance.now();
    openedAt = 0;
    routed = null;
    notice = '';
    closedError = '';
    frameError = '';
    updateListMarks();
    renderDeck();
    let st;
    try {
      // framed: the editor stays off the screen until the frame has placed it (no flash at the top-left).
      st = await hostRequest(id, '', 'POST', { framed: hasShell() });
    } catch (err) {
      if (id !== selectedId || e !== epoch) return;
      wantPlay = false;
      closedError = errText(err);
      closedRetry = err.status !== 409; // 409: a reason retrying doesn't change (format, 32-bit…)
      host = normHost({ state: 'closed', error: closedError });
      updateListMarks();
      renderDeck();
      return;
    }
    if (id !== selectedId || e !== epoch) return;
    openedAt = performance.now();
    applyHost(st);
    schedulePoll(POLL_OPENING_MS);
  }

  async function closeSelected() {
    const id = selectedId;
    if (!id) return;
    if (audioActive()) cursor = position();
    wantPlay = false;
    ending = true;
    epoch++;
    stopPolling();
    try {
      await hostRequest(id, '', 'DELETE');
    } catch (err) {
      V.toast('Could not close the plugin: ' + errText(err), true);
    } finally {
      ending = false;
    }
    if (selectedId === id) deselect();
  }

  // Nothing selected any more (closed by us, by its frame, or by itself).
  function deselect() {
    const id = selectedId;
    midiReset(); // its worker is gone: nothing to release there
    selectedId = null;
    host = null;
    wantPlay = false;
    routed = null;
    notice = '';
    closedError = '';
    frameError = '';
    epoch++;
    stopPolling();
    dropFrame();
    if (id) pictureChanged(id); // a picture may have been taken while it was open
    updateListMarks();
    renderDeck();
  }

  // The plugin's saved picture may be new: show the current one.
  function pictureChanged(id) {
    imgStamp[id] = Date.now();
    const p = pluginById(id);
    if (p && !p.has_image) loadPlugins();
    else if (p) { last.list = null; renderList(); }
  }

  // A picture set or changed anywhere else (the plugin's sheet, the picker, a batch): app.js
  // announces it ('vext:pictures'). The new one shows, and the list is fetched again.
  document.addEventListener('vext:pictures', (e) => {
    const ids = (e.detail && e.detail.ids) || [];
    const now = Date.now();
    for (const id of ids) imgStamp[id] = now;
    if (plugins.length) loadPlugins();
  });

  async function loadPlugins() {
    const seq = ++pluginsSeq;
    if (!plugins.length) { pluginsState = 'loading'; renderList(); }
    try {
      const [r, u] = await Promise.all([
        V.api('/api/playground/plugins'),
        V.api('/api/playground/unavailable').catch(() => null), // only the "Can't open here" group needs it
      ]);
      if (seq !== pluginsSeq) return;
      plugins = Array.isArray(r) ? r : [];
      if (Array.isArray(u)) unavailable = u;
      pluginsState = 'ready';
      pluginsError = '';
    } catch (err) {
      if (seq !== pluginsSeq) return;
      pluginsError = errText(err);
      if (plugins.length) V.toast('Could not refresh the plugin list: ' + pluginsError, true);
      else pluginsState = 'error';
    }
    renderList();
    renderDeck();
  }

  // ---------- background check ----------
  // Loads each listed plugin that hasn't opened yet, headlessly, two at a time
  // (POST /api/playground/check); the ones that fail move under "Can't open here".

  function renderCheck() {
    if (!els) return;
    let html;
    if (check && check.running) {
      const of = check.total ? ` ${check.done} of ${check.total}` : '';
      html = `<span class="pg-check-run" title="${esc((check.current || []).join(', '))}"><span class="spinner" aria-hidden="true"></span>Checking${of}</span>
        <button type="button" data-act="check-cancel">Cancel</button>`;
    } else {
      html = '<button type="button" data-act="check" title="Load each plugin that hasn\'t been opened yet, in the background, to find the ones that can\'t open">Check…</button>';
    }
    setHtml('check', els.check, html);
  }

  function scheduleCheckPoll() {
    clearTimeout(checkTimer);
    checkTimer = active && check && check.running ? setTimeout(pollCheck, CHECK_POLL_MS) : null;
  }

  async function pollCheck() {
    const was = check;
    try { check = await V.api('/api/playground/check'); } catch { /* backend busy: next round */ }
    if (was && was.running && check && !check.running) {
      loadPlugins(); // a cancelled check still recorded what it found
      if (!check.cancelled) V.toast(check.total
        ? `Checked ${check.done} ${check.done === 1 ? 'plugin' : 'plugins'}: ${check.ok} opened, ${check.failed} can't open here.`
        : 'Nothing to check: every plugin listed has opened before.');
    }
    renderCheck();
    scheduleCheckPoll();
  }

  async function startCheck() {
    const ok = await V.confirmDialog('Check the plugins that haven\'t been opened yet? VEXT loads each one in the ' +
      'background, two at a time, without showing it; this can take several minutes. Some plugins open a license or ' +
      'registration window while loading: answer or close it (one left waiting is marked as too slow, and you can try ' +
      'it again by hand). Plugins that can\'t open move to "Can\'t open here".');
    if (!ok) return;
    try {
      check = await V.api('/api/playground/check', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ only_unverified: true }),
      });
    } catch (err) {
      V.toast('Could not start the check: ' + errText(err), true);
      return;
    }
    renderCheck();
    scheduleCheckPoll();
  }

  async function cancelCheck() {
    try {
      check = await V.api('/api/playground/check', { method: 'DELETE' });
    } catch (err) {
      V.toast('Could not cancel the check: ' + errText(err), true);
    }
    renderCheck();
    scheduleCheckPoll();
  }

  // ---------- sources ----------

  // The page's sound goes through the open plugin (once per opened plugin).
  async function routeBrowser() {
    const id = selectedId;
    if (source !== 'browser' || !id || !isOpen() || host.is_instrument || routed === id) return;
    routed = id; // a failure isn't retried on every poll; switching the source tries again
    wantPlay = false;
    if (audioActive()) cursor = position();
    await command('/source', { kind: 'browser' });
  }

  // The tab shown is the source: Browser routes the page's sound, Audio waits for Play. Signal is the
  // file's source too (the rendered signal is the file): between Audio and Signal only the view changes.
  function setSource(kind) {
    const view = kind === 'signal' ? 'signal' : kind === 'file' ? 'audio' : fileView;
    if (kind === 'signal') kind = 'file';
    if (kind !== 'browser' && kind !== 'file') return;
    if (kind === 'browser' && !hasShell()) return;
    if (kind === source) {
      if (kind === 'file' && view !== fileView) {
        fileView = view;
        try { localStorage.setItem(TAB_KEY, view === 'signal' ? 'signal' : 'file'); } catch { /* storage unavailable */ }
        applyTab();
        renderDeck();
      }
      return;
    }
    source = kind;
    fileView = view;
    notice = '';
    try { localStorage.setItem(TAB_KEY, kind === 'file' && view === 'signal' ? 'signal' : kind); } catch { /* storage unavailable */ }
    if (kind === 'file' && brec.on) stopBrowserRecord(); // it records the Browser tab: leaving it ends the recording
    // Browser and Audio never play together: what plays in the tab being left pauses.
    if (kind === 'file') pauseBrowserMedia();
    else if (recElPlaying() || (cmpEl && !cmpEl.paused)) pausePlayback(); // the page's players: Play goes on from there
    if (kind === 'browser') {
      stopRecEl();
      routed = null;
      routeBrowser(); // stops the file, if one plays
    } else if (selectedId && isOpen() && routed === selectedId) {
      // The page's own sound comes back (it keeps playing, hidden); the plugin waits for a file.
      routed = null;
      hostRequest(selectedId, '/source', 'POST', { kind: 'file' }).catch(() => { /* playing a file switches anyway */ });
    }
    applyTab();
    renderDeck();
    if (kind === 'file') loadAudio();
  }

  // ---------- recording the browser into the Audio tab ----------
  // The Browser tab's Record button. The backend (app/browser_record.py) records what the browser plays
  // (after the open plugin, if there is one) into a WAV; stopping opens it as the Audio tab's file.

  const brecBlocker = () => (isOpen() && host.is_instrument
    ? 'An instrument doesn\'t take the browser\'s sound: close it to record the browser.' : '');

  function renderBrec() {
    if (!els || !els.brec) return;
    const on = brec.on;
    const why = on ? '' : brecBlocker();
    const through = !on && isOpen() && !host.is_instrument ? ` (after ${pluginName(selectedId)})` : '';
    const title = on ? 'Stop recording: it becomes the Audio tab\'s file'
      : why || `Record what plays in the browser into the Audio tab${through}`;
    const off = !on && (!!why || brec.busy);
    const html = `<button type="button" class="pg-rbtn pg-rec${on ? ' is-on' : ''}" data-act="brec" aria-pressed="${on}"
        title="${esc(title)}"${off || brec.stopping ? ' disabled' : ''}>${ICON.record}<span>${on ? `<span class="pg-brec-time">${fmtTime(brec.seconds)}</span>` : 'Record'}</span></button>` +
      (on ? '<span class="pg-brec-meter" title="Level of what is being recorded" aria-hidden="true"><span class="pg-brec-level"></span></span>' : '');
    setHtml('brec', els.brec, html);
    paintBrec();
  }

  // The time and the level move without rewriting the button.
  function paintBrec() {
    if (!els || !els.brec || !brec.on) return;
    const t = els.brec.querySelector('.pg-brec-time');
    if (t) t.textContent = fmtTime(brec.seconds);
    const level = els.brec.querySelector('.pg-brec-level');
    if (level) {
      const pct = brec.peak > 0 ? Math.max(0, Math.min(1, (20 * Math.log10(brec.peak) + 60) / 60)) : 0;
      level.style.width = `${Math.round(pct * 100)}%`;
    }
  }

  // Like V.confirmDialog (app.js: the same modal, in the same root so the browser view hides), with a
  // checkbox. Resolves { ok, off }: off = "Don't show again" was ticked.
  function brecHintDialog(message) {
    return new Promise((resolve) => {
      const root = document.getElementById('confirm-root');
      root.innerHTML = `
        <div class="modal-backdrop">
          <div class="modal" role="dialog" aria-modal="true">
            <p>${esc(message)}</p>
            <label class="pg-dlg-check"><input type="checkbox" class="pg-dlg-off"><span>Don't show again</span></label>
            <div class="actions-row">
              <button type="button" class="pg-dlg-cancel">Cancel</button>
              <button type="button" class="primary pg-dlg-ok">Record anyway</button>
            </div>
          </div>
        </div>`;
      const box = root.querySelector('.pg-dlg-off');
      const finish = (ok) => {
        const off = !!box.checked;
        root.innerHTML = '';
        document.removeEventListener('keydown', onKey);
        resolve({ ok, off: ok && off });
      };
      const onKey = (e) => { if (e.key === 'Escape') finish(false); };
      document.addEventListener('keydown', onKey);
      root.querySelector('.pg-dlg-ok').onclick = () => finish(true);
      root.querySelector('.pg-dlg-cancel').onclick = () => finish(false);
      root.querySelector('.modal-backdrop').onclick = (e) => { if (e.target === e.currentTarget) finish(false); };
      root.querySelector('.pg-dlg-ok').focus();
    });
  }

  async function startBrowserRecord() {
    if (brec.on || brec.busy || brecBlocker()) return;
    brec.busy = true;
    renderBrec();
    try {
      // The recording replaces the Audio tab's file (and the recording after the plugin that goes with it).
      if (file || (audio && audio.status === 'decoding')) {
        const name = (audio && audio.name) || (file && file.name) || 'the file';
        const ok = await V.confirmDialog(`Recording replaces ${name} in the Audio tab (and its recording after the plugin). Record anyway?`);
        if (!ok) return;
      }
      // An open plugin changes what is recorded: the browser's sound after it.
      if (isOpen() && !host.is_instrument) {
        let hint = true;
        try { hint = localStorage.getItem(BREC_HINT_KEY) !== 'off'; } catch { /* storage unavailable: ask */ }
        if (hint) {
          const name = pluginName(selectedId);
          const r = await brecHintDialog(`${name} is open: the recording will be the browser's sound after ${name}. ` +
            'For an accurate recording of the browser alone, close the plugin first.');
          if (!r.ok) return;
          if (r.off) { try { localStorage.setItem(BREC_HINT_KEY, 'off'); } catch { /* storage unavailable */ } }
        }
      }
      const st = await V.api('/api/playground/browser-record', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ on: true }),
      });
      brec.on = true;
      brec.seconds = num(st && st.seconds) || 0;
      brec.peak = 0;
      brec.through = (st && st.through) || null;
      brecSchedule();
    } catch (err) {
      V.toast(`Couldn't start recording the browser: ${errText(err)}`, true);
    } finally {
      brec.busy = false;
      renderBrec();
    }
  }

  function brecSchedule() {
    clearTimeout(brec.timer);
    brec.timer = brec.on && !brec.stopping ? setTimeout(brecPoll, BREC_POLL_MS) : null;
  }

  async function brecPoll() {
    brec.timer = null;
    if (!brec.on || brec.stopping) return;
    let st = null;
    try { st = await V.api('/api/playground/browser-record'); } catch { /* backend busy: next round */ }
    if (!brec.on || brec.stopping) return;
    if (st) {
      brec.seconds = num(st.seconds) || 0;
      brec.peak = num(st.peak) || 0;
      brec.through = st.through || null;
      paintBrec();
      // The backend ended it by itself (the sample rate changed): what was recorded is kept, so finish it as a Stop.
      if (st.on === false || st.error) { stopBrowserRecord(st.error || ''); return; }
    }
    brecSchedule();
  }

  // Stop: the backend finishes the WAV and opens it as the Audio tab's file; the page shows that tab.
  async function stopBrowserRecord(note = '') {
    if (!brec.on || brec.stopping) return;
    brec.stopping = true;
    clearTimeout(brec.timer);
    brec.timer = null;
    renderBrec();
    let r = null;
    try {
      r = await V.api('/api/playground/browser-record', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ on: false }),
      });
    } catch (err) {
      V.toast(errText(err), true);
    }
    const seconds = (r && num(r.seconds)) || brec.seconds;
    brec.on = false;
    brec.stopping = false;
    brec.peak = 0;
    if (note) V.toast(note, true);
    if (r) {
      await beforeNewFile(); // as for any new file: nothing of the old one plays on
      autoEditsPending = true; // Configuration > Audio's automatic edits, as soon as it is decoded
      if (r.audio) applyAudio(r.audio); else loadAudio();
      if (source !== 'file') setSource('file');
      // The page would play on behind the Audio tab (a YouTube video, say): pause it.
      V.api('/api/browser/audio/pause', { method: 'POST' }).catch(() => {});
      const auto = autoEditsText(await autoEdits());
      V.toast(`Recorded ${fmtTime(seconds)} from the browser${auto ? `, ${auto}` : ''} — it's the Audio tab's file now`);
    }
    renderBrec();
  }

  // A recording that runs on the backend (the page was reloaded) is picked up again.
  async function brecResume() {
    if (brec.on || brec.busy) return;
    brec.busy = true; // one check at a time (the page's start and the section's load can both ask)
    try {
      const st = await V.api('/api/playground/browser-record');
      if (!st || brec.on || (!st.on && !st.pending)) return;
      brec.seconds = num(st.seconds) || 0;
      brec.on = true;
      // Pending: it stopped by itself while the page was away (a rate change). It is collected as a Stop.
      if (!st.on) { await stopBrowserRecord(st.error || ''); return; }
      brecSchedule();
      renderBrec();
    } catch { /* no recording, or the backend is busy */ } finally {
      brec.busy = false;
    }
  }

  function applyTab() {
    if (!els) return;
    const audioTab = source === 'file';
    const signalTab = audioTab && fileView === 'signal';
    els.web.classList.toggle('is-tab-audio', audioTab);
    els.web.classList.toggle('is-tab-signal', signalTab);
    const shown = !audioTab ? 'browser' : signalTab ? 'signal' : 'file';
    for (const b of els.srcBtns) {
      const on = b.dataset.src === shown;
      b.setAttribute('aria-selected', String(on));
      b.tabIndex = on ? 0 : -1;
      b.classList.toggle('active', on);
    }
    els.browserPanel.hidden = audioTab;
    els.audioPanel.hidden = !audioTab || signalTab;
    els.signalPanel.hidden = !signalTab;
    // One waveform view (the file and its recording), shown in whichever of the two file tabs is open.
    const home = signalTab ? els.signalWave : els.audioPanel;
    if (els.wave.parentElement !== home) home.appendChild(els.wave);
    if (signalTab && !signal && V.PlaygroundSignal) signal = V.PlaygroundSignal.create(els.signal, signalHost);
    if (signal) signal.show(signalTab);
    placeBrowser(); // the browser hides at once (Audio) or comes back (Browser)
    if (audioTab && wv) wv.redraw();
    if (audioTab) startTick();
  }

  // ---------- the Audio tab: the file, its edits, the recording ----------

  async function loadAudio() {
    const seq = ++audioSeq;
    let st = null;
    try { st = await V.api('/api/playground/audio'); } catch { /* backend busy: the next poll */ }
    if (seq !== audioSeq || !st) return;
    applyAudio(st);
  }

  function applyAudio(st) {
    const prev = audio;
    audio = st && typeof st === 'object' ? st : null;
    const ready = !!audio && audio.status === 'ready' && !!audio.info;
    const was = file;
    file = ready ? { path: audio.source_path, name: audio.name, duration: audio.info.duration_s } : null;
    if (!ready || !was || was.path !== file.path) {
      if (!ready || (was && was.path !== file.path)) { sel = null; cursor = 0; dropAutoLoop(); }
    }
    if (ready && pendingLoop) { setLoopMode(pendingLoop); pendingLoop = null; }
    const srcTrack = ready ? { ...audio.info, version: audio.version } : null;
    const rec = audio && audio.recording;
    const recTrack = rec && rec.exists ? { ...rec } : null;
    if (wv) {
      wv.setSource(srcTrack);
      wv.setRecording(recTrack);
      if (!ready) wv.setSelection(null);
    }
    // Picked just now: it plays as soon as it's decoded (and a plugin can hear it).
    if (ready && audioWanted && (!prev || prev.status !== 'ready' || prev.source_path !== audio.source_path)) {
      audioWanted = false;
      const at = Math.min(resumeAt, Math.max(0, file.duration - 0.05));
      resumeAt = 0;
      if (!playBlocker()) startPlayback(at);
    }
    if (audio && audio.status === 'error') audioWanted = false;
    if (ready && autoEditsPending) {
      autoEditsPending = false;
      applyAutoEdits();
    }
    renderAudio();
    renderDeck();
    scheduleAudioPoll();
  }

  // Polled while something changes by itself: decoding, or the recording growing.
  function scheduleAudioPoll() {
    if (audioTimer) return; // one is on its way (the host poll calls this more often than it runs)
    const decoding = audio && audio.status === 'decoding';
    const growing = !!host && (host.recording || !!(audio && audio.recording && audio.recording.armed));
    if (!(active || keepOn) || !(decoding || growing)) return;
    audioTimer = setTimeout(async () => {
      audioTimer = null;
      await loadAudio();
    }, AUDIO_POLL_MS);
  }

  async function chooseFile() {
    const dialog = tauri() && tauri().dialog;
    if (!dialog || typeof dialog.open !== 'function') {
      // No system dialog here (a plain browser tab): the path is typed (or the file dropped).
      els.pathForm.hidden = false;
      els.pathInput.focus();
      return;
    }
    let picked;
    try {
      picked = await dialog.open({
        multiple: false, directory: false, title: 'Open an audio file',
        filters: [{ name: 'Audio', extensions: audioExtensions }, { name: 'All files', extensions: ['*'] }],
      });
    } catch (err) {
      V.toast('Could not open the file dialog: ' + errText(err), true);
      return;
    }
    if (Array.isArray(picked)) picked = picked[0];
    if (picked && typeof picked === 'object') picked = picked.path;
    if (picked) openAudio(String(picked));
  }

  // A new file replaces the one loaded (only one at a time); the backend decodes it.
  async function openAudio(path) {
    path = String(path || '').trim().replace(/^"(.*)"$/, '$1');
    if (!path) return;
    if (source !== 'file') setSource('file');
    await beforeNewFile();
    notice = '';
    audioWanted = true;
    autoEditsPending = true;
    try {
      applyAudio(await V.api('/api/playground/audio/open', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ path }),
      }));
      els.pathForm.hidden = true;
    } catch (err) {
      audioWanted = false;
      notice = `Can't open that file: ${errText(err)}`;
      renderDeck();
    }
  }

  // A file dropped without the desktop app (no path): its bytes go to the backend.
  async function uploadAudio(f) {
    if (!f) return;
    if (source !== 'file') setSource('file');
    await beforeNewFile();
    notice = '';
    audioWanted = true;
    autoEditsPending = true;
    try {
      applyAudio(await V.api(`/api/playground/audio/upload?${new URLSearchParams({ name: f.name || 'audio' })}`, {
        method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: f,
      }));
    } catch (err) {
      audioWanted = false;
      notice = `Can't open that file: ${errText(err)}`;
      renderDeck();
    }
  }

  async function beforeNewFile() {
    stopRecEl();
    pageHold = null;
    autoEditsPending = false;
    wantPlay = false;
    cursor = 0;
    sel = null;
    dropAutoLoop();
    if (wv) wv.setSelection(null);
    if (selectedId && isOpen() && (audioActive() || host.recording)) {
      ending = true;
      try { await command('/stop'); } finally { ending = false; }
    }
  }

  // `edges`: ignore the selection, a fade goes on the file's own start / end.
  async function edit(op, edges = false) {
    if (!file || editBusy) return;
    const body = { op };
    if (!edges && sel && sel.b - sel.a >= 0.001) Object.assign(body, { start_s: round3(sel.a), end_s: round3(sel.b) });
    else if (op !== 'normalize') body.length_s = fadeLen;
    editBusy = true;
    renderAudio();
    try {
      applyAudio(await V.api('/api/playground/audio/edit', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
      }));
    } catch (err) {
      V.toast(errText(err), true);
    } finally {
      editBusy = false;
      renderAudio();
    }
  }

  // Configuration > Audio's automatic edits for a new file or browser recording, once decoded:
  // Auto Normalize (the whole file to -1 dBFS), then Auto Fade In/Out (the file's own start and
  // end, the chosen fade length; a browser recording starts and ends abruptly). Each is a normal
  // edit: Undo takes it off.
  async function autoEdits() {
    let s = {};
    try { s = (await V.settings()) || {}; } catch { /* backend busy: the defaults */ }
    return { normalize: s.audio_auto_normalize === '1', fades: (s.audio_auto_fade ?? '1') === '1' };
  }
  const autoEditsText = (a) => [a.normalize && 'normalized', a.fades && 'with a short fade in and out'].filter(Boolean).join(' and ');

  async function applyAutoEdits() {
    const a = await autoEdits();
    if (a.normalize) await edit('normalize', true);
    if (a.fades) {
      await edit('fade_in', true);
      await edit('fade_out', true);
    }
  }

  // Takes the file out of the Audio tab (and the recording after the plugin that belongs to it).
  // The file on disk is never touched.
  async function removeFile() {
    if (!audio || audio.status === 'empty' || (host && host.recording)) return;
    const name = audio.name || 'the file';
    const extra = hasRecording() ? ', and the recording after the plugin (it can\'t be brought back: save it first to keep it)' : '';
    const ok = await V.confirmDialog(`Remove ${name} from the Audio tab${extra}? The file on disk is not touched.`);
    if (!ok) return;
    await beforeNewFile(); // nothing of it plays on
    audioWanted = false;
    // Both players let go of the files (load() ends their pending requests): the server can delete them.
    for (const el of [recEl, cmpEl]) {
      if (el && el.getAttribute('src')) { el.removeAttribute('src'); el.load(); }
    }
    try {
      applyAudio(await V.api('/api/playground/audio', { method: 'DELETE' }));
    } catch (err) {
      V.toast(errText(err), true);
      loadAudio();
    }
  }

  async function undoEdit() {
    if (!audio || !audio.can_undo || editBusy) return;
    editBusy = true;
    try {
      applyAudio(await V.api('/api/playground/audio/undo', { method: 'POST' }));
    } catch (err) {
      V.toast(errText(err), true);
    } finally {
      editBusy = false;
      renderAudio();
    }
  }

  // A selection played once from inside it: the plugin's stream ends exactly at its end (and so
  // does a recording). Not while looping (the loop repeats it).
  const untilRange = (at) => (!loopOn && sel && sel.b - sel.a >= 0.01 && at >= sel.a - 0.001 && at < sel.b
    ? [round3(sel.a), round3(sel.b)] : null);
  // What Loop repeats: the selection, else (or with Loop All) the whole file.
  const loopRange = () => (sel && sel.b - sel.a >= 0.01 && !loopAll ? [round3(sel.a), round3(sel.b)]
    : (file ? [0, round3(file.duration)] : null));

  // The selection went away without the wave telling (a new file): a loop made for it goes too.
  function dropAutoLoop() {
    if (!loopAuto) return;
    loopOn = false;
    loopAuto = false;
    loopAll = false;
    if (wv) wv.setLoop(false);
  }

  // `auto`: switched on because a selection was made (and off again when it is cleared, unless the
  // user took over by pressing Loop themselves).
  async function setLoop(on, auto = false, all = false) {
    loopOn = on;
    loopAuto = on && auto;
    loopAll = on && all;
    if (wv) wv.setLoop(on);
    if (recEl) recEl.loop = on && (!sel || loopAll);
    renderTransport();
    if (selectedId && isOpen() && audioActive()) {
      const r = on ? loopRange() : null;
      await command('/loop', r ? { start_s: r[0], end_s: r[1] } : {});
    }
  }

  // The loop as one choice (the Analyzer's timeline): 'off', 'sel' (the selection, else the whole file), 'all'.
  const loopMode = () => (!loopOn ? 'off' : loopAll ? 'all' : 'sel');
  function setLoopMode(mode) {
    return mode === 'off' ? setLoop(false) : setLoop(true, false, mode === 'all');
  }

  function onSelect(next) {
    const had = !!sel && sel.b - sel.a >= 0.01;
    sel = next;
    const has = !!sel && sel.b - sel.a >= 0.01;
    if (recEl) recEl.loop = loopOn && (!has || loopAll);
    // Selecting a region loops it by default (press Loop to turn that off); making the selection is
    // what switches it on, so adjusting one afterwards keeps whatever the user chose.
    if (has && !had && !loopOn) {
      setLoop(true, true); // also tells the plugin's stream the range
    } else if (!has && had && loopOn && loopAuto) {
      setLoop(false);
    } else if (loopOn && selectedId && isOpen() && audioActive()) {
      // A loop follows the selection (playing on from where it is).
      const r = loopRange();
      command('/loop', r ? { start_s: r[0], end_s: r[1] } : {});
    }
    renderAudio();
    renderTransport();
  }

  // ---- the recording ----

  const recInfo = () => (audio && audio.recording) || {};
  const hasRecording = () => !!recInfo().exists;

  function recordBlocker() {
    if (!file) return 'Open an audio file first.';
    if (!selectedId) return 'Pick a plugin: what comes out of it is recorded.';
    const p = phase();
    if (p === 'starting') return 'Waiting for the plugin to open…';
    if (p === 'closed') return 'Open the plugin first.';
    if (host.is_instrument) return 'This plugin is an instrument: a file can\'t play through it.';
    return '';
  }

  async function toggleRecord() {
    if (host && host.recording) {
      await command('/record', { on: false });
      loadAudio();
      return;
    }
    const why = recordBlocker();
    if (why) { notice = why; renderDeck(); return; }
    stopRecEl();
    const st = await command('/record', { on: true });
    if (!st) return;
    // Recording needs the file playing through the plugin: start it (from the selection, if any).
    if (!audioActive() || host.dry) { playFrom = sel ? sel.a : cursor; await startPlayback(playFrom); }
    else if (phase() === 'paused') await resumePlayback();
    loadAudio();
  }

  // The recording, as recorded (without the plugin): through the open plugin's
  // audio stream (same device), else in the page.
  async function playRecording() {
    if (!hasRecording()) return;
    // Its own button pauses and resumes the recording the same way the main one does the file.
    const held = pageHold ? pageHold.which : phase() === 'paused' ? (host.dry ? 'recording' : 'source') : null;
    if (held === 'recording') { await resumePlayback(); return; }
    if (playingWhich() === 'recording' && !held) { await pausePlayback(); return; }
    pageHold = null;
    const at = sel ? sel.a : position();
    playFrom = Math.max(0, at || 0);
    if (selectedId && isOpen() && !host.is_instrument) {
      stopRecEl();
      wantPlay = false;
      routed = null;
      const r = loopOn ? loopRange() : null;
      cursor = Math.max(0, at || 0);
      const u = untilRange(cursor);
      await command('/play', { which: 'recording', start_s: round3(cursor), ...(r ? { loop: r } : {}), ...(u ? { until: u } : {}) });
      startTick();
      return;
    }
    await playInPage('recording', at);
  }

  // With a recording (and not recording again), Play plays the original and the recording together:
  // into the open plugin (what one plugin made goes on into the next), else in the page to compare.
  const comparing = () => hasRecording() && !(host && host.recording);
  const pluginTakesFile = () => !!selectedId && isOpen() && !host.is_instrument;
  // The M buttons. In the page they are the two gains of the tap graph (an element's own `volume` no
  // longer matters once it is routed through it; without the graph it is the fallback); into a plugin
  // the worker's mix is told.
  const srcMuted = () => recElWhich === 'source' && muteSrc && comparing();
  function setMute(which, muted) {
    const el = which === 'src' ? recEl : cmpEl;
    if (!el) return;
    const routed = which === 'src' ? tap.srcNode : tap.cmpNode;
    const gain = which === 'src' ? tap.gSrc : tap.gRec;
    if (routed && gain && tap.ctx) {
      el.volume = 1;
      gain.gain.setTargetAtTime(muted ? 0 : 1, tap.ctx.currentTime, 0.008);
    } else {
      el.volume = muted ? 0 : 1;
    }
  }
  function applyMutes() {
    setMute('src', srcMuted());
    setMute('rec', muteRec);
    if (host && host.mix && audioActive()) command('/mix', { mute_source: muteSrc, mute_recording: muteRec });
  }

  // ---- the in-page tap: what plays in the page, for the Analyzer ----
  // recEl and cmpEl go through one AudioContext, created on the first play (after a user gesture):
  //   recEl -> gSrc -> speakers;  cmpEl -> gRec -> speakers      (the M mutes)
  //   the worklet's input 0 = recEl, input 1 = recEl (one sound) or cmpEl (comparing)     (before / after, pre-mute)
  // The worklet posts ~1024-frame blocks only while VEXT.audioBus has listeners and something plays.
  // No AudioWorklet (or no context that can run): no tap, and the elements play on their own as before.

  function ensureGraph() {
    if (tap.ctx) return Promise.resolve(true);
    if (!tap.pending) {
      tap.pending = buildGraph().finally(() => { tap.pending = null; });
    }
    return tap.pending;
  }

  async function buildGraph() {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return false;
    let ctx = null;
    try {
      ctx = new AC({ latencyHint: 'interactive' });
      // Not allowed to run yet (no gesture): leave the elements alone, they play on their own and the next play tries again.
      if (ctx.state !== 'running') await Promise.race([ctx.resume().catch(() => {}), new Promise(r => setTimeout(r, 400))]);
      if (ctx.state !== 'running') { ctx.close().catch(() => {}); return false; }
      tap.gSrc = ctx.createGain();
      tap.gRec = ctx.createGain();
      tap.gSrc.connect(ctx.destination);
      tap.gRec.connect(ctx.destination);
      tap.afterSrc = ctx.createGain();
      tap.afterCmp = ctx.createGain();
      tap.afterCmp.gain.value = 0;
      try {
        if (!ctx.audioWorklet) throw new Error('no AudioWorklet');
        await ctx.audioWorklet.addModule(TAP_WORKLET);
        const node = new AudioWorkletNode(ctx, 'vext-analyzer-tap', {
          numberOfInputs: 2, numberOfOutputs: 0, channelCount: 2, channelCountMode: 'explicit',
        });
        node.port.onmessage = (e) => {
          const d = e.data;
          if (!tap.on || !d || !d.before || !V.audioBus || typeof V.audioBus.pushLocal !== 'function') return;
          V.audioBus.pushLocal({ rate: ctx.sampleRate, before: d.before, after: d.after });
        };
        tap.afterSrc.connect(node, 0, 1);
        tap.afterCmp.connect(node, 0, 1);
        tap.node = node;
      } catch { tap.node = null; /* the sound still plays through the gains, just without a tap */ }
      tap.ctx = ctx;
      tap.sync = setInterval(syncTap, TAP_SYNC_MS);
      return true;
    } catch {
      if (ctx) ctx.close().catch(() => {});
      return false;
    }
  }

  // Routes the page's players through the graph (once each; an element can only be routed once).
  async function attachTap() {
    if (!(await ensureGraph())) return;
    const ctx = tap.ctx;
    for (const which of ['src', 'cmp']) {
      const el = which === 'src' ? recEl : cmpEl;
      if (!el || tap[`${which}Node`]) continue;
      let node;
      try { node = ctx.createMediaElementSource(el); } catch { continue; }
      tap[`${which}Node`] = node;
      if (which === 'src') {
        node.connect(tap.gSrc);
        node.connect(tap.afterSrc);
        if (tap.node) node.connect(tap.node, 0, 0);
      } else {
        node.connect(tap.gRec);
        node.connect(tap.afterCmp);
      }
      setMute(which, which === 'src' ? srcMuted() : muteRec);
    }
  }

  // Comparing: after = the recording's player; one sound: after = before.
  function setTapCompare(on) {
    if (!tap.afterSrc) return;
    tap.afterSrc.gain.value = on ? 0 : 1;
    tap.afterCmp.gain.value = on ? 1 : 0;
  }

  const pagePlaying = () => recElPlaying() || (!!cmpEl && !cmpEl.paused && !cmpEl.ended);

  // The worklet posts only while someone listens and something plays here (nothing else must look like page sound).
  function syncTap() {
    // Nothing plays in the page: the context goes quiet a moment later (resumeTap() wakes it for the next play).
    if (pagePlaying()) { clearTimeout(tap.idle); tap.idle = null; }
    else if (tap.ctx && !tap.idle && tap.ctx.state === 'running') tap.idle = setTimeout(suspendTap, TAP_IDLE_MS);
    if (!tap.node) return;
    const want = pagePlaying() && !!V.audioBus && typeof V.audioBus.wanted === 'function' && !!V.audioBus.wanted();
    if (want === tap.on) return;
    tap.on = want;
    tap.node.port.postMessage({ on: want });
  }

  function suspendTap() {
    tap.idle = null;
    if (tap.ctx && tap.ctx.state === 'running' && !pagePlaying()) tap.ctx.suspend().catch(() => {});
  }

  // Before the page plays: the context runs again (a suspended one would make the sound silent).
  async function resumeTap() {
    clearTimeout(tap.idle);
    tap.idle = null;
    if (tap.ctx && tap.ctx.state !== 'running') await tap.ctx.resume().catch(() => {});
  }

  // The page's players load with CORS (the desktop app's page and the backend are different origins):
  // a cross-origin element routed through an AudioContext would otherwise be silent.
  function newPlayer() {
    const el = new Audio();
    el.crossOrigin = 'anonymous';
    el.preload = 'auto';
    for (const ev of ['play', 'pause', 'ended']) el.addEventListener(ev, syncTap);
    return el;
  }

  // No plugin: original (as edited) and recording together in the page, same time line; the mutes pick what's heard.
  async function playCompare(at) {
    if (audioActive()) await command('/stop');
    if (!cmpEl) cmpEl = newPlayer();
    const path = `/api/playground/audio/recording.wav?v=${recInfo().version || 0}`;
    if (!cmpEl.src.endsWith(path)) cmpEl.src = `${V.API_BASE || ''}${path}`;
    cmpEl.loop = loopOn && !sel;
    await playInPage('source', at, true);
    applyMutes();
    try {
      cmpEl.currentTime = Math.max(0, recEl ? recEl.currentTime : at || 0);
      if (cmpEl.currentTime < (cmpEl.duration || Infinity)) await cmpEl.play();
    } catch { /* a recording shorter than the start point just stays silent */ }
  }

  // Plays in the page (no plugin open): the file as edited (source.wav) or the recording.
  async function playInPage(which, at, compare = false) {
    if (!recEl) {
      recEl = newPlayer();
      recEl.addEventListener('ended', () => { cursor = playFrom; pageHold = null; if (cmpEl) cmpEl.pause(); renderDeck(); });
      recEl.addEventListener('pause', () => renderDeck());
      recEl.addEventListener('play', () => { renderDeck(); startTick(); });
    }
    const path = which === 'source'
      ? `/api/playground/audio/source.wav?v=${(audio && audio.version) || 0}`
      : `/api/playground/audio/recording.wav?v=${recInfo().version || 0}`;
    if (!recEl.src.endsWith(path)) recEl.src = `${V.API_BASE || ''}${path}`;
    recElWhich = which;
    await attachTap();
    await resumeTap();
    setTapCompare(compare);
    setMute('src', srcMuted());
    recEl.loop = loopOn && !sel;
    try {
      recEl.currentTime = Math.max(0, at || 0);
      await recEl.play();
    } catch (err) {
      V.toast(`Couldn't play the ${which === 'source' ? 'file' : 'recording'}: ` + errText(err), true);
    }
  }

  function stopRecEl() {
    if (cmpEl && !cmpEl.paused) cmpEl.pause();
    if (recEl && !recEl.paused) {
      cursor = recEl.currentTime;
      recEl.pause();
    }
  }

  async function clearRecording() {
    if (!hasRecording()) return;
    const ok = await V.confirmDialog('Clear the recording? It can\'t be brought back (save it first if you want to keep it).');
    if (!ok) return;
    stopRecEl();
    // Both players let go of the file (load() ends their pending requests): the server can delete it.
    for (const el of [recEl, cmpEl]) {
      if (el && el.getAttribute('src')) { el.removeAttribute('src'); el.load(); }
    }
    try {
      applyAudio(await V.api('/api/playground/audio/recording', { method: 'DELETE' }));
    } catch (err) {
      V.toast(errText(err), true);
    }
  }

  // ---- Capture: a picture of the waveforms (as shown: zoom, selection), saved where the user picks ----

  function showCaptureMenu(on) {
    if (!els || !els.capMenu) return;
    els.capMenu.hidden = !on;
    els.capBtn.setAttribute('aria-expanded', String(!!on));
    if (on) {
      const first = els.capMenu.querySelector('button:not(:disabled):not([hidden])');
      if (first) first.focus();
    }
  }

  const CAPTURE_LABEL = { source: 'Original', recording: 'After the plugin', both: 'Original and after the plugin' };

  async function captureWave(which) {
    showCaptureMenu(false);
    if (!wv || !file) return;
    let blob;
    try {
      blob = await wv.snapshot(which);
    } catch (err) {
      V.toast(`Couldn't capture the waveforms: ${errText(err)}`, true);
      return;
    }
    await savePicture(blob, CAPTURE_LABEL[which], which !== 'source');
  }

  // Saves a captured picture: Windows' save dialog (suggested `<file> - <what> (<plugin>).png`; the plugin
  // is named when `withPlugin` and one is open), then the bytes go to the backend, which writes them.
  async function savePicture(blob, what, withPlugin = false) {
    const base = ((file && file.name) || 'Audio').replace(/\.[^.]+$/, '');
    const plugin = withPlugin && selectedId && isOpen() ? ` (${pluginName(selectedId)})` : '';
    const name = `${base} - ${what}${plugin}`.replace(/[<>:"/\\|?*]+/g, ' ');
    let path = null;
    const dialog = tauri() && tauri().dialog;
    if (dialog && typeof dialog.save === 'function') {
      try {
        path = await dialog.save({ title: 'Save the picture', defaultPath: `${name}.png`,
          filters: [{ name: 'PNG picture', extensions: ['png'] }] });
        if (!path) return; // cancelled
      } catch {
        path = null; // this build can't show a save dialog: it goes to Pictures\VEXT
      }
    }
    const q = new URLSearchParams({ name });
    if (path) q.set('path', path);
    try {
      const r = await V.api(`/api/playground/audio/picture?${q}`, {
        method: 'POST', headers: { 'Content-Type': 'image/png' }, body: blob,
      });
      V.toast(`Saved: ${r.path}`);
    } catch (err) {
      V.toast(errText(err), true);
    }
  }

  async function saveRecording() {
    if (!hasRecording()) return;
    const base = ((file && file.name) || 'Recording').replace(/\.[^.]+$/, '');
    const plugin = selectedId && isOpen() ? pluginName(selectedId) : '';
    const suggested = `${base} (${plugin || 'processed'}).wav`.replace(/[<>:"/\\|?*]+/g, ' ');
    let path = null;
    const dialog = tauri() && tauri().dialog;
    if (dialog && typeof dialog.save === 'function') {
      try {
        path = await dialog.save({ title: 'Save the recording', defaultPath: suggested,
          filters: [{ name: 'WAV audio', extensions: ['wav'] }] });
        if (!path) return; // cancelled
      } catch {
        path = null; // this build can't show a save dialog: it goes to Music\VEXT
      }
    }
    try {
      const r = await V.api('/api/playground/audio/recording/save', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ path }),
      });
      V.toast(`Saved: ${r.path}`);
    } catch (err) {
      V.toast(errText(err), true);
    }
  }

  // ---------- file transport ----------

  // The worker takes commands in order, so of two quick plays the later one wins.
  async function startPlayback(at) {
    if (!file || playBlocker()) return;
    pageHold = null;
    if (media && media.playing) pauseBrowserMedia(); // Browser and Audio never play together
    if (!pluginTakesFile()) { await (comparing() ? playCompare(at) : playInPage('source', at)); return; }
    stopRecEl();
    playsInFlight++;
    wantPlay = true;
    routed = null;
    cursor = Math.max(0, at || 0);
    const r = loopOn ? loopRange() : null;
    try {
      const u = untilRange(cursor);
      // The recording plays along into the plugin (the backend leaves it out while Record is on).
      const mix = comparing() ? { with_recording: true, mute_source: muteSrc, mute_recording: muteRec } : {};
      const st = await command('/play', { which: 'source', start_s: round3(cursor), ...(r ? { loop: r } : {}), ...(u ? { until: u } : {}), ...mix });
      if (!st) wantPlay = false;
      else startTick();
    } finally {
      playsInFlight--;
    }
  }

  async function resumePlayback() {
    if (media && media.playing) pauseBrowserMedia(); // Browser and Audio never play together
    if (pageHold) return resumeInPage();
    wantPlay = true;
    await command('/resume');
  }

  // Pause keeps the playhead where it is (and the start point Stop goes back to).
  async function pausePlayback() {
    if (recElPlaying() || (cmpEl && !cmpEl.paused)) {
      pageHold = { which: recElWhich || 'source', compare: !!cmpEl && !cmpEl.paused };
      if (recEl) cursor = recEl.currentTime;
      if (recEl) recEl.pause();
      if (cmpEl) cmpEl.pause();
      renderDeck();
      return;
    }
    if (phase() === 'playing') {
      wantPlay = false; // paused is not "stopped": a plugin change must not restart it
      await command('/pause');
    }
  }

  // Goes on from where the page's players were paused.
  function resumeInPage() {
    const { which, compare } = pageHold;
    pageHold = null;
    return compare ? playCompare(cursor) : playInPage(which, cursor);
  }

  // back: where the playhead goes (default the selection's start, else the file's).
  async function stopPlayback(back) {
    wantPlay = false;
    pageHold = null;
    ending = true;
    if (recElPlaying()) { recEl.pause(); recEl.currentTime = 0; }
    if (cmpEl) cmpEl.pause();
    try {
      if (audioActive() || (host && host.recording)) await command('/stop');
    } finally {
      ending = false;
    }
    cursor = back != null ? back : (sel ? sel.a : 0);
    renderDeck();
    loadAudio();
  }

  // The Audio tab's Play button and Space: starts from the start point (the selection's start, else the
  // playhead); pressed while playing it pauses, pressed while paused it goes on from there. Only Stop
  // (the square) goes back to the start point. A recording through the plugin is the exception: it ends.
  function onPlayPause() {
    if (isPaused()) return resumePlayback();
    if (host && host.recording && (audioActive() || recElPlaying())) return stopPlayback(playFrom);
    if (recElPlaying() || phase() === 'playing' || (cmpEl && !cmpEl.paused)) return pausePlayback();
    const why = playBlocker();
    if (why) { notice = why; renderDeck(); return; }
    playFrom = sel ? sel.a : cursor;
    return startPlayback(playFrom);
  }

  // The Analyzer's Play / Stop button keeps its old meaning: playing, it stops back at the start point.
  function onPlayStop() {
    if (isPaused()) return resumePlayback();
    if (recElPlaying() || audioActive() || (cmpEl && !cmpEl.paused)) return stopPlayback(playFrom);
    const why = playBlocker();
    if (why) { notice = why; renderDeck(); return; }
    playFrom = sel ? sel.a : cursor;
    return startPlayback(playFrom);
  }

  // A selection plays to its end: then it stops back at the start point (or, looping, starts over).
  function atSelectionEnd(t) {
    if (!sel || sel.b - sel.a < 0.01 || t < sel.b || t > sel.b + 0.5 || endStopping || (host && host.recording)) return;
    if (playFrom < sel.a - 0.001 || playFrom >= sel.b) return; // started outside it: plays on
    if (loopOn && loopAll) return; // the whole file repeats: the selection's end is nothing special
    if (loopOn) {
      if (audioActive()) return; // the plugin's stream loops it itself
      if (recElPlaying()) { recEl.currentTime = sel.a; if (cmpEl && !cmpEl.paused) cmpEl.currentTime = sel.a; }
      return;
    }
    endStopping = true;
    stopPlayback(playFrom).finally(() => { endStopping = false; });
  }

  async function seekTo(t) {
    const d = duration();
    t = Math.max(0, d ? Math.min(t, Math.max(0, d - 0.001)) : t);
    if (recElPlaying()) { recEl.currentTime = t; if (cmpEl && !cmpEl.paused) cmpEl.currentTime = t; return; }
    if (audioActive()) {
      await command('/seek', { seconds: round3(t) });
    } else {
      cursor = t;
      renderDeck();
    }
  }

  // The playhead and the time follow the sound, once per frame while something plays.
  function startTick() {
    if (!tickRaf) tickRaf = requestAnimationFrame(tick);
  }

  function tick() {
    tickRaf = 0;
    if (!els || !(active || keepOn) || source !== 'file') return;
    let playing = (host && audioActive()) || recElPlaying();
    let t = position();
    if (playing) {
      // The selection's end can stop the in-page playback right here, which moves the
      // playhead back to the start point: read both again so the stale end-of-selection time isn't drawn.
      atSelectionEnd(t);
      playing = (host && audioActive()) || recElPlaying();
      t = position();
    }
    const heard = playing && (running() || recElPlaying());
    if (wv) wv.setPlayhead(file ? t : null, heard);
    els.cur.textContent = fmtTimeMs(file ? t : null);
    if (playing) tickRaf = requestAnimationFrame(tick);
  }

  // ---------- the browser's player (the Analyzer's timeline and Play / Pause) ----------

  function applyMedia(m) {
    if (!m || typeof m !== 'object' || !('has' in m)) return;
    // Another video (or page): the timeline's selection and loop were for the one before.
    const key = `${m.title || ''}|${Math.round(num(m.d) || 0)}`;
    if (media && media.has && m.has && key !== `${media.title}|${Math.round(media.d)}`) {
      bsel = null;
      if (bLoop === 'sel') { bLoop = 'off'; bLoopAuto = false; }
    }
    media = { has: !!m.has, playing: !!m.playing, t: num(m.t) || 0, d: num(m.d) || 0,
      loop: m.loop === 'all' || Array.isArray(m.loop) ? m.loop : null, title: m.title || '' };
    mediaAt = performance.now();
  }
  async function refreshMedia() {
    mediaAskAt = performance.now();
    try { applyMedia(await V.api('/api/browser/media')); } catch { /* backend busy: asked again */ }
  }
  // Asked on demand (the timeline draws, the Analyzer's button), at most every MEDIA_ASK_MS.
  function askMedia() {
    if (hasShell() && performance.now() - mediaAskAt >= MEDIA_ASK_MS) refreshMedia();
  }
  // Seconds into the browser's media that are playing now (moved on since the last report while it plays).
  function mediaPosition() {
    if (!media) return 0;
    const t = media.t + (media.playing ? (performance.now() - mediaAt) / 1000 : 0);
    return media.d ? Math.min(t, media.d) : t;
  }
  async function mediaPost(path, body) {
    try {
      applyMedia(await V.api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }));
      return true;
    } catch (err) {
      V.toast(errText(err), true);
      return false;
    }
  }
  // What the page's script loops: the timeline's selection, the whole media, or nothing.
  function sendBrowserLoop() {
    const r = bLoop === 'off' ? null : bLoop === 'sel' && bsel ? [round3(bsel.a), round3(bsel.b)] : 'all';
    return mediaPost('/api/browser/media/loop', { range: r });
  }
  // The browser's media pauses (every page): leaving the Browser tab, or the Audio tab starting to play.
  function pauseBrowserMedia() {
    if (!hasShell()) return;
    V.api('/api/browser/audio/pause', { method: 'POST' }).catch(() => { /* nothing plays there */ });
    if (media) media.playing = false;
  }

  // ---------- the web browser ----------

  function applyNav(s) {
    if (!s || typeof s !== 'object') return;
    nav.url = typeof s.url === 'string' ? s.url : nav.url;
    nav.title = typeof s.title === 'string' ? s.title : nav.title;
    nav.can_go_back = !!s.can_go_back;
    nav.can_go_forward = !!s.can_go_forward;
    nav.loading = !!s.loading;
    renderChrome();
    renderDeck();
  }

  async function refreshNav() {
    if (!hasShell()) return;
    try { applyNav(await invoke('browser_state')); } catch { /* not created yet: its events will tell */ }
  }

  onShellEvent('browser://state', applyNav);

  function browse(action, url) {
    if (!hasShell()) return;
    invoke('browser_go', url ? { action, url } : { action }).catch((err) => {
      V.toast('The browser couldn\'t do that: ' + errText(err), true);
    });
  }

  // What the address field holds, as an address to open: a web address as typed
  // (https:// added when there's none), anything else a search.
  function toUrl(text) {
    const s = text.trim();
    if (!s) return null;
    const search = SEARCH_URL + encodeURIComponent(s);
    let candidate = null;
    if (/^https?:\/\//i.test(s)) candidate = s;
    else if (!/\s/.test(s) && /^(localhost|127\.0\.0\.1|\[::1\])(:\d+)?([/?#].*)?$/i.test(s)) candidate = 'http://' + s;
    else if (!/\s/.test(s) && /^([a-z0-9-]+\.)+[a-z]{2,}(:\d+)?([/?#].*)?$/i.test(s)) candidate = 'https://' + s;
    else if (!/\s/.test(s) && /^\d{1,3}(\.\d{1,3}){3}(:\d+)?([/?#].*)?$/.test(s)) candidate = 'http://' + s;
    if (!candidate) return search;
    try {
      const u = new URL(candidate);
      return /^https?:$/.test(u.protocol) ? u.href : search;
    } catch {
      return search;
    }
  }

  // The rectangle the browser covers, in window coordinates (CSS px = logical px:
  // the desktop app doesn't zoom). Hidden while the section is off screen, while
  // an overlay covers it (a context menu opened elsewhere doesn't), or when there's no room for it.
  function wantedRect() {
    const rect = browserArea();
    return rect && !overlayCovers(rect) ? rect : null;
  }

  // Where the browser sits while this section shows it, overlays aside.
  function browserArea() {
    if (!els || !active || source !== 'browser' || !hasShell() || browserError) return null;
    const r = els.view.getBoundingClientRect();
    if (r.width < 40 || r.height < 40) return null;
    return { x: Math.round(r.left), y: Math.round(r.top), width: Math.round(r.width), height: Math.round(r.height) };
  }
  // The context menu places itself beside it when it can (contextmenu.js place), so the page keeps showing.
  V.nativeArea = browserArea;

  const overlayOpen = () => (typeof V.overlayOpen === 'function' ? V.overlayOpen() : false);
  const overlayCovers = (rect) => (typeof V.overlayCovers === 'function' ? V.overlayCovers(rect) : overlayOpen());

  function placeBrowser() {
    cancelAnimationFrame(placeRaf);
    placeRaf = 0;
    if (!hasShell() || browserError) return;
    const r = wantedRect();
    if (r) { lastRect = r; showAsked = true; }
    // Never shown: nothing to hide (the shell creates it on the first show). Once a show
    // is on its way, a hide goes after it, even if that show hasn't finished yet.
    if (!r && !showAsked) return;
    const base = r || lastRect || { x: 0, y: 0, width: 100, height: 100 };
    placeWanted = { ...base, visible: !!r };
    if (!placeBusy) pumpPlace();
  }

  function schedulePlace() {
    if (!placeRaf) placeRaf = requestAnimationFrame(placeBrowser);
  }

  // One call at a time, newest wins: a show must never land before an older hide.
  async function pumpPlace() {
    placeBusy = true;
    try {
      while (placeWanted) {
        const rect = placeWanted;
        placeWanted = null;
        const key = JSON.stringify(rect);
        if (key === placeSent) continue;
        try {
          await invoke('browser_place', rect);
          placeSent = key;
          if (rect.visible && !browserShown) {
            browserShown = true;
            renderView();
            refreshNav();
          }
        } catch (err) {
          placeSent = '';
          placeWanted = null;
          browserError = errText(err);
          renderView();
        }
      }
    } finally {
      placeBusy = false;
    }
  }

  const resizeWatch = typeof ResizeObserver === 'function' ? new ResizeObserver(() => schedulePlace()) : null;
  window.addEventListener('resize', () => { if (active) schedulePlace(); });
  // An overlay hides the browser right away (it is drawn already); closing it shows the browser again.
  document.addEventListener('vext:overlay', (e) => {
    if (!active) return;
    if (e.detail && e.detail.open) placeBrowser();
    else schedulePlace();
  });

  // ---------- rendering ----------

  function setHtml(key, el, html) {
    if (last[key] === html) return;
    last[key] = html;
    el.innerHTML = html;
  }

  function renderAll() {
    renderChrome();
    renderView();
    renderDeck();
    renderAudio();
    renderList();
    renderCheck();
  }

  function renderChrome() {
    if (!els) return;
    const shell = hasShell();
    els.back.disabled = !shell || !nav.can_go_back;
    els.forward.disabled = !shell || !nav.can_go_forward;
    els.reload.disabled = !shell;
    // While a page loads, Reload stops it instead (like any browser).
    const stopping = shell && nav.loading;
    if (els.reload.dataset.nav !== (stopping ? 'stop' : 'reload')) {
      els.reload.dataset.nav = stopping ? 'stop' : 'reload';
      els.reload.innerHTML = stopping ? ICON.stopLoad : ICON.reload;
      const label = stopping ? 'Stop loading' : 'Reload';
      els.reload.setAttribute('aria-label', label);
      els.reload.title = label;
    }
    els.home.disabled = !shell;
    els.url.disabled = !shell;
    if (!addressDirty || document.activeElement !== els.url) {
      addressDirty = false;
      if (els.url.value !== nav.url) els.url.value = nav.url;
    }
    els.url.title = nav.url;
    els.title.textContent = nav.title;
    els.title.title = nav.title;
    els.title.hidden = !nav.title;
    els.chrome.classList.toggle('is-loading', shell && nav.loading);
    els.chrome.setAttribute('aria-busy', String(shell && nav.loading));
    for (const b of els.layoutBtns) {
      const on = b.dataset.layout === layout;
      b.setAttribute('aria-checked', String(on));
      b.classList.toggle('active', on);
    }
  }

  function renderView() {
    if (!els) return;
    let html = '';
    if (!hasShell()) {
      html = `
        <div class="pg-view-msg">
          <strong>The web browser needs the VEXT desktop app.</strong>
          <span>In a browser tab like this one, web pages can't play through a plugin. You can still pick a plugin
            (its window opens on your screen) and play an audio file through it.</span>
        </div>`;
    } else if (browserError) {
      html = `
        <div class="pg-view-msg">
          <div class="pg-error">The web browser couldn't open: ${esc(browserError)}</div>
          <button type="button" data-act="retry-browser">Try again</button>
        </div>`;
    } else if (!browserShown) {
      html = '<div class="pg-view-msg is-quiet"><span class="spinner" aria-hidden="true"></span><span>Opening the web browser…</span></div>';
    }
    setHtml('view', els.view, html);
  }

  function sessionHtml() {
    if (!selectedId) return '';
    const name = esc(pluginName(selectedId));
    const ph = phase();
    if (ph === 'starting' || awaitingEditor()) {
      return `<span class="pg-chip"><span class="spinner" aria-hidden="true"></span><span class="pg-chip-name">Opening ${name}…</span></span>`;
    }
    if (ph === 'closed') return '';
    const where = hasShell() && host.editor_hwnd ? '' : ' title="Its window is open on your screen"';
    return `<span class="pg-chip"${where}><span class="pg-dot is-on" aria-hidden="true"></span>` +
      `<span class="pg-chip-name">${name}</span>` +
      `<button type="button" class="pg-chip-x" data-act="close" aria-label="Close ${name}" title="Close the plugin">${ICON.close}</button></span>`;
  }

  // What the Browser source line says, depending on the plugin.
  function browserNote() {
    if (!selectedId) return 'Pick a plugin, then play something on this page: you hear it through the plugin.';
    const ph = phase();
    if (ph === 'starting') return 'The page\'s sound goes through the plugin as soon as it\'s open.';
    if (ph === 'closed') return 'The plugin isn\'t open: the page plays as usual.';
    if (host.is_instrument) {
      return source === 'file'
        ? 'This plugin is an instrument: play it with the keys below or a MIDI keyboard (a file can\'t play through it).'
        : 'This plugin is an instrument: play it with the keys below or a MIDI keyboard. The page plays as usual.';
    }
    if (host.source_kind !== 'browser') {
      return routed === selectedId && !notice && !host.error
        ? 'Connecting the page\'s sound to the plugin…'
        : 'The page plays as usual: its sound isn\'t going through the plugin.';
    }
    if (!hearingPage()) return 'Play something on this page: you hear it through the plugin.';
    return 'What plays on this page goes through the plugin.';
  }

  const hearingPage = () => !!host && host.source_kind === 'browser' && performance.now() - soundAt < SOUND_HOLD_MS;

  // The Plugin buffer readout while sound plays through the plugin (host.audio from the worker's reports):
  // the buffer's target, the plugin's CPU load, and the dropouts so far.
  function bufferText() {
    if (!host || !host.audio || host.audio.target_ms == null) return '';
    const a = host.audio;
    const parts = [`Buffer ${Math.round(Number(a.target_ms))} ms`];
    if (a.load_pct != null) parts.push(`CPU ${Math.round(Number(a.load_pct))} %`);
    const drops = (Number(a.starves) || 0) + (Number(a.xruns) || 0);
    if (drops > 0) parts.push(`${drops} dropout${drops === 1 ? '' : 's'}`);
    return parts.join(' · ');
  }

  // Above 100 % CPU for two reports in a row: the plugin can't keep up at any buffer size.
  function overloadHtml() {
    if (overloads < 2 || !selectedId) return '';
    return `<span class="pg-buffer-warn">${esc(pluginName(selectedId))} needs more CPU than real time at this buffer size: `
      + 'it will stutter whatever the Plugin buffer. Raise Buffer size in Configuration › Audio.</span>';
  }

  function renderDeck() {
    if (!els) return;
    const shell = hasShell();
    for (const b of els.srcBtns) {
      if (b.dataset.src === 'browser') {
        b.disabled = !shell;
        b.title = shell ? 'A web browser: what plays on the page goes through the plugin' : 'Needs the VEXT desktop app';
      }
    }
    els.deck.classList.toggle('is-file', source === 'file');
    els.browserNote.textContent = browserNote();
    setHtml('session', els.session, sessionHtml());
    const buffer = bufferText();
    setHtml('buffer', els.buffer, buffer ? esc(buffer) : '');
    els.buffer.hidden = !buffer;

    renderBrec();
    renderTransport();
    renderStatus();
    renderMidi();
    renderRecordTools();
    updateListMarks();
  }

  function renderTransport() {
    if (!els) return;
    const p = phase();
    const isRunning = running() || recElPlaying();
    const blocker = playBlocker();
    const d = duration();

    // The main button pauses and resumes; the square is Stop's alone. While a recording through the
    // plugin runs the button still ends it (a pause would leave a gap in what is recorded).
    const play = els.play;
    const recordingNow = !!(host && host.recording);
    const paused = isPaused();
    play.disabled = !isRunning && !audioActive(p) && !paused && !!blocker;
    const label = recordingNow ? 'Stop' : isRunning ? 'Pause' : paused ? 'Resume' : 'Play';
    if (play.dataset.icon !== label) {
      play.dataset.icon = label;
      play.innerHTML = label === 'Stop' ? ICON.stop : label === 'Pause' ? ICON.pause : ICON.play;
      play.setAttribute('aria-label', label);
    }
    play.title = play.disabled ? blocker
      : recordingNow ? 'Stop recording, back to the start point (Space)'
        : isRunning ? 'Pause (Space)'
          : paused ? 'Resume from here (Space)' : `Play${sel ? ' the selection' : ''} (Space)`;
    els.stop.disabled = !audioActive(p) && !recElPlaying() && cursor === 0 && !pageHold && !recordingNow;
    els.stop.title = 'Stop, back to the start point';
    els.loop.setAttribute('aria-pressed', String(loopOn && !loopAll));
    els.loop.disabled = !file;
    els.loop.title = loopOn && !loopAll ? `Loop on: ${sel ? 'the selection' : 'the whole file'} repeats`
      : 'Loop the selection (or the whole file)';
    els.loopAll.setAttribute('aria-pressed', String(loopAll));
    els.loopAll.disabled = !file;
    els.loopAll.title = loopAll ? 'Loop All on: the whole file repeats, whatever is selected' : 'Loop the whole file, even with a selection';
    els.cur.textContent = fmtTimeMs(file ? position() : null);
    els.dur.textContent = fmtTimeMs(d || null);
    els.selInfo.textContent = sel ? `Selection ${fmtTimeMs(sel.a)} – ${fmtTimeMs(sel.b)} (${fmtTimeMs(sel.b - sel.a)})` : '';
    els.selInfo.hidden = !sel;
    if (!tickRaf && wv) wv.setPlayhead(file ? position() : null, false);
  }

  // What the Audio tab's toolbar and pane headers say.
  function renderAudio() {
    if (!els || !wv) return;
    const st = audio || { status: 'empty' };
    const info = st.info || null;
    const ready = !!file;
    els.afileName.textContent = st.status === 'empty' ? 'No file' : (st.name || 'Audio file');
    els.afileName.title = st.path || '';
    els.afileName.classList.toggle('is-empty', st.status === 'empty');
    let meta = '';
    if (st.status === 'decoding') meta = `Decoding… ${Math.round((st.progress || 0) * 100)}%`;
    else if (st.status === 'error') meta = 'Couldn\'t decode it';
    else if (info) {
      const ch = info.channels === 1 ? 'mono' : 'stereo';
      const src = info.source_channels > 2 ? ` (from ${info.source_channels} channels)` : '';
      const fmt = (info.codec || info.format || '').replace(/\s*\(.*\)\s*$/, '');
      meta = [fmtTimeMs(info.duration_s), `${(info.rate / 1000).toLocaleString('en-US')} kHz`, ch + src, fmt].filter(Boolean).join(' · ');
    }
    els.afileMeta.textContent = meta;
    els.afileMeta.title = info ? [info.format, info.codec, info.bit_rate ? `${Math.round(info.bit_rate / 1000)} kbps` : ''].filter(Boolean).join(' · ') : '';
    els.choose.lastElementChild.textContent = ready || st.status === 'decoding' ? 'Change…' : 'Open…';
    const tools = !ready || editBusy;
    for (const b of els.editBtns) b.disabled = tools;
    els.undo.disabled = tools || !(st.can_undo);
    els.fade.disabled = !ready;
    for (const b of els.zoomBtns) b.disabled = !ready;
    els.capBtn.disabled = !ready;
    if (!ready) showCaptureMenu(false);
    for (const b of els.capMenu.querySelectorAll('[data-act="capture-recording"], [data-act="capture-both"]')) {
      b.disabled = !recInfo().exists;
      b.title = recInfo().exists ? '' : 'Nothing recorded yet';
    }
    const range = sel ? 'the selection' : null;
    els.fadeIn.title = `Fade in: ${range || `the first ${fadeLen} s`}`;
    els.fadeOut.title = `Fade out: ${range || `the last ${fadeLen} s`}`;
    els.normalize.title = `Normalize ${range || 'the whole file'} to −1 dBFS`;

    // Top pane: the file.
    const top = wv.head('source');
    top.label.textContent = 'Original';
    const edits = (st.edits || []).map(e => (e.type === 'fade_in' ? 'fade in' : e.type === 'fade_out' ? 'fade out' : 'normalize'));
    top.info.textContent = !ready ? '' : edits.length ? `Edited: ${edits.join(', ')} (the file on disk is untouched)` : '';
    if (st.status === 'decoding') {
      wv.message('source', `<span class="spinner" aria-hidden="true"></span><span>Decoding ${esc(st.name || 'the file')}… ${Math.round((st.progress || 0) * 100)}%</span>`);
    } else if (st.status === 'error') {
      wv.message('source', `<div class="pg-error">${esc(st.error || 'FFmpeg couldn\'t read that file.')}</div><button type="button" data-act="choose">Open another file…</button>`);
    } else if (!ready) {
      wv.message('source', `<strong>Drop an audio file here</strong><span>or <button type="button" class="pg-linkbtn" data-act="choose">open one</button>: any format FFmpeg reads (WAV, MP3, FLAC, AAC/M4A, Ogg, Opus, AIFF, WMA…). One file at a time; the file itself is never changed.</span>`);
    } else {
      wv.message('source', '');
    }

    // Bottom pane: the recording.
    const bottom = wv.head('recording');
    bottom.label.textContent = 'After the plugin';
    const rec = recInfo();
    bottom.info.textContent = rec.exists ? `${(rec.rate / 1000).toLocaleString('en-US')} kHz · recorded at the plugin's output` : '';
    if (!ready) wv.message('recording', '');
    else if (!rec.exists && host && host.recording) wv.message('recording', '<span class="pg-rec-dot" aria-hidden="true"></span><span>Recording starts with the sound…</span>');
    else if (!rec.exists) {
      wv.message('recording', '<span>Press <strong>Record</strong>: the file plays through the plugin and what comes out is drawn here, on the same time line as the original.</span>');
    } else wv.message('recording', '');
    renderRecordTools();
  }

  // The recording pane's buttons: Record, Play (as recorded), Save, Clear.
  function renderRecordTools() {
    if (!els || !wv) return;
    const tools = wv.head('recording').tools;
    const rec = recInfo();
    const armed = !!(host && host.recording);
    const blocker = recordBlocker();
    const playingRec = playingWhich() === 'recording' && !isPaused();
    const muteBtn = (act, on, what) => `<button type="button" class="pg-rbtn pg-mute${on ? ' is-on' : ''}" data-act="${act}" aria-pressed="${on}"
        title="${on ? `Unmute the ${what}` : `Mute the ${what}`} (Play plays the original, the recording, or both: into the open plugin, else here to compare)">M</button>`;
    // The original's own Remove, like the recording's Clear below.
    const hasFile = !!audio && audio.status !== 'empty';
    const removeBtn = `<button type="button" class="pg-rbtn" data-act="src-clear" title="Remove the file from the Audio tab (the file on disk is not touched)"
        aria-label="Remove the file"${hasFile && !armed ? '' : ' disabled'}>${ICON.trash}</button>`;
    setHtml('srcTools', wv.head('source').tools, (rec.exists && !armed ? muteBtn('mute-src', muteSrc, 'original') : '') + removeBtn);
    const html = (rec.exists && !armed ? muteBtn('mute-rec', muteRec, 'recording') : '') + `
      <button type="button" class="pg-rbtn pg-rec${armed ? ' is-on' : ''}" data-act="record" aria-pressed="${armed}"
        title="${esc(armed ? 'Stop recording' : blocker || 'Record what comes out of the plugin while the file plays')}"${!armed && blocker ? ' disabled' : ''}>${ICON.record}<span>${armed ? 'Recording' : 'Record'}</span></button>
      <button type="button" class="pg-rbtn" data-act="rec-play" title="${playingRec ? 'Pause the recording' : 'Play the recording as recorded (without the plugin)'}"
        aria-label="${playingRec ? 'Pause the recording' : 'Play the recording'}"${rec.exists && !armed ? '' : ' disabled'}>${playingRec ? ICON.pause : ICON.play}</button>
      <button type="button" class="pg-rbtn" data-act="rec-save" title="Save the recording as a WAV file" aria-label="Save the recording"${rec.exists && !armed ? '' : ' disabled'}>${ICON.save}</button>
      <button type="button" class="pg-rbtn" data-act="rec-clear" title="Clear the recording" aria-label="Clear the recording"${rec.exists && !armed ? '' : ' disabled'}>${ICON.trash}</button>`;
    setHtml('recTools', tools, html);
    els.web.classList.toggle('is-recording', armed);
  }

  function renderStatus() {
    if (!els) return;
    const p = phase();
    let html = '';
    if (closedError && p === 'closed' && selectedId) {
      const name = pluginName(selectedId);
      const text = closedError.startsWith(CANT_OPEN)
        ? `Can't open ${name}: ${closedError.slice(CANT_OPEN.length)}`
        : `${name} couldn't stay open: ${closedError}`;
      html = `<div class="pg-error">${esc(text)}</div>${closedRetry ? '<button type="button" data-act="open">Try again</button>' : ''}`
        + '<button type="button" data-act="show-log">Details in Log</button>';
    } else if (host && host.state === 'starting' && host.dialog && host.message) {
      html = `<span class="pg-notice">${esc(host.message)}</span>`;
    } else if (host && host.state !== 'closed' && host.error && !audible()) {
      html = `<div class="pg-error">${esc(host.error)}</div>`;
    } else if (notice || (frameError && isOpen())) {
      html = `<span class="pg-notice">${esc(notice || frameError)}</span>`;
    } else if (source === 'file' && file && playBlocker() && p !== 'starting' && !(host && host.is_instrument)) {
      html = `<span>${esc(playBlocker())}</span>`;
    } else if (host && host.state !== 'closed' && hostNote(host)) {
      html = `<span>${esc(hostNote(host))}</span>`;
    }
    const warn = overloadHtml();
    if (warn) html = warn + html;
    els.status.hidden = !html;
    setHtml('status', els.status, html);
  }

  // variant 'backdrop': the picture's tiny blurred copy, shown behind it (scaled up) so the
  // whole picture fits whatever its aspect ratio (see .plugin-pic in style.css).
  function imageUrl(p, variant = '') {
    const q = new URLSearchParams({ ...(variant ? { variant } : {}), ...(imgStamp[p.id] ? { t: imgStamp[p.id] } : {}) });
    return `${V.API_BASE || ''}/api/plugins/${p.id}/image${String(q) ? `?${q}` : ''}`;
  }

  function initial(name) {
    const m = String(name || '').match(/[A-Za-z0-9]/);
    return m ? m[0].toUpperCase() : '?';
  }

  const isInstrumentType = (p) => /^(synth|instrument)/i.test(p.plugin_type || '');

  // "Opening…" / "Open" on the selected plugin's row.
  function stateTag(id) {
    if (id !== selectedId || !host) return '';
    if (host.state === 'starting' || awaitingEditor()) return '<span class="tag pg-state-tag is-busy" title="Opening…">Opening…</span>';
    if (host.state === 'open') return '<span class="tag pg-state-tag" title="Open">Open</span>';
    return '';
  }

  function pluginHtml(p) {
    const sel = p.id === selectedId;
    const cats = (p.categories || []).slice(0, 3).join(', ');
    const meta = [p.vendor || 'Unknown vendor', cats].filter(Boolean).join(' · ');
    const thumb = p.has_image
      ? `<span class="pg-thumb" style="--pic-backdrop: url('${esc(imageUrl(p, 'backdrop'))}')"><img src="${esc(imageUrl(p))}" alt="" loading="lazy" decoding="async"></span>`
      : `<span class="pg-thumb is-empty" aria-hidden="true">${esc(initial(p.name))}</span>`;
    return `
      <div class="pg-plugin${sel ? ' is-selected' : ''}" data-plugin-id="${p.id}">
        <button type="button" class="pg-plugin-pick" data-pick="${p.id}" aria-pressed="${sel}" title="${esc(p.name)}">
          ${thumb}
          <span class="pg-plugin-text">
            <span class="pg-plugin-name">${esc(p.name)}</span>
            <span class="pg-plugin-meta">${esc(meta)}</span>
          </span>
          <span class="pg-plugin-tags">${isInstrumentType(p) ? '<span class="tag">Instrument</span>' : ''}<span class="pg-state">${stateTag(p.id)}</span></span>
        </button>
        <button type="button" class="row-menu-btn" data-plugin-id="${p.id}" aria-label="Actions" title="Actions">⋯</button>
      </div>`;
  }

  // Selection and state marks, updated in place: rebuilding hundreds of tiles
  // (and their pictures) just to move a highlight would flicker.
  function updateListMarks() {
    if (!els) return;
    for (const row of els.list.querySelectorAll('.pg-plugin[data-plugin-id]')) {
      const id = Number(row.dataset.pluginId);
      const sel = id === selectedId;
      row.classList.toggle('is-selected', sel);
      const pick = row.querySelector('.pg-plugin-pick');
      if (pick) pick.setAttribute('aria-pressed', String(sel));
      const state = row.querySelector('.pg-state');
      const tag = stateTag(id);
      if (state && state.innerHTML !== tag) state.innerHTML = tag;
    }
    last.list = null; // the DOM no longer matches the last string written
  }

  function pluginMatches(p, words) {
    if (!words.length) return true;
    const hay = [p.name, p.vendor, p.plugin_type, ...(p.categories || [])].join(' ').toLowerCase();
    return words.every(w => hay.includes(w));
  }

  // The catalog's plugins the Playground can't open, and why. Reasons that hold for a whole
  // format (FL Studio's own plugins) are one line each; the rest are listed one by one, with
  // Try again when a load attempt failed (the cause may be gone: a DLL installed, a license activated).
  function unavailableHtml(words) {
    if (!unavailable.length || pluginsState === 'error') return '';
    const groups = new Map();
    const rows = [];
    for (const p of unavailable) {
      const issue = p.host_issue || {};
      if (issue.code === 'format') groups.set(issue.reason, (groups.get(issue.reason) || 0) + 1);
      else if (pluginMatches(p, words)) rows.push(p);
    }
    const lines = words.length ? '' : [...groups].map(([reason, n]) =>
      `<div class="pg-unavail-group">${n} ${n === 1 ? 'plugin' : 'plugins'}: ${esc(reason)}.</div>`).join('');
    const items = rows.map(p => `
      <div class="pg-unavail-item" data-plugin-id="${p.id}">
        <span class="pg-plugin-text">
          <span class="pg-plugin-name" title="${esc(p.name)}">${esc(p.name)}</span>
          <span class="pg-unavail-reason">${esc([p.vendor, p.host_issue.reason].filter(Boolean).join(' · '))}</span>
        </span>
        ${canRetry(p) ? `<button type="button" data-pick="${p.id}" title="${p.host_issue.at ? `Last try ${esc(new Date(p.host_issue.at).toLocaleString())}` : 'Open it anyway'}">Try again</button>` : ''}
        <button type="button" class="row-menu-btn" data-plugin-id="${p.id}" aria-label="Actions" title="Actions">⋯</button>
      </div>`).join('');
    if (!lines && !items) return '';
    return `
      <details class="pg-unavail"${unavailOpen ? ' open' : ''}>
        <summary>Can't open here <span class="pg-count">${unavailable.length}</span></summary>
        ${lines}${items}
      </details>`;
  }

  function renderList() {
    if (!els) return;
    const words = filterText.toLowerCase().split(/\s+/).filter(Boolean);
    const shown = plugins.filter(p => pluginMatches(p, words));
    let html;
    if (pluginsState === 'loading' && !plugins.length) {
      html = '<div class="pg-empty">Loading…</div>';
    } else if (pluginsState === 'error') {
      html = `<div class="pg-empty"><div class="pg-error">Could not load the plugins: ${esc(pluginsError)}</div>
        <button type="button" data-act="retry-plugins">Try again</button></div>`;
    } else if (!plugins.length) {
      html = `<div class="pg-empty">No plugins can be tested yet. The Playground opens 64-bit VST3, VST2, CLAP and Waves plugins; scan to find them.
        <div><button type="button" data-act="go-scan">Go to Scan →</button></div></div>`;
    } else if (!shown.length) {
      html = `<div class="pg-empty">No plugins match “${esc(filterText.trim())}”.</div>`;
    } else {
      html = shown.map(pluginHtml).join('');
    }
    html += unavailableHtml(words);
    setHtml('list', els.list, html);
    els.count.textContent = plugins.length
      ? (shown.length === plugins.length ? String(plugins.length) : `${shown.length} of ${plugins.length}`)
      : '';
  }

  // The plugins' own view (Small images | Tiles), chosen in the plugins header (VEXT's
  // shared selector, app.js) and kept apart from the other sections' views.
  function applyView() {
    if (!els) return;
    const grid = typeof V.view === 'function' && V.view('playground') === 'grid';
    els.list.classList.toggle('is-grid', grid);
  }

  function applyLayout() {
    if (!els) return;
    els.pg.classList.toggle('is-side', layout === 'side');
    els.pg.classList.toggle('is-stacked', layout === 'stacked');
    renderChrome();
    schedulePlace();
  }

  function setLayout(next) {
    if (next !== 'side' && next !== 'stacked') return;
    layout = next;
    try { localStorage.setItem(LAYOUT_KEY, layout); } catch { /* storage unavailable */ }
    applyLayout();
  }

  // ---------- MIDI: instruments, the on-screen keys, the typing keyboard ----------
  // An open instrument renders all the time (the backend starts its sound as soon
  // as it has loaded), so it plays from a MIDI keyboard (Configuration > Audio >
  // MIDI input), from the keys drawn here, or from the computer keyboard ("Typing
  // keys", like a DAW's typing keyboard: Z S X D C … one octave, Q 2 W 3 E … the
  // next, - and = shift the octave). Notes go to POST /host/midi one request at
  // a time, so a quick tap's note-off never overtakes its note-on. Effects that
  // take MIDI (VST2/CLAP with a note input) show the light and can open the keys.

  const TYPING_KEY = 'pa-pg-typing';
  const OCTAVE_KEY = 'pa-pg-octave';
  const KEY_MIN_W = 22;            // narrowest white key (px): how many octaves fit
  const MAX_OCTAVES = 4;
  const TYPING = {
    KeyZ: 0, KeyS: 1, KeyX: 2, KeyD: 3, KeyC: 4, KeyV: 5, KeyG: 6, KeyB: 7, KeyH: 8, KeyN: 9, KeyJ: 10, KeyM: 11,
    Comma: 12, KeyL: 13, Period: 14, Semicolon: 15, Slash: 16,
    KeyQ: 12, Digit2: 13, KeyW: 14, Digit3: 15, KeyE: 16, KeyR: 17, Digit5: 18, KeyT: 19, Digit6: 20, KeyY: 21,
    Digit7: 22, KeyU: 23, KeyI: 24, Digit9: 25, KeyO: 26, Digit0: 27, KeyP: 28,
  };
  const BLACK = new Set([1, 3, 6, 8, 10]);

  let typing = (() => { try { return localStorage.getItem(TYPING_KEY) === '1'; } catch { return false; } })();
  let baseOctave = (() => {
    try {
      const raw = localStorage.getItem(OCTAVE_KEY);
      const v = raw == null || raw === '' ? NaN : Number(raw);
      return Number.isInteger(v) && v >= 0 && v <= 7 ? v : 3;
    } catch { return 3; }
  })();
  let keysOpen = null;             // effects that take MIDI: keys shown on request (instruments: always)
  let octaves = 2;                 // octaves drawn (as many as fit, up to MAX_OCTAVES)
  let keysBuilt = '';              // "base:octaves" the drawn keys are for
  const held = new Map();          // note -> Set of what holds it (pointer ids, key codes)
  const pointers = new Map();      // pointerId -> note under it
  const typedKeys = new Map();     // KeyboardEvent.code -> note
  let midiQueue = [];
  let midiSending = false;
  let midiCount = -1;              // host.midi.count at the last poll
  let litUntil = 0;
  let litTimer = null;

  function midiShown() { return isOpen() && !!host.accepts_midi; }
  function keysShown() { return midiShown() && (host.is_instrument ? keysOpen !== false : keysOpen === true); }
  const noteName = (n) => `${['C', 'C♯', 'D', 'D♯', 'E', 'F', 'F♯', 'G', 'G♯', 'A', 'A♯', 'B'][n % 12]}${Math.floor(n / 12) - 1}`;
  const baseNote = () => (baseOctave + 1) * 12;

  function sendMidi(events) {
    if (!selectedId || !isOpen()) return;
    midiQueue.push(...events);
    pumpMidi();
  }

  async function pumpMidi() {
    if (midiSending) return;
    midiSending = true;
    try {
      while (midiQueue.length) {
        const id = selectedId;
        const batch = midiQueue.splice(0, 64);
        if (!id || !isOpen()) { midiQueue = []; break; }
        try {
          await hostRequest(id, '/midi', 'POST', { events: batch });
        } catch (err) {
          if (id === selectedId && err && err.status === 409) { midiQueue = []; notice = errText(err); renderDeck(); }
        }
      }
    } finally {
      midiSending = false;
    }
  }

  function flash(ms = 160) {
    litUntil = Math.max(litUntil, performance.now() + ms);
    paintActivity();
  }

  function noteOn(note, velocity, by) {
    if (note < 0 || note > 127) return;
    let who = held.get(note);
    if (!who) { who = new Set(); held.set(note, who); }
    const first = who.size === 0;
    who.add(by);
    if (first) {
      sendMidi([[0x90, note, Math.max(1, Math.min(127, Math.round(velocity)))]]);
      markKey(note, true);
      flash();
    }
  }

  function noteOff(note, by) {
    const who = held.get(note);
    if (!who || !who.delete(by)) return;
    if (who.size) return;
    held.delete(note);
    sendMidi([[0x80, note, 0]]);
    markKey(note, false);
    flash(80);
  }

  // Every note this page holds is let go (leaving the section, the window losing focus).
  function releaseAll() {
    const notes = [...held.keys()];
    held.clear();
    pointers.clear();
    typedKeys.clear();
    if (notes.length) sendMidi(notes.map(n => [0x80, n, 0]));
    for (const n of notes) markKey(n, false);
  }

  // The plugin is gone (or about to be): forget what was held, send nothing.
  function midiReset() {
    held.clear();
    pointers.clear();
    typedKeys.clear();
    midiQueue = [];
    midiCount = -1;
    keysOpen = null;
    if (els && els.midi) for (const k of els.midi.querySelectorAll('.pg-key.is-down')) k.classList.remove('is-down');
  }

  async function panicNow() {
    const id = selectedId;
    held.clear(); pointers.clear(); typedKeys.clear(); midiQueue = [];
    if (els) for (const k of els.midi.querySelectorAll('.pg-key.is-down')) k.classList.remove('is-down');
    if (id && isOpen()) await command('/panic');
  }

  async function soundOn() {
    await command('/sound', { on: true });
  }

  function shiftOctave(delta) {
    const next = Math.max(0, Math.min(8 - octaves, baseOctave + delta));
    if (next === baseOctave) return;
    releaseAll();
    baseOctave = next;
    try { localStorage.setItem(OCTAVE_KEY, String(baseOctave)); } catch { /* storage unavailable */ }
    renderMidi();
  }

  function setTyping(on) {
    typing = on;
    try { localStorage.setItem(TYPING_KEY, on ? '1' : '0'); } catch { /* storage unavailable */ }
    if (!on) for (const [code, note] of [...typedKeys]) { typedKeys.delete(code); noteOff(note, `k:${code}`); }
    renderMidi();
  }

  function markKey(note, down) {
    const key = els && els.midi && els.midi.querySelector(`.pg-key[data-note="${note}"]`);
    if (key) key.classList.toggle('is-down', down);
  }

  // The keys: white keys share the width; black keys sit on the boundaries.
  function buildKeys(box) {
    const first = baseNote();
    const last = Math.min(127, first + octaves * 12);
    const whites = [];
    const blacks = [];
    for (let n = first; n <= last; n++) (BLACK.has(n % 12) ? blacks : whites).push(n);
    let html = '';
    whites.forEach((n, i) => {
      const label = n % 12 === 0 ? `<span class="pg-key-label">${noteName(n)}</span>` : '';
      const cls = `${held.has(n) ? ' is-down' : ''}${i === whites.length - 1 ? ' is-last' : ''}`;
      html += `<div class="pg-key is-white${cls}" data-note="${n}" title="${noteName(n)}">${label}</div>`;
    });
    for (const n of blacks) {
      const before = whites.filter(w => w < n).length;
      html += `<div class="pg-key is-black${held.has(n) ? ' is-down' : ''}" data-note="${n}" title="${noteName(n)}" ` +
        `style="left: calc(${before} * var(--pg-white) - var(--pg-black-w) / 2)"></div>`;
    }
    box.style.setProperty('--pg-whites', String(whites.length));
    box.innerHTML = html;
  }

  function midiStatus() {
    const m = host.midi || {};
    const inputs = m.inputs || null;
    if (host.sound_off) return { text: host.sound_off, warn: true, soundOn: true };
    if (host.is_instrument && host.source_kind !== 'instrument') {
      return { text: host.error ? 'Its sound isn\'t playing.' : 'Starting its sound…', warn: !!host.error, soundOn: !!host.error };
    }
    const failed = inputs && inputs.failed && inputs.failed.length
      ? ` ${inputs.failed.map(f => `${f.id}: ${f.error}`).join('; ')}.` : '';
    let text;
    if (!inputs) text = 'Opening the MIDI inputs…';
    else if (inputs.open.length) text = `MIDI: ${inputs.open.join(', ')}.`;
    else text = 'No MIDI keyboard is open (Configuration › MIDI).';
    if (!host.is_instrument) text = `It takes MIDI while sound plays through it. ${text}`;
    return { text: text + failed, warn: !!failed, soundOn: false };
  }

  function renderMidi() {
    if (!els || !els.midi) return;
    const shown = midiShown();
    els.deck.classList.toggle('is-instrument', isOpen() && !!host.is_instrument);
    els.midi.hidden = !shown;
    if (!shown) { setHtml('midi', els.midi, ''); keysBuilt = ''; return; }
    const st = midiStatus();
    const keys = keysShown();
    const btn = (act, label, title, extra = '') =>
      `<button type="button" class="pg-midi-btn" data-midi-act="${act}" title="${esc(title)}"${extra}>${label}</button>`;
    const html = `
      <div class="pg-midi-bar">
        <span class="pg-midi-led" title="MIDI activity" aria-hidden="true"></span>
        <span class="pg-midi-meter" title="Output level" aria-hidden="true"><span class="pg-midi-level"></span></span>
        <span class="pg-midi-text${st.warn ? ' is-warn' : ''}">${esc(st.text)}</span>
        ${st.soundOn ? btn('sound-on', 'Turn sound on', 'Start the instrument\'s sound (it may crash again)') : ''}
        <span class="pg-midi-tools">
          ${keys ? `<span class="pg-oct">
            ${btn('oct-down', '‹', 'Octave down (−)', ` aria-label="Octave down"${baseOctave <= 0 ? ' disabled' : ''}`)}
            <span class="pg-oct-label" title="Lowest key">${noteName(baseNote())}</span>
            ${btn('oct-up', '›', 'Octave up (=)', ` aria-label="Octave up"${baseOctave >= 8 - octaves ? ' disabled' : ''}`)}
          </span>
          ${btn('typing', 'Typing keys', 'Play with the computer keyboard: Z S X D C… and Q 2 W 3 E…; − and = change the octave',
            ` aria-pressed="${typing}"`)}` : ''}
          ${host.is_instrument ? '' : btn('keys', keys ? 'Hide keys' : 'Keys', 'Show the on-screen keys')}
          ${btn('panic', 'Panic', 'All notes off')}
        </span>
      </div>
      <div class="pg-keys" role="group" aria-label="On-screen keyboard"${keys ? '' : ' hidden'}></div>`;
    if (last.midi !== html) {
      setHtml('midi', els.midi, html);
      keysBuilt = '';
    }
    if (keys) fitKeys();
    paintActivity();
  }

  // As many octaves as fit the deck's width; rebuilt only when that or the octave changes.
  function fitKeys() {
    const box = els.midi.querySelector('.pg-keys');
    if (!box) return;
    const width = box.clientWidth || els.midi.clientWidth || 0;
    const fit = Math.max(1, Math.min(MAX_OCTAVES, Math.floor(width / (7 * KEY_MIN_W)) || 1));
    if (fit !== octaves) {
      octaves = fit;
      if (baseOctave > 8 - octaves) baseOctave = 8 - octaves;
      setHtml('midi', els.midi, ''); // the octave buttons' limits changed
      last.midi = null;
      renderMidi();
      return;
    }
    const key = `${baseOctave}:${octaves}`;
    if (keysBuilt !== key) {
      keysBuilt = key;
      buildKeys(box);
    }
  }

  // The light: on while notes are held here or by the plugin, and briefly for each message.
  function paintActivity() {
    if (!els || !els.midi || els.midi.hidden) return;
    const m = (host && host.midi) || {};
    if (typeof m.count === 'number') {
      if (midiCount >= 0 && m.count > midiCount) litUntil = Math.max(litUntil, performance.now() + 300);
      midiCount = m.count;
    }
    const lit = held.size > 0 || (m.held || 0) > 0 || performance.now() < litUntil;
    const led = els.midi.querySelector('.pg-midi-led');
    if (led) led.classList.toggle('is-lit', lit);
    const level = els.midi.querySelector('.pg-midi-level');
    if (level) {
      const peak = host && host.audio ? Number(host.audio.peak_out) || 0 : 0;
      const pct = peak > 0 ? Math.max(0, Math.min(1, (20 * Math.log10(peak) + 60) / 60)) : 0;
      level.style.width = `${Math.round(pct * 100)}%`;
    }
    clearTimeout(litTimer);
    if (performance.now() < litUntil) litTimer = setTimeout(paintActivity, litUntil - performance.now() + 10);
  }

  const typingTarget = (t) => t instanceof Element && (t.isContentEditable
    || (t.matches('input, textarea, select') && !t.matches('input[type="checkbox"], input[type="radio"], input[type="range"]')));

  document.addEventListener('keydown', (e) => {
    if (!typing || !active || !keysShown() || e.ctrlKey || e.metaKey || e.altKey || typingTarget(e.target)) return;
    if (typeof V.overlayOpen === 'function' && V.overlayOpen()) return;
    if (e.code === 'Minus' || e.code === 'Equal') {
      e.preventDefault();
      if (!e.repeat) shiftOctave(e.code === 'Equal' ? 1 : -1);
      return;
    }
    const offset = TYPING[e.code];
    if (offset == null) return;
    e.preventDefault();
    if (e.repeat || typedKeys.has(e.code)) return;
    const note = baseNote() + offset;
    if (note > 127) return;
    typedKeys.set(e.code, note);
    noteOn(note, 100, `k:${e.code}`);
  });
  document.addEventListener('keyup', (e) => {
    const note = typedKeys.get(e.code);
    if (note == null) return;
    typedKeys.delete(e.code);
    noteOff(note, `k:${e.code}`);
  });
  window.addEventListener('blur', () => { if (held.size) releaseAll(); });
  document.addEventListener('visibilitychange', () => { if (document.hidden && held.size) releaseAll(); });

  function bindMidi() {
    const box = els.midi;
    const keyAt = (x, y) => {
      const el = document.elementFromPoint(x, y);
      const key = el && el.closest && el.closest('.pg-key');
      return key && box.contains(key) ? Number(key.dataset.note) : null;
    };
    // Top of a key = soft, bottom = loud.
    const velocityAt = (e, note) => {
      const key = box.querySelector(`.pg-key[data-note="${note}"]`);
      if (!key) return 100;
      const r = key.getBoundingClientRect();
      return 40 + 87 * Math.max(0, Math.min(1, (e.clientY - r.top) / (r.height || 1)));
    };
    box.addEventListener('pointerdown', (e) => {
      const key = e.target.closest && e.target.closest('.pg-key');
      if (!key || e.button > 0) return;
      e.preventDefault();
      const note = Number(key.dataset.note);
      try { key.parentElement.setPointerCapture(e.pointerId); } catch { /* the pointer is gone already */ }
      pointers.set(e.pointerId, note);
      noteOn(note, velocityAt(e, note), `p:${e.pointerId}`);
    });
    box.addEventListener('pointermove', (e) => {
      if (!pointers.has(e.pointerId)) return;
      const was = pointers.get(e.pointerId);
      const now = keyAt(e.clientX, e.clientY);
      if (now === was) return;
      if (was != null) noteOff(was, `p:${e.pointerId}`);
      if (now != null) noteOn(now, velocityAt(e, now), `p:${e.pointerId}`);
      pointers.set(e.pointerId, now);
    });
    const up = (e) => {
      if (!pointers.has(e.pointerId)) return;
      const note = pointers.get(e.pointerId);
      pointers.delete(e.pointerId);
      if (note != null) noteOff(note, `p:${e.pointerId}`);
    };
    box.addEventListener('pointerup', up);
    box.addEventListener('pointercancel', up);
    box.addEventListener('lostpointercapture', up);
    box.addEventListener('contextmenu', (e) => { if (e.target.closest && e.target.closest('.pg-key')) e.preventDefault(); });
    box.addEventListener('click', (e) => {
      const btn = e.target.closest && e.target.closest('[data-midi-act]');
      if (!btn || btn.disabled) return;
      switch (btn.dataset.midiAct) {
        case 'oct-down': shiftOctave(-1); break;
        case 'oct-up': shiftOctave(1); break;
        case 'typing': setTyping(!typing); break;
        case 'keys': keysOpen = !keysShown(); if (!keysOpen) releaseAll(); renderMidi(); break;
        case 'panic': panicNow(); break;
        case 'sound-on': soundOn(); break;
        default: break;
      }
    });
    if (typeof ResizeObserver === 'function') new ResizeObserver(() => { if (keysShown()) fitKeys(); }).observe(box);
  }

  // ---------- building and events ----------

  function build(el) {
    root = el;
    Object.keys(last).forEach(k => delete last[k]);
    const navBtn = (act, label) =>
      `<button type="button" class="pg-nav" data-nav="${act}" aria-label="${label}" title="${label}">${ICON[act]}</button>`;
    const tool = (act, icon, label, cls = '') =>
      `<button type="button" class="pg-nav${cls}" data-act="${act}" aria-label="${label}" title="${label}">${ICON[icon]}</button>`;
    el.innerHTML = `
      <div class="pg${hasShell() ? '' : ' no-shell'}">
        <section class="pg-web" aria-label="Sound to play through the plugin">
          <div class="pg-tabs">
            <div class="pg-tablist" role="tablist" aria-label="Sound to play through the plugin">
              <button type="button" role="tab" class="pg-tab" data-src="browser" id="pg-tab-browser" aria-controls="pg-panel-browser">${ICON.globe}<span>Browser</span></button>
              <button type="button" role="tab" class="pg-tab" data-src="file" id="pg-tab-audio" aria-controls="pg-panel-audio"
                title="An audio file: play it through the plugin and record what comes out">${ICON.wave}<span>Audio</span></button>
              <button type="button" role="tab" class="pg-tab" data-src="signal" id="pg-tab-signal" aria-controls="pg-panel-signal"
                title="Test signals (oscillators, wavetables, noise, sweeps, bursts…): play them through the plugin and record what comes out">${ICON.signal}<span>Signal</span></button>
            </div>
            <div class="pg-layouts" role="radiogroup" aria-label="Layout">
              <button type="button" role="radio" data-layout="side" aria-label="Plugins on the side" title="Plugins on the side">${ICON.side}</button>
              <button type="button" role="radio" data-layout="stacked" aria-label="Plugins below" title="Plugins below">${ICON.stacked}</button>
            </div>
          </div>
          <div class="pg-panel pg-panel-browser" id="pg-panel-browser" role="tabpanel" aria-labelledby="pg-tab-browser">
            <div class="pg-chrome">
              ${navBtn('back', 'Back')}${navBtn('forward', 'Forward')}${navBtn('reload', 'Reload')}${navBtn('home', 'Home')}
              <form class="pg-address" role="search">
                <input type="text" class="pg-url" placeholder="Search, or type a web address" aria-label="Address or search"
                  autocomplete="off" spellcheck="false" enterkeyhint="go">
              </form>
              <span class="pg-page-title" hidden></span>
              <div class="pg-progress" aria-hidden="true"></div>
            </div>
            <div class="pg-view"></div>
          </div>
          <div class="pg-panel pg-panel-audio" id="pg-panel-audio" role="tabpanel" aria-labelledby="pg-tab-audio" hidden>
            <div class="pg-chrome pg-abar">
              <button type="button" class="pg-open" data-act="choose" title="Open an audio file (or drop one on the waveform)">${ICON.open}<span>Open…</span></button>
              <span class="pg-afile">
                <span class="pg-afile-name is-empty">No file</span>
                <span class="pg-afile-meta"></span>
              </span>
              <span class="pg-atools" role="group" aria-label="Edit">
                ${tool('fade-in', 'fadeIn', 'Fade in', ' pg-edit')}${tool('fade-out', 'fadeOut', 'Fade out', ' pg-edit')}
                <select class="pg-fade-len" aria-label="Fade length when nothing is selected" title="Fade length when nothing is selected">
                  ${FADE_LENGTHS.map(v => `<option value="${v}"${v === fadeLen ? ' selected' : ''}>${v} s</option>`).join('')}
                </select>
                ${tool('normalize', 'normalize', 'Normalize', ' pg-edit')}${tool('undo', 'undo', 'Undo the last edit')}
              </span>
              <span class="pg-azoom" role="group" aria-label="Zoom">
                ${tool('zoom-out', 'zoomOut', 'Zoom out (−)', ' pg-zoom')}${tool('zoom-in', 'zoomIn', 'Zoom in (+)', ' pg-zoom')}${tool('zoom-fit', 'zoomFit', 'Show the whole file', ' pg-zoom')}
              </span>
              <span class="pg-acap">
                <button type="button" class="pg-capbtn" data-act="capture" aria-haspopup="menu" aria-expanded="false"
                  title="Capture the waveforms as they are shown and save the picture">${ICON.capture}<span>Capture</span></button>
                <div class="pg-capmenu" role="menu" aria-label="Capture and save" hidden>
                  <button type="button" role="menuitem" data-act="capture-source">Original</button>
                  <button type="button" role="menuitem" data-act="capture-recording">After the plugin</button>
                  <button type="button" role="menuitem" data-act="capture-both">Both</button>
                </div>
              </span>
            </div>
            <div class="pg-wave"></div>
          </div>
          <div class="pg-panel pg-panel-signal" id="pg-panel-signal" role="tabpanel" aria-labelledby="pg-tab-signal" hidden>
            <div class="pg-signal"></div>
            <div class="pg-signal-wave"></div>
          </div>
          <div class="pg-deck">
            <div class="pg-deck-row">
              <span class="pg-src-browser pg-browser-note"></span>
              <span class="pg-brec"></span>
              <span class="pg-src-file pg-transport">
                <button type="button" class="primary pg-tbtn" data-act="play" aria-label="Play">${ICON.play}</button>
                <button type="button" class="pg-tbtn" data-act="stop" aria-label="Stop" title="Stop">${ICON.stop}</button>
                <button type="button" class="pg-tbtn pg-loop" data-act="loop" aria-label="Loop" aria-pressed="false">${ICON.loop}</button>
                <button type="button" class="pg-tbtn pg-loop pg-loopall" data-act="loop-all" aria-label="Loop all" aria-pressed="false">All</button>
                <span class="pg-times"><span class="pg-time pg-cur">–:––.–––</span><span class="pg-time-sep">/</span><span class="pg-time pg-dur">–:––.–––</span></span>
                <span class="pg-sel-info" hidden></span>
              </span>
              <span class="pg-deck-end">
                <span class="pg-buffer" hidden title="Plugin buffer: how far ahead of the sound the plugin runs. CPU: the plugin's processing time as a share of real time."></span>
                <span class="pg-session"></span>
              </span>
            </div>
            <div class="pg-midi" hidden></div>
            <form class="pg-path pg-src-file" hidden>
              <input type="text" class="pg-path-input" placeholder="Full path of an audio file, e.g. C:\\Music\\song.wav"
                aria-label="Full path of an audio file" autocomplete="off" spellcheck="false">
              <button type="submit">Open</button>
            </form>
            <div class="pg-status" role="status" aria-live="polite" hidden></div>
          </div>
        </section>
        <section class="pg-plugins" aria-labelledby="pg-plugins-h">
          <div class="pg-plugins-head">
            <div class="pg-plugins-title">
              <h3 id="pg-plugins-h">Plugins <span class="pg-count"></span></h3>
              <span class="pg-check"></span>
            </div>
            <input type="text" class="pg-filter" placeholder="Filter by name, vendor or category" aria-label="Filter plugins" autocomplete="off" spellcheck="false">
            ${typeof V.viewToggleHtml === 'function' ? V.viewToggleHtml('playground') : ''}
          </div>
          <div class="pg-list"></div>
        </section>
      </div>`;
    const $ = (sel) => el.querySelector(sel);
    els = {
      pg: $('.pg'), web: $('.pg-web'), chrome: $('.pg-panel-browser .pg-chrome'), address: $('.pg-address'), url: $('.pg-url'),
      title: $('.pg-page-title'),
      back: $('[data-nav="back"]'), forward: $('[data-nav="forward"]'), reload: $('[data-nav="reload"]'), home: $('[data-nav="home"]'),
      layoutBtns: [...el.querySelectorAll('[data-layout]')],
      browserPanel: $('.pg-panel-browser'), audioPanel: $('.pg-panel-audio'),
      signalPanel: $('.pg-panel-signal'), signal: $('.pg-signal'), signalWave: $('.pg-signal-wave'),
      view: $('.pg-view'), deck: $('.pg-deck'), srcBtns: [...el.querySelectorAll('.pg-tab[data-src]')],
      browserNote: $('.pg-browser-note'), brec: $('.pg-brec'), choose: $('.pg-open'),
      afileName: $('.pg-afile-name'), afileMeta: $('.pg-afile-meta'),
      editBtns: [...el.querySelectorAll('.pg-edit')], undo: $('[data-act="undo"]'), fade: $('.pg-fade-len'),
      fadeIn: $('[data-act="fade-in"]'), fadeOut: $('[data-act="fade-out"]'), normalize: $('[data-act="normalize"]'),
      zoomBtns: [...el.querySelectorAll('.pg-zoom')], wave: $('.pg-wave'),
      capBtn: $('.pg-capbtn'), capMenu: $('.pg-capmenu'),
      pathForm: $('.pg-path'), pathInput: $('.pg-path-input'),
      session: $('.pg-session'), buffer: $('.pg-buffer'),
      play: $('.pg-transport [data-act="play"]'), stop: $('.pg-transport [data-act="stop"]'), loop: $('.pg-loop:not(.pg-loopall)'), loopAll: $('.pg-loopall'),
      cur: $('.pg-cur'), dur: $('.pg-dur'), selInfo: $('.pg-sel-info'),
      status: $('.pg-status'),
      filter: $('.pg-filter'), list: $('.pg-list'), count: $('.pg-count'), check: $('.pg-check'),
      midi: $('.pg-midi'),
    };
    els.filter.value = filterText;
    if (file && file.path) els.pathInput.value = '';
    // A click on the waveform puts the playhead (and the start point) there.
    wv = V.WaveView ? V.WaveView.create(els.wave, {
      onSeek: (t) => { cursor = t; playFrom = t; seekTo(t); startTick(); renderTransport(); },
      onSelect: (s) => onSelect(s),
    }) : null;
    if (wv) {
      if (sel) wv.setSelection(sel);
      wv.setLoop(loopOn);
    }
    if (resizeWatch) resizeWatch.observe(els.view);
    bind(el);
    bindMidi();
  }

  function bind(el) {
    els.address.addEventListener('submit', (e) => {
      e.preventDefault();
      const url = toUrl(els.url.value);
      if (!url) return;
      addressDirty = false;
      nav.url = url;
      els.url.blur();
      renderChrome();
      browse('go', url);
    });
    els.url.addEventListener('input', () => { addressDirty = true; });
    // The first click selects the whole address (so typing replaces it); the mouseup
    // that follows would otherwise drop the selection again. Later clicks place the caret.
    let selectOnUp = false;
    els.url.addEventListener('mousedown', () => { selectOnUp = document.activeElement !== els.url; });
    els.url.addEventListener('mouseup', (e) => {
      if (!selectOnUp) return;
      selectOnUp = false;
      if (els.url.selectionStart === els.url.selectionEnd) { e.preventDefault(); els.url.select(); }
    });
    els.url.addEventListener('focus', () => els.url.select());
    els.url.addEventListener('keydown', (e) => {
      if (e.key !== 'Escape') return;
      addressDirty = false;
      els.url.value = nav.url;
      els.url.blur();
    });
    els.url.addEventListener('blur', () => { if (!addressDirty) renderChrome(); });

    els.pathForm.addEventListener('submit', (e) => {
      e.preventDefault();
      openAudio(els.pathInput.value);
    });
    els.fade.addEventListener('change', () => {
      const v = Number(els.fade.value);
      if (!FADE_LENGTHS.includes(v)) return;
      fadeLen = v;
      try { localStorage.setItem(FADE_KEY, String(v)); } catch { /* storage unavailable */ }
      renderAudio();
      els.fade.blur(); // a focused list would keep Space for itself
    });
    // Tabs: arrow keys move between them (the WAI tabs pattern).
    el.querySelector('.pg-tablist').addEventListener('keydown', (e) => {
      if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
      const tabs = els.srcBtns.filter(b => !b.disabled);
      const i = tabs.indexOf(document.activeElement);
      if (i < 0) return;
      e.preventDefault();
      const next = tabs[(i + (e.key === 'ArrowRight' ? 1 : tabs.length - 1)) % tabs.length];
      next.focus();
      setSource(next.dataset.src);
    });
    // A file dropped on the Audio tab without the desktop app (the app's own drops: see onShellEvent below).
    els.audioPanel.addEventListener('dragover', (e) => {
      if (!e.dataTransfer || ![...(e.dataTransfer.types || [])].includes('Files')) return;
      e.preventDefault();
      els.audioPanel.classList.add('is-drop');
    });
    els.audioPanel.addEventListener('dragleave', (e) => {
      if (!els.audioPanel.contains(e.relatedTarget)) els.audioPanel.classList.remove('is-drop');
    });
    els.audioPanel.addEventListener('drop', (e) => {
      els.audioPanel.classList.remove('is-drop');
      const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      if (!f) return;
      e.preventDefault();
      uploadAudio(f);
    });

    els.filter.addEventListener('input', () => {
      filterText = els.filter.value;
      // The results glide to their new places (js/fx.js; instant with visual effects off).
      if (V.fx && typeof V.fx.flip === 'function') V.fx.flip(els.list, renderList); else renderList();
    });
    // <details> state survives the list being rewritten ('toggle' doesn't bubble).
    el.addEventListener('toggle', (e) => {
      if (e.target instanceof HTMLDetailsElement && e.target.classList.contains('pg-unavail')) unavailOpen = e.target.open;
    }, true);

    el.addEventListener('click', (e) => {
      const t = e.target instanceof Element ? e.target : null;
      if (!t || t.closest('.row-menu-btn')) return;
      // A mouse click leaves the button focused, where Space would press it again instead of playing:
      // let go of it once the click has been handled. (Keyboard clicks have detail 0 and keep their focus.)
      if (e.detail > 0) {
        queueMicrotask(() => {
          const b = t.closest('button, [role="tab"], input[type="radio"], input[type="checkbox"]');
          if (b && document.activeElement === b) b.blur();
        });
      }
      const navBtn = t.closest('[data-nav]');
      if (navBtn) { if (!navBtn.disabled) browse(navBtn.dataset.nav); return; }
      const lay = t.closest('[data-layout]');
      if (lay) { setLayout(lay.dataset.layout); return; }
      const src = t.closest('[data-src]');
      if (src) { if (!src.disabled) setSource(src.dataset.src); return; }
      const pick = t.closest('[data-pick]');
      if (pick) { selectPlugin(pick.dataset.pick); return; }
      const act = t.closest('[data-act]');
      if (!act || act.disabled) return;
      switch (act.dataset.act) {
        case 'play': onPlayPause(); break;
        case 'stop': stopPlayback(); break;
        case 'loop': setLoop(!(loopOn && !loopAll)); break;
        case 'loop-all': setLoop(!loopAll, false, !loopAll); break;
        case 'choose': chooseFile(); break;
        case 'fade-in': edit('fade_in'); break;
        case 'fade-out': edit('fade_out'); break;
        case 'normalize': edit('normalize'); break;
        case 'undo': undoEdit(); break;
        case 'zoom-in': if (wv) wv.zoom('in'); break;
        case 'zoom-out': if (wv) wv.zoom('out'); break;
        case 'zoom-fit': if (wv) wv.zoom('fit'); break;
        case 'record': toggleRecord(); break;
        case 'brec': if (brec.on) stopBrowserRecord(); else startBrowserRecord(); break;
        case 'rec-play': playRecording(); break;
        case 'mute-src': muteSrc = !muteSrc; applyMutes(); renderRecordTools(); break;
        case 'mute-rec': muteRec = !muteRec; applyMutes(); renderRecordTools(); break;
        case 'rec-save': saveRecording(); break;
        case 'rec-clear': clearRecording(); break;
        case 'src-clear': removeFile(); break;
        case 'capture': showCaptureMenu(els.capMenu.hidden); break;
        case 'capture-source': captureWave('source'); break;
        case 'capture-recording': captureWave('recording'); break;
        case 'capture-both': captureWave('both'); break;
        case 'open': openSelected(); break;
        case 'show-log': V.navigate('logs'); break;
        case 'close': closeSelected(); break;
        case 'retry-browser': browserError = ''; placeSent = ''; renderView(); schedulePlace(); break;
        case 'retry-plugins': loadPlugins(); break;
        case 'go-scan': V.navigate('scan'); break;
        case 'check': startCheck(); break;
        case 'check-cancel': cancelCheck(); break;
        default: break;
      }
    });

    // A picture that can't be loaded (deleted since the list came) falls back to the initial.
    el.addEventListener('error', (e) => {
      const img = e.target;
      if (!(img instanceof HTMLImageElement)) return;
      const thumb = img.closest('.pg-thumb');
      if (!thumb) { img.remove(); return; }
      const row = img.closest('[data-plugin-id]');
      const p = row && pluginById(Number(row.dataset.pluginId));
      if (p) p.has_image = false;
      thumb.classList.add('is-empty');
      thumb.style.removeProperty('--pic-backdrop');
      thumb.textContent = initial(p && p.name);
      last.list = null;
    }, true);
  }

  // The Capture menu closes on a click elsewhere or Escape.
  document.addEventListener('pointerdown', (e) => {
    if (!els || !els.capMenu || els.capMenu.hidden) return;
    if (!(e.target instanceof Element) || !e.target.closest('.pg-acap')) showCaptureMenu(false);
  }, true);
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape' || !els || !els.capMenu || els.capMenu.hidden) return;
    e.preventDefault();
    e.stopPropagation();
    showCaptureMenu(false);
    els.capBtn.focus();
  }, true);

  // Space pressed in the plugin's editor: the worker keeps it from the plugin and reports it, at once
  // through the frame (plugin-frame://space, {seq}) and in the host state (space_seq) when polled.
  // Each press is acted on once, whichever arrives first. `act` false (polled state): the first
  // number seen in a session is only noted (its presses came before this page was looking).
  function onEditorSpace(seq, act = true) {
    if (seq == null) { spaceSeq = null; return; }
    if (spaceSeq == null || seq < spaceSeq) { spaceSeq = act ? seq - 1 : seq; } // a new session
    if (seq <= spaceSeq) return;
    spaceSeq = seq;
    editorSpace();
  }
  // Space reached the plugin's editor (or its frame's bar) instead of VEXT's page: it plays and pauses the
  // Audio tab here, and whatever the Analyzer's button controls while the Analyzer shows.
  function editorSpace() {
    if (overlayOpen()) return;
    if (active && source === 'file') onPlayPause();
    else if (!active && typeof V.currentSection === 'function' && V.currentSection() === 'analyzer' && V.playground.canPlay()) {
      V.playground.playPause();
    }
  }
  onShellEvent('plugin-frame://space', (payload) => {
    const seq = payload && num(payload.seq);
    if (seq) onEditorSpace(seq);
    else editorSpace(); // the frame's own bar had the keyboard
  });

  // Space plays and pauses (it resumes from where it paused, else plays from the start point) in the
  // Audio tab, wherever the focus is except where Space is for typing or for the open list or menu. A
  // button the mouse (or Tab) left focused doesn't keep it: Space used to press that button again.
  const spaceIsOurs = (e) => {
    if (e.code !== 'Space' || !active || source !== 'file' || e.ctrlKey || e.metaKey || e.altKey) return false;
    const t = e.target instanceof Element ? e.target : null;
    // The Signal tab's knobs are sliders that leave Space alone (a dragged knob keeps the focus).
    if (typingTarget(t) || (t && t.closest('a, [role="slider"]:not(.ak), .pg-capmenu'))) return false;
    return !overlayOpen() && !(typing && keysShown());
  };
  document.addEventListener('keydown', (e) => {
    if (!spaceIsOurs(e)) return;
    e.preventDefault();
    if (!e.repeat) onPlayPause();
  });
  // A button clicks when Space comes back up: that must not happen either.
  document.addEventListener('keyup', (e) => {
    if (spaceIsOurs(e)) e.preventDefault();
  }, true);

  // Files dropped on the desktop app's window (the shell gives their paths): the first opens in the Audio tab.
  const dropPos = (payload) => {
    const p = payload && payload.position;
    if (!p) return null;
    const scale = window.devicePixelRatio || 1;
    return { x: p.x / scale, y: p.y / scale };
  };
  onShellEvent('tauri://drag-enter', (payload) => {
    if (!active || !els) return;
    if (payload && Array.isArray(payload.paths) && payload.paths.length) els.web.classList.add('is-drop-target');
  });
  onShellEvent('tauri://drag-over', (payload) => {
    if (!active || !els) return;
    const pos = dropPos(payload);
    const r = els.web.getBoundingClientRect();
    els.web.classList.toggle('is-drop', !!pos && pos.x >= r.left && pos.x <= r.right && pos.y >= r.top && pos.y <= r.bottom);
  });
  onShellEvent('tauri://drag-leave', () => {
    if (els) els.web.classList.remove('is-drop', 'is-drop-target');
  });
  onShellEvent('tauri://drag-drop', (payload) => {
    if (els) els.web.classList.remove('is-drop', 'is-drop-target');
    if (!active || !els || overlayOpen()) return;
    const paths = payload && Array.isArray(payload.paths) ? payload.paths : [];
    if (!paths.length) return;
    const pos = dropPos(payload);
    const r = els.web.getBoundingClientRect();
    if (pos && !(pos.x >= r.left && pos.x <= r.right && pos.y >= r.top && pos.y <= r.bottom)) return;
    if (paths.length > 1) V.toast('One file at a time: opening the first one.');
    openAudio(String(paths[0]));
  });

  document.addEventListener('vext:viewchange', applyView);

  // ---------- section lifecycle ----------

  async function load(el, params) {
    params = params || {};
    active = true;
    keepOn = false;              // back from the Analyzer (or first time): the section polls and ticks on its own again
    clearInterval(holdTimer);
    holdTimer = null;
    if (el !== root || !els || !el.contains(els.list)) build(el);
    applyView();
    applyLayout();
    renderAll();
    applyTab();
    loadAudio();
    loadDecoding();
    brecResume();
    clearInterval(placeTimer);
    placeTimer = setInterval(placeBrowser, PLACE_CHECK_MS);
    refreshNav();
    const want = Number(params.pluginId) || null;
    pollCheck(); // a check started earlier (maybe before leaving) shows its progress
    await loadPlugins();
    if (!active) return;
    if (want && want !== selectedId) {
      const other = unavailableById(want);
      if (pluginsState === 'ready' && !pluginById(want) && !canRetry(other)) {
        V.toast(other ? `${other.name} can't be opened here: ${other.host_issue.reason}.`
          : 'That plugin can\'t be opened in the Playground.', true);
      } else {
        selectPlugin(want);
        return;
      }
    } else if (want && phase() === 'closed') {
      openSelected();
      return;
    }
    if (selectedId) schedulePoll(0);
  }

  // `next`: the section being shown instead. The Analyzer shows (and controls, see V.playground below)
  // what plays here, so going there pauses nothing: the file, the page players and the browser recording
  // go on, and the host is still polled. Any other section: the file pauses and the recording stops.
  function unload(next) {
    releaseAll(); // keys held while leaving would hang (the instrument keeps running)
    active = false;
    clearInterval(placeTimer);
    placeTimer = null;
    placeBrowser(); // hidden now, not on the next frame
    clearTimeout(checkTimer); // a running check goes on; its progress is fetched again on return
    checkTimer = null;
    if (next === 'analyzer') { startHold(); return; }
    keepOn = false;
    awayFully();
  }

  // The part of leaving that the Analyzer postpones.
  function awayFully() {
    if (!(phase() === 'starting' || awaitingEditor())) stopPolling();
    clearTimeout(audioTimer);
    audioTimer = null;
    cancelAnimationFrame(tickRaf);
    tickRaf = 0;
    stopRecEl();
    // Leaving pauses the file: nothing would be on screen to stop it from.
    // (A web page goes on playing, like a browser in the background.)
    if (selectedId && running()) {
      epoch++;
      hostRequest(selectedId, '/pause', 'POST').catch(() => {});
      host.paused = true;
    }
    wantPlay = false;
    if (brec.on) stopBrowserRecord();
  }

  // The hold: what plays goes on, and the host is polled, while the Analyzer shows (or its own transport is
  // used, see holdHere()). It ends with the user going to another section, or once nothing has played for
  // QUIET_HOLD_MS while the section is not known.
  function startHold() {
    quietAt = 0;
    keepOn = true;
    clearInterval(holdTimer);
    holdTimer = setInterval(watchKeepOn, HOLD_WATCH_MS);
  }

  // The Analyzer's transport works while the Playground is not on screen: it takes the hold (the Playground's
  // load() ends it again) and the host is polled from here on.
  function holdHere() {
    if (active) return;
    startHold();
    schedulePoll(0);
    scheduleAudioPoll();
  }

  function endHold() {
    keepOn = false;
    clearInterval(holdTimer);
    holdTimer = null;
    awayFully();
  }

  function watchKeepOn() {
    if (!keepOn) { clearInterval(holdTimer); holdTimer = null; return; }
    const now = performance.now();
    if (soundingHere()) quietAt = 0;
    else if (!quietAt) quietAt = now;
    const section = typeof V.currentSection === 'function' ? V.currentSection() : null;
    if (section === 'analyzer' || section === 'playground') return; // the Playground's load() ends the hold itself
    // Another section ends it at once; with the section not known, only once nothing has played for a while.
    if (!section && !(quietAt && now - quietAt >= QUIET_HOLD_MS)) return;
    endHold();
  }

  // ---------- what the Analyzer section uses ----------

  // The host's last report counts while the section or the hold polls it, or when it is recent.
  const hostFresh = () => active || keepOn || performance.now() - hostAt < HOST_FRESH_MS;
  // The file (through the plugin, not an instrument) plays now, by the host's last report.
  const hostPlaying = () => phase() === 'playing' && !host.is_instrument;
  // Something plays here now: in the page, or the file through the plugin.
  const soundingHere = () => pagePlaying() || hostPlaying();

  // The Analyzer asks while nothing polls the host: one poll, at most every KICK_MS, so its answer converges.
  function askHost() {
    if (!active && !keepOn) kickPoll();
    return hostFresh();
  }
  function kickPoll() {
    if (!selectedId || performance.now() - kickAt < KICK_MS) return;
    refreshHost().catch(() => { /* the next question asks again */ });
  }
  // One host poll now. Its answer is applied unless a command (or another plugin) came in meanwhile.
  async function refreshHost() {
    const id = selectedId;
    if (!id) return;
    kickAt = performance.now();
    const e = epoch;
    const sent = performance.now();
    let st = null;
    try { st = await hostRequest(id); } catch { return; } // backend busy: the next question asks again
    if (st && id === selectedId && e === epoch) {
      hostLag = Math.min(0.25, (performance.now() - sent) / 2000);
      applyHost(st);
    }
  }

  // What plays in the Audio tab / through the plugin, in words (V.audioBus' status can't name it).
  function describe() {
    askHost();
    const live = hostFresh();
    const plugin = selectedId && isOpen() ? pluginName(selectedId) : '';
    if (plugin && host.is_instrument) return plugin;
    const through = plugin ? ` through ${plugin}` : '';
    if (source === 'browser' || (host && host.source_kind === 'browser')) return `the browser${through}`;
    if (!file && !(audio && audio.status === 'decoding')) return 'the Audio tab';
    const which = recElPlaying() || live ? playingWhich() : null;
    if (which === 'recording') return 'the recording';
    if ((cmpEl && !cmpEl.paused) || (live && host && host.mix && audioActive())) return `the Audio tab's file and its recording${through}`;
    return `the Audio tab's file${through}`;
  }

  // Whether the file (or the page's sound) plays here now: what the Analyzer's button says.
  const isPlaying = () => {
    askHost();
    return pagePlaying() || (hostPlaying() && hostFresh());
  };

  // The Analyzer's Play / Pause and its timeline act on the tab shown here: the Audio tab's file, or the
  // browser's player.
  const browserTab = () => source === 'browser' && hasShell();
  // The Audio tab's file plays or is paused through the plugin, or in the page.
  const fileActive = () => recElPlaying() || audioActive() || !!pageHold || (!!cmpEl && !cmpEl.paused);

  // ---------- the Signal tab (js/playground-signal.js) ----------
  // The backend renders the designed signal to a WAV and opens it as the Playground's file
  // (POST /api/playground/signal), so it plays through the plugin, loops and records like any file.
  // Rendered again while it plays, it goes on from the same place once decoded (a short gap).
  async function renderSignal(patch) {
    if (host && host.recording) throw new Error('Stop recording before changing the signal.');
    const playing = running() || (!!cmpEl && !cmpEl.paused);
    const at = playing ? position() : 0;
    await beforeNewFile();
    const r = await V.api('/api/playground/signal', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ patch }),
    });
    audioWanted = playing;
    resumeAt = at;
    applyAudio(r.audio);
    return r;
  }

  // What the designer may ask of the Playground.
  const signalHost = {
    render: renderSignal,
    // The loaded file is a rendered signal (rendering again replaces it without asking).
    fileIsSignal: () => !!(audio && /[\\/]signals[\\/]signal-[^\\/]+\.wav$/i.test(audio.path || '')),
    fileName: () => (audio && audio.name) || null,
    hasRecording: () => !!(audio && audio.recording && audio.recording.exists),
    recording: () => !!(host && host.recording),
    playing: () => running() || (!!cmpEl && !cmpEl.paused),
    setLoop: (mode) => { if (file && !(audio && audio.status === 'decoding')) setLoopMode(mode); else pendingLoop = mode; },
    loopMode: () => loopMode(),
  };

  V.playground = {
    hasFile: () => !!file,
    isPlaying,
    tab: () => (browserTab() ? 'browser' : 'file'),
    // Something the Analyzer's Play can start: the file, or a player in the browser.
    canPlay() {
      if (!browserTab()) return !!file;
      askMedia();
      return !!(media && media.has);
    },
    // Whether what the tab shows plays now (the Analyzer's button says Pause then).
    playing() {
      if (!browserTab()) return isPlaying();
      askMedia();
      return !!(media && media.playing);
    },
    // Play / Pause / Resume of the tab shown, like its own button (Space in the Audio tab).
    async playPause() {
      if (browserTab()) {
        await mediaPost('/api/browser/media', { action: 'toggle' });
        return;
      }
      if (!active) {
        holdHere();
        await refreshHost(); // decided on the host as it is now
      }
      if (!file) return;
      await onPlayPause();
      startTick();
    },
    // The Analyzer's timeline: what it shows of the tab, and moving / selecting / looping it.
    timeline() {
      if (browserTab()) {
        askMedia();
        const m = media || { has: false, playing: false, d: 0, title: '' };
        return { kind: 'browser', has: m.has, duration: m.d, position: mediaPosition(), playing: m.playing,
          buffering: false, sel: bsel, loop: bLoop, title: m.title, track: null };
      }
      askHost();
      const ready = !!file && !!audio && !!audio.info;
      return { kind: 'file', has: !!file, duration: duration(), position: file ? position() : 0,
        playing: running() || recElPlaying(), buffering: !!(host && host.buffering && audible()),
        sel, loop: loopMode(), title: file ? file.name : '',
        track: ready ? { ...audio.info, version: audio.version } : null };
    },
    seek(t) {
      if (browserTab()) {
        bsel = null;
        if (bLoop === 'sel' && bLoopAuto) { bLoop = 'off'; bLoopAuto = false; sendBrowserLoop(); }
        return mediaPost('/api/browser/media', { action: 'seek', t: Math.max(0, t) });
      }
      if (!file) return undefined;
      if (!active) holdHere();
      // Like a click on the waveform: the selection goes, the playhead (and the start point) moves there.
      if (wv) wv.setSelection(null);
      if (sel) onSelect(null);
      cursor = t;
      playFrom = t;
      const done = seekTo(t);
      startTick();
      renderTransport();
      return done;
    },
    // A selection (null: none), like dragging on the waveform: making one loops it unless Loop is set already.
    select(next) {
      const s2 = next && next.b - next.a >= 0.01 ? { a: next.a, b: next.b } : null;
      if (browserTab()) {
        const had = !!bsel;
        bsel = s2;
        if (s2 && !had && bLoop === 'off') { bLoop = 'sel'; bLoopAuto = true; }
        else if (!s2 && had && bLoop === 'sel' && bLoopAuto) { bLoop = 'off'; bLoopAuto = false; }
        return sendBrowserLoop();
      }
      if (!file) return undefined;
      if (!active) holdHere();
      if (wv) wv.setSelection(s2);
      onSelect(s2);
      return undefined;
    },
    // 'off' | 'sel' | 'all'
    setLoop(mode) {
      if (browserTab()) {
        bLoop = ['sel', 'all'].includes(mode) ? mode : 'off';
        bLoopAuto = false;
        return sendBrowserLoop();
      }
      if (!file) return undefined;
      if (!active) holdHere();
      return setLoopMode(mode);
    },
    // Play / Stop like the Audio tab's own button (and Space there). Without a file it does nothing.
    // Paused (the Analyzer's Scan pauses it), Play resumes it: from there, it is not a Stop.
    async toggle() {
      if (!active) {
        holdHere();
        await refreshHost(); // decided on the host as it is now
        if (soundingHere()) return undefined; // it plays already: the Analyzer's view was behind
      }
      if (!file) return undefined;
      if (phase() === 'paused') return source === 'file' ? resumePlayback() : startPlayback(position());
      return onPlayStop();
    },
    // Stops whatever plays here, back to the start point.
    stop() {
      if (!active) holdHere();
      return isPlaying() || audioActive() || pageHold ? stopPlayback(playFrom) : undefined;
    },
    describe,
  };

  // The file dialog's formats, once (the backend's list; FFmpeg reads more).
  let decodingAsked = false;
  async function loadDecoding() {
    if (decodingAsked) return;
    decodingAsked = true;
    try {
      const d = await V.api('/api/playground/decoding');
      if (d && Array.isArray(d.extensions) && d.extensions.length) audioExtensions = d.extensions;
    } catch { decodingAsked = false; }
  }

  try { localStorage.removeItem(OLD_ANALYZER_KEY); } catch { /* storage unavailable */ }
  // The file is known before the section is first shown (the Analyzer can be the first one opened).
  loadAudio();

  V.registerSection('playground', { load, unload });
})();
