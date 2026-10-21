// Configuration > Audio, MIDI and Performance (one part per tab): how the Playground plays the plugin being tested
// (driver type, output device, sample rate, buffer size, Safe Mode, Test, the Automatic switches:
// Auto Fade In/Out and Auto Normalize for the Playground's new files and recordings, applied by
// js/playground.js applyAutoEdits; Auto Level Compensation = Auto-Balance on for every plugin
// opened, applied by plugin_host._after_load) and
// which MIDI keyboards play it. Every control saves as soon as it changes
// (PUT /api/settings/{key}); the backend applies the change at once to what
// the open plugin plays (its sound restarts for a moment) and to its MIDI inputs.
// Performance: the Plugin memory limit (plugin_memory_limit_gb, a job object per
// plugin worker, plugin_host._Job), applied to the next plugin opened.
// Data: GET /api/audio/config?driver=&device= (devices of a driver type and the
// sample rates the chosen device opens at), POST /api/audio/test, GET /api/midi/devices.
// Classic script in an IIFE; talks to the rest of the app only via window.VEXT.
(function () {
  const V = window.VEXT;
  if (!V || typeof V.registerSection !== 'function') return;

  const esc = (v) => V.escHtml(v);
  const errText = (err) => (err && err.message) || String(err);
  const fmtHz = (r) => `${Number(r).toLocaleString('en-US')} Hz`;
  const fmtMs = (ms) => `${(Math.round(ms * 10) / 10).toLocaleString('en-US')} ms`;

  const DEFAULTS = {
    audio_driver: 'wasapi', audio_output_device: '', audio_sample_rate: '48000',
    audio_block_size: '512', audio_safe_mode: '0', audio_plugin_buffer: 'auto', midi_inputs: '', midi_channel: '0',
    midi_block_pitchbend: '0', plugin_memory_limit_gb: '1',
    audio_auto_fade: '1', audio_auto_normalize: '0', audio_auto_balance: '0',
  };
  // Configuration > Audio's automatic switches: [setting, label, tooltip, toast when on, toast when off].
  const AUTO_SWITCHES = [
    ['audio_auto_fade', 'Auto Fade In/Out', 'A fade in and a fade out on every file loaded or recorded in the Playground',
      'New files and recordings get a fade in and out.', 'No automatic fades.'],
    ['audio_auto_balance', 'Auto Level Compensation', 'Auto-Balance turned on for every plugin you open',
      'Auto-Balance turns on for every plugin you open.', 'Plugins open with their own Auto-Balance setting.'],
    ['audio_auto_normalize', 'Auto Normalize', 'Every file loaded or recorded in the Playground normalized to -1 dBFS',
      'New files and recordings are normalized.', 'No automatic normalize.'],
  ];
  const BLOCKS = ['64', '128', '256', '512', '1024', '2048', '4096'];
  const PLUGIN_BUFFERS = ['auto', '25', '50', '100', '200', '400', '800']; // host_worker.PLUGIN_BUFFERS (ms)
  const AUTO_START_MS = 60; // host_worker.AUTO_START_S: where Auto starts
  const MEMORY_GB = ['1', '2', '4', '8', '12', '16']; // plugin_host.MEMORY_LIMITS_GB
  const SAFE_EXTRA = 2; // buffers Safe Mode adds (host_worker.SAFE_EXTRA_BLOCKS)
  const DRIVER_NOTES = {
    wasapi: 'Shared with other apps. Windows converts the sample rate when the device runs at another one.',
    wasapi_exclusive: 'Talks to the device directly for the lowest latency. Other apps go silent while VEXT plays, and only the device\'s own sample rates work.',
    directsound: 'For older drivers. More latency than WASAPI.',
    mme: 'For older drivers. The most latency of all: small buffers don\'t make it faster.',
    asio: 'The audio interface\'s own low-latency driver. While VEXT plays through it, other apps may not be able to use the interface, and the interface switches to the sample rate chosen here.',
  };

  let root = null;
  let part = 'audio';  // audio | midi | performance: the Configuration tab shown
  let values = { ...DEFAULTS };
  let config = null;          // GET /api/audio/config for values.audio_driver
  let configError = '';
  let configBusy = false;
  let midi = null;            // GET /api/midi/devices
  let midiError = '';
  let decoding = null;        // GET /api/playground/decoding
  let decodingError = '';
  let test = null;            // last Test result
  let testing = false;
  let seq = 0;
  const bound = new WeakSet();

  const blockMs = (block, rate) => (Number(block) / (Number(rate) || 48000)) * 1000;

  // ---------- markup ----------

  function driverOptions() {
    const drivers = (config && config.drivers) || [
      { id: 'wasapi', label: 'WASAPI', available: true }, { id: 'wasapi_exclusive', label: 'WASAPI (exclusive)', available: true },
      { id: 'directsound', label: 'DirectSound', available: true }, { id: 'mme', label: 'MME', available: true },
      { id: 'asio', label: 'ASIO', available: false },
    ];
    return drivers.map(d => `<option value="${esc(d.id)}"${d.available || d.id === values.audio_driver ? '' : ' disabled'}>` +
      `${esc(d.label)}${d.available ? '' : ' (no driver installed)'}</option>`).join('');
  }

  function deviceOptions() {
    const list = (config && config.outputs) || [];
    const saved = values.audio_output_device || '';
    const resolved = list.find(d => d.id === (config && config.device));
    const driver = values.audio_driver;
    const dflt = driver === 'asio' ? 'First ASIO driver' : driver.startsWith('wasapi') ? 'System default' : 'Default device';
    let html = `<option value="">${dflt}${!saved && resolved ? ` (${esc(resolved.name)})` : ''}</option>`;
    if (saved && !list.some(d => d.id === saved)) {
      // Stored as "name, host API"; shown as "name (not connected)".
      const name = saved.replace(/, (Windows WASAPI|Windows DirectSound|MME|ASIO)$/, '');
      html += `<option value="${esc(saved)}">${esc(name)}${config ? ' (not connected)' : ''}</option>`;
    }
    html += list.map(d => `<option value="${esc(d.id)}">${esc(d.name)}${d.api_default && driver !== 'asio' ? ' (default)' : ''}</option>`).join('');
    return html;
  }

  // The Audio tab's decoding (GET /api/playground/decoding): FFmpeg's libraries, built in via PyAV.
  function decodingHtml() {
    if (decodingError) return `<p class="hint aud-note is-warn">Couldn't check the audio decoding: ${esc(decodingError)}</p>`;
    if (!decoding) return '<p class="hint aud-note"><span class="spinner" aria-hidden="true"></span> Checking…</p>';
    if (!decoding.ok) {
      return `<p class="hint aud-note is-warn">${esc(decoding.engine)} isn't working: ${esc(decoding.error || 'unknown error')}. ` +
        'The Playground\'s Audio tab can\'t open files until it is (reinstalling VEXT restores it).</p>';
    }
    const version = [decoding.ffmpeg ? `FFmpeg ${esc(decoding.ffmpeg)}` : '', decoding.pyav ? `PyAV ${esc(decoding.pyav)}` : '']
      .filter(Boolean).join(' · ');
    return `<p class="aud-decoding-line"><span class="aud-ok" aria-hidden="true">✓</span>
        <span><strong>${esc(decoding.engine)}</strong>${version ? ` <span class="hint">${version}</span>` : ''}</span></p>
      <p class="hint aud-note">The Playground's Audio tab opens ${esc(decoding.formats || 'any format FFmpeg reads')}.
        It comes with VEXT: nothing to install, and the separate FFmpeg program isn't needed (decoding, fades and saving
        the recording all run inside VEXT).</p>`;
  }

  function deviceNote() {
    if (configBusy && !config) return '<p class="hint aud-note"><span class="spinner" aria-hidden="true"></span> Listing the devices…</p>';
    if (configError) return `<p class="hint aud-note is-warn">${esc(configError)}</p>`;
    if (config && config.error) return `<p class="hint aud-note is-warn">${esc(config.error)}</p>`;
    const saved = values.audio_output_device;
    const parts = [];
    if (config && saved && !(config.outputs || []).some(d => d.id === saved)) {
      parts.push('<p class="hint aud-note is-warn">The saved device isn\'t connected right now: until it is, the Playground uses the default device.</p>');
    }
    if (config && !(config.outputs || []).length && !config.error) {
      parts.push('<p class="hint aud-note is-warn">This driver type has no output devices.</p>');
    }
    parts.push(`<p class="hint aud-note">${esc(DRIVER_NOTES[values.audio_driver] || '')}</p>`);
    return parts.join('');
  }

  // The rates the chosen device opens at. Shared modes open at any rate (Windows
  // converts); the device's own rate is marked when it's known.
  function rateOptions() {
    const r = (config && config.rates) || { rates: [44100, 48000, 88200, 96000, 176400, 192000], native: [] };
    const native = r.native || [];
    const converted = native.length && native.length < r.rates.length;
    let html = r.rates.map(rate => {
      const note = converted ? (native.includes(rate) ? ' (device rate)' : ' (converted by Windows)') : '';
      return `<option value="${rate}">${fmtHz(rate)}${note}</option>`;
    }).join('');
    const saved = Number(values.audio_sample_rate);
    if (config && !r.rates.includes(saved)) {
      html = `<option value="${saved}">${fmtHz(saved)} (this device can't)</option>` + html;
    }
    return html;
  }

  function rateNote() {
    const r = config && config.rates;
    const saved = Number(values.audio_sample_rate);
    if (r && r.rates && r.rates.length && !r.rates.includes(saved)) {
      const at = config.device_rate ? fmtHz(config.device_rate) : 'its own rate';
      return `<p class="hint aud-note is-warn">This device doesn't play at ${fmtHz(saved)}: the Playground falls back to ${esc(at)}. Choose one of its rates.</p>`;
    }
    return '';
  }

  // Buffer sizes with the time each one is at the chosen rate.
  function blockOptions() {
    const rate = Number(values.audio_sample_rate) || 48000;
    return BLOCKS.map(b => `<option value="${b}">${Number(b).toLocaleString('en-US')} samples (${fmtMs(blockMs(b, rate))})</option>`).join('');
  }

  function pluginBufferOptions() {
    return PLUGIN_BUFFERS.map(b => `<option value="${b}">${b === 'auto' ? 'Auto (recommended)' : `${b} ms`}</option>`).join('');
  }

  function latencyHtml() {
    const rate = Number(values.audio_sample_rate) || 48000;
    const buf = blockMs(values.audio_block_size, rate);
    const safe = values.audio_safe_mode === '1';
    const extra = SAFE_EXTRA * buf;
    const auto = values.audio_plugin_buffer === 'auto';
    const plugin = auto ? AUTO_START_MS : Number(values.audio_plugin_buffer);
    let html = `<p class="hint aud-latency">Each buffer is <strong>${fmtMs(buf)}</strong>. VEXT adds ${safe
      ? `${SAFE_EXTRA + 1} buffers (${fmtMs(buf + extra)}, Safe Mode)` : `one buffer (${fmtMs(buf)})`} to the device's own latency.
      Through a plugin, the Plugin buffer adds <strong>${fmtMs(plugin)}</strong>${auto ? ' (Auto starts there and grows only if a plugin needs it)' : ''}.
      Below 256 samples, heavy plugins may crackle: use a bigger buffer or Safe Mode.</p>`;
    const a = test && test.ok && test.audio;
    if (a) {
      html += `<p class="hint aud-latency">Last test: <strong>${fmtMs(a.latency_ms)}</strong> output latency measured by the device
        (${esc(a.device_name || 'default device')}, ${fmtHz(a.rate)}, ${Number(a.block).toLocaleString('en-US')} samples${a.safe ? ', Safe Mode' : ''}).</p>`;
    }
    return html;
  }

  function testHtml() {
    if (testing) return '<span class="aud-test-run"><span class="spinner" aria-hidden="true"></span> Playing a test tone…</span>';
    if (!test) return '';
    if (!test.ok) return `<div class="pg-error">${esc(test.error || 'The test failed.')}</div>`;
    const a = test.audio || {};
    const label = ((config && config.drivers) || []).find(d => d.id === a.driver);
    const parts = [a.device_name || 'the default device', label ? label.label : a.hostapi, fmtHz(a.rate),
      `${Number(a.block).toLocaleString('en-US')} samples`].filter(Boolean);
    let html = `<span class="aud-test-ok">Played a test tone through ${esc(parts.join(' · '))}${a.safe ? ' · Safe Mode' : ''}.
      Output latency ${fmtMs(a.latency_ms)}${a.xruns ? ` · ${a.xruns} dropout${a.xruns === 1 ? '' : 's'}` : ''}.</span>`;
    if (test.via === 'plugin') html += '<span class="hint">Mixed into the sound of the plugin open in the Playground (it holds the device).</span>';
    for (const n of test.notes || []) html += `<span class="aud-test-note">${esc(n)}</span>`;
    return html;
  }

  // ---------- MIDI markup ----------

  function savedMidi() {
    const v = values.midi_inputs;
    if (v === '*') return '*';
    try { const ids = JSON.parse(v || '[]'); return Array.isArray(ids) ? ids : []; } catch { return []; }
  }

  function midiListHtml() {
    if (midiError) return `<p class="hint aud-note is-warn">Couldn't list the MIDI inputs: ${esc(midiError)}</p>`;
    if (!midi) return '<p class="hint aud-note"><span class="spinner" aria-hidden="true"></span> Listing the MIDI inputs…</p>';
    const saved = savedMidi();
    const all = saved === '*';
    const connected = midi.inputs || [];
    const ids = connected.map(d => d.id);
    const extra = all ? [] : saved.filter(id => !ids.includes(id));
    let html = `<label class="aud-check aud-check-all"><input type="checkbox" data-midi="all"${all ? ' checked' : ''}> Every connected MIDI input</label>`;
    if (!connected.length && !extra.length) {
      html += '<p class="hint aud-note">No MIDI inputs are connected. Plug in a keyboard and press Refresh; the Playground\'s on-screen keyboard works without one.</p>';
      return html;
    }
    html += '<div class="aud-midi-list">';
    for (const d of connected) {
      const on = all || saved.includes(d.id);
      html += `<label class="aud-check"><input type="checkbox" data-midi-id="${esc(d.id)}"${on ? ' checked' : ''}${all ? ' disabled' : ''}> ${esc(d.name)}${d.id !== d.name ? ` <span class="hint">(${esc(d.id.slice(d.name.length).trim())})</span>` : ''}</label>`;
    }
    for (const id of extra) {
      html += `<label class="aud-check is-missing"><input type="checkbox" data-midi-id="${esc(id)}" checked> ${esc(id)} <span class="hint">(not connected)</span></label>`;
    }
    return html + '</div>';
  }

  function midiStatusHtml() {
    if (!midi) return '';
    if (midi.open == null) return '<p class="hint aud-note">They open while a plugin is open in the Playground (and are free for other apps otherwise).</p>';
    let html = midi.open.length
      ? `<p class="hint aud-note">Open in the Playground now: ${esc(midi.open.join(', '))}.</p>`
      : '<p class="hint aud-note">The Playground has no MIDI input open now.</p>';
    for (const f of midi.failed || []) html += `<p class="hint aud-note is-warn">${esc(f.id)}: ${esc(f.error)}.</p>`;
    return html;
  }

  function channelOptions() {
    let html = '<option value="0">All channels</option>';
    for (let c = 1; c <= 16; c++) html += `<option value="${c}">Channel ${c}</option>`;
    return html;
  }

  function render() {
    if (!root) return;
    // One Configuration tab per part (owner, 2026-10-15): Audio (device, rate, buffer),
    // MIDI and Performance. load() gets the part; the helpers skip what isn't drawn.
    const parts = {
      audio: `
        <div class="subsection first">
          <h3>Audio device</h3>
          <p class="hint">Where the Playground plays the plugin you're testing. Changes apply at once: whatever is playing restarts
            with them (a short gap).</p>
          <div class="aud-fields">
            <label>Driver type
              <select id="aud-driver" data-key="audio_driver" aria-label="Driver type">${driverOptions()}</select>
            </label>
            <label class="aud-grow">Output device
              <span class="aud-row">
                <select id="aud-device" data-key="audio_output_device" aria-label="Output device">${deviceOptions()}</select>
                <button type="button" data-act="refresh">Refresh</button>
              </span>
            </label>
          </div>
          <div class="aud-device-note">${deviceNote()}</div>
        </div>

        <div class="subsection">
          <h3>Sample rate and buffer</h3>
          <div class="aud-fields">
            <label>Sample rate
              <select id="aud-rate" data-key="audio_sample_rate">${rateOptions()}</select>
            </label>
            <label>Buffer size
              <select id="aud-block" data-key="audio_block_size">${blockOptions()}</select>
            </label>
            <label>Plugin buffer
              <select id="aud-plugin-buffer" data-key="audio_plugin_buffer">${pluginBufferOptions()}</select>
            </label>
            <label class="switch-row aud-safe" title="Trades latency for stability">
              <input type="checkbox" class="switch" id="aud-safe"${values.audio_safe_mode === '1' ? ' checked' : ''}> Safe Mode
            </label>
          </div>
          <div class="aud-rate-note">${rateNote()}</div>
          <p class="hint aud-note">Heavy plugins (iZotope RX, spectral and linear-phase processors) are run ahead of the sound by
            this much, so their slow moments never reach your ears as gaps. Auto starts at ${AUTO_START_MS} ms and grows only if a
            plugin needs it. Knobs, Bypass and edits are heard after this delay; Play, Stop and seeking are immediate.
            Instruments aren't buffered.</p>
          <p class="hint aud-note">Safe Mode keeps ${SAFE_EXTRA} more buffers queued ahead of the device (and a bigger cushion for the web
            browser's sound), so heavy plugins and busy moments don't crackle. It adds
            <strong class="aud-safe-ms">${fmtMs(SAFE_EXTRA * blockMs(values.audio_block_size, values.audio_sample_rate))}</strong> of latency.</p>
          <div class="aud-latency-box">${latencyHtml()}</div>
          <div class="aud-test">
            <button type="button" data-act="test">Test</button>
            <div class="aud-test-result" role="status" aria-live="polite">${testHtml()}</div>
          </div>
        </div>

        <div class="subsection">
          <h3>Automatic</h3>
          <div class="aud-auto">
            ${AUTO_SWITCHES.map(([key, label, title]) => `
            <label class="switch-row" title="${esc(title)}">
              <input type="checkbox" class="switch" data-auto-key="${key}"${values[key] === '1' ? ' checked' : ''}> ${label}
            </label>`).join('')}
          </div>
          <p class="hint aud-note"><strong>Auto Fade In/Out</strong>: a file you load and a recording of the browser get a fade in
            and a fade out, as long as the Fade length chosen in the Playground's Audio tab. <strong>Auto Level Compensation</strong>:
            every plugin opens with Auto-Balance on, which keeps what comes out about as loud as what goes in (peaks under
            -1 dBFS); you can still switch it off in the plugin's frame. <strong>Auto Normalize</strong>: a file you load and a
            recording are brought to -1 dBFS peak once they finish loading. The automatic edits are normal edits:
            Undo in the Audio tab takes them off.</p>
        </div>

        <div class="subsection">
          <h3>Audio decoding</h3>
          <div class="aud-decoding">${decodingHtml()}</div>
        </div>
`,
      midi: `
        <div class="subsection first">
          <h3>MIDI input</h3>
          <p class="hint">MIDI keyboards play the instrument open in the Playground (and effects that take MIDI). Changes apply at once.</p>
          <div class="aud-midi">${midiListHtml()}</div>
          <div class="aud-fields aud-midi-fields">
            <label>Listen to
              <select id="aud-channel" data-key="midi_channel">${channelOptions()}</select>
            </label>
            <button type="button" data-act="refresh-midi">Refresh</button>
          </div>
          <label class="switch-row aud-bend" title="Keeps the keyboard's pitch wheel from bending the plugin">
            <input type="checkbox" class="switch" id="aud-bend"${values.midi_block_pitchbend === '1' ? ' checked' : ''}> Bypass pitch bend
          </label>
          <p class="hint aud-note">Pitch bend is the standard MIDI message (status <code>En</code>, 14-bit, centred at 8192) a
            keyboard's pitch wheel sends. Bypassed, it never reaches the plugin: useful when a wheel drifts off centre or
            jitters and leaves the instrument slightly out of tune. Turning it on recentres any bend already applied.</p>
          <div class="aud-midi-status">${midiStatusHtml()}</div>
        </div>
`,
      performance: `
        <div class="subsection first">
          <h3>Performance</h3>
          <div class="aud-fields">
            <label>Plugin memory limit
              <select id="aud-memory" data-key="plugin_memory_limit_gb">${memoryOptions()}</select>
            </label>
          </div>
          <p class="hint aud-note">The most memory one plugin may use while it's open in the Playground (each plugin runs
            in its own process). It protects your PC from a plugin that runs away with memory; it doesn't make plugins
            open faster. A plugin that needs more stops with a message saying so. Big sample libraries (Kontakt, Vienna,
            large ROMplers) can need several GB. Applies to the next plugin you open.</p>
        </div>`,
    };
    root.innerHTML = `
      <div class="aud">
${parts[part] || parts.audio}
      </div>`;
    applyValues();
  }

  function memoryOptions() {
    return MEMORY_GB.map(g => `<option value="${g}">${g} GB${g === DEFAULTS.plugin_memory_limit_gb ? ' (default)' : ''}</option>`).join('');
  }

  function applyValues() {
    if (!root) return;
    for (const sel of root.querySelectorAll('select[data-key]')) {
      const v = values[sel.dataset.key] ?? '';
      sel.value = v;
      if (sel.value !== v && sel.options.length) sel.value = DEFAULTS[sel.dataset.key];
    }
  }

  // Parts that change without rebuilding the whole pane (keeps focus where it is).
  function refreshParts() {
    if (!root || !root.querySelector('.aud')) { render(); return; }
    const set = (sel, html) => { const el = root.querySelector(sel); if (el) el.innerHTML = html; };
    set('#aud-driver', driverOptions());
    set('#aud-device', deviceOptions());
    set('.aud-device-note', deviceNote());
    set('.aud-decoding', decodingHtml());
    set('#aud-rate', rateOptions());
    set('.aud-rate-note', rateNote());
    set('#aud-block', blockOptions());
    set('.aud-safe-ms', fmtMs(SAFE_EXTRA * blockMs(values.audio_block_size, values.audio_sample_rate)));
    set('.aud-latency-box', latencyHtml());
    set('.aud-test-result', testHtml());
    set('.aud-midi', midiListHtml());
    set('.aud-midi-status', midiStatusHtml());
    const safe = root.querySelector('#aud-safe');
    if (safe) safe.checked = values.audio_safe_mode === '1';
    const bend = root.querySelector('#aud-bend');
    if (bend) bend.checked = values.midi_block_pitchbend === '1';
    root.querySelectorAll('[data-auto-key]').forEach(c => { c.checked = values[c.dataset.autoKey] === '1'; });
    applyValues();
  }

  // ---------- data ----------

  async function loadConfig(opts = {}) {
    const s = seq;
    configBusy = true;
    if (!config) refreshParts();
    const q = new URLSearchParams({ driver: values.audio_driver });
    if (opts.device != null) q.set('device', opts.device);
    if (opts.refresh) q.set('refresh', 'true');
    try {
      const c = await V.api(`/api/audio/config?${q}`);
      if (s !== seq) return null;
      config = c;
      configError = '';
    } catch (err) {
      if (s !== seq) return null;
      configError = `Couldn't list the audio devices: ${errText(err)}`;
    } finally {
      configBusy = false;
    }
    return config;
  }

  async function loadDecoding() {
    try {
      decoding = await V.api('/api/playground/decoding');
      decodingError = '';
    } catch (err) {
      decodingError = errText(err);
    }
  }

  async function loadMidi() {
    try {
      midi = await V.api('/api/midi/devices');
      midiError = '';
    } catch (err) {
      midi = null;
      midiError = errText(err);
    }
  }

  async function put(key, value) {
    await V.api(`/api/settings/${encodeURIComponent(key)}`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ value }),
    });
    values[key] = value;
  }

  // The saved rate must be one the device opens at: otherwise the nearest one it has (48 kHz first).
  async function fitRate() {
    const r = config && config.rates;
    if (!r || !r.rates || !r.rates.length) return;
    const saved = Number(values.audio_sample_rate);
    if (r.rates.includes(saved)) return;
    const pick = (r.native && r.native.includes(48000)) || r.rates.includes(48000) ? 48000
      : r.rates.reduce((a, b) => (Math.abs(b - saved) < Math.abs(a - saved) ? b : a));
    try {
      await put('audio_sample_rate', String(pick));
      V.toast(`This device doesn't play at ${fmtHz(saved)}: the sample rate is now ${fmtHz(pick)}.`);
    } catch { /* the note explains it */ }
  }

  async function save(sel) {
    const key = sel.dataset.key;
    const value = sel.value;
    const prev = values[key];
    if (value === prev) return;
    sel.disabled = true;
    try {
      await put(key, value);
      if (key === 'audio_driver') {
        // Devices belong to one driver type: start from its default device.
        test = null;
        if (values.audio_output_device) await put('audio_output_device', '');
        config = null;
        refreshParts();
        await loadConfig();
        await fitRate();
      } else if (key === 'audio_output_device') {
        test = null;
        await loadConfig();
        await fitRate();
      }
      V.toast('Saved.');
    } catch (err) {
      values[key] = prev;
      V.toast('Could not save: ' + errText(err), true);
    } finally {
      sel.disabled = false;
      refreshParts();
    }
  }

  async function saveSafe(input) {
    const value = input.checked ? '1' : '0';
    const prev = values.audio_safe_mode;
    input.disabled = true;
    try {
      await put('audio_safe_mode', value);
      V.toast(input.checked ? 'Safe Mode on.' : 'Safe Mode off.');
    } catch (err) {
      values.audio_safe_mode = prev;
      V.toast('Could not save: ' + errText(err), true);
    } finally {
      input.disabled = false;
      refreshParts();
    }
  }

  async function saveBend(input) {
    const prev = values.midi_block_pitchbend;
    input.disabled = true;
    try {
      await put('midi_block_pitchbend', input.checked ? '1' : '0');
      V.toast(input.checked ? 'Pitch bend bypassed.' : 'Pitch bend passes through again.');
    } catch (err) {
      values.midi_block_pitchbend = prev;
      V.toast('Could not save: ' + errText(err), true);
    } finally {
      input.disabled = false;
      refreshParts();
    }
  }

  async function saveAuto(input) {
    const key = input.dataset.autoKey;
    const spec = AUTO_SWITCHES.find(s => s[0] === key);
    const prev = values[key];
    input.disabled = true;
    try {
      await put(key, input.checked ? '1' : '0');
      V.toast(input.checked ? spec[3] : spec[4]);
    } catch (err) {
      values[key] = prev;
      V.toast('Could not save: ' + errText(err), true);
    } finally {
      input.disabled = false;
      refreshParts();
    }
  }

  async function saveMidi() {
    const all = root.querySelector('[data-midi="all"]');
    let value;
    if (all && all.checked) value = '*';
    else {
      const ids = [...root.querySelectorAll('[data-midi-id]')].filter(c => c.checked).map(c => c.dataset.midiId);
      value = ids.length ? JSON.stringify(ids) : '';
    }
    if (value === values.midi_inputs) return;
    const prev = values.midi_inputs;
    try {
      await put('midi_inputs', value);
      V.toast('Saved.');
      setTimeout(async () => { await loadMidi(); refreshParts(); }, 700); // the worker reopens its inputs
    } catch (err) {
      values.midi_inputs = prev;
      V.toast('Could not save: ' + errText(err), true);
    }
    refreshParts();
  }

  async function runTest() {
    testing = true;
    refreshParts();
    try {
      test = await V.api('/api/audio/test', { method: 'POST' });
    } catch (err) {
      test = { ok: false, error: errText(err) };
    } finally {
      testing = false;
      refreshParts();
    }
  }

  function bind(el) {
    if (bound.has(el)) return;
    bound.add(el);
    el.addEventListener('change', (e) => {
      const t = e.target;
      if (!(t instanceof Element) || !el.contains(t)) return;
      if (t.matches('select[data-key]')) save(t);
      else if (t.id === 'aud-safe') saveSafe(t);
      else if (t.id === 'aud-bend') saveBend(t);
      else if (t.matches('[data-auto-key]')) saveAuto(t);
      else if (t.matches('[data-midi], [data-midi-id]')) saveMidi();
    });
    el.addEventListener('click', async (e) => {
      const btn = e.target.closest && e.target.closest('[data-act]');
      if (!btn || btn.disabled || !el.contains(btn)) return;
      const s = seq;
      btn.disabled = true;
      try {
        if (btn.dataset.act === 'refresh') {
          await loadConfig({ refresh: true });
          await fitRate();
        } else if (btn.dataset.act === 'refresh-midi') {
          await loadMidi();
        } else if (btn.dataset.act === 'test') {
          await runTest();
        }
      } finally {
        btn.disabled = false;
        if (s === seq) refreshParts();
      }
    });
  }

  async function load(el, params) {
    root = el;
    part = (params && params.part) || 'audio';
    bind(el);
    const s = ++seq;
    if (!el.querySelector('.aud')) el.innerHTML = '<p class="hint">Loading…</p>';
    const getSettings = typeof V.settings === 'function' ? V.settings(true) : V.api('/api/settings');
    const settings = await getSettings.catch(err => ({ __error: err }));
    if (s !== seq) return;
    if (settings && settings.__error) {
      V.toast('Could not load the audio settings: ' + errText(settings.__error), true);
    } else {
      values = { ...DEFAULTS };
      for (const k of Object.keys(DEFAULTS)) {
        if (settings && settings[k] != null) values[k] = String(settings[k]);
      }
    }
    render();
    await Promise.all([part === 'audio' ? loadConfig() : null, part === 'audio' && !decoding ? loadDecoding() : null,
      part === 'midi' ? loadMidi() : null]);
    if (s !== seq) return;
    refreshParts();
  }

  V.registerSection('config-audio', {
    load(el, params) { return load(el, params); },
    unload() { seq++; }, // a load still in flight no longer renders
  });
})();
