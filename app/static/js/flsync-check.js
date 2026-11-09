// The Check & fix tab of FL Studio Sync (the header and the Organize tab are js/flsync.js). It lists every
// plugin .fst in Effects and Generators with its thumbnail and what VEXT knows about the plugin, and fixes
// what it can: VEXT's picture as the thumbnail at the global size, or only a new size for the thumbnail it
// has (Resize; one row, the ticked ones, or all of them),
// a link for a plugin VEXT doesn't recognize, ignoring it (no longer flagged), or taking it out of FL's
// browser (Remove: its files move to Plugin database\Removed.vext-<time>\, nothing is deleted); a plugin FL
// detected but the browse tree doesn't show gets FL's own .fst copied in (Add to FL's browser). The answers of the /api/fl-sync
// endpoints replace the rows they touch, so a fix never needs a full re-check.
(function () {
  const V = window.VEXT;
  const CHUNK = 150;          // rows painted per frame, so 800+ rows don't block the page
  const BULK_CONFIRM = 20;    // fixing more pictures than this asks first
  const SIZE_NAMES = { 256: 'Large', 128: 'Medium', 64: 'Compact' };
  const FILTERS = [
    { key: 'all', label: 'All', count: 'fst' },
    { key: 'issues', label: 'Problems', count: 'with_issues' },
    { key: 'nopicture', label: 'No picture', count: 'no_picture' },
    { key: 'standardize', label: 'Non-standard size', count: 'can_standardize' },
    { key: 'unknown', label: 'Not recognized', count: 'unknown' },
    { key: 'linked', label: 'Linked', count: 'linked' },
    { key: 'ignored', label: 'Ignored', count: 'ignored' },
    { key: 'notinfl', label: 'VEXT not in FL', count: 'not_in_fl' },
    { key: 'notfiled', label: 'Not filed', count: 'not_filed' },
    { key: 'orphans', label: 'Orphan files', count: 'orphans' },
  ];
  const VIEW_FILTERS = ['notinfl', 'notfiled', 'orphans'];   // lists of other things, not .fst rows
  const ENTRY_TESTS = {
    all: () => true,
    issues: (e) => e.issues.length > 0,
    nopicture: (e) => !e.picture,
    standardize: (e) => e.can_standardize,
    unknown: (e) => !e.plugin_id && !e.ignored,
    linked: (e) => e.linked,
    ignored: (e) => e.ignored,
  };
  const EMPTY = {
    all: 'No plugin .fst files in FL Studio\'s Effects or Generators yet. Organize the plugins, or run "Find installed plugins" in FL\'s Plugin Manager first.',
    issues: 'No problems: every plugin has a working thumbnail.',
    nopicture: 'Every plugin has a picture as its FL thumbnail.',
    standardize: 'Every thumbnail is Large, Medium or Compact.',
    unknown: 'Every plugin in the browse tree is recognized by VEXT.',
    linked: 'No plugin is linked by hand. Use Link on a plugin VEXT doesn\'t recognize.',
    ignored: 'Nothing is ignored. Use Ignore on a plugin VEXT doesn\'t recognize to stop flagging it.',
    notinfl: 'Every VEXT plugin is in FL Studio\'s plugin database.',
    notfiled: 'Everything FL Studio detected is in the browse tree.',
    orphans: 'No orphan files: every .png and .nfo has its .fst beside it.',
  };
  const HINTS = {
    notinfl: 'A VEXT plugin missing from FL Studio\'s database most likely hasn\'t been scanned by FL\'s Plugin Manager yet: open it and run "Find installed plugins".',
    notfiled: 'FL Studio knows these, but no .fst for them is in Effects or Generators. Add to FL\'s browser copies FL\'s own entry into the plugin\'s vendor folder with VEXT\'s picture; one FL\'s database doesn\'t have needs a scan in FL\'s Plugin Manager first.',
    orphans: 'These .png or .nfo files have no .fst with the same name beside them.',
  };

  let ui = null;               // the mounted tab's elements
  let inv = null;              // the last inventory: entries, orphans, not_in_fl, not_filed, counts, root
  let byFst = new Map();       // .fst path -> its entry (the same objects as inv.entries)
  let filter = 'all';
  let section = '';            // '' = both sections, or 'Effects' / 'Generators'
  let query = '';
  let checking = false;
  let ran = false;             // a check has run at least once
  let paintToken = 0;          // a newer paint cancels the chunks still waiting
  let viewItems = [];          // what the list shows now (entries, or the items of a view)
  let dialog = null;           // the open link dialog
  const picked = new Set();    // .fst paths ticked for a bulk fix
  const stamps = new Map();    // .fst path -> time of its last fix or link (cache-buster for its thumbnail)

  const esc = (s) => V.escHtml(s);
  const globalSize = () => (V.flSync && V.flSync.thumbSize && V.flSync.thumbSize()) || 256;
  const sizeLabel = (px) => `${SIZE_NAMES[px] || 'Custom'} (${px} px)`;
  const folderPath = (e) => e.section + (e.folder ? '\\' + e.folder.split('/').join('\\') : '');
  const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
  const post = (path, body) => V.api(path, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  const selectedEntries = () => [...picked].map((f) => byFst.get(f)).filter(Boolean);

  // Entries from an older backend may lack the newer fields.
  function normalize(e) {
    return { ...e, size_name: e.size_name || null, can_standardize: !!e.can_standardize, linked: !!e.linked, ignored: !!e.ignored, issues: e.issues || [] };
  }

  function thumbUrl(e) {
    const q = new URLSearchParams({ path: e.picture });
    if (stamps.has(e.fst)) q.set('v', String(stamps.get(e.fst)));
    return `${V.API_BASE || ''}/api/fl-sync/thumbnail?${q}`;
  }

  // A fix can change this plugin's thumbnail: VEXT has a picture and the thumbnail isn't already it at the global size.
  // Resize: the thumbnail it has, at the global size (any of the three sizes is fine; this just matches them).
  function canResize(e) {
    return !!e.picture && !!e.size && e.size[1] !== globalSize();
  }

  // The row's own "use VEXT's picture": only when it has none, or shows one VEXT didn't make.
  function offersVext(e) {
    return e.vext_picture && (!e.picture || e.source !== 'vext');
  }

  // Fix all / bulk fix: VEXT's picture at the global size (resizes VEXT's own thumbnails too).
  function canFix(e) {
    if (!e.vext_picture) return false;
    return !e.picture || e.can_standardize || e.source !== 'vext' || !e.size || e.size[1] !== globalSize();
  }

  // The counts of the current inventory. The server's counts give the lists (not in FL, not filed, orphans).
  function recount() {
    const es = inv.entries;
    const count = (test) => es.filter(test).length;
    return {
      ...inv.counts,
      fst: es.length,
      with_issues: count((e) => e.issues.length > 0),
      no_picture: count((e) => !e.picture),
      vext_pictures: count((e) => e.source === 'vext'),
      can_add_picture: count((e) => e.vext_picture && e.source !== 'vext'),
      can_standardize: count((e) => e.can_standardize),
      unknown: count((e) => !e.plugin_id && !e.ignored),
      linked: count((e) => e.linked),
      ignored: count((e) => e.ignored),
      not_in_fl: inv.not_in_fl.length,
      not_filed: inv.not_filed.length,
      orphans: inv.orphans.length,
    };
  }

  // Updates the counts and tells the header (vext:flsync-inventory) after every load or change.
  function announce() {
    inv.counts = recount();
    document.dispatchEvent(new CustomEvent('vext:flsync-inventory', { detail: { counts: inv.counts, root: inv.root } }));
    updateChips();
  }

  // The items of the current filter and section, before the search.
  function filtered() {
    if (filter === 'notinfl') return inv.not_in_fl;
    if (filter === 'notfiled') return inv.not_filed;
    if (filter === 'orphans') return inv.orphans;
    const test = ENTRY_TESTS[filter];
    return inv.entries.filter((e) => test(e) && (!section || e.section === section));
  }

  function searched(items) {
    const q = query.trim().toLowerCase();
    if (!q) return items;
    const hit = (...parts) => parts.some((s) => String(s || '').toLowerCase().includes(q));
    if (filter === 'notinfl') return items.filter((p) => hit(p.name, p.vendor, p.host_path));
    if (filter === 'notfiled') return items.filter((p) => hit(p.name, p.vendor, p.fl_name));
    if (filter === 'orphans') return items.filter((o) => hit(o.path));
    return items.filter((e) => hit(e.name, e.plugin_name, e.vendor, folderPath(e), e.section));
  }

  // ---------- rows ----------

  function entryRow(e) {
    const size = globalSize();
    const px = e.size ? e.size[1] : null;
    const badge = e.picture && px ? `<span class="flc-size">${esc(e.size_name || `${px} px`)}</span>` : '';
    const thumb = e.picture
      ? `<img src="${esc(thumbUrl(e))}" alt="" loading="lazy" decoding="async">${badge}`
      : '<span class="flc-none">No picture</span>';
    const vext = e.plugin_id
      ? `<strong>${esc(e.plugin_name)}</strong>${e.vendor ? ` <span class="flc-sub">${esc(e.vendor)}</span>` : ''}
         <span class="flc-badges">${e.linked ? '<span class="badge">Linked</span>' : ''}${e.vext_picture ? '' : '<span class="badge muted">No VEXT picture</span>'}</span>`
      : e.ignored
        ? '<span class="badge muted">Ignored</span> <span class="flc-sub">Not recognized; not flagged any more</span>'
        : '<span class="badge warn">Not recognized</span> <span class="flc-sub">Link it, ignore it, or remove it from FL\'s browser</span>';
    const status = e.ignored ? '<span class="badge muted">Ignored</span>' : e.issues.length
      ? e.issues.map((i) => `<span class="flc-issue">${esc(i)}</span>`).join('')
      : '<span class="badge ok">OK</span>';
    const acts = [];
    // The main button only for a real problem: no picture, or a size that is none of the three.
    if (canResize(e)) {
      acts.push(`<button type="button" class="flc-btn${e.can_standardize ? ' primary' : ''}" data-act="resize" data-fst="${esc(e.fst)}" title="The same picture, ${esc(sizeLabel(size))}">Resize to ${esc(SIZE_NAMES[size])}</button>`);
    }
    if (offersVext(e)) {
      acts.push(`<button type="button" class="flc-btn${e.picture ? '' : ' primary'}" data-act="fix" data-fst="${esc(e.fst)}" title="Write VEXT's picture as FL's thumbnail at ${esc(sizeLabel(size))}">${e.picture ? 'Use VEXT picture' : 'Fix picture'}</button>`);
    }
    if (e.plugin_id && !e.vext_picture) {
      acts.push(`<button type="button" class="flc-btn" data-act="set-pic" data-id="${Number(e.plugin_id)}">Set a picture…</button>`);
    }
    if (e.linked) {
      acts.push(`<button type="button" class="flc-btn" data-act="link" data-fst="${esc(e.fst)}">Change link…</button>`);
      acts.push(`<button type="button" class="flc-btn" data-act="unlink" data-fst="${esc(e.fst)}">Unlink</button>`);
    } else if (!e.plugin_id) {
      acts.push(`<button type="button" class="flc-btn" data-act="link" data-fst="${esc(e.fst)}">Link…</button>`);
      acts.push(e.ignored
        ? `<button type="button" class="flc-btn" data-act="unignore" data-fst="${esc(e.fst)}" title="Flag it again as not recognized">Unignore</button>`
        : `<button type="button" class="flc-btn" data-act="ignore" data-fst="${esc(e.fst)}" title="Leave it as it is and stop flagging it">Ignore</button>`);
      acts.push(`<button type="button" class="flc-btn danger" data-act="remove" data-fst="${esc(e.fst)}" title="Take it out of FL Studio's browser: its .fst, .nfo and picture move to Plugin database\\Removed.vext-…">Remove…</button>`);
    }
    if (V.isTauri()) acts.push(`<button type="button" class="flc-btn" data-act="reveal" data-fst="${esc(e.fst)}">Show in Explorer</button>`);
    return `<div class="flc-row${e.issues.length ? ' has-issues' : ''}" data-fst="${esc(e.fst)}">
      <label class="flc-check"><input type="checkbox" data-check="${esc(e.fst)}"${picked.has(e.fst) ? ' checked' : ''} aria-label="Select ${esc(e.name)}"></label>
      <div class="flc-thumb">${thumb}</div>
      <div class="flc-fl"><strong>${esc(e.name)}</strong><span class="flc-path">${esc(folderPath(e))}</span></div>
      <div class="flc-info">
        <div class="flc-vext">${vext}</div>
        <div class="flc-status">${status}</div>
      </div>
      <div class="flc-actions">${acts.join('')}</div>
    </div>`;
  }

  // The rows of the views (not in FL, not filed, orphans): plain text, the explanation from the old UI.
  function viewRow(item) {
    if (filter === 'notinfl') {
      return `<div class="flc-row flc-plain">
        <div class="flc-fl"><strong>${esc(item.name)}</strong><span class="flc-path">${esc(item.vendor || '')}${item.vendor ? ' · ' : ''}${esc(item.format)}</span></div>
        <div class="flc-info"><div class="flc-vext"><span class="flc-path">${esc(item.host_path)}</span></div>
          <div class="flc-status"><span class="flc-issue">${esc(item.reason)}: probably not scanned by FL's Plugin Manager yet.</span></div></div>
        <div class="flc-actions"><button type="button" class="flc-btn" data-act="pm">Open FL Plugin Manager</button></div>
      </div>`;
    }
    if (filter === 'notfiled') {
      return `<div class="flc-row flc-plain">
        <div class="flc-fl"><strong>${esc(item.name)}</strong><span class="flc-path">${esc(item.vendor || '')}${item.vendor ? ' · ' : ''}${esc(item.format)}</span></div>
        <div class="flc-info"><div class="flc-vext"><span class="flc-path">${esc(item.host_path)}</span></div>
          <div class="flc-status"><span class="flc-issue">${item.can_add
            ? `FL Studio knows it as "${esc(item.fl_name)}", but no .fst for it is in Effects\\ or Generators\\.`
            : `FL Studio knows it as "${esc(item.fl_name)}", but its plugin database has no entry VEXT can copy: scan it with FL's Plugin Manager.`}</span></div></div>
        <div class="flc-actions">${item.can_add
          ? `<button type="button" class="flc-btn primary" data-act="add-detected" data-id="${Number(item.id)}" title="Copies FL's own entry into ${esc(item.vendor || 'the section')}'s folder with VEXT's picture at ${esc(sizeLabel(globalSize()))}">Add to FL's browser</button>`
          : '<button type="button" class="flc-btn" data-act="pm">Open FL Plugin Manager</button>'}</div>
      </div>`;
    }
    const name = item.path.split(/[\\/]/).pop();
    const where = item.section + (item.folder ? '\\' + item.folder.split('/').join('\\') : '');
    return `<div class="flc-row flc-plain">
      <div class="flc-fl"><strong>${esc(name)}</strong><span class="flc-path">${esc(where)}</span></div>
      <div class="flc-info"><div class="flc-vext"><span class="flc-path">${esc(item.path)}</span></div>
        <div class="flc-status"><span class="flc-issue">No .fst with this name beside it.</span></div></div>
      <div class="flc-actions">${V.isTauri() ? `<button type="button" class="flc-btn" data-act="reveal" data-path="${esc(item.path)}">Show in Explorer</button>` : ''}</div>
    </div>`;
  }

  // Paints the rows in chunks, one chunk per frame. A newer paint cancels the rest.
  function renderList() {
    if (!ui) return;
    const token = ++paintToken;
    if (!inv) {
      viewItems = [];
      ui.summary.textContent = '';
      ui.hint.hidden = true;
      ui.list.innerHTML = `<div class="flc-state">${checking
        ? 'Checking the browse tree…'
        : 'Not checked yet. <button type="button" class="flc-btn primary" data-act="check">Check now</button>'}</div>`;
      updateBulk();
      return;
    }
    const total = filtered();
    const items = searched(total);
    const view = VIEW_FILTERS.includes(filter);
    viewItems = items;
    ui.summary.textContent = checking ? 'Checking the browse tree…' : `Showing ${items.length} of ${total.length}`;
    ui.hint.textContent = HINTS[filter] || '';
    ui.hint.hidden = !HINTS[filter];
    // Not filed: add every one FL's database has an entry for, in one go.
    const addable = filter === 'notfiled' ? items.filter((x) => x.can_add) : [];
    ui.viewbar.hidden = !addable.length;
    ui.viewbar.innerHTML = addable.length
      ? `<button type="button" class="primary" data-act="add-all-detected">Add all ${addable.length} to FL's browser</button>
         <span class="flc-sub">With VEXT's pictures at ${esc(sizeLabel(globalSize()))}. FL's own Installed folder is only read.</span>`
      : '';
    if (!items.length) {
      let msg = EMPTY[filter];
      if (total.length && query.trim()) msg = `Nothing matches “${query.trim()}”.`;
      else if (!total.length && section && !view) msg = `Nothing in ${section} for this filter.`;
      ui.list.innerHTML = `<div class="flc-state">${esc(msg)}</div>`;
    } else {
      const rowHtml = view ? viewRow : entryRow;
      ui.list.innerHTML = '';
      let i = 0;
      const paint = () => {
        if (token !== paintToken) return;
        ui.list.insertAdjacentHTML('beforeend', items.slice(i, i + CHUNK).map(rowHtml).join(''));
        i += CHUNK;
        if (i < items.length) requestAnimationFrame(paint);
      };
      paint();
    }
    updateBulk();
  }

  // After a fix, a link or a removal: a row that no longer matches the filter, the section or the search
  // leaves the list at once (its tick goes too); the others are repainted in place. `gone`: entries that
  // no longer exist at all (removed).
  function refreshRows(changed, gone = []) {
    const stay = new Set(searched(filtered()));
    const rows = new Map();
    ui.list.querySelectorAll('.flc-row[data-fst]').forEach((r) => rows.set(r.dataset.fst, r));
    const leaving = new Set([...gone, ...changed.filter((e) => !stay.has(e))]);
    for (const e of leaving) {
      const row = rows.get(e.fst);
      if (row) row.remove();
      picked.delete(e.fst);
    }
    for (const e of changed) {
      const row = rows.get(e.fst);
      if (row && !leaving.has(e)) row.outerHTML = entryRow(e);
    }
    if (!leaving.size) return;
    viewItems = viewItems.filter((e) => !leaving.has(e));
    if (!viewItems.length) renderList();   // the filter's empty state
    else ui.summary.textContent = `Showing ${viewItems.length} of ${filtered().length}`;
  }

  // Puts the entries an answer sends back in place of the old ones: no re-check.
  function applyEntries(fresh) {
    const now = Date.now();
    const changed = [];
    for (const f of fresh) {
      let e = byFst.get(f.fst);
      if (e) {
        Object.assign(e, normalize(f));
      } else {
        e = normalize(f);
        inv.entries.push(e);
        byFst.set(e.fst, e);
      }
      stamps.set(e.fst, now);     // the thumbnail may have changed: fetch it again
      changed.push(e);
    }
    announce();
    refreshRows(changed);
    updateBulk();
  }

  // ---------- toolbar, bulk bar ----------

  function updateChips() {
    if (!ui) return;
    const fixable = inv ? inv.entries.filter(canFix).length : 0;
    ui.fixAll.disabled = !fixable || checking;
    ui.fixAll.textContent = fixable ? `Fix all pictures (${fixable})` : 'Fix all pictures';
    const resizable = inv ? inv.entries.filter(canResize).length : 0;
    ui.resizeAll.disabled = !resizable || checking;
    ui.resizeAll.textContent = `Resize all to ${SIZE_NAMES[globalSize()]}${resizable ? ` (${resizable})` : ''}`;
    ui.chips.forEach(({ btn, count }) => {
      btn.querySelector('.flc-n').textContent = inv ? inv.counts[count] ?? 0 : '';
      btn.setAttribute('aria-pressed', String(btn.dataset.filter === filter));
    });
    const view = VIEW_FILTERS.includes(filter);
    ui.segs.forEach((b) => {
      b.setAttribute('aria-pressed', String(b.dataset.section === section));
      b.disabled = view;   // the views have no section
    });
  }

  function updateBulk() {
    if (!ui) return;
    const sel = selectedEntries();
    const fixable = sel.filter(canFix);
    ui.bulk.hidden = VIEW_FILTERS.includes(filter) || !sel.length;
    ui.bulkCount.textContent = `${sel.length} selected`;
    ui.bulkFix.textContent = `Fix pictures (${fixable.length})`;
    ui.bulkFix.disabled = !fixable.length;
    const resizable = sel.filter(canResize);
    ui.bulkResize.hidden = !resizable.length;
    ui.bulkResize.textContent = `Resize (${resizable.length})`;
    // Only entries VEXT doesn't recognize can be removed (the others are VEXT's to file).
    const removable = sel.filter((e) => !e.plugin_id);
    ui.bulkRemove.hidden = !removable.length;
    ui.bulkRemove.textContent = `Remove (${removable.length})`;
    const ignorable = removable.filter((e) => !e.ignored);
    ui.bulkIgnore.hidden = !ignorable.length;
    ui.bulkIgnore.textContent = `Ignore (${ignorable.length})`;
    const skipped = sel.length - fixable.length;
    ui.bulkNote.textContent = skipped ? `${skipped} will be skipped (nothing to fix, or no VEXT picture).` : '';
  }

  function syncChecks() {
    ui.list.querySelectorAll('input[data-check]').forEach((c) => { c.checked = picked.has(c.dataset.check); });
  }

  // Fixes these entries' pictures at the global size. One answer for all of them.
  async function fixEntries(list, btn) {
    const size = globalSize();
    if (btn) btn.disabled = true;
    try {
      const r = await post('/api/fl-sync/fix-pictures', { fst: list.map((e) => e.fst), size });
      const skipped = r.skipped || [];
      list.forEach((e) => picked.delete(e.fst));
      applyEntries(r.entries || []);
      syncChecks();
      const parts = [r.fixed ? `${plural(r.fixed, 'picture', 'pictures')} fixed at ${sizeLabel(size)}.` : 'No picture was fixed.'];
      if (skipped.length) parts.push(`${skipped.length} skipped: ${skipped[0].reason}${skipped.length > 1 ? ' (and others)' : ''}.`);
      if (r.backup) parts.push(`Replaced thumbnails were backed up to ${r.backup}.`);
      V.toast(parts.join(' '), !r.fixed);
    } catch (err) {
      V.handleActionError(err);
    } finally {
      if (btn && btn.isConnected) btn.disabled = false;
    }
  }

  // ---------- the link dialog ----------

  function closeLink() {
    if (!dialog) return;
    document.removeEventListener('keydown', dialog.onKey, true);
    clearTimeout(dialog.timer);
    dialog.back.remove();
    if (dialog.prev && dialog.prev.isConnected) dialog.prev.focus();
    dialog = null;
  }

  // A plugin row in the dialog: the suggestions (with a score) or the search results.
  function pickRow(p, score) {
    const has = p.has_picture !== undefined ? p.has_picture : p.has_image;
    const pct = score == null ? null : Math.round(score * 100);
    return `<div class="candidate-card flc-pick">
      <div class="info"><strong>${esc(p.name)}</strong> <span class="path">${esc(p.vendor || '')}${p.vendor ? ' · ' : ''}${esc(p.format || '')}</span></div>
      ${pct == null ? '' : `<span class="flc-score" title="Match ${pct}%"><span style="width: ${pct}%"></span></span>`}
      ${p.ignored ? '<span class="badge warn" title="Ignored in VEXT: linking brings it back into the catalog">Ignored in VEXT</span>' : ''}
      <span class="badge ${has ? 'ok' : 'muted'}">${has ? 'Has picture' : 'No picture'}</span>
      ${p.ignored
    ? `<button type="button" class="flc-btn" data-link-ignored="${esc(p.host_path)}" title="Brings it back into VEXT's catalog, then links it">Bring back and link</button>`
    : `<button type="button" class="flc-btn" data-link-to="${Number(p.id)}">Link</button>`}
    </div>`;
  }

  // "Include ignored plugins" in the link dialog, remembered.
  const INC_KEY = 'pa-fls-link-ignored';
  const includeIgnored = () => { try { return localStorage.getItem(INC_KEY) === '1'; } catch { return false; } };

  function openLink(e) {
    closeLink();
    const size = globalSize();
    const back = document.createElement('div');
    back.className = 'modal-backdrop flc-link-backdrop';
    back.innerHTML = `
      <div class="modal flc-link" role="dialog" aria-modal="true" aria-labelledby="flc-link-title">
        <h2 id="flc-link-title">Link “${esc(e.name)}” to a VEXT plugin</h2>
        <p class="hint">FL Studio calls it <strong>${esc(e.name)}</strong>, in ${esc(folderPath(e))}. Pick the VEXT plugin it is; the link stays until you change or remove it.</p>
        ${e.linked ? `<p class="flc-link-now">Linked now to <strong>${esc(e.plugin_name)}</strong>${e.vendor ? ` · ${esc(e.vendor)}` : ''}.</p>` : ''}
        <h3 class="flc-link-h">Suggestions</h3>
        <div class="card-list flc-link-cands"><div class="empty-state">Looking for matches…</div></div>
        <h3 class="flc-link-h">Search VEXT's plugins</h3>
        <input type="search" class="flc-link-search" placeholder="Plugin name or vendor" aria-label="Search VEXT's plugins">
        <div class="card-list flc-link-results"><div class="empty-state">Type to search.</div></div>
        <label class="flc-link-inc" title="Plugins you ignored in VEXT (Configuration › Ignored plugins) aren't in the catalog. Linking one brings it back."><input type="checkbox"${includeIgnored() ? ' checked' : ''}> Include ignored plugins</label>
        <label class="flc-link-pic"><input type="checkbox" checked> Also make its picture the thumbnail: ${esc(sizeLabel(size))}</label>
        <div class="flc-link-actions">
          ${e.linked ? '<button type="button" class="danger" data-link-unlink>Unlink</button>' : ''}
          <button type="button" data-link-cancel>Cancel</button>
        </div>
      </div>`;
    document.body.appendChild(back);

    const search = back.querySelector('.flc-link-search');
    const candsEl = back.querySelector('.flc-link-cands');
    const resultsEl = back.querySelector('.flc-link-results');
    const picCheck = back.querySelector('.flc-link-pic input');
    const incCheck = back.querySelector('.flc-link-inc input');
    const mine = { back, prev: document.activeElement, timer: 0 };
    // Focus stays inside the dialog; Escape closes it (before the page sees the key).
    mine.onKey = (ev) => {
      if (ev.key === 'Escape') {
        ev.preventDefault();
        ev.stopPropagation();
        closeLink();
        return;
      }
      if (ev.key !== 'Tab') return;
      const items = [...back.querySelectorAll('button:not([disabled]), input:not([disabled])')];
      if (!items.length) return;
      const first = items[0];
      const last = items[items.length - 1];
      const at = document.activeElement;
      if (ev.shiftKey && (at === first || !back.contains(at))) {
        ev.preventDefault();
        last.focus();
      } else if (!ev.shiftKey && (at === last || !back.contains(at))) {
        ev.preventDefault();
        first.focus();
      }
    };
    document.addEventListener('keydown', mine.onKey, true);
    dialog = mine;

    // Answers for a dialog that was closed meanwhile are dropped.
    const stillOpen = () => dialog === mine;

    const loadCandidates = () => {
      const params = { name: e.name };
      if (incCheck.checked) params.include_ignored = 'true';
      candsEl.innerHTML = '<div class="empty-state">Looking for matches…</div>';
      V.api(`/api/fl-sync/link-candidates?${new URLSearchParams(params)}`).then((r) => {
        if (!stillOpen()) return;
        const list = r.candidates || [];
        candsEl.innerHTML = list.length
          ? list.map((c) => pickRow(c, c.score)).join('')
          : '<div class="empty-state">No close matches. Search below.</div>';
      }).catch((err) => {
        if (stillOpen()) candsEl.innerHTML = `<div class="empty-state">${esc(err.message)}</div>`;
      });
    };
    loadCandidates();

    const findPlugins = async (q) => {
      if (!q) {
        resultsEl.innerHTML = '<div class="empty-state">Type to search.</div>';
        return;
      }
      resultsEl.innerHTML = '<div class="empty-state">Searching…</div>';
      try {
        let rows = await V.api(`/api/plugins?${new URLSearchParams({ q })}`);
        if (incCheck.checked) {
          const ign = await V.api(`/api/fl-sync/ignored-plugins?${new URLSearchParams({ q })}`);
          rows = [...(ign.plugins || []), ...rows];   // first: they're what the box asked for
        }
        if (!stillOpen() || search.value.trim() !== q) return;
        const shown = rows.slice(0, 30);
        resultsEl.innerHTML = shown.length
          ? shown.map((p) => pickRow(p, null)).join('')
            + (rows.length > shown.length ? `<div class="empty-state">${rows.length - shown.length} more: refine the search.</div>` : '')
          : '<div class="empty-state">No VEXT plugin matches.</div>';
      } catch (err) {
        if (stillOpen()) resultsEl.innerHTML = `<div class="empty-state">${esc(err.message)}</div>`;
      }
    };
    search.addEventListener('input', () => {
      clearTimeout(mine.timer);
      mine.timer = setTimeout(() => findPlugins(search.value.trim()), 250);
    });
    // Ignored plugins in or out: both lists again; the choice is remembered.
    incCheck.addEventListener('change', () => {
      try { localStorage.setItem(INC_KEY, incCheck.checked ? '1' : ''); } catch { /* storage off */ }
      loadCandidates();
      findPlugins(search.value.trim());
    });

    // Links the .fst to a plugin (null = remove the link). The answer replaces the rows; the dialog closes.
    const linkTo = async (pluginId, name, btn, ignoredPath) => {
      if (btn) btn.disabled = true;
      try {
        const r = await post('/api/fl-sync/link', {
          name: e.name, plugin_id: pluginId, ignored_host_path: ignoredPath || null,
          fix_picture: (pluginId != null || !!ignoredPath) && picCheck.checked, size,
        });
        closeLink();
        applyEntries(r.entries || []);
        if (ignoredPath) V.toast(`“${name}” is back in VEXT's catalog and linked.${r.fixed ? ' Picture fixed.' : ''}`);
        else if (pluginId == null) V.toast('Link removed.');
        else V.toast(`Linked to “${name}”.${r.fixed ? ' Picture fixed.' : ''}`);
      } catch (err) {
        if (btn) btn.disabled = false;
        V.handleActionError(err);
      }
    };
    back.addEventListener('click', (ev) => {
      if (ev.target === back) {
        closeLink();
        return;
      }
      const b = ev.target.closest('button');
      if (!b || !back.contains(b)) return;
      if (b.hasAttribute('data-link-cancel')) closeLink();
      else if (b.hasAttribute('data-link-unlink')) linkTo(null, '', b);
      else if (b.dataset.linkTo) linkTo(Number(b.dataset.linkTo), b.closest('.flc-pick').querySelector('strong').textContent, b);
      else if (b.dataset.linkIgnored) linkTo(null, b.closest('.flc-pick').querySelector('strong').textContent, b, b.dataset.linkIgnored);
    });
    search.focus();
  }

  // ---------- actions ----------

  async function check() {
    if (checking || !ui) return;
    checking = true;
    ui.check.disabled = true;
    ui.summary.textContent = 'Checking the browse tree…';
    try {
      const data = await V.api('/api/fl-sync/inventory');
      inv = {
        ...data,
        entries: (data.entries || []).map(normalize),
        orphans: data.orphans || [],
        not_in_fl: data.not_in_fl || [],
        not_filed: data.not_filed || [],
      };
      byFst = new Map(inv.entries.map((e) => [e.fst, e]));
      for (const f of [...picked]) if (!byFst.has(f)) picked.delete(f);
      ran = true;
      announce();
      ui.repair.disabled = false;
    } catch (err) {
      V.handleActionError(err);
    } finally {
      checking = false;
      ui.check.disabled = false;
      updateChips();
      renderList();
    }
  }

  async function repair() {
    if (!(await V.confirmDialog('Write a .nfo for every .png that sits beside its .fst without one pointing at it, '
      + 'and hide plugin .nfo/.png files the way FL Studio does? No picture is changed.'))) return;
    try {
      const r = await V.api('/api/fl-sync/repair', { method: 'POST' });
      V.toast(`${r.nfo_written} .nfo written, ${r.hidden} file(s) hidden.`);
      check();
    } catch (err) {
      V.handleActionError(err);
    }
  }

  async function reveal(path) {
    try {
      await V.revealInExplorer(path);
    } catch (err) {
      V.handleActionError(err);
    }
  }

  async function openPluginManager() {
    try {
      await V.api('/api/fl-sync/open-plugin-manager', { method: 'POST' });
      V.toast('FL Studio\'s Plugin Manager is opening. Run "Find installed plugins" there, then Check again.');
    } catch (err) {
      V.toast(err.message, true);
    }
  }

  async function unlink(e) {
    try {
      const r = await post('/api/fl-sync/link', { name: e.name, plugin_id: null, fix_picture: false, size: globalSize() });
      applyEntries(r.entries || []);
      V.toast('Link removed.');
    } catch (err) {
      V.handleActionError(err);
    }
  }

  // Every plugin VEXT has a picture for gets it at the global size (the server goes through the whole
  // tree), then the list is checked again.
  async function fixAll(btn) {
    const n = inv ? inv.entries.filter(canFix).length : 0;
    if (!n) return;
    const size = globalSize();
    if (!(await V.confirmDialog(`Give ${plural(n, 'plugin', 'plugins')} VEXT's picture as FL Studio's thumbnail at `
      + `${sizeLabel(size)}? Thumbnails VEXT didn't make are backed up first to Plugin database\\Thumbnails.vext-backup-<date>.`))) return;
    btn.disabled = true;
    btn.textContent = 'Fixing all pictures…';
    ui.summary.textContent = `Writing ${plural(n, 'thumbnail', 'thumbnails')}… this can take a minute.`;
    try {
      const r = await post('/api/fl-sync/fix-all-pictures', { size });
      const parts = [`${plural(r.written, 'thumbnail', 'thumbnails')} written at ${sizeLabel(size)}, ${r.current} already current.`];
      if (r.failed) parts.push(`${r.failed} couldn't be written.`);
      if (r.backup) parts.push(`Replaced thumbnails were backed up to ${r.backup}.`);
      V.toast(parts.join(' '), !!r.failed);
      await check();
    } catch (err) {
      V.handleActionError(err);
    } finally {
      if (btn.isConnected) updateChips();
    }
  }

  // Takes unrecognized entries out of FL's browser. Their files move, so the rows simply go.
  async function removeEntries(list, btn) {
    list = list.filter((e) => !e.plugin_id);
    if (!list.length) return;
    const what = list.length === 1 ? `“${list[0].name}”` : plural(list.length, 'entry', 'entries');
    if (!(await V.confirmDialog(`Remove ${what} from FL Studio's browser? VEXT doesn't recognize `
      + `${list.length === 1 ? 'it' : 'them'}. The .fst, its .nfo and its picture move to Plugin database\\Removed.vext-<time>\\ `
      + '(nothing is deleted: move them back to restore). FL Studio shows the change after it rescans its browser.'))) return;
    if (btn) btn.disabled = true;
    try {
      const r = await post('/api/fl-sync/remove', { fst: list.map((e) => e.fst) });
      const gone = new Set(r.removed || []);
      // The server answers resolved paths; compare without case (Windows).
      const lower = new Set([...gone].map((x) => x.toLowerCase()));
      const removed = list.filter((e) => lower.has(e.fst.toLowerCase()));
      for (const e of removed) { byFst.delete(e.fst); picked.delete(e.fst); }
      inv.entries = inv.entries.filter((e) => !removed.includes(e));
      announce();
      refreshRows([], removed);
      const skipped = r.skipped || [];
      const parts = [removed.length ? `${plural(removed.length, 'entry', 'entries')} removed, moved to ${r.folder}.` : 'Nothing was removed.'];
      if (skipped.length) parts.push(`${skipped.length} skipped: ${skipped[0].reason}${skipped.length > 1 ? ' (and others)' : ''}.`);
      V.toast(parts.join(' '), !removed.length);
    } catch (err) {
      V.handleActionError(err);
    } finally {
      if (btn && btn.isConnected) btn.disabled = false;
      updateBulk();
    }
  }

  // Resize: the same picture at the global size. One answer for all of them.
  async function resizeEntries(list, btn) {
    list = list.filter(canResize);
    if (!list.length) return;
    const size = globalSize();
    if (list.length > BULK_CONFIRM && !(await V.confirmDialog(
      `Resize ${list.length} thumbnails to ${sizeLabel(size)}? The pictures stay the same; ones VEXT didn't make are backed up first.`))) return;
    if (btn) btn.disabled = true;
    try {
      const r = await post('/api/fl-sync/resize-pictures', { fst: list.map((e) => e.fst), size });
      list.forEach((e) => picked.delete(e.fst));
      applyEntries(r.entries || []);
      syncChecks();
      const skipped = r.skipped || [];
      const parts = [`${plural(r.resized, 'thumbnail', 'thumbnails')} resized to ${sizeLabel(size)}.`];
      if (skipped.length) parts.push(`${skipped.length} skipped: ${skipped[0].reason}${skipped.length > 1 ? ' (and others)' : ''}.`);
      if (r.backup) parts.push(`Originals backed up to ${r.backup}.`);
      V.toast(parts.join(' '), !r.resized);
    } catch (err) {
      V.handleActionError(err);
    } finally {
      if (btn && btn.isConnected) btn.disabled = false;
      updateBulk();
    }
  }

  // Every thumbnail at the global size, the same images; then the list is checked again.
  async function resizeAll(btn) {
    const n = inv ? inv.entries.filter(canResize).length : 0;
    if (!n) return;
    const size = globalSize();
    if (!(await V.confirmDialog(`Resize ${plural(n, 'thumbnail', 'thumbnails')} to ${sizeLabel(size)}, including the ones `
      + 'with a non-standard size? The pictures stay the same (FL\'s, yours or VEXT\'s); ones VEXT didn\'t make are '
      + 'backed up first to Plugin database\\Thumbnails.vext-backup-<date>.'))) return;
    btn.disabled = true;
    btn.textContent = 'Resizing…';
    ui.summary.textContent = `Resizing ${plural(n, 'thumbnail', 'thumbnails')}… this can take a minute.`;
    try {
      const r = await post('/api/fl-sync/resize-all-pictures', { size });
      const parts = [`${plural(r.resized, 'thumbnail', 'thumbnails')} resized to ${sizeLabel(size)}.`];
      if (r.failed) parts.push(`${r.failed} couldn't be resized.`);
      if (r.backup) parts.push(`Originals backed up to ${r.backup}.`);
      V.toast(parts.join(' '), !!r.failed);
      await check();
    } catch (err) {
      V.handleActionError(err);
    } finally {
      if (btn.isConnected) updateChips();
    }
  }

  // Ignore / Unignore: one request per name (an .fst can be in more than one folder: all of them follow).
  async function ignoreEntries(list, on, btn) {
    const names = [...new Set(list.filter((e) => !e.plugin_id).map((e) => e.name))];
    if (!names.length) return;
    if (btn) btn.disabled = true;
    try {
      const fresh = [];
      for (const name of names) {
        const r = await post('/api/fl-sync/ignore', { name, ignored: on });
        fresh.push(...(r.entries || []));
      }
      applyEntries(fresh);
      V.toast(on ? `${plural(names.length, 'plugin', 'plugins')} ignored: not flagged any more (the Ignored filter lists them).`
        : `${plural(names.length, 'plugin', 'plugins')} flagged again as not recognized.`);
    } catch (err) {
      V.handleActionError(err);
    } finally {
      if (btn && btn.isConnected) btn.disabled = false;
      updateBulk();
    }
  }

  // Not filed → in the browse tree: FL's own .fst is copied in, with VEXT's picture. The new entries join
  // the list and the rows leave the Not filed view.
  async function addDetected(items, btn) {
    items = items.filter((x) => x.can_add && x.fl_nfo);
    if (!items.length) return;
    if (items.length > BULK_CONFIRM && !(await V.confirmDialog(`Add ${items.length} plugins to FL Studio's browser, `
      + `each in its vendor folder with VEXT's picture at ${sizeLabel(globalSize())}?`))) return;
    if (btn) btn.disabled = true;
    try {
      const r = await post('/api/fl-sync/add-detected', {
        items: items.map((x) => ({ plugin_id: x.id, nfo: x.fl_nfo })), size: globalSize(),
      });
      const added = r.added || [];
      const skipped = r.skipped || [];
      // What was sent and not skipped is in the browse tree now.
      const failed = new Set(skipped.map((x) => Number(x.plugin_id)));
      const done = new Set(items.map((x) => Number(x.id)).filter((id) => !failed.has(id)));
      inv.not_filed = inv.not_filed.filter((x) => !done.has(Number(x.id)));
      applyEntries(added);
      renderList();
      const parts = [added.length ? `${plural(added.length, 'plugin', 'plugins')} added to FL Studio's browser.` : 'Nothing was added.'];
      if (skipped.length) parts.push(`${skipped.length} skipped: ${skipped[0].reason}${skipped.length > 1 ? ' (and others)' : ''}.`);
      V.toast(parts.join(' '), !added.length);
    } catch (err) {
      V.handleActionError(err);
    } finally {
      if (btn && btn.isConnected) btn.disabled = false;
    }
  }

  async function bulkFix(btn) {
    const list = selectedEntries().filter(canFix);
    if (!list.length) return;
    if (list.length > BULK_CONFIRM && !(await V.confirmDialog(
      `Fix ${list.length} pictures at ${sizeLabel(globalSize())}? Thumbnails VEXT didn't make are backed up first.`))) return;
    fixEntries(list, btn);
  }

  async function onAct(act, b) {
    const e = b.dataset.fst ? byFst.get(b.dataset.fst) : null;
    switch (act) {
      case 'check': return check();
      case 'repair': return repair();
      case 'fix': return e && fixEntries([e], b);
      case 'set-pic': return V.showPluginModal(Number(b.dataset.id));
      case 'link': return e && openLink(e);
      case 'unlink': return e && unlink(e);
      case 'reveal': return reveal(b.dataset.path || (e && e.fst));
      case 'pm': return openPluginManager();
      case 'organize': return document.getElementById('fls-tab-organize').click();
      case 'select-fixable':
        viewItems.filter(canFix).forEach((x) => picked.add(x.fst));
        syncChecks();
        return updateBulk();
      case 'clear':
        picked.clear();
        syncChecks();
        return updateBulk();
      case 'bulk-fix': return bulkFix(b);
      case 'fix-all': return fixAll(b);
      case 'resize': return e && resizeEntries([e], b);
      case 'bulk-resize': return resizeEntries(selectedEntries(), b);
      case 'resize-all': return resizeAll(b);
      case 'remove': return e && removeEntries([e], b);
      case 'bulk-remove': return removeEntries(selectedEntries(), b);
      case 'ignore': return e && ignoreEntries([e], true, b);
      case 'unignore': return e && ignoreEntries([e], false, b);
      case 'bulk-ignore': return ignoreEntries(selectedEntries().filter((x) => !x.ignored), true, b);
      case 'add-detected': return addDetected(inv.not_filed.filter((x) => Number(x.id) === Number(b.dataset.id)), b);
      case 'add-all-detected': return addDetected(viewItems, b);
    }
  }

  // A plugin's picture changed (the plugin sheet): its rows learn whether VEXT has a picture now.
  async function onPictures(ev) {
    if (!inv) return;
    const ids = new Set(((ev.detail && ev.detail.ids) || []).map(Number));
    const hits = inv.entries.filter((e) => e.plugin_id && ids.has(Number(e.plugin_id)));
    if (!hits.length) return;
    const byId = new Map();
    await Promise.all([...new Set(hits.map((e) => e.plugin_name))].map(async (name) => {
      try {
        const rows = await V.api(`/api/plugins?${new URLSearchParams({ q: name || '' })}`);
        for (const r of rows) byId.set(Number(r.id), r);
      } catch (err) {
        // The rows keep what they had.
      }
    }));
    for (const e of hits) {
      const r = byId.get(Number(e.plugin_id));
      if (r) e.vext_picture = !!r.has_image;
    }
    announce();
    refreshRows(hits);
    updateBulk();
  }

  // ---------- mount and show ----------

  function buildUi(el) {
    el.innerHTML = `
      <div class="flc">
        <div class="flc-toolbar">
          <div class="flc-chips" role="group" aria-label="Filter the list">
            ${FILTERS.map((f) => `<button type="button" class="flc-chip" data-filter="${f.key}" aria-pressed="false">${esc(f.label)} <span class="flc-n"></span></button>`).join('')}
          </div>
          <div class="flc-tools">
            <input type="search" class="flc-search" placeholder="Filter by name, VEXT name, vendor or folder" aria-label="Filter by name, vendor or folder">
            <div class="flc-seg" role="group" aria-label="Section">
              <button type="button" data-section="" aria-pressed="true">All</button>
              <button type="button" data-section="Effects" aria-pressed="false">Effects</button>
              <button type="button" data-section="Generators" aria-pressed="false">Generators</button>
            </div>
            <button type="button" data-act="resize-all" disabled title="Every thumbnail, the same images, at the size chosen above">Resize all</button>
            <button type="button" class="primary" data-act="fix-all" disabled title="Every plugin VEXT has a picture for gets it as FL's thumbnail at the size chosen above">Fix all pictures</button>
            <button type="button" data-act="check">Check again</button>
            <button type="button" data-act="repair" disabled title="Writes a missing .nfo for a .png already beside its .fst, and hides plugin .nfo/.png files as FL does">Fix .nfo and hidden files</button>
          </div>
        </div>
        <p class="flc-summary" aria-live="polite"></p>
        <p class="hint flc-hint" hidden></p>
        <div class="flc-viewbar" hidden></div>
        <div class="flc-list"></div>
        <div class="flc-bulk" hidden>
          <span class="flc-bulk-count"></span>
          <span class="flc-bulk-note"></span>
          <span class="flc-spacer"></span>
          <button type="button" data-act="select-fixable">Select all fixable in this view</button>
          <button type="button" data-act="bulk-resize" hidden>Resize (0)</button>
          <button type="button" class="primary" data-act="bulk-fix">Fix pictures (0)</button>
          <button type="button" data-act="bulk-ignore" hidden>Ignore (0)</button>
          <button type="button" class="danger" data-act="bulk-remove" hidden>Remove (0)</button>
          <button type="button" data-act="clear">Clear</button>
        </div>
      </div>`;
    const q = (sel) => el.querySelector(sel);
    ui = {
      chips: [...el.querySelectorAll('.flc-chip')].map((btn) => ({
        btn, count: FILTERS.find((f) => f.key === btn.dataset.filter).count,
      })),
      segs: [...el.querySelectorAll('.flc-seg button')],
      search: q('.flc-search'),
      check: q('[data-act="check"]'),
      fixAll: q('[data-act="fix-all"]'),
      resizeAll: q('[data-act="resize-all"]'),
      repair: q('[data-act="repair"]'),
      summary: q('.flc-summary'),
      hint: q('.flc-hint'),
      viewbar: q('.flc-viewbar'),
      list: q('.flc-list'),
      bulk: q('.flc-bulk'),
      bulkCount: q('.flc-bulk-count'),
      bulkNote: q('.flc-bulk-note'),
      bulkFix: q('[data-act="bulk-fix"]'),
      bulkResize: q('[data-act="bulk-resize"]'),
      bulkRemove: q('[data-act="bulk-remove"]'),
      bulkIgnore: q('[data-act="bulk-ignore"]'),
    };
    // Clicks, ticks and typing are handled here, once, for the rows painted later too.
    el.addEventListener('click', (ev) => {
      const b = ev.target.closest('button');
      if (!b || !el.contains(b)) return;
      if (b.dataset.filter) setFilter(b.dataset.filter, false);
      else if (b.hasAttribute('data-section')) setSection(b.dataset.section);
      else if (b.dataset.act) onAct(b.dataset.act, b);
    });
    el.addEventListener('change', (ev) => {
      const c = ev.target;
      if (!c.matches || !c.matches('input[data-check]')) return;
      if (c.checked) picked.add(c.dataset.check);
      else picked.delete(c.dataset.check);
      updateBulk();
    });
    ui.search.addEventListener('input', () => {
      query = ui.search.value;
      renderList();
    });
    // An image that won't load shows a note instead of a broken picture (errors don't bubble: capture).
    ui.list.addEventListener('error', (ev) => {
      if (ev.target.tagName === 'IMG') ev.target.outerHTML = '<span class="flc-none">No preview</span>';
    }, true);
    document.addEventListener('vext:pictures', onPictures);
    // A new global size changes which rows can be fixed.
    document.addEventListener('vext:flsync-size', () => { if (inv) { updateChips(); renderList(); updateBulk(); } });
  }

  function setFilter(key, reset) {
    filter = key;
    if (reset && ui) {
      section = '';
      query = '';
      ui.search.value = '';
    }
    updateChips();
    renderList();
  }

  function setSection(value) {
    section = value;
    updateChips();
    renderList();
  }

  function mount(el) {
    if (ui || !el) return;
    buildUi(el);
    updateChips();
    renderList();
  }

  // Shows a view. The first time it runs the check; a filter (from a header tile) is selected and its
  // search and section are cleared, so the list is what the tile counted.
  function show(f) {
    if (!ui) mount(document.getElementById('flsync-check'));
    if (!ui) return;
    if (FILTERS.some((x) => x.key === f)) setFilter(f, true);
    else renderList();
    if (!ran && !checking) check();
  }

  V.flSyncCheck = {
    mount,
    show,
    refresh() { if (ran) check(); },
    inventory() { return inv; },
  };
})();
