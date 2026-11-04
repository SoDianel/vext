// The FL preset index, a panel inside the bubble window (fl-bubble.js): one searchable list, sidebar
// categories/vendors and three view modes. Confirmation, focus handoff and native dragging share the exact
// FST key in every view.
(() => {
  'use strict';

  const T = window.__TAURI__ || null;
  const CFG = window.__VEXT__ || {};
  const invoke = T && T.core && typeof T.core.invoke === 'function' ? T.core.invoke : null;
  const $ = (id) => document.getElementById(id);
  const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const SKIP_KEY = 'fl-launcher-skip-confirm';

  const titleEl = $('fln-title');
  const countEl = $('fln-count');
  const filter = $('fln-filter');
  const grid = $('fln-grid');
  const empty = $('fln-empty');
  const confirmBar = $('fln-confirm');
  const confirmTitle = $('fln-confirm-title');
  const confirmDestination = $('fln-confirm-destination');
  const dontAsk = $('fln-dont-ask');
  const statusEl = $('fln-status');
  const statusText = $('fln-status-text');
  const connText = $('fln-conn');
  const resultEl = $('fln-result');
  const groupsEl = $('fln-groups');
  const headingEl = $('fln-heading');
  const viewsEl = $('fln-views');
  const typesEl = $('fln-types');
  const modesEl = $('fln-group-modes');

  const api = async (path, options = {}) => {
    const method = String(options.method || 'GET').toUpperCase();
    const headers = { ...(options.headers || {}) };
    if (method !== 'GET') headers['X-VEXT-Token'] = CFG.token || '';
    const res = await fetch((CFG.apiBase || '') + path, { ...options, method, headers });
    let data = null;
    try { data = await res.json(); } catch (_) { /* no body */ }
    if (!res.ok) {
      const detail = data && (typeof data.detail === 'string' ? data.detail : data.error);
      throw new Error(detail || `the backend answered ${res.status}`);
    }
    return data;
  };
  const post = (path, body) => api('/api/fl-launch/' + path, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });

  // The footer's last FL result; also the preset chooser's toast.
  function say(message, error = false) {
    resultEl.textContent = message;
    resultEl.classList.toggle('is-error', !!error);
  }
  const presets = window.VextFlPreset.create({ api, esc, toast: say });

  let query = { kind: 'category', value: '', title: '' };
  let type = 'all';
  let view = 'cards';
  let loaded = false;
  try { const saved = localStorage.getItem('fl-launcher-view'); if (['cards', 'list', 'compact'].includes(saved)) view = saved; } catch (_) { /* defaults */ }
  let plugins = [];
  let listSeq = 0;
  let connection = { connected: false, running: false, message: 'Checking FL Studio connection…' };
  let pending = null; // {id, pick} waiting for the confirm bar
  let polling = false;
  let down = null;
  let suppressClick = 0;
  let nativeDrag = false; // a preset drag into FL is running: the bubble must not fold the panel under it

  // ---------- connection ----------

  async function refreshConnection() {
    try { connection = await api('/api/fl-launch/connection'); }
    catch (_) { connection = { connected: false, running: false, message: 'FL connection unavailable' }; }
    const state = connection.connected ? 'connected' : connection.running ? 'running' : 'closed';
    statusEl.dataset.state = state;
    statusText.textContent = { connected: 'Connected', running: 'Waiting', closed: 'FL closed' }[state];
    statusEl.title = connection.message || '';
    connText.textContent = connection.message || '';
    grid.classList.toggle('is-offline', !connection.connected);
    return connection;
  }

  async function waitForFL() {
    if (polling) return;
    polling = true;
    try {
      for (let i = 0; i < 60; i++) {
        const result = await api('/api/fl-launch/status');
        if (!['pending', 'activating'].includes(result.state)) {
          say(result.message || 'No FL request.', ['unknown', 'error'].includes(result.state));
          return;
        }
        await new Promise((resolve) => setTimeout(resolve, 500));
      }
      say('FL has not confirmed the result. Check FL before trying again.', true);
    } catch (error) { say(error.message, true); }
    finally { polling = false; }
  }

  // ---------- searchable FL preset index ----------

  function cardHtml(p) {
    // Own preset picture, then catalog picture, then the filed copy's picture (the same preset-image route).
    const presetUrl = (CFG.apiBase || '') + '/api/fl-launch/preset-image?key=' + encodeURIComponent(p.key);
    const catalogUrl = (CFG.apiBase || '') + `/api/plugins/${p.id}/image`;
    const source = p.has_preset_image ? presetUrl : p.has_image ? catalogUrl : p.has_filed_image ? presetUrl : '';
    const picture = source
      ? `<img src="${esc(source)}" alt="" draggable="false" loading="lazy">`
      : '<svg class="logo-mark" aria-hidden="true"><use href="/static/logo-mark.svg#mark"/></svg>';
    return `<div class="fln-card" role="button" tabindex="0" data-plugin-id="${p.id ?? ''}" data-preset-key="${esc(p.key || '')}" title="${esc(p.name + (p.key ? '\n' + p.key : ''))}">
      ${view === 'compact' ? '' : `<div class="fln-pic${source ? '' : ' is-empty'}">${picture}</div>`}
      <div class="fln-name">${esc(p.name)}</div>
      <div class="fln-vendor">${esc(p.vendor)}</div>
      <div class="fln-badges"><span>${esc(p.plugin_type || 'Plugin')}</span><span>${esc(p.format)}</span></div>
    </div>`;
  }

  function matchesBase(p) {
    const isEffect = String(p.plugin_type || '').toLowerCase() === 'effect';
    if (type === 'effect' && !isEffect || type === 'instrument' && isEffect) return false;
    const haystack = [p.name, p.vendor, p.plugin_type, p.format, p.key, ...(p.categories || []).map(([name]) => name)].join(' ').toLowerCase();
    return filter.value.trim().toLowerCase().split(/\s+/).every((word) => haystack.includes(word));
  }

  function inGroup(p) {
    if (!query.value) return true;
    const wanted = query.value.toLowerCase();
    return query.kind === 'vendor' ? String(p.vendor || '').toLowerCase() === wanted
      : (p.categories || []).some(([, slug]) => slug.toLowerCase() === wanted) || wanted === 'fl-uncategorized' && !(p.categories || []).length;
  }

  function syncButtons(container, attribute, value) {
    container.querySelectorAll('button').forEach((button) => {
      button.setAttribute('aria-pressed', String(button.dataset[attribute] === value));
    });
  }

  function renderGroups(base) {
    const groups = new Map();
    for (const plugin of plugins) {
      const entries = query.kind === 'vendor' ? [[plugin.vendor || 'Unassigned', plugin.vendor || 'Unassigned']]
        : (plugin.categories || []).length ? plugin.categories : [['Uncategorized', 'fl-uncategorized']];
      for (const [name, value] of entries) {
        const key = value.toLowerCase();
        if (!groups.has(key)) groups.set(key, { name, value, count: 0 });
      }
    }
    for (const plugin of base) {
      const values = query.kind === 'vendor' ? [plugin.vendor || 'Unassigned']
        : (plugin.categories || []).length ? plugin.categories.map(([, slug]) => slug) : ['fl-uncategorized'];
      for (const value of new Set(values.map((v) => v.toLowerCase()))) {
        const group = groups.get(value);
        if (group) group.count++;
      }
    }
    const row = (name, value, count) => `<button type="button" class="fln-group${query.value.toLowerCase() === value.toLowerCase() ? ' is-selected' : ''}" data-group="${esc(value)}" data-name="${esc(name)}" aria-current="${query.value.toLowerCase() === value.toLowerCase() ? 'true' : 'false'}"><span class="fln-group-mark" aria-hidden="true">${esc(value ? name.slice(0, 1).toUpperCase() : '*')}</span><span class="fln-group-name">${esc(name)}</span><span class="fln-group-count">${count}</span></button>`;
    groupsEl.innerHTML = row('All plugins', '', base.length) +
      [...groups.values()].sort((a, b) => a.name.localeCompare(b.name)).map((group) => row(group.name, group.value, group.count)).join('');
    groupsEl.setAttribute('aria-label', query.kind === 'vendor' ? 'Vendors' : 'Categories');
  }

  function render() {
    const base = plugins.filter(matchesBase);
    const shown = base.filter(inGroup);
    grid.dataset.view = view;
    grid.innerHTML = shown.map(cardHtml).join('');
    countEl.textContent = `${shown.length} / ${plugins.length} presets`;
    headingEl.textContent = query.value ? query.title || query.value : 'All plugins';
    titleEl.textContent = 'FL plugin index';
    document.title = 'FL plugin index - VEXT';
    empty.hidden = shown.length > 0;
    if (!shown.length) empty.textContent = !loaded ? 'Loading FL presets...'
      : plugins.length ? 'No presets match. Try another group or clear the filters.' : "No .fst presets found in FL Studio's plugin database.";
    renderGroups(base);
    syncButtons(viewsEl, 'view', view);
    syncButtons(typesEl, 'type', type);
    syncButtons(modesEl, 'mode', query.kind);
  }

  const sortedPlugins = (items) => items.slice().sort((a, b) => a.name.localeCompare(b.name) || String(a.key || '').localeCompare(String(b.key || '')));

  function cachedList() {
    try {
      const snapshot = JSON.parse(localStorage.getItem('fl-launcher-snapshot') || 'null');
      if (!snapshot || !CFG.token || snapshot.token !== CFG.token || Date.now() - snapshot.at > 15000) return null;
      return sortedPlugins(snapshot.plugins);
    } catch (_) { return null; }
  }

  async function loadList() {
    const seq = ++listSeq;
    const cached = cachedList();
    if (cached) { plugins = cached; loaded = true; }
    render();
    try {
      const answer = await api('/api/fl-launch/menu?include_plugins=true');
      if (seq !== listSeq) return;
      const incoming = sortedPlugins(answer.plugins || []);
      const changed = JSON.stringify(incoming) !== JSON.stringify(plugins);
      plugins = incoming;
      loaded = true;
      if (changed || !cached) render();
      try { localStorage.setItem('fl-launcher-snapshot', JSON.stringify({ token: CFG.token, at: Date.now(), plugins })); } catch (_) { /* storage quota */ }
    } catch (error) {
      if (seq !== listSeq) return;
      if (!loaded) { empty.hidden = false; empty.textContent = 'Could not read FL presets. Reopen the index to retry.'; }
      say(error.message, true);
    }
  }

  function setQuery(kind, value, title) {
    query = { kind: kind === 'vendor' ? 'vendor' : 'category', value: String(value || ''), title: String(title || '') };
    filter.value = '';
    type = 'all';
    cancelConfirm();
    return loadList();
  }

  viewsEl.addEventListener('click', (event) => {
    const button = event.target.closest('[data-view]');
    if (!button || !['cards', 'list', 'compact'].includes(button.dataset.view)) return;
    view = button.dataset.view;
    try { localStorage.setItem('fl-launcher-view', view); } catch (_) { /* storage unavailable */ }
    render();
  });
  typesEl.addEventListener('click', (event) => {
    const button = event.target.closest('[data-type]');
    if (!button || !['all', 'effect', 'instrument'].includes(button.dataset.type)) return;
    type = button.dataset.type;
    cancelConfirm();
    render();
  });
  modesEl.addEventListener('click', (event) => {
    const button = event.target.closest('[data-mode]');
    if (!button || !['category', 'vendor'].includes(button.dataset.mode)) return;
    query = { kind: button.dataset.mode, value: '', title: '' };
    cancelConfirm();
    render();
  });
  groupsEl.addEventListener('click', (event) => {
    const button = event.target.closest('[data-group]');
    if (!button) return;
    query.value = button.dataset.group;
    query.title = button.dataset.name;
    cancelConfirm();
    render();
    groupsEl.querySelector('[aria-current="true"]')?.focus({ preventScroll: true });
  });
  $('fln-reset').addEventListener('click', () => {
    query.value = ''; query.title = ''; type = 'all'; filter.value = '';
    cancelConfirm(); render(); filter.focus();
  });

  // ---------- click to load (confirm first) ----------

  const destinationText = (pick) => pick.kind === 'Generators'
    ? 'FL may replace the instrument on the selected Channel Rack channel, depending on its preset-loading behavior.'
    : 'FL will open it on the selected mixer insert. An occupied effect slot may be replaced; select an empty slot first.';

  function skipConfirm() {
    // Stamped with the per-launch API token: sessionStorage would end with this window,
    // but "don't ask again" lasts until VEXT closes.
    try { return !!CFG.token && localStorage.getItem(SKIP_KEY) === CFG.token; } catch (_) { return false; }
  }

  function askConfirm(id, pick) {
    pending = { id, pick };
    confirmTitle.textContent = `Load “${pick.name}” in FL Studio?`;
    confirmDestination.textContent = destinationText(pick);
    dontAsk.checked = false;
    confirmBar.hidden = false;
  }

  function cancelConfirm() {
    pending = null;
    confirmBar.hidden = true;
  }

  // Focus FL first (the controller's MIDI needs FL in front), then send the load.
  async function send(id, pick) {
    cancelConfirm();
    try {
      if (!invoke) throw new Error('Use the VEXT desktop app to hand focus to FL Studio.');
      await invoke('fl_focus');
      await post('load', { plugin_id: id, key: pick.key });
      say('Sent to FL Studio…');
      await waitForFL();
    } catch (error) { say(error.message, true); }
  }

  async function load(id, key = '') {
    try {
      say('Finding FL preset…');
      // Resolve while the connection check runs, as the main window does.
      const [current, pick] = await Promise.all([refreshConnection(), key ? api('/api/fl-launch/preset?key=' + encodeURIComponent(key)) : presets.preset(Number(id))]);
      if (!current.connected) throw new Error(current.message);
      if (!pick) { say(''); return; }
      if (skipConfirm()) return send(key ? null : Number(id), pick);
      askConfirm(key ? null : Number(id), pick);
    } catch (error) { say(error.message, true); }
  }

  const cardFor = (target) => (target && target.closest ? target.closest('[data-plugin-id]') : null);

  function clickCard(card) {
    if (!card) return undefined;
    if (!connection.connected) {
      say(connection.message, true);
      return undefined;
    }
    return load(card.dataset.pluginId, card.dataset.presetKey || '');
  }

  $('fln-send').addEventListener('click', () => {
    const current = pending;
    if (!current) return undefined;
    if (dontAsk.checked) {
      try { localStorage.setItem(SKIP_KEY, CFG.token || ''); } catch (_) { /* storage unavailable: asks again */ }
    }
    return send(current.id, current.pick);
  });
  $('fln-cancel').addEventListener('click', () => cancelConfirm());
  // The bubble folds the panel back into the circle on this event.
  $('fln-close').addEventListener('click', () => document.dispatchEvent(new CustomEvent('fl-index:close')));
  filter.addEventListener('input', render);

  grid.addEventListener('click', (event) => {
    // The click that ends a drag is not a load.
    if (Date.now() < suppressClick) return undefined;
    return clickCard(cardFor(event.target));
  });
  grid.addEventListener('keydown', (event) => {
    if ((event.key === 'Enter' || event.key === ' ') && cardFor(event.target)) {
      event.preventDefault();
      return clickCard(cardFor(event.target));
    }
    return undefined;
  });
  // A picture that fails to load shows the placeholder.
  grid.addEventListener('error', (event) => {
    const img = event.target;
    if (!img || img.tagName !== 'IMG') return;
    if (img.parentElement) img.parentElement.classList.add('is-empty');
    img.remove();
  }, true);

  // ---------- drag into FL (8 px with the left button held) ----------

  document.addEventListener('pointerdown', (event) => {
    if (!invoke || event.button !== 0 || event.target.closest('input, textarea, select, button, [contenteditable]')) return;
    const card = cardFor(event.target);
    if (card) down = { id: Number(card.dataset.pluginId), key: card.dataset.presetKey || '', x: event.clientX, y: event.clientY, started: false };
  });
  document.addEventListener('pointerup', () => { down = null; });
  document.addEventListener('pointercancel', () => { down = null; });
  document.addEventListener('dragstart', (event) => { if (invoke && cardFor(event.target)) event.preventDefault(); });
  document.addEventListener('pointermove', (event) => {
    const state = down;
    if (!state || state.started || !(event.buttons & 1) || Math.hypot(event.clientX - state.x, event.clientY - state.y) < 8) return undefined;
    state.started = true;
    suppressClick = Date.now() + 1500;
    nativeDrag = true;
    // OLE needs the button held: startDrag checks it again after the preset is known.
    return (state.key ? presets.startDragKey(state.key, () => down === state) : presets.startDrag(state.id, () => down === state))
      .catch((error) => say(error.message || String(error), true))
      .finally(() => {
        nativeDrag = false;
        if (down === state) down = null;
        suppressClick = Date.now() + 300;
      });
  });

  // ---------- panel ----------

  document.addEventListener('keydown', (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'f') { event.preventDefault(); filter.focus(); }
    if (event.key !== 'Escape') return;
    if (!confirmBar.hidden) cancelConfirm();
    else document.dispatchEvent(new CustomEvent('fl-index:close'));
  });

  window.VextFlLauncher = {
    load, setQuery, refresh: loadList, cancelConfirm, isDragging: () => nativeDrag,
  };

  refreshConnection();
  setInterval(refreshConnection, 2000);
  loadList();
})();
