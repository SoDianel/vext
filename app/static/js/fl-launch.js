// The FL Studio controller in the main window: its setup (Configuration > DAWs > FL Studio), the
// FL Studio Sync tools (connection visor and the launcher bubble switch), and native drags of catalog
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
  // The launcher bubble needs two answers: /api/fl-launch/menu (is FL set up) and the setting.
  let menu = { configured: false, reason: '' };
  let bubbleOn = false;
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

  // The bubble switch: on only while the setting is on, and off (disabled) until FL is set up.
  function paintTools() {
    paintVisor();
    const toggle = document.getElementById('fl-bubble-switch');
    if (!toggle) return;
    const desktop = V.isTauri();
    const reason = !desktop ? 'Desktop app only.' : (menu.configured ? '' : (menu.reason || 'Set up FL Studio first.'));
    toggle.checked = bubbleOn;
    toggle.disabled = !desktop || !menu.configured;
    const note = document.getElementById('fl-bubble-reason');
    note.textContent = reason;
    note.hidden = !reason;
  }

  async function refreshTools() {
    const [menuResult, settingsResult] = await Promise.allSettled([V.api('/api/fl-launch/menu'), V.settings(true)]);
    menu = menuResult.status === 'fulfilled' ? menuResult.value : { configured: false, reason: 'FL Studio Sync is unavailable.' };
    bubbleOn = settingsResult.status === 'fulfilled' && String(settingsResult.value.fl_bubble_enabled) === '1';
    paintTools();
    return menu;
  }

  // Keeps the bubble in step with the setting and the setup: shown only while the setting is on and FL
  // is set up. Runs at start, when FL presence or the controller changes, and after Install/Update.
  // An older desktop shell has no bubble command, so the call is allowed to fail.
  async function syncBubble() {
    await refreshTools();
    if (!V.isTauri()) return;
    try { await window.__TAURI__.core.invoke('fl_bubble_set', { enabled: bubbleOn && menu.configured }); }
    catch (_) { /* an older shell: no bubble */ }
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
        <p class="hint">Keep loopMIDI running. In FL’s MIDI settings (F10), enable the selected port and choose controller type VEXT. Open the Plugins Browser tab. The launcher bubble needs this connection; dragging a card into FL needs only a matching FL preset.</p>
        <p class="hint fl-launch-path">Controller: ${esc(setup.script_path)}</p>
        <p data-fl-message role="status">${esc(setup.update_available ? 'Controller update available. Update it, then reload the script in FL.' : setup.result.message || (setup.installed ? 'Controller installed. Test the connection with FL open.' : 'Install the controller to enable the launcher bubble.'))}</p>`;
      panel.querySelector('[data-fl-port]').onchange = async event => {
        try {
          await V.api('/api/settings/fl_launch_port', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ value: event.target.value }) });
          say('MIDI output saved. Select the same input in FL.');
        } catch (error) { say(error.message, true); }
      };
      panel.querySelector('[data-fl-install]').onclick = async () => {
        if (!(await V.confirmDialog(`Install or update the VEXT controller?\n\n${setup.script_path}\n\nWrites the controller and its local connection settings. An existing VEXT controller is backed up. Other controllers are unchanged.`))) return;
        try {
          await post('install', { path: setup.script_path, confirmed: true });
          await mount();
          await syncBubble();
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

  // The FL Studio Sync tools: the setup link (opens Configuration > DAWs) and the bubble switch.
  function wireTools() {
    const setup = document.getElementById('fl-visor-setup');
    if (setup) setup.onclick = () => V.navigate('config', { pane: 'daws' });
    const toggle = document.getElementById('fl-bubble-switch');
    if (!toggle) return;
    toggle.onchange = async () => {
      const enabled = toggle.checked;
      try {
        await V.api('/api/settings/fl_bubble_enabled', {
          method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ value: enabled ? '1' : '0' }),
        });
        bubbleOn = enabled;
        await window.__TAURI__.core.invoke('fl_bubble_set', { enabled });
      } catch (error) { V.toast(String(error.message || error), true); }
      paintTools();
    };
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

  V.flLaunch = { mount, refreshConnection, disabledReason, startSync, syncBubble };
  wireTools();
  // app.js checks FL's presence before this file loads, so restore the bubble here at start too.
  syncBubble().catch(() => { /* the setting or menu is unavailable: no bubble */ });
})();
