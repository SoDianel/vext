// Database › Metadata: everything VEXT keeps about plugins, vendors, categories and pictures in one table
// (GET /api/metadata): the researched data of installed plugins, and what is kept for plugins, vendors and
// categories no longer installed (app/metadata_archive.py gives it back when they return). Clearing
// (POST /api/metadata/clear {ids}) erases the ticked rows, or one row's data, for good; the confirm says
// so when an installed plugin's data is among them. Same look as Configuration › Storage (css/config-storage.css);
// the order selector is the Scan tab's .browse-sort.
(function () {
  const V = window.VEXT;
  if (!V || V.dbMetadata) return;

  const FILTERS = [
    ['', 'All metadata'],
    ['installed', 'Installed'],
    ['orphan', 'Orphan (no plugin)'],
    ['no_plugin', 'No plugin'],
    ['image_only', 'Images without plugin'],
    ['stale', 'Other leftovers'],
  ];
  const ORDERS = [['name', 'Name'], ['vendor', 'Vendor'], ['status', 'Status'], ['date', 'Date']];
  const STATUS = {
    installed: { label: 'Installed', cls: 'ok' },
    no_plugin: { label: 'No plugin', cls: 'warn' },
    image_only: { label: 'Image only', cls: 'muted' },
    stale: { label: 'Leftover', cls: 'muted' },
  };
  const STATUS_RANK = new Map(Object.keys(STATUS).map((k, n) => [k, n]));
  // Sort keys: null means "nothing to sort by", and such rows always go last.
  const KEY = {
    name: (i) => (i.name ? String(i.name) : null),
    vendor: (i) => (i.vendor ? String(i.vendor) : null),
    status: (i) => (STATUS_RANK.has(i.status) ? STATUS_RANK.get(i.status) : null),
    date: (i) => {
      const t = i.date ? Date.parse(i.date) : NaN;
      return Number.isNaN(t) ? null : t;
    },
  };

  // Pure: the rows the table shows, in order. status '' is All, 'orphan' is any row with orphan === true,
  // the other values are item statuses. The search looks at name, vendor, path and the kept chips.
  function visibleItems(items, query, status, sortKey, sortDir) {
    const q = String(query || '').trim().toLowerCase();
    const list = items.filter((i) => {
      if (status === 'orphan' && i.orphan !== true) return false;
      if (status && status !== 'orphan' && i.status !== status) return false;
      if (!q) return true;
      return [i.name, i.vendor, i.path, ...(i.fields || [])].filter(Boolean).join(' ').toLowerCase().includes(q);
    });
    const keyOf = KEY[sortKey] || KEY.name;
    const dir = sortDir === 'desc' ? -1 : 1;
    const text = (s) => String(s || '');
    list.sort((a, b) => {
      const x = keyOf(a);
      const y = keyOf(b);
      if ((x === null) !== (y === null)) return x === null ? 1 : -1;
      let c = 0;
      if (x !== null) c = typeof x === 'string' ? x.localeCompare(y, undefined, { sensitivity: 'base', numeric: true }) : x - y;
      if (c) return c * dir;
      return text(a.name).localeCompare(text(b.name), undefined, { sensitivity: 'base', numeric: true })
        || text(a.id).localeCompare(text(b.id));
    });
    return list;
  }

  const esc = (s) => V.escHtml(s == null ? '' : String(s));
  const plural = (n, one, many) => (n === 1 ? one : many);
  const when = (iso) => {
    const d = iso ? new Date(iso) : null;
    return d && !Number.isNaN(d.getTime()) ? d.toLocaleDateString() : '';
  };

  let root = null;
  let data = null;   // the last answer: {items, counts}
  let busy = false;
  let refs = null;   // the controls frame() drew (null when there is no table)
  const picked = new Set(); // item ids ticked (kept across filtering; only the shown ones count and clear)
  const view = { query: '', status: '', sortKey: 'name', sortDir: 'asc' };

  const byId = (id) => (data ? data.items.find((i) => i.id === id) : null);
  const shown = () => visibleItems(data.items, view.query, view.status, view.sortKey, view.sortDir);

  function prune() {
    for (const id of [...picked]) if (!byId(id)) picked.delete(id);
  }

  // The confirm text: a warning first when any item belongs to an installed plugin (orphan === false).
  function confirmText(items) {
    const n = items.length;
    const k = items.filter((i) => i.orphan === false).length;
    const noun = plural(n, 'item', 'items');
    if (!k) return `Clear ${n} ${noun} for good? If a plugin comes back, it starts without this data.`;
    let lead;
    if (k === n && n === 1) lead = 'This belongs to an installed plugin';
    else if (k === 1) lead = '1 of these belongs to an installed plugin';
    else lead = `${k} of these belong to installed plugins`;
    const pictures = k === 1 ? 'Its pictures stay' : 'Their pictures stay';
    return `${lead}: their links, descriptions and your edits are erased (the next scan restores scanned names, `
      + `vendors and types). ${pictures}. Clear ${n} ${noun}?`;
  }

  function row(item) {
    const id = esc(item.id);
    const name = esc(item.name);
    const isPicked = picked.has(item.id);
    const sub = [item.vendor, item.format].filter(Boolean).join(' · ');
    const st = STATUS[item.status] || { label: item.status || '', cls: 'muted' };
    const side = item.kind === 'picture' && item.bytes ? V.fmtSize(item.bytes) : when(item.date);
    const chips = (item.fields || []).map((f) => `<span class="dm-chip">${esc(f)}</span>`).join('');
    return `<tr data-id="${id}"${isPicked ? ' class="is-picked"' : ''}>
      <td class="dm-check"><input type="checkbox" data-pick="${id}"${isPicked ? ' checked' : ''} aria-label="Select ${name}"></td>
      <td class="dm-name"><strong>${name}</strong>${sub ? `<span class="dm-sub">${esc(sub)}</span>` : ''}${item.path ? `<span class="dm-path" title="${esc(item.path)}">${esc(item.path)}</span>` : ''}</td>
      <td><span class="dm-chips">${chips}</span></td>
      <td><span class="badge ${st.cls}">${esc(st.label)}</span></td>
      <td class="dm-side">${esc(side)}</td>
      <td class="dm-acts"><button type="button" class="cs-btn danger" data-clear="${id}" aria-label="Clear ${name}"${busy ? ' disabled' : ''}>Clear</button></td>
    </tr>`;
  }

  // Draws the page skeleton (toolbar, table, action bar) from the view state; paint() fills it. Called when a
  // load or a clear starts and ends, so typing in the search box never rebuilds the controls.
  function frame() {
    if (!root) return;
    refs = null;
    if (!data) {
      root.innerHTML = `<div class="cs"><p class="cs-state">${busy ? 'Looking…' : 'Not loaded yet.'}</p></div>`;
      return;
    }
    const total = data.items.length;
    const body = total ? `
      <div class="dm-toolbar">
        <input type="search" class="dm-search" data-role="search" value="${esc(view.query)}" placeholder="Search name, vendor, path or kept data" aria-label="Search metadata">
        <select data-role="status" aria-label="Show">${FILTERS.map(([v, t]) => `<option value="${v}"${v === view.status ? ' selected' : ''}>${esc(t)}</option>`).join('')}</select>
        <div class="browse-sort">
          <select data-role="sort-key" aria-label="Order by">${ORDERS.map(([v, t]) => `<option value="${v}"${v === view.sortKey ? ' selected' : ''}>${esc(t)}</option>`).join('')}</select>
          <button type="button" class="browse-sort-dir" data-role="sort-dir" title="Ascending order">
            <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><path d="M8 13V3M4.5 6.5 8 3l3.5 3.5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>
          </button>
        </div>
      </div>
      <p class="dm-count" data-role="count" aria-live="polite"></p>
      <div class="cs-list dm-wrap">
        <table class="dm-table">
          <thead><tr>
            <th class="dm-check"><input type="checkbox" data-role="all" aria-label="Select all shown items"></th>
            <th>Name</th><th>Kept</th><th>Status</th><th class="dm-side">Date</th><th></th>
          </tr></thead>
          <tbody data-role="rows"></tbody>
        </table>
      </div>
      <div class="cs-bar-actions">
        <span data-role="bar-text"></span>
        <span class="cs-spacer"></span>
        <button type="button" class="danger" data-act="clear-selected" disabled>Clear selected (0)</button>
      </div>` : '<p class="cs-state">Nothing kept: VEXT has no researched data to show.</p>';
    root.innerHTML = `<div class="cs">
      <div class="cs-head">
        <div><h3>Metadata</h3><p class="hint">What VEXT keeps about plugins: researched links and descriptions, your edits,
          pictures, and what is left of plugins, vendors and categories that are gone. A scan keeps it when a plugin goes
          away and gives it back when the plugin is installed again, so nothing is researched twice.</p></div>
        <div class="cs-total"><span class="cs-total-num">${total}</span><span class="cs-total-label">${plural(total, 'item', 'items')} kept</span>
          <button type="button" class="cs-btn" data-act="refresh"${busy ? ' disabled' : ''}>Refresh</button></div>
      </div>
      ${body}
    </div>`;
    if (total) {
      refs = {
        search: root.querySelector('[data-role="search"]'),
        status: root.querySelector('[data-role="status"]'),
        sortKey: root.querySelector('[data-role="sort-key"]'),
        dir: root.querySelector('[data-role="sort-dir"]'),
        count: root.querySelector('[data-role="count"]'),
        all: root.querySelector('[data-role="all"]'),
        rows: root.querySelector('[data-role="rows"]'),
        barText: root.querySelector('[data-role="bar-text"]'),
        clear: root.querySelector('[data-act="clear-selected"]'),
      };
    }
    paint();
  }

  // Fills the table, the count line, the header checkbox and the action bar from the view state.
  function paint() {
    if (!refs || !data) return;
    const list = shown();
    const n = list.length;
    const t = data.items.length;
    const orphans = list.filter((i) => i.orphan === true).length;
    const countText = n === t ? `${n} ${plural(n, 'item', 'items')}` : `${n} of ${t} items`;
    refs.count.textContent = `${countText} · ${orphans} orphan`;
    refs.rows.innerHTML = n
      ? list.map(row).join('')
      : '<tr><td colspan="6" class="cs-state dm-empty">No item matches.</td></tr>';

    const desc = view.sortDir === 'desc';
    refs.dir.classList.toggle('desc', desc);
    refs.dir.setAttribute('aria-label', desc ? 'Descending order' : 'Ascending order');
    refs.dir.title = desc ? 'Descending order' : 'Ascending order';
    refs.dir.disabled = busy;
    refs.search.disabled = busy;
    refs.status.disabled = busy;
    refs.sortKey.disabled = busy;

    const shownPicked = list.filter((i) => picked.has(i.id)).length;
    refs.all.checked = n > 0 && shownPicked === n;
    refs.all.indeterminate = shownPicked > 0 && shownPicked < n;
    refs.all.disabled = !n || busy;

    // Only what is shown counts and is cleared: a ticked row the filter hides is never cleared unseen.
    const sel = shownPicked;
    refs.barText.textContent = sel ? `${sel} selected` : 'Tick items to clear, or use a row\'s button.';
    refs.clear.textContent = `Clear selected (${sel})`;
    refs.clear.disabled = !sel || busy;
  }

  async function load() {
    busy = true;
    frame();
    try {
      data = await V.api('/api/metadata');
      prune();
    } catch (err) {
      V.handleActionError(err);
    } finally {
      busy = false;
      frame();
    }
  }

  async function clear(ids) {
    const items = ids.map(byId).filter(Boolean);
    if (!items.length || busy) return;
    if (!(await V.confirmDialog(confirmText(items)))) return;
    busy = true;
    frame();
    try {
      const r = await V.api('/api/metadata/clear', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ids: items.map((i) => i.id) }),
      });
      data = r.now || data;
      items.forEach((i) => picked.delete(i.id));
      prune();
      V.toast(`Cleared ${items.length} ${plural(items.length, 'item', 'items')}.`);
    } catch (err) {
      V.handleActionError(err);
    } finally {
      busy = false;
      frame();
    }
  }

  function onClick(ev) {
    const b = ev.target.closest('button');
    if (!b || !root.contains(b) || b.disabled) return;
    if (b.dataset.clear) { clear([b.dataset.clear]); return; }
    switch (b.dataset.role || b.dataset.act) {
      case 'sort-dir':
        view.sortDir = view.sortDir === 'desc' ? 'asc' : 'desc';
        paint();
        break;
      case 'refresh': load(); break;
      case 'clear-selected': clear(shown().filter((i) => picked.has(i.id)).map((i) => i.id)); break;
      default: break;
    }
  }

  function onChange(ev) {
    const c = ev.target;
    if (!c || !c.matches) return;
    if (c.matches('input[data-pick]')) {
      if (c.checked) picked.add(c.dataset.pick); else picked.delete(c.dataset.pick);
    } else if (c.matches('input[data-role="all"]')) {
      shown().forEach((i) => (c.checked ? picked.add(i.id) : picked.delete(i.id)));
    } else if (c.matches('select[data-role="status"]')) {
      view.status = c.value;
    } else if (c.matches('select[data-role="sort-key"]')) {
      view.sortKey = c.value;
    } else {
      return;
    }
    paint();
  }

  function onInput(ev) {
    if (!ev.target.matches || !ev.target.matches('input[data-role="search"]')) return;
    view.query = ev.target.value;
    paint();
  }

  // Every time the pane is shown: a scan may have kept or given back data since.
  V.dbMetadata = {
    load(el) {
      if (!el) return;
      if (root !== el) {
        root = el;
        el.addEventListener('click', onClick);
        el.addEventListener('change', onChange);
        el.addEventListener('input', onInput);
      }
      load();
    },
    visibleItems,
  };
})();
