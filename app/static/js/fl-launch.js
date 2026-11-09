// The FL Studio controller in the main window: its setup (Configuration > DAWs > FL Studio), the
// FL Studio Sync connection visor, and native drags of catalog
// cards into FL. The preset picker and the drag helper are js/fl-preset.js (shared with the launcher).
(() => {
  const V = window.VEXT;
  if (!V || V.flLaunch) return;
  const esc = V.escHtml;
  const post = (path, body = {}) => V.api('/api/fl-launch/' + path, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  const preset = window.VextFlPreset ? window.VextFlPreset.create({ api: V.api, esc, toast: V.toast }) : null;

  let connection = { connected: false, message: 'Checking FL Studio connection…' };
  // Setup status controls the FL connection visor only.
  let menu = { configured: false, reason: '' };
  let pollTimer = null;
  let panel = null;
  let down = null;
  let suppressClick = 0;

  const disabledReason = () => connection.connected ? '' : connection.message;

  async function refreshConnection() {
    try { connection = await V.api('/api/fl-launch/connection'); }
    catch (_) { connection = { connected: false, message: 'FL connection unavailable' }; }
    paintVisor();
    return connection;
  }

  function paintVisor() {
    const visor = document.getElementById('fl-visor');
    if (!visor) return;
    const state = connection.connected ? 'connected' : connection.running ? 'running' : 'closed';
    visor.dataset.state = state;
    visor.title = connection.message;
    document.getElementById('fl-visor-text').textContent = { connected: 'FL connected', running: 'FL disconnected', closed: 'FL closed' }[state];
    document.getElementById('fl-visor-setup').hidden = menu.configured;
  }

  async function refreshTools() {
    try { menu = await V.api('/api/fl-launch/menu'); }
    catch (_) { menu = { configured: false, reason: 'FL Studio Sync is unavailable.' }; }
    paintVisor();
    return menu;
  }

  // FL Studio Sync opened: read the tools, and poll the connection every 2 s while the page is shown.
  // The poll stops itself once another section is shown.
  function startSync() {
    refreshTools();
    refreshConnection();
    if (pollTimer) return;
    pollTimer = setInterval(() => {
      const page = document.getElementById('panel-flsync');
      if (!page || !page.classList.contains('active')) {
        clearInterval(pollTimer);
        pollTimer = null;
        return;
      }
      refreshConnection();
    }, 2000);
  }

  function say(message, error = false) {
    const output = panel && panel.querySelector('[data-fl-message]');
    if (output) output.textContent = message;
    V.toast(message, error);
  }

  async function waitForFL() {
    try {
      for (let i = 0; i < 60; i++) {
        const result = await V.api('/api/fl-launch/status');
        if (!['pending', 'activating'].includes(result.state)) {
          say(result.message || 'No FL request.', ['unknown', 'error'].includes(result.state));
          return;
        }
        await new Promise(resolve => setTimeout(resolve, 500));
      }
      say('FL has not confirmed the result. Check FL before trying again.', true);
    } catch (error) { say(error.message, true); }
  }

  // The FL badge on the DAW panel's summary: FL installed, then the controller installed and current.
  function paintBadge(installed, setup) {
    const badge = document.getElementById('daw-badge-fl');
    if (!badge) return;
    const [text, tone] = !installed ? ['Not found', 'muted']
      : !setup.installed ? ['Controller not installed', 'warn']
        : setup.update_available ? ['Controller update available', 'warn']
          : ['Configured', 'ok'];
    badge.textContent = text;
    badge.className = `badge ${tone}`;
  }

  // Configuration > DAWs > FL Studio: the controller setup (MIDI output, install or update, test).
  async function mount() {
    panel = document.getElementById('fl-controller-root');
    if (!panel) return;
    try {
      const [setup, s] = await Promise.all([V.api('/api/fl-launch/setup'), V.settings(true)]);
      paintBadge(s.fl_studio_installed, setup);
      panel.innerHTML = `<div class="fl-launch-controls"><label>MIDI output <select data-fl-port>${[...new Set([setup.port, ...setup.outputs])]
        .map(name => `<option value="${esc(name)}"${name === setup.port ? ' selected' : ''}>${esc(name)}${setup.outputs.includes(name) ? '' : ' (not connected)'}</option>`).join('')}</select></label>
        <button type="button" data-fl-install>${setup.installed ? 'Update controller…' : 'Install controller…'}</button>
        <button type="button" data-fl-test${setup.installed ? '' : ' disabled'}>Test connection</button></div>
        <p class="hint" data-fl-loopback>${esc(setup.loopback.message)}</p>
        <p class="hint" data-fl-managed>${esc(setup.loopback.managed?.message || 'Managed MIDI status unavailable.')}</p>
        <p class="hint">VEXT creates its MIDI port at startup when the installed driver is available. For an external cable such as loopMIDI, keep its provider running. In FL’s MIDI settings (F10), enable the selected port and choose controller type VEXT. Open the Plugins Browser tab. Loading a preset from the plugin browser needs this connection; dragging a card into FL needs only a matching FL preset.</p>
        <p class="hint fl-launch-path">Controller: ${esc(setup.script_path)}</p>
        <p data-fl-message role="status">${esc(setup.update_available ? 'Controller update available. Update it, then reload the script in FL.' : setup.result.message || (setup.installed ? 'Controller installed. Test the connection with FL open.' : 'Install the controller to load presets into FL Studio.'))}</p>`;
      const portControl = panel.querySelector('[data-fl-port]');
      const testControl = panel.querySelector('[data-fl-test]');
      const installControl = panel.querySelector('[data-fl-install]');
      const diagnosticOutput = panel.querySelector('[data-fl-loopback]');
      let savedPort = setup.port;
      portControl.onchange = async event => {
        const selected = event.target.value;
        portControl.disabled = true;
        testControl.disabled = true;
        installControl.disabled = true;
        try {
          await V.api('/api/settings/fl_launch_port', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ value: selected }) });
          savedPort = selected;
          try {
            const diagnostic = await V.api('/api/midi/loopback');
            diagnosticOutput.textContent = diagnostic.message;
            panel.querySelector('[data-fl-managed]').textContent = diagnostic.managed?.message || 'Managed MIDI status unavailable.';
            say('MIDI output saved. Select the same input in FL.');
          } catch (_) {
            diagnosticOutput.textContent = 'MIDI output saved, but device status could not be refreshed. Reopen setup to refresh.';
            say('MIDI output saved, but device status could not be refreshed.', true);
          }
        } catch (error) {
          portControl.value = savedPort;
          say('Could not save MIDI output: ' + error.message, true);
        } finally {
          portControl.disabled = false;
          testControl.disabled = !setup.installed;
          installControl.disabled = false;
        }
      };
      panel.querySelector('[data-fl-install]').onclick = async () => {
        if (!(await V.confirmDialog(`Install or update the VEXT controller?\n\n${setup.script_path}\n\nWrites the controller and its local connection settings. An existing VEXT controller is backed up. Other controllers are unchanged.`))) return;
        try {
          await post('install', { path: setup.script_path, confirmed: true });
          await mount();
          await refreshTools();
          say('Controller installed. Choose VEXT in FL’s MIDI settings; restart FL if it is not listed.');
        } catch (error) { say(error.message, true); }
      };
      panel.querySelector('[data-fl-test]').onclick = async () => {
        try { await post('ping'); await waitForFL(); }
        catch (error) { say(error.message, true); }
      };
    } catch (error) {
      const badge = document.getElementById('daw-badge-fl');
      if (badge) { badge.textContent = 'Unavailable'; badge.className = 'badge muted'; }
      panel.textContent = error.message;
    }
  }

  // Open the controller setup from the connection visor.
  function wireTools() {
    const setup = document.getElementById('fl-visor-setup');
    if (setup) setup.onclick = () => V.navigate('config', { pane: 'daws' });
  }

  // Catalog lists also live in group dialogs; there is no panel-browse in the current UI.
  const cardFor = target => target.closest('[data-plugin-id]:not(.row-menu-btn)');
  document.addEventListener('pointerdown', event => {
    // The Playground's whole card is a pick button. Its content is a drag source too.
    if (!V.isTauri() || event.button !== 0 || event.target.closest('button:not(.pg-plugin-pick), a, input, select, textarea, [contenteditable]')) return;
    const card = cardFor(event.target);
    if (card) down = { id: Number(card.dataset.pluginId), x: event.clientX, y: event.clientY, held: true, started: false };
  });
  document.addEventListener('pointerup', () => { down = null; });
  document.addEventListener('pointercancel', () => { down = null; });
  document.addEventListener('dragstart', event => { if (V.isTauri() && cardFor(event.target)) event.preventDefault(); });
  document.addEventListener('pointermove', async event => {
    const state = down;
    if (!state || state.started || !(event.buttons & 1) || Math.hypot(event.clientX - state.x, event.clientY - state.y) < 8) return;
    state.started = true;
    suppressClick = Date.now() + 1500;
    try {
      if (!preset) throw new Error('The FL preset helper (js/fl-preset.js) did not load.');
      // OLE needs the button held: the helper checks this again after its lookup, so a late answer
      // after release starts no drag.
      await preset.startDrag(state.id, () => down === state);
    } catch (error) { V.toast(String(error.message || error), true); }
    finally { down = null; suppressClick = Date.now() + 300; }
  });
  document.addEventListener('click', event => {
    if (Date.now() < suppressClick && cardFor(event.target)) { event.preventDefault(); event.stopImmediatePropagation(); }
  }, true);

  V.flLaunch = { mount, refreshConnection, disabledReason, startSync };
  wireTools();
})();
