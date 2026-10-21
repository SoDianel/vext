// Configuration › Storage: what VEXT keeps that can go to free space (GET /api/storage), each item
// measured, and clearing the ones picked (POST /api/storage/clear). Caches come back on their own when
// needed; backups (VEXT's copies made before changing FL Studio's Plugin database) are gone for good, so
// they ask first and say so. What the backend never touches is listed in app/storage.py.
(function () {
  const V = window.VEXT;
  if (!V || V.configStorage) return;

  const GROUPS = [
    { kind: 'cache', title: 'Cache', hint: 'Made again when needed. Clearing it is safe.' },
    { kind: 'backup', title: 'Backups', hint: 'Copies VEXT made before changing FL Studio\'s Plugin database. Deleting them can\'t be undone.' },
  ];

  let root = null;
  let data = null;          // the last answer: {items, total}
  let busy = false;
  const picked = new Set(); // item keys ticked

  const esc = (s) => V.escHtml(s);
  // "0 B" rather than fmtSize's dash: an empty item is still worth showing as empty.
  const size = (n) => (n ? V.fmtSize(n) : '0 B');
  const byKey = (k) => (data ? data.items.find((i) => i.key === k) : null);

  function row(item, max) {
    const pct = max ? Math.max(item.size ? 2 : 0, Math.round((item.size / max) * 100)) : 0;
    const open = V.isTauri() && item.location
      ? `<button type="button" class="cs-btn" data-open="${esc(item.location)}" title="${esc(item.location)}">Open folder</button>` : '';
    return `<div class="cs-row${item.size ? '' : ' is-empty'}" data-key="${esc(item.key)}">
      <label class="cs-check"><input type="checkbox" data-pick="${esc(item.key)}"${picked.has(item.key) ? ' checked' : ''}${item.size ? '' : ' disabled'} aria-label="Select ${esc(item.label)}"></label>
      <div class="cs-text">
        <strong>${esc(item.label)}</strong>
        <span class="cs-detail">${esc(item.detail)}</span>
        <span class="cs-where">${esc(item.location || '')}</span>
      </div>
      <div class="cs-size">
        <span class="cs-num">${esc(size(item.size))}</span>
        <span class="cs-files">${item.files} ${item.files === 1 ? 'file' : 'files'}</span>
        <span class="cs-bar" aria-hidden="true"><span style="width: ${pct}%"></span></span>
      </div>
      <div class="cs-acts">${open}<button type="button" class="cs-btn${item.kind === 'backup' ? ' danger' : ''}" data-clear="${esc(item.key)}"${item.size ? '' : ' disabled'}>${item.kind === 'backup' ? 'Delete' : 'Clear'}</button></div>
    </div>`;
  }

  function render() {
    if (!root) return;
    if (!data) {
      root.innerHTML = `<div class="cs"><p class="cs-state">${busy ? 'Measuring…' : 'Not measured yet.'}</p></div>`;
      return;
    }
    const max = Math.max(1, ...data.items.map((i) => i.size));
    const sel = data.items.filter((i) => picked.has(i.key) && i.size);
    const selSize = sel.reduce((n, i) => n + i.size, 0);
    root.innerHTML = `<div class="cs">
      <div class="cs-head">
        <div><h3>Storage</h3><p class="hint">Caches and backups VEXT can delete to free space. Your catalog, plugin pictures,
          quarantine and the Playground's open file are never touched here.</p></div>
        <div class="cs-total"><span class="cs-total-num">${esc(size(data.total))}</span><span class="cs-total-label">can be freed</span>
          <button type="button" class="cs-btn" data-act="refresh"${busy ? ' disabled' : ''}>Measure again</button></div>
      </div>
      ${GROUPS.map((g) => {
    const items = data.items.filter((i) => i.kind === g.kind);
    const total = items.reduce((n, i) => n + i.size, 0);
    return `<section class="cs-group">
          <div class="cs-group-head"><h4>${esc(g.title)} <span class="cs-group-size">${esc(size(total))}</span></h4><span class="hint">${esc(g.hint)}</span></div>
          <div class="cs-list">${items.map((i) => row(i, max)).join('')}</div>
        </section>`;
  }).join('')}
      <div class="cs-bar-actions">
        <span>${sel.length ? `${sel.length} selected · ${esc(size(selSize))}` : 'Tick what to clear, or use a row\'s button.'}</span>
        <span class="cs-spacer"></span>
        <button type="button" data-act="pick-cache"${busy ? ' disabled' : ''}>Select all cache</button>
        <button type="button" class="${sel.some((i) => i.kind === 'backup') ? 'danger' : 'primary'}" data-act="clear-picked"${sel.length && !busy ? '' : ' disabled'}>Clear selected</button>
      </div>
    </div>`;
  }

  async function measure() {
    busy = true;
    render();
    try {
      data = await V.api('/api/storage');
      for (const k of [...picked]) { const i = byKey(k); if (!i || !i.size) picked.delete(k); }
    } catch (err) {
      V.handleActionError(err);
    } finally {
      busy = false;
      render();
    }
  }

  async function clear(keys) {
    const items = keys.map(byKey).filter((i) => i && i.size);
    if (!items.length) return;
    const backups = items.filter((i) => i.kind === 'backup');
    const total = items.reduce((n, i) => n + i.size, 0);
    const list = items.map((i) => `${i.label} (${size(i.size)})`).join(', ');
    const msg = `Free ${size(total)}: ${list}?`
      + (backups.length ? ' The backups are deleted for good: thumbnails VEXT replaced and entries taken out of FL Studio\'s browser can\'t be restored after this.' : '');
    if (!(await V.confirmDialog(msg))) return;
    busy = true;
    render();
    try {
      const r = await V.api('/api/storage/clear', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ keys: items.map((i) => i.key) }),
      });
      data = r.now || data;
      items.forEach((i) => picked.delete(i.key));
      const left = (r.results || []).reduce((n, x) => n + x.left, 0);
      V.toast(`Freed ${size(r.freed)}.${left ? ` ${left} ${left === 1 ? 'file is' : 'files are'} in use and stayed (the web cache while VEXT is open, the sound playing now); they can go next time.` : ''}`);
    } catch (err) {
      V.handleActionError(err);
    } finally {
      busy = false;
      render();
    }
  }

  function onClick(ev) {
    const b = ev.target.closest('button');
    if (!b || !root.contains(b) || b.disabled) return;
    if (b.dataset.open) { V.revealInExplorer(b.dataset.open).catch((err) => V.toast(err.message || String(err), true)); return; }
    if (b.dataset.clear) { clear([b.dataset.clear]); return; }
    switch (b.dataset.act) {
      case 'refresh': measure(); break;
      case 'clear-picked': clear([...picked]); break;
      case 'pick-cache':
        data.items.filter((i) => i.kind === 'cache' && i.size).forEach((i) => picked.add(i.key));
        render();
        break;
      default: break;
    }
  }

  function onChange(ev) {
    const c = ev.target;
    if (!c.matches || !c.matches('input[data-pick]')) return;
    if (c.checked) picked.add(c.dataset.pick); else picked.delete(c.dataset.pick);
    render();
  }

  // Every time the pane is shown: measured again (sizes change while VEXT runs).
  function load(el) {
    if (!el) return;
    if (root !== el) {
      root = el;
      el.addEventListener('click', onClick);
      el.addEventListener('change', onChange);
    }
    measure();
  }

  V.configStorage = { load };
})();
