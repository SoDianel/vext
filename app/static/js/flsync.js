// FL Studio Sync, the header and the Organize tab (js/flsync-check.js is the Check & fix tab).
// The header: the Plugin database folder, the global Thumbnails size (Large 256 / Medium 128 /
// Compact 64, used by Apply and by every Fix) and status tiles from the last check. Each tile opens
// Check & fix with its filter. The tabs follow the WAI tabs pattern and remember the last one.
// Organize files FL's browse tree: the same features and safety rules as the old form (Preview first,
// Apply only after a Preview of the current choices, a confirm before every Apply, root-bound plugins
// get a destination, the dangerous wipe is behind Advanced).
(function () {
  const V = window.VEXT;
  const KEY_SIZE = 'pa-fls-thumb-size';
  const KEY_TAB = 'pa-fls-tab';
  const SIZES = [{ px: 256, name: 'Large' }, { px: 128, name: 'Medium' }, { px: 64, name: 'Compact' }];
  // Status tiles: [filter for Check & fix, label, key in the inventory's counts].
  const TILES = [
    ['all', '.fst files', 'fst'],
    ['issues', 'Problems', 'with_issues'],
    ['nopicture', 'No picture', 'no_picture'],
    ['standardize', 'Non-standard size', 'can_standardize'],
    ['unknown', 'Not recognized', 'unknown'],
    ['notinfl', 'Not in FL', 'not_in_fl'],
    ['notfiled', 'Not filed', 'not_filed'],
  ];

  const $ = (id) => document.getElementById(id);
  const esc = (s) => V.escHtml(s);

  let built = false;
  let checkMounted = false;
  let options = null;        // {vendor: [...], category: [...]}, fetched on each visit
  let overrides = {};        // {Effects: {key: value}, Generators: {...}}: the destinations picked for root-bound plugins
  let previewed = false;     // Apply needs a Preview of the current choices
  let resultKind = null;     // 'preview' | 'applied' | null: what the result area shows
  let lastCounts = null;     // the last inventory's counts and root (from vext:flsync-inventory)
  let lastRoot = null;
  const choice = {
    sections: { Effects: true, Generators: true },
    mode: 'vendor',          // 'vendor' | 'category'
    scope: 'new',            // 'new' | 'full'
    pictures: true,
    replace: false,
    dangerous: false,
    saveBackup: true,
  };

  // The global thumbnail size: 256 | 128 | 64 (localStorage pa-fls-thumb-size, default 256).
  function thumbSize() {
    let px = 256;
    try { px = parseInt(localStorage.getItem(KEY_SIZE), 10); } catch { /* storage off: default */ }
    return SIZES.some(s => s.px === px) ? px : 256;
  }
  function setThumbSize(px) {
    try { localStorage.setItem(KEY_SIZE, String(px)); } catch { /* storage off: lasts this visit */ }
    // Check & fix offers Fix picture by the size: it repaints.
    document.dispatchEvent(new CustomEvent('vext:flsync-size', { detail: { size: px } }));
  }
  const sizeName = (px) => (SIZES.find(s => s.px === px) || SIZES[0]).name;

  function currentTab() {
    try { return localStorage.getItem(KEY_TAB) === 'check' ? 'check' : 'organize'; } catch { return 'organize'; }
  }

  function selectedSections() {
    return ['Effects', 'Generators'].filter(s => choice.sections[s]);
  }

  // A stat tile: the big number with its label (the dashboard's look, in a small grid).
  const stat = (n, label, sub) => `
    <div class="fls-stat">
      <span class="fls-stat-label">${label}</span>
      <span class="fls-stat-num">${n ?? 0}</span>
      ${sub ? `<span class="fls-stat-sub">${esc(sub)}</span>` : ''}
    </div>`;

  // ---------- header ----------

  function buildHead() {
    $('flsync-head').innerHTML = `
      <div class="fls-head-bar">
        <div class="fls-head-main">
          <p class="fls-root" id="fls-root"></p>
          <div class="fls-head-actions">
            <button type="button" id="fls-open-db" title="Opens FL Studio's Plugin database folder in Explorer">Open folder</button>
            <button type="button" id="fls-open-pm" title="Opens FL Studio's Plugin Manager. Use Find installed plugins there, then Check again">Open FL Plugin Manager</button>
          </div>
        </div>
        <div class="fls-thumbs">
          <span class="fls-label" id="fls-thumb-label">Thumbnails</span>
          <div class="segmented fls-seg" role="group" aria-labelledby="fls-thumb-label">
            ${SIZES.map(s => `<button type="button" data-px="${s.px}" aria-pressed="false">${s.name} <span class="fls-px">${s.px}</span></button>`).join('')}
          </div>
          <p class="fls-hint">Height in FL's browser; width follows the picture.</p>
        </div>
      </div>
      <div class="fls-tiles" id="fls-tiles" role="group" aria-label="Status from the last check"></div>`;

    $('fls-open-db').onclick = async () => {
      try { await V.api('/api/fl-sync/open-database', { method: 'POST' }); } catch (e) { V.toast(e.message, true); }
    };
    $('fls-open-pm').onclick = async () => {
      try {
        await V.api('/api/fl-sync/open-plugin-manager', { method: 'POST' });
        V.toast('Run Find installed plugins there, then Check again.');
      } catch (e) { V.toast(e.message, true); }
    };
    document.querySelectorAll('#flsync-head [data-px]').forEach(b => {
      b.onclick = () => { setThumbSize(parseInt(b.dataset.px, 10)); paintHead(); paintOrganize(); };
    });
    // One handler for every tile: a filtered tile opens Check & fix on that filter; the muted
    // "Not checked yet" tile opens it on the default one.
    $('fls-tiles').onclick = (e) => {
      const b = e.target.closest('button');
      if (!b) return;
      openCheck(b.dataset.filter);
    };
  }

  function paintHead() {
    const px = thumbSize();
    document.querySelectorAll('#flsync-head [data-px]').forEach(b => {
      b.setAttribute('aria-pressed', String(parseInt(b.dataset.px, 10) === px));
    });
    $('fls-root').textContent = lastRoot || "FL Studio's Plugin database";
    const tiles = $('fls-tiles');
    if (!lastCounts) {
      tiles.innerHTML = `
        <button type="button" class="fls-tile fls-tile-todo">
          <span class="fls-tile-label">Not checked yet</span>
          <span class="fls-tile-sub">Check now</span>
        </button>`;
    } else {
      tiles.innerHTML = TILES.map(([filter, label, key]) => {
        const n = lastCounts[key] || 0;
        const warn = filter === 'issues' && n ? ' is-warn' : '';
        return `<button type="button" class="fls-tile${warn}" data-filter="${filter}">
          <span class="fls-tile-num">${n}</span><span class="fls-tile-label">${label}</span></button>`;
      }).join('');
    }
    const tab = $('fls-tab-count');
    const problems = lastCounts ? lastCounts.with_issues || 0 : 0;
    tab.textContent = problems;
    tab.hidden = !problems;
    tab.setAttribute('aria-label', `${problems} with problems`);
  }

  // The inventory from the Check & fix module, whenever it fires (and on each visit).
  document.addEventListener('vext:flsync-inventory', (e) => {
    lastCounts = (e.detail && e.detail.counts) || null;
    lastRoot = (e.detail && e.detail.root) || null;
    paintHead();
  });

  // ---------- tabs ----------

  function checkModule() { return V.flSyncCheck || null; }

  // Opens Check & fix (mounting it the first time) with an optional filter.
  function openCheck(filter) {
    showTab('check', filter);
  }

  function showTab(name, filter) {
    const tabs = [...document.querySelectorAll('.fls-tab')];
    tabs.forEach(t => {
      const on = t.dataset.flsTab === name;
      t.setAttribute('aria-selected', String(on));
      t.tabIndex = on ? 0 : -1;
    });
    $('flsync-organize').hidden = name !== 'organize';
    $('flsync-check').hidden = name !== 'check';
    try { localStorage.setItem(KEY_TAB, name); } catch { /* storage off: the tab isn't remembered */ }

    if (name !== 'check') return;
    const check = checkModule();
    if (!check) {
      $('flsync-check').innerHTML = '<div class="empty-state">Check & fix isn\'t available in this build.</div>';
      return;
    }
    // Mount once, then show: show() runs the check the first time and keeps the last one after.
    if (!checkMounted) {
      check.mount($('flsync-check'));
      checkMounted = true;
    }
    check.show(filter);
  }

  function wireTabs() {
    const tabs = [...document.querySelectorAll('.fls-tab')];
    tabs.forEach((t, i) => {
      t.addEventListener('click', () => showTab(t.dataset.flsTab));
      // WAI tabs: arrows move (and select) across the tabs, Home and End jump to the ends.
      t.addEventListener('keydown', (e) => {
        let j = null;
        if (e.key === 'ArrowRight') j = (i + 1) % tabs.length;
        else if (e.key === 'ArrowLeft') j = (i - 1 + tabs.length) % tabs.length;
        else if (e.key === 'Home') j = 0;
        else if (e.key === 'End') j = tabs.length - 1;
        if (j === null) return;
        e.preventDefault();
        tabs[j].focus();
        showTab(tabs[j].dataset.flsTab);
      });
    });
  }

  // ---------- Organize ----------

  const MODE_NOTE = {
    vendor: 'Each plugin goes in a folder named after its maker.',
    category: 'Plugins are sorted by what they do. A plugin can land in more than one category.',
  };
  const SCOPE_NOTE = {
    new: 'Only what FL Studio\'s last "Find installed plugins" put in New\\. Everything already filed stays where it is.',
    full: 'Every plugin FL Studio has detected (read only). Each one is filed where VEXT\'s data says, and anything already filed in the wrong folder is moved.',
  };

  function buildOrganize() {
    $('flsync-organize').innerHTML = `
      <div class="fls-grid">
        <section class="fls-card" aria-labelledby="fls-c-sections">
          <h3 id="fls-c-sections">Sections</h3>
          <p class="fls-card-hint">Each section runs on its own. Turn one off to sync only the other.</p>
          <div class="fls-chips" role="group" aria-label="Sections">
            <button type="button" class="fls-chip" data-section="Effects" aria-pressed="true">Effects</button>
            <button type="button" class="fls-chip" data-section="Generators" aria-pressed="true">Generators</button>
          </div>
        </section>

        <section class="fls-card" aria-labelledby="fls-c-mode">
          <h3 id="fls-c-mode">Organize by</h3>
          <div class="segmented fls-seg" role="group" aria-label="Organize by">
            <button type="button" data-mode="vendor" aria-pressed="true">Vendor</button>
            <button type="button" data-mode="category" aria-pressed="false">Category</button>
          </div>
          <p class="fls-card-hint" id="fls-mode-note"></p>
        </section>

        <section class="fls-card" aria-labelledby="fls-c-scope">
          <h3 id="fls-c-scope">Plugins</h3>
          <div class="segmented fls-seg" role="group" aria-label="Plugins to organize">
            <button type="button" data-scope="new" aria-pressed="true">Only new</button>
            <button type="button" data-scope="full" aria-pressed="false">Complete</button>
          </div>
          <p class="fls-card-hint" id="fls-scope-note"></p>
        </section>

        <section class="fls-card" aria-labelledby="fls-c-thumbs">
          <h3 id="fls-c-thumbs">Thumbnails</h3>
          <label class="fls-switch">
            <span class="fls-switch-text">
              <strong>Add VEXT pictures</strong>
              <span class="fls-sub" id="fls-pic-sub"></span>
            </span>
            <input type="checkbox" class="switch" id="fls-pictures">
          </label>
          <label class="fls-switch">
            <span class="fls-switch-text">
              <strong>Replace thumbnails VEXT didn't make</strong>
              <span class="fls-sub">Backed up first, to Thumbnails.vext-backup</span>
            </span>
            <input type="checkbox" class="switch" id="fls-replace">
          </label>
        </section>
      </div>

      <details class="fls-danger" id="fls-advanced">
        <summary>Advanced <span class="fls-sub">Wipe and rebuild from scratch</span></summary>
        <div class="fls-danger-body">
          <label class="fls-switch">
            <span class="fls-switch-text">
              <strong>Dangerous: wipe and rebuild from scratch instead</strong>
              <span class="fls-sub">A clean-slate rebuild of the chosen sections. Leave it off unless you specifically want one.</span>
            </span>
            <input type="checkbox" class="switch" id="fls-dangerous">
          </label>
          <label class="fls-switch fls-save-row" id="fls-save-backup-row" hidden>
            <span class="fls-switch-text">
              <strong>Save a backup of the current folder first</strong>
              <span class="fls-sub">Backed up, not deleted, before the wipe.</span>
            </span>
            <input type="checkbox" class="switch" id="fls-save-backup" checked>
          </label>
        </div>
      </details>

      <div class="fls-result" id="fls-result" aria-live="polite"></div>

      <div class="fls-actionbar">
        <p class="fls-sentence" id="fls-sentence"></p>
        <div class="fls-actions">
          <button type="button" id="fls-preview">Preview</button>
          <button type="button" class="primary fls-apply" id="fls-apply" disabled>Apply</button>
        </div>
      </div>`;

    document.querySelectorAll('#flsync-organize [data-section]').forEach(b => {
      b.onclick = () => {
        const name = b.dataset.section;
        // At least one section stays on: Apply has nothing to do without one.
        if (choice.sections[name] && selectedSections().length === 1) {
          V.toast('Keep at least one of Effects or Generators on.', true);
          return;
        }
        choice.sections[name] = !choice.sections[name];
        changed(true);
      };
    });
    document.querySelectorAll('#flsync-organize [data-mode]').forEach(b => {
      b.onclick = () => { choice.mode = b.dataset.mode; changed(true); };
    });
    document.querySelectorAll('#flsync-organize [data-scope]').forEach(b => {
      b.onclick = () => { choice.scope = b.dataset.scope; changed(true); };
    });
    $('fls-pictures').onchange = (e) => { choice.pictures = e.target.checked; changed(false); };
    $('fls-replace').onchange = (e) => { choice.replace = e.target.checked; changed(false); };
    $('fls-dangerous').onchange = (e) => { choice.dangerous = e.target.checked; changed(false); };
    $('fls-save-backup').onchange = (e) => { choice.saveBackup = e.target.checked; changed(false); };
    $('fls-preview').onclick = preview;
    $('fls-apply').onclick = apply;
  }

  // A choice changed: the last Preview no longer describes it, so Apply waits for a new one.
  // Changes that alter which plugins are listed also drop the destinations picked so far.
  function changed(resetOverrides) {
    previewed = false;
    if (resetOverrides) overrides = {};
    paintOrganize();
  }

  function sentence() {
    const secs = selectedSections();
    if (!secs.length) return 'Pick Effects or Generators to organize.';
    const list = secs.join(' and ');
    const modeText = choice.mode === 'vendor' ? 'by vendor' : 'by category';
    const px = thumbSize();
    let thumbs = 'with thumbnails left as they are';
    if (choice.pictures) {
      thumbs = `with ${sizeName(px)} thumbnails`;
      if (choice.replace) thumbs += ', replacing the ones VEXT didn\'t make (backed up first)';
    }
    if (choice.dangerous) {
      return `${choice.saveBackup ? 'Backs up and clears' : 'Clears'} ${list} and rebuilds it from scratch ${modeText}, ${thumbs}.`;
    }
    if (choice.scope === 'full') return `Files every plugin FL Studio has detected into ${list} ${modeText}, ${thumbs}.`;
    return `Files the new plugins of ${list} ${modeText}, ${thumbs}.`;
  }

  function paintOrganize() {
    const root = $('flsync-organize');
    if (!root.firstChild) return;
    const px = thumbSize();
    root.querySelectorAll('[data-section]').forEach(b => {
      b.setAttribute('aria-pressed', String(!!choice.sections[b.dataset.section]));
    });
    root.querySelectorAll('[data-mode]').forEach(b => {
      b.setAttribute('aria-pressed', String(b.dataset.mode === choice.mode));
    });
    root.querySelectorAll('[data-scope]').forEach(b => {
      b.setAttribute('aria-pressed', String(b.dataset.scope === choice.scope));
    });
    $('fls-mode-note').textContent = MODE_NOTE[choice.mode];
    $('fls-scope-note').textContent = SCOPE_NOTE[choice.scope];
    $('fls-pictures').checked = choice.pictures;
    $('fls-pic-sub').textContent = `${sizeName(px)} (${px} px)`;
    $('fls-replace').checked = choice.replace;
    $('fls-replace').disabled = !choice.pictures;
    $('fls-dangerous').checked = choice.dangerous;
    $('fls-save-backup-row').hidden = !choice.dangerous;
    $('fls-save-backup').checked = choice.saveBackup;

    const stale = !previewed && resultKind === 'preview';
    $('fls-sentence').innerHTML = esc(sentence())
      + (stale ? '<span class="fls-stale">The choices changed since the preview: Preview again before Apply.</span>' : '');
    const apply = $('fls-apply');
    apply.disabled = !previewed || !selectedSections().length;
    apply.classList.toggle('is-danger', choice.dangerous);
  }

  async function preview() {
    const btn = $('fls-preview');
    overrides = {};
    btn.disabled = true;
    try {
      if (!options) options = await V.api('/api/fl-sync/options');
      const data = await V.api(`/api/fl-sync/preview?${new URLSearchParams({ mode: choice.mode, scope: choice.scope })}`);
      renderPreview(data);
      previewed = true;
      resultKind = 'preview';
      paintOrganize();
    } catch (e) { V.handleActionError(e); } finally { btn.disabled = false; }
  }

  function renderPreview(data) {
    const el = $('fls-result');
    const full = choice.scope === 'full';
    const pics = choice.pictures;
    const dests = (options && options[choice.mode]) || [];
    const sections = Object.entries(data).map(([section, s]) => `
      <div class="fls-result-sec">
        <h4 class="fls-result-title">${esc(section)}</h4>
        <div class="fls-stats">
          ${stat(s.total, 'Detected / In New', full ? `${s.new_count} in New\\` : 'in New\\')}
          ${stat(s.filed_count, 'Would be filed')}
          ${stat(s.root_count, 'Left at the root')}
          ${stat(s.renamed_count, 'Names synced', 'from FL Studio')}
          ${pics ? stat(s.picture_count, 'With a VEXT picture') : ''}
        </div>
      </div>`).join('');

    const rootRows = Object.entries(data).flatMap(([section, s]) =>
      (s.root_bound || []).map(e => ({ section, ...e })));
    const rootBox = rootRows.length ? `
      <div class="fls-root-box">
        <h4 class="fls-result-title">Left at the root: pick a destination to file one instead (optional)</h4>
        <div class="fls-table-wrap">
          <table class="fls-root-table">
            <thead><tr><th>Plugin</th><th>Why it is at the root</th><th>Destination</th></tr></thead>
            <tbody>
              ${rootRows.map(e => `
                <tr>
                  <td><strong>${esc(e.ps_name)}</strong> <span class="fls-sub">${esc(e.section)}</span></td>
                  <td class="fls-sub">${esc(e.reason || '')}</td>
                  <td>
                    <select class="fls-override" data-section="${esc(e.section)}" data-key="${esc(e.key)}" aria-label="Destination for ${esc(e.ps_name)}">
                      <option value="">Leave at root</option>
                      ${dests.map(o => `<option value="${esc(o)}">${esc(o)}</option>`).join('')}
                    </select>
                  </td>
                </tr>`).join('')}
            </tbody>
          </table>
        </div>
      </div>` : '';

    el.innerHTML = `<h3 class="fls-result-head">Preview of these choices</h3>${sections}${rootBox}`;
    el.querySelectorAll('.fls-override').forEach(sel => {
      sel.onchange = () => {
        const section = sel.dataset.section, key = sel.dataset.key;
        overrides[section] = overrides[section] || {};
        if (sel.value) overrides[section][key] = sel.value;
        else delete overrides[section][key];
      };
    });
  }

  function renderApplied(data) {
    $('fls-result').innerHTML = `<h3 class="fls-result-head">Applied</h3>` + Object.entries(data).map(([section, s]) => {
      const pic = s.pictures;
      return `
        <div class="fls-result-sec">
          <h4 class="fls-result-title">${esc(section)}</h4>
          <div class="fls-stats">
            ${stat(s.applied, 'Filed')}
            ${stat(s.unmatched, 'Left at the root')}
            ${stat(s.moved, 'Moved', 'already-filed plugins')}
            ${stat(s.skipped, 'Skipped', 'nowhere to file them')}
            ${stat(s.renamed, 'Renamed', 'synced from FL')}
            ${stat(s.assigned, 'Assignments', 'vendor or category saved')}
          </div>
          ${pic ? `<p class="fls-note">Thumbnails: ${pic.added} added, ${pic.updated} updated, ${pic.current} already current, ${pic.kept} kept (not made by VEXT), ${pic.missing} without a VEXT picture.</p>` : ''}
          ${s.thumbnail_backup ? `<p class="fls-note">Replaced thumbnails backed up to: <code>${esc(s.thumbnail_backup)}</code></p>` : ''}
          ${s.backup ? `<p class="fls-note">Backup: <code>${esc(s.backup)}</code></p>` : ''}
          ${s.new_folder_removed ? `<p class="fls-note">Installed\\${esc(section)}\\New\\ removed (now empty).</p>` : ''}
        </div>`;
    }).join('');
  }

  async function apply() {
    const sections = selectedSections();
    if (!sections.length) { V.toast('Pick at least one of Effects/Generators.', true); return; }
    const { mode, scope, dangerous, saveBackup, pictures } = choice;
    const replaceThumbs = pictures && choice.replace;
    const px = thumbSize();
    const picNote = pictures
      ? ` Filed plugins get their VEXT picture as FL thumbnail (${sizeName(px)}, ${px} px)${replaceThumbs ? ', replacing (after a backup) thumbnails VEXT didn\'t make' : ''}.`
      : '';
    const msg = (dangerous
      ? `DANGEROUS: this will ${saveBackup ? 'back up (not delete) the current' : 'clear the current'} `
        + `${sections.join('/')} folder(s) in place, then rebuild from scratch using ${mode} subfolders. `
        + `Installed\\...\\New\\ will be deleted once empty.`
      : scope === 'full'
        ? `This will reorganize ${sections.join('/')} completely by ${mode}, from every plugin FL Studio has detected: `
          + `each is filed where VEXT's data says and anything filed in the wrong folder is moved. FL's Installed\\ `
          + `folders are only read (New\\ is emptied).`
        : `This will file only the new entries from Installed\\${sections.join('\\New\\, Installed\\')}\\New\\ `
          + `by ${mode}. Everything already filed stays where it is.`) + picNote + ' Proceed?';
    if (!(await V.confirmDialog(msg))) return;

    const btn = $('fls-apply');
    btn.disabled = true;
    try {
      const data = await V.api('/api/fl-sync/apply', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          mode, overrides, sections, dangerous_wipe: dangerous, save_backup: saveBackup,
          scope, pictures, replace_thumbnails: replaceThumbs, thumb_size: px,
        }),
      });
      renderApplied(data);
      // The filing changed: the next Apply needs a new Preview, and the picks were used up.
      previewed = false;
      resultKind = 'applied';
      overrides = {};
      V.toast('FL Studio browse tree synced.');
      const check = checkModule();
      if (check && check.refresh) check.refresh();
    } catch (e) { V.handleActionError(e); } finally { paintOrganize(); }
  }

  // ---------- load: every time the section is shown ----------

  async function load() {
    if (!built) {
      buildHead();
      buildOrganize();
      wireTabs();
      built = true;
    }
    // The last inventory (if a check ran this session) fills the header at once.
    const check = checkModule();
    const inv = check && check.inventory ? check.inventory() : null;
    if (inv) {
      lastCounts = inv.counts || null;
      lastRoot = inv.root || null;
    }
    paintHead();

    // A fresh visit starts from the current folder: no old result, no picks, Apply off.
    previewed = false;
    resultKind = null;
    overrides = {};
    $('fls-result').innerHTML = '';
    paintOrganize();
    showTab(currentTab());

    try {
      options = await V.api('/api/fl-sync/options');
    } catch (e) { options = null; V.toast(e.message, true); }
  }

  V.flSync = {
    load,
    thumbSize,
  };
})();
