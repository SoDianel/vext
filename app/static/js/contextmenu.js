// Plugin context menu: right-click any row/tile/card that represents one plugin (anything carrying
// data-plugin-id), or press its trailing "⋯" button (.row-menu-btn). Left click stays with the row
// (it opens the details modal); nothing here handles it.
//
// Every entry mirrors a button of the plugin detail modal (showPluginModal in app.js): the same API
// calls, toasts, confirmation text and disabled rules, so the menu never allows something the modal
// refuses. Classic script; it talks to the app only through window.VEXT (see the shared UI spec,
// "Context menu contract") and exposes VEXT.contextMenu = { openForPlugin(id, opts), close() }.
(() => {
  const V = window.VEXT;
  // No app runtime (or this file already ran once, see the double-execution note in app.js).
  if (!V || V.contextMenu) return;

  const MARGIN = 6;           // gap kept between the menu and the window edges
  const LOADING_DELAY = 120;  // a fast fetch shows the finished menu directly, without a loading flash
  const SEP = 'sep';

  // 16px line icons in currentColor, so they follow the theme and the highlighted row.
  const ICONS = {
    info: '<circle cx="8" cy="8" r="6"/><path d="M8 7.3v3.7M8 5.1v.1"/>',
    play: '<path d="M5.5 3.8v8.4L12 8z"/>',
    edit: '<path d="M3 13l.7-2.8 6.9-6.9 2.1 2.1-6.9 6.9z"/><path d="M9.4 4.5l2.1 2.1"/>',
    research: '<circle cx="7" cy="7" r="4.2"/><path d="M10.1 10.1l3.4 3.4"/>',
    folder: '<path d="M2 4.2h4.1l1.5 1.6H14v6.6H2z"/>',
    copy: '<rect x="5.5" y="5.5" width="7.5" height="7.5" rx="1.3"/><path d="M3 10.3V4.3C3 3.6 3.6 3 4.3 3h6"/>',
    move: '<path d="M2 4.2h4.1l1.5 1.6H14v6.6H2z"/><path d="M5.8 9.3h4.2M8.6 7.8l1.5 1.5-1.5 1.5"/>',
    quarantine: '<rect x="2.5" y="3" width="11" height="3" rx=".8"/><path d="M3.5 6v6.5h9V6M6.5 8.7h3"/>',
    cloud: '<path d="M4.7 12.5h6.6a2.6 2.6 0 0 0 .3-5.2 3.6 3.6 0 0 0-6.9-.8 3 3 0 0 0 0 6z"/>',
    ignore: '<circle cx="8" cy="8" r="5.6"/><path d="M4.1 4.1l7.8 7.8"/>',
  };
  const icon = (name) => `<svg class="cm-icon" viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" ` +
    `fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round">${ICONS[name] || ''}</svg>`;

  const esc = (v) => (typeof V.escHtml === 'function'
    ? V.escHtml(v)
    : String(v ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])));
  const isTauri = () => typeof V.isTauri === 'function' && V.isTauri();
  const currentSection = () => { try { return V.currentSection ? V.currentSection() : null; } catch { return null; } };

  let menu = null;       // the open menu element (null while closed)
  let items = [];        // its .cm-item buttons, in display order
  let entries = [];      // the entry objects those buttons point at (data-cm index)
  let anchor = null;     // { x, y } pointer point, or { rect } of the "⋯" button the menu hangs from
  let trigger = null;    // where focus goes back to on Escape / after picking an entry
  let target = null;     // the row/card outlined while its menu is open
  let pluginId = null;
  let section = null;    // section at open time: leaving it closes the menu
  let token = 0;         // bumped on every open/close so a late fetch can't fill a newer menu
  let loadingTimer = 0;
  let watchTimer = 0;
  let suppress = null;   // { btn, at }: the press on "⋯" that just closed its own menu (toggle, not reopen)

  // ---------- entries ----------

  // Same disabled rules as the modal's buttons: nothing new may be proposed while an action is pending
  // or applied-but-unresolved, and an FL Cloud plugin is pinned to its install path.
  function buildEntries(p, settings) {
    const a = p.action_status;
    const busy = a
      ? `A ${a.action} action is ${a.applied ? 'applied but not resolved yet' : 'already proposed'} for this plugin. Resolve it first (Actions / Quarantine).`
      : '';
    const pinned = p.fl_cloud ? 'FL Cloud plugin — must stay at its install path' : '';
    const tauri = isTauri();
    const list = [
      { label: 'Details', icon: 'info', isDefault: true, run: () => V.showPluginModal(p.id) },
      // Same rule as the plugin sheet: hidden for formats no host loads (FL-native, AAX); disabled
      // with the reason for the other plugins that can't open, unless a retry may help (`retry`).
      (p.hostable || (p.host_issue && p.host_issue.code !== 'format')) && {
        label: p.hostable ? 'Test in Playground' : 'Try again in Playground', icon: 'play',
        disabled: p.hostable || p.host_issue.retry ? '' : `Can't be opened in the Playground: ${p.host_issue.reason}.`,
        run: () => { V.closeModal(); V.navigate('playground', { pluginId: p.id }); },
      },
      { label: 'Edit metadata', icon: 'edit', run: () => V.showPluginEditForm(p) },
      {
        label: 'Research with AI', icon: 'research',
        hint: 'Opens the details and asks an AI for vendor, type and categories. Nothing is applied until you review it.',
        run: () => research(p.id),
      },
      p.host_path && {
        label: tauri ? 'Reveal in Explorer' : 'Open folder', icon: 'folder',
        hint: tauri ? '' : 'Best-effort: opens the containing folder. If it only shows a page, use Copy path and paste it into Explorer\'s address bar.',
        run: () => reveal(p.host_path, tauri),
      },
      p.host_path && { label: 'Copy path', icon: 'copy', hint: p.host_path, run: () => copyPath(p.host_path) },
      SEP,
      // proposeAction toasts, closes any modal and reloads the section by itself.
      {
        label: 'Propose: move to vendor subfolder', icon: 'move', disabled: pinned || busy,
        run: () => V.proposeAction(p.id, 'reorganize', `Move into a "${p.vendor || 'Unknown'}" subfolder`),
      },
      {
        label: 'Propose: quarantine', icon: 'quarantine', disabled: pinned || busy,
        run: () => V.proposeAction(p.id, 'quarantine', 'Manually flagged for quarantine from plugin detail'),
      },
      settings.fl_studio_installed && {
        label: p.fl_cloud ? 'Unmark FL Cloud' : 'Mark as FL Cloud', icon: 'cloud',
        hint: p.fl_cloud ? '' : 'Keeps it where it is installed: never moved, deduped or quarantined.',
        disabled: !p.fl_cloud && busy,
        run: () => setFlCloud(p),
      },
      SEP,
      {
        label: 'Ignore…', icon: 'ignore', danger: true, disabled: busy,
        hint: 'Removes it from the catalog and skips it on future rescans. The file itself is never touched.',
        run: () => ignore(p),
      },
    ].filter(Boolean);
    // Hidden entries can leave separators at an edge or two in a row.
    return list.filter((e, i) => e !== SEP || (i > 0 && i < list.length - 1 && list[i - 1] !== SEP));
  }

  // "Research with AI" is the modal's own research button: open the details, then press it, so the
  // agent choice, progress text and review panel all stay in one place.
  async function research(id) {
    V.closeModal();               // a stale modal's button must not be the one found below
    await V.showPluginModal(id);
    const btn = await waitFor('#modal-research', 4000);
    if (!btn) return;
    btn.scrollIntoView({ block: 'nearest' });
    if (!btn.disabled) btn.click();
  }

  const waitFor = (selector, ms) => new Promise((resolve) => {
    const start = Date.now();
    (function poll() {
      const el = document.querySelector(selector);
      if (el || Date.now() - start > ms) resolve(el);
      else setTimeout(poll, 50);
    })();
  });

  async function reveal(hostPath, tauri) {
    if (tauri) {
      try { await V.revealInExplorer(hostPath); } catch (err) { V.toast('Could not open Explorer: ' + err, true); }
      return;
    }
    // Plain browser tab: the same best-effort file:// link to the containing folder as the modal's
    // "Open folder" (never the plugin file itself, which the browser might try to run).
    const parts = hostPath.split('\\').filter(Boolean);
    parts.pop();
    window.open('file:///' + parts.map(encodeURIComponent).join('/') + '/', '_blank', 'noopener');
  }

  async function copyPath(hostPath) {
    try {
      await navigator.clipboard.writeText(hostPath);
      V.toast('Path copied.');
    } catch {
      V.toast('Could not copy automatically — path: ' + hostPath, true);
    }
  }

  async function setFlCloud(p) {
    try {
      await V.api(`/api/plugins/${p.id}/fl-cloud`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled: !p.fl_cloud }),
      });
      V.toast(p.fl_cloud ? 'Unmarked FL Cloud.' : 'Marked FL Cloud — it will stay where it is installed.');
      V.refresh();
    } catch (e) { V.handleActionError(e); }
  }

  async function ignore(p) {
    if (!(await V.confirmDialog(
      `Ignore "${p.name}"? It'll disappear from the catalog now, and future rescans will skip this exact file entirely — not cataloged, not FL-matched, not linked. The file itself is never touched. Undo any time from the Configuration tab's "Ignored plugins" list.`
    ))) return;
    try {
      await V.api(`/api/plugins/${p.id}/ignore`, { method: 'POST' });
      V.toast('Ignored — won\'t be scanned again.');
      V.refresh();
    } catch (e) { V.handleActionError(e); }
  }

  // ---------- open / render / close ----------

  // opts: { x, y } (a viewport point, e.g. the pointer) or { anchor: Element } (hang under it, right
  // edges aligned). Optional: trigger (focus returns there), target (outlined while open), keyboard
  // (true = highlight the first entry, as when opened from the keyboard).
  function openForPlugin(id, opts = {}) {
    id = parseInt(id, 10);
    if (!id) return;
    close(false);
    const my = ++token;
    pluginId = id;
    const anchorEl = opts.anchor instanceof Element ? opts.anchor : null;
    anchor = anchorEl ? { rect: anchorEl.getBoundingClientRect() }
      : Number.isFinite(opts.x) && Number.isFinite(opts.y) ? { x: opts.x, y: opts.y }
      : { x: Math.round(innerWidth / 2 - 120), y: Math.round(innerHeight / 3) };
    trigger = opts.trigger || anchorEl || document.activeElement;
    target = opts.target || null;
    section = currentSection();
    const keyboard = !!opts.keyboard;

    if (target) target.classList.add('cm-target');
    if (trigger && trigger.classList && trigger.classList.contains('row-menu-btn')) trigger.setAttribute('aria-expanded', 'true');
    // The row's hover description would otherwise float over the menu.
    const tip = document.getElementById('hover-tooltip');
    if (tip) tip.style.display = 'none';

    menu = document.createElement('div');
    menu.className = 'cm is-pending';
    menu.setAttribute('role', 'menu');
    menu.setAttribute('aria-label', 'Plugin actions');
    menu.tabIndex = -1;
    menu.addEventListener('pointermove', onMenuPointerMove);
    menu.addEventListener('pointerleave', onMenuPointerLeave);
    menu.addEventListener('click', onMenuClick);
    menu.addEventListener('contextmenu', (e) => e.preventDefault());
    document.body.appendChild(menu);
    listen(true);

    loadingTimer = setTimeout(() => {
      if (my === token) show('<div class="cm-loading" role="none"><span class="cm-spinner"></span>Loading…</div>', [], keyboard);
    }, LOADING_DELAY);
    // Leaving the section (sidebar, VEXT.navigate, anything) closes the menu; no event is needed for it.
    watchTimer = setInterval(() => { if (menu && currentSection() !== section) close(false); }, 250);

    Promise.all([
      V.api('/api/plugins/' + id),
      Promise.resolve().then(() => V.settings()).catch(() => ({})),
    ]).then(([p, settings]) => {
      if (my !== token) return;
      clearTimeout(loadingTimer);
      menu.setAttribute('aria-label', `Actions for ${p.name}`);
      const sub = [p.format, p.vendor].filter(Boolean).join(' · ');
      const head = `<div class="cm-head" aria-hidden="true"><div class="cm-name">${esc(p.name)}</div>` +
        (sub ? `<div class="cm-sub">${esc(sub)}</div>` : '') + '</div>';
      const list = buildEntries(p, settings || {});
      show(head + list.map(entryHtml).join(''), list, keyboard);
    }, (err) => {
      if (my !== token) return;
      clearTimeout(loadingTimer);
      show(`<div class="cm-error" role="none">Couldn't load this plugin.<span>${esc(err && err.message || err)}</span></div>`, [], keyboard);
    });
  }

  const entryHtml = (e, i) => {
    if (e === SEP) return '<div class="cm-sep" role="separator"></div>';
    const title = e.disabled || e.hint;
    return `<button type="button" class="cm-item${e.danger ? ' is-danger' : ''}${e.isDefault ? ' is-default' : ''}" ` +
      `role="menuitem" tabindex="-1" data-cm="${i}"${e.disabled ? ' aria-disabled="true"' : ''}` +
      `${title ? ` title="${esc(title)}"` : ''}>${icon(e.icon)}<span class="cm-label">${esc(e.label)}</span></button>`;
  };

  function show(html, list, keyboard) {
    if (!menu) return;
    menu.innerHTML = html;
    entries = list;
    items = [...menu.querySelectorAll('.cm-item')];
    if (menu.classList.contains('is-pending')) {
      menu.classList.remove('is-pending');
      menu.classList.add('is-shown');   // play the open animation once, not again on loading → filled
    }
    place();
    if (keyboard && items.length) focusItem(0);
    else if (!menu.contains(document.activeElement)) menu.focus({ preventScroll: true });
  }

  // Native-menu placement: from the pointer it opens down-right and flips left/up at the window
  // edges; from "⋯" it hangs below the button (above it when there is no room). Always clamped.
  function place() {
    if (!menu || !anchor) return;
    const vw = document.documentElement.clientWidth;
    const vh = document.documentElement.clientHeight;
    menu.style.maxHeight = Math.max(120, vh - MARGIN * 2) + 'px';
    const w = menu.offsetWidth, h = menu.offsetHeight;
    let x, y, ax, ay;
    if (anchor.rect) {
      const r = anchor.rect;
      x = r.right - w;
      y = r.bottom + 4;
      if (y + h > vh - MARGIN && r.top - 4 - h >= MARGIN) y = r.top - 4 - h;
      ax = r.right; ay = y < r.top ? r.top : r.bottom;
    } else {
      x = anchor.x; y = anchor.y;
      if (x + w > vw - MARGIN) x -= w;
      if (y + h > vh - MARGIN) y -= h;
      ax = anchor.x; ay = anchor.y;
    }
    x = Math.max(MARGIN, Math.min(x, vw - w - MARGIN));
    y = Math.max(MARGIN, Math.min(y, vh - h - MARGIN));
    // A native view (the Playground's browser) would have to hide under the menu: move the menu
    // beside it when there's room: right side first (the plugin list is there in Side layout),
    // then left, then under it (Stacked layout).
    const nat = typeof V.nativeArea === 'function' ? V.nativeArea() : null;
    if (nat && x < nat.x + nat.width && x + w > nat.x && y < nat.y + nat.height && y + h > nat.y) {
      if (nat.x + nat.width + MARGIN + w <= vw - MARGIN) x = nat.x + nat.width + MARGIN;
      else if (nat.x - MARGIN - w >= MARGIN) x = nat.x - MARGIN - w;
      else if (nat.y + nat.height + MARGIN + h <= vh - MARGIN) y = nat.y + nat.height + MARGIN;
    }
    menu.style.left = x + 'px';
    menu.style.top = y + 'px';
    menu.style.transformOrigin = `${Math.min(Math.max(ax - x, 0), w)}px ${Math.min(Math.max(ay - y, 0), h)}px`;
    // Where the menu sits decides whether it covers the Playground's browser (app.js overlayCovers).
    document.dispatchEvent(new CustomEvent('vext:overlay', { detail: { open: true } }));
  }

  // restoreFocus: true for Escape/Tab/picking an entry; false when something else took the focus
  // (outside click, scroll, resize, window blur, section change).
  function close(restoreFocus) {
    if (!menu) return;
    token++;
    clearTimeout(loadingTimer);
    clearInterval(watchTimer);
    listen(false);
    const back = !restoreFocus ? null
      : trigger && trigger.isConnected ? trigger
      : document.querySelector(`.row-menu-btn[data-plugin-id="${pluginId}"]`);   // the list re-rendered meanwhile
    menu.remove();
    menu = null;
    items = [];
    entries = [];
    if (target) target.classList.remove('cm-target');
    if (trigger && trigger.classList && trigger.classList.contains('row-menu-btn')) trigger.setAttribute('aria-expanded', 'false');
    trigger = target = anchor = null;
    if (back && typeof back.focus === 'function') back.focus({ preventScroll: true });
  }

  function activate(el) {
    const entry = entries[Number(el.dataset.cm)];
    if (!entry || entry.disabled) return;   // disabled entries stay put, like a native menu
    close(true);
    // Run synchronously so clipboard/opener calls still count as part of the user's click.
    try {
      const result = entry.run();
      if (result && typeof result.catch === 'function') result.catch((err) => V.handleActionError(err));
    } catch (err) { V.handleActionError(err); }
  }

  const focusItem = (i) => {
    if (!items.length) return;
    items[((i % items.length) + items.length) % items.length].focus({ preventScroll: true });
  };

  // ---------- listeners while open ----------

  function listen(on) {
    const m = on ? 'addEventListener' : 'removeEventListener';
    document[m]('pointerdown', onOutsidePointerDown, true);
    document[m]('keydown', onKey, true);
    document[m]('scroll', onScroll, true);
    document[m]('wheel', onWheel, { capture: true, passive: true });
    window[m]('resize', dismiss);
    window[m]('blur', dismiss);
    document[m]('vext:viewchange', dismiss);
    document[m]('vext:sectionchange', dismiss);
  }

  const dismiss = () => close(false);
  const outside = (e) => !(menu && e.target instanceof Node && menu.contains(e.target));
  const onScroll = (e) => { if (outside(e)) close(false); };
  const onWheel = (e) => { if (outside(e)) close(false); };

  function onOutsidePointerDown(e) {
    if (!outside(e)) return;
    // Pressing the "⋯" that opened this menu closes it; its click must not reopen it right away.
    const ownBtn = trigger && trigger.classList && trigger.classList.contains('row-menu-btn') && trigger.contains(e.target);
    suppress = ownBtn ? { btn: trigger, at: Date.now() } : null;
    close(false);
  }

  function onMenuPointerMove(e) {
    const el = e.target.closest('.cm-item');
    if (el && document.activeElement !== el) el.focus({ preventScroll: true });
  }

  function onMenuPointerLeave() {
    if (menu && items.includes(document.activeElement)) menu.focus({ preventScroll: true });
  }

  function onMenuClick(e) {
    const el = e.target.closest('.cm-item');
    if (el) activate(el);
  }

  function onKey(e) {
    if (!menu) return;
    const k = e.key;
    if (k === 'Escape' || k === 'Tab') {
      e.preventDefault();
      e.stopPropagation();   // one Escape closes only the menu, not a modal underneath
      close(true);
      return;
    }
    if (menu.classList.contains('is-pending')) return;
    const i = items.indexOf(document.activeElement);
    if (k === 'ArrowDown') focusItem(i < 0 ? 0 : i + 1);
    else if (k === 'ArrowUp') focusItem(i < 0 ? -1 : i - 1);
    else if (k === 'Home' || k === 'PageUp') focusItem(0);
    else if (k === 'End' || k === 'PageDown') focusItem(-1);
    else if (k === 'Enter' || k === ' ') { if (i >= 0) activate(items[i]); }
    else if (k.length === 1 && /\S/.test(k) && !e.ctrlKey && !e.metaKey && !e.altKey) {
      // Type-ahead: the next entry starting with that letter.
      const ch = k.toLowerCase();
      for (let n = 1; n <= items.length; n++) {
        const el = items[(i + n + items.length) % items.length];
        if (el.textContent.trim().toLowerCase().startsWith(ch)) { el.focus({ preventScroll: true }); break; }
      }
    } else return;
    e.preventDefault();
    e.stopPropagation();
  }

  // ---------- delegated openers (always on) ----------

  // Text fields and selected text keep the browser's own menu (cut / copy / paste).
  function keepNativeMenu(e) {
    if (e.target.closest('input, textarea, select, [contenteditable]:not([contenteditable="false"])')) return true;
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || !sel.toString().trim()) return false;
    for (let i = 0; i < sel.rangeCount; i++) {
      const range = sel.getRangeAt(i);
      if (e.button !== 2) { if (range.intersectsNode(e.target)) return true; continue; }
      for (const r of range.getClientRects()) {
        if (e.clientX >= r.left && e.clientX <= r.right && e.clientY >= r.top && e.clientY <= r.bottom) return true;
      }
    }
    return false;
  }

  document.addEventListener('contextmenu', (e) => {
    if (e.defaultPrevented || !(e.target instanceof Element)) return;
    if (menu && menu.contains(e.target)) return;
    const host = e.target.closest('[data-plugin-id]');
    if (!host || keepNativeMenu(e)) return;
    e.preventDefault();
    const row = e.target.closest('[data-plugin-id]:not(.row-menu-btn)') || host;
    const opts = { target: row, trigger: document.activeElement };
    // Menu key / Shift+F10 (no pointer): open from the row's "⋯", or under the row's left edge.
    if (e.button !== 2 && !e.pointerType) {
      const btn = row.querySelector('.row-menu-btn');
      const r = row.getBoundingClientRect();
      Object.assign(opts, btn ? { anchor: btn } : { x: r.left + 12, y: r.bottom }, { keyboard: true });
    } else {
      Object.assign(opts, { x: e.clientX, y: e.clientY });
    }
    openForPlugin(host.dataset.pluginId, opts);
  });

  // Capture phase: runs before the row's own click handler (open details), which never sees it.
  document.addEventListener('click', (e) => {
    const btn = e.target instanceof Element && e.target.closest('.row-menu-btn');
    if (!btn) return;
    e.preventDefault();
    e.stopPropagation();
    const justClosed = suppress && suppress.btn === btn && Date.now() - suppress.at < 1000;
    suppress = null;
    if (justClosed) return;
    if (menu && trigger === btn) { close(true); return; }
    const host = btn.closest('[data-plugin-id]');
    openForPlugin(btn.dataset.pluginId || (host && host.dataset.pluginId), {
      anchor: btn,
      trigger: btn,
      target: btn.closest('[data-plugin-id]:not(.row-menu-btn)'),
      keyboard: e.detail === 0,   // Enter/Space on the button: highlight the first entry
    });
  }, true);

  V.contextMenu = { openForPlugin, close: () => close(false) };
})();
