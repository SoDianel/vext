// Dashboard: the landing section. One GET /api/dashboard call rendered as text-first
// metrics and small hand-drawn charts (inline SVG and CSS, no chart library). Every value
// is also plain text (a label, a legend or the chart's aria-label); the marks illustrate.
//
//   Formats       small multiples of rings: outer = share of the plugins, inner = share of
//                 the disk space, the count in the middle; then Type and Architecture as
//                 two thin proportional strips
//   Metadata      coverage rings (share of the plugins with a vendor, a type, categories,
//                 settled links), each a button into its review; the vendor decisions below
//   To do         files to clean up and the queue, each row with a micro bar of its share
//                 of the library
//   Playground / FL Studio  ring gauges (a count against its whole)
//   Categories    thin bars; Vendors: lollipops, with the size on disk beside them
//
// With visual effects on (VEXT.fx, css/fx.css) the first render of a visit animates:
// numbers count up, rings draw, strips wipe in, bars and stems grow, blocks rise in turn. A refresh keeps the frame and only counts changed numbers to their new
// value. With effects off everything is drawn at once. Hovering or focusing a mark shows
// its exact value (the same text its aria-label carries).
//
// Every count that points at work to do is a button into the section that handles it
// (VEXT.navigate), so this page doubles as the app's to-do list.
// Classic script in an IIFE; talks to the rest of the app only via window.VEXT.
(function () {
  const V = window.VEXT;
  if (!V || typeof V.registerSection !== 'function') return;

  const esc = (v) => V.escHtml(v);
  const num = (n) => Number(n || 0).toLocaleString('en-US');
  const plural = (n, one, many) => `${num(n)} ${n === 1 ? one : (many || one + 's')}`;
  const motion = () => !!(V.fx && V.fx.enabled());

  // Share of a whole as text. A real non-zero sliver never reads as "0%".
  // Nor does an almost-whole read as "100%".
  const pctText = (n, total) => {
    if (!total || !n) return '0%';
    const p = (n / total) * 100;
    if (p < 1) return '<1%';
    if (p > 99 && n < total) return '>99%';
    return `${Math.round(p)}%`;
  };

  // Same 1024 steps as the app's fmtSize, split so the unit can be set small
  // next to a big numeral, and with a space for the labels.
  function sizeParts(bytes) {
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let n = Number(bytes) || 0;
    let i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    const dec = i === 0 || n >= 100 ? 0 : 1;
    return [n.toFixed(dec), units[i], dec];
  }
  const fmtBytes = (b) => sizeParts(b).slice(0, 2).join(' ');

  // "6 days ago" as parts, so the number can be the big numeral and the words
  // stay small. Words-only results ("yesterday", "last week") come back whole.
  function relativeParts(ms) {
    const s = (ms - Date.now()) / 1000;
    if (Math.abs(s) < 60) return { text: 'Just now' };
    const steps = [['year', 31536000], ['month', 2592000], ['week', 604800], ['day', 86400], ['hour', 3600], ['minute', 60]];
    const [unit, size] = steps.find(([, sec]) => Math.abs(s) >= sec);
    const parts = new Intl.RelativeTimeFormat('en', { numeric: 'auto' }).formatToParts(Math.round(s / size), unit);
    const int = parts.find(p => p.type === 'integer');
    if (!int) {
      const text = parts.map(p => p.value).join('');
      return { text: text.charAt(0).toUpperCase() + text.slice(1) };
    }
    const rest = parts.filter(p => p.type === 'literal').map(p => p.value).join('').trim();
    return { num: int.value, unit: rest };
  }

  const FORMAT_LABELS = { 'FL-NATIVE': 'FL native', WAVES: 'Waves' };
  const TYPE_LABELS = { effect: 'Effects', synth: 'Instruments', instrument: 'Instruments' };
  const CATEGORY_PREVIEW = 8;

  // data-nav key → [section, params]. Params are the Actions section's contract:
  // group = pending|cleanup|organize|metadata, chip = the Metadata sub-tab.
  // `focus` is a hint only (which Cleanup subsection to scroll to); safe to ignore.
  const NAV = {
    scan: ['scan'],
    flagged: ['actions', { group: 'cleanup', focus: 'flagged' }],
    dedup_candidates: ['actions', { group: 'cleanup', focus: 'duplicates' }],
    unorganized: ['actions', { group: 'organize' }],
    missing_vendor: ['actions', { group: 'metadata', chip: 'vendor' }],
    ambiguous_vendor: ['actions', { group: 'metadata', chip: 'ambiguous' }],
    inferred_vendor: ['actions', { group: 'metadata', chip: 'inferred' }],
    missing_type: ['actions', { group: 'metadata', chip: 'type' }],
    without_categories: ['actions', { group: 'metadata', chip: 'categories' }],
    missing_links: ['actions', { group: 'metadata', chip: 'links' }],
    pending_actions: ['actions', { group: 'pending' }],
    quarantine: ['quarantine'],
    playground: ['playground'],
    flsync: ['flsync'],
  };

  let root = null;
  let data = null;
  let shown = null;            // the data the page currently shows (count from it on a refresh)
  let showAllCategories = false;
  let requestSeq = 0;
  let enterTimer = 0;
  const bound = new WeakSet();

  // ---------- building blocks ----------

  // A tooltip's two lines: the value (strong) and what it is.
  const tip = (value, label) => `data-tip-v="${esc(value)}" data-tip-l="${esc(label)}"`;

  function block(id, title, meta, body, order) {
    return `
      <section class="dash-block" aria-labelledby="dash-h-${id}"${order == null ? '' : ` style="--b:${order}"`}>
        <div class="dash-block-head">
          <h3 class="dash-label" id="dash-h-${id}">${title}</h3>
          ${meta ? `<span class="dash-meta">${meta}</span>` : ''}
        </div>
        ${body}
      </section>`;
  }

  // A number that can count up: the real text for assistive tech, the moving one hidden.
  function countText(key, value, text, decimals = 0) {
    return `<span class="dash-sr">${esc(text)}</span><span class="dash-count" aria-hidden="true" data-count="${key}" data-to="${value}" data-dec="${decimals}">${esc(text)}</span>`;
  }

  // One ring (or two, nested). Shares are 0..1; null = no inner ring.
  function ringSvg(outer, inner) {
    const arc = (cls, r, p) => (p > 0
      ? `<circle class="${cls}" cx="32" cy="32" r="${r}" pathLength="100" style="--p:${Math.max(0.6, Math.min(100, p * 100)).toFixed(2)}"/>`
      : '');
    return `
      <svg class="dash-ring-svg" viewBox="0 0 64 64" aria-hidden="true" focusable="false">
        <circle class="dash-ring-track" cx="32" cy="32" r="28"/>
        ${arc('dash-ring-arc', 28, outer)}
        ${inner == null ? '' : `<circle class="dash-ring-track is-inner" cx="32" cy="32" r="21"/>${arc('dash-ring-arc is-inner', 21, inner)}`}
      </svg>`;
  }

  // A count against its whole as a ring gauge, with the numbers as text beside it.
  function gauge(label, value, total, i) {
    const p = total ? Math.min(1, (value || 0) / total) : 0;
    const pct = pctText(value, total);
    const t = `${label}: ${num(value)} of ${num(total)} (${pct})`;
    return `
      <div class="dash-gauge" role="img" tabindex="0" aria-label="${esc(t)}" ${tip(`${num(value)} of ${num(total)}`, `${label} · ${pct}`)} style="--i:${i}">
        <div class="dash-ring is-small">${ringSvg(p, null)}<span class="dash-ring-num">${pct}</span></div>
        <div class="dash-gauge-text">
          <span class="dash-gauge-label">${esc(label)}</span>
          <span><strong>${num(value)}</strong> <span class="dash-muted">of ${num(total)}</span></span>
        </div>
      </div>`;
  }

  // A single 100% strip plus its legend (the legend carries every value as text).
  function legend(segs, total, what) {
    return `<ul class="dash-legend">${segs.map((s) => `
      <li tabindex="0" data-hl="${s.color}" ${tip(plural(s.count, 'plugin'), `${s.label} · ${pctText(s.count, total)} of ${what}`)}>
        <span class="dash-swatch" style="background:var(--dash-${s.color})" aria-hidden="true"></span>${esc(s.label)}
        <strong>${num(s.count)}</strong><span class="dash-muted">${pctText(s.count, total)}</span></li>`).join('')}
    </ul>`;
  }

  function strip(name, segs) {
    const shownSegs = segs.filter(s => s.count > 0);
    const total = shownSegs.reduce((a, s) => a + s.count, 0);
    if (!total) return '<div class="dash-none">Nothing recorded yet.</div>';
    const parts = shownSegs.map((s) => `<span class="dash-seg" data-hl="${s.color}" style="flex-grow:${s.count};background:var(--dash-${s.color})" ${tip(plural(s.count, 'plugin'), `${s.label} · ${pctText(s.count, total)} of the library`)}></span>`).join('');
    const aria = `${name}: ` + shownSegs.map(s => `${s.label} ${num(s.count)} (${pctText(s.count, total)})`).join(', ');
    return `
      <div class="dash-part">
        <div class="dash-strip" role="img" aria-label="${esc(aria)}">${parts}</div>
        ${legend(shownSegs, total, 'the library')}
      </div>`;
  }

  // One thin bar scaled to the largest value in its chart, value riding the tip.
  function bar(value, max, text) {
    const p = max > 0 ? Math.max(0, Math.min(1, (value || 0) / max)) : 0;
    return `
      <div class="dash-bar" aria-hidden="true">
        <span class="dash-bar-fill${value > 0 ? '' : ' is-zero'}" style="--p:${p.toFixed(4)}"></span>
        <span class="dash-bar-value">${esc(text)}</span>
      </div>`;
  }

  // A list row that navigates somewhere. Zero rows stay (and still navigate) but go quiet.
  // `total` adds a micro bar: the row's share of the library (-1: an empty slot, so rows
  // without one still line up with those that have it).
  function navRow(nav, label, value, tone, hint, extra = '', total = 0, i = 0) {
    const zero = !value;
    const mini = total > 0
      ? `<span class="dash-mini" aria-hidden="true"><span style="--p:${Math.min(1, (value || 0) / total).toFixed(4)}"></span></span>`
      : total < 0 ? '<span class="dash-mini is-empty" aria-hidden="true"></span>' : '';
    return `
      <button type="button" class="dash-row${zero ? ' is-zero' : ''}${total ? ' has-mini' : ''}" data-nav="${nav}"${hint ? ` title="${esc(hint)}"` : ''} style="--i:${i}">
        <span class="dash-dot${zero ? '' : ' ' + tone}" aria-hidden="true"></span>
        <span class="dash-row-label">${label}</span>
        ${mini}
        <span class="dash-row-value">${num(value)}${extra}</span>
        <span class="dash-chev" aria-hidden="true">›</span>
      </button>`;
  }

  // ---------- sections of the page ----------

  function kpisHtml(d) {
    const [sizeNum, sizeUnit, sizeDec] = sizeParts(d.total_size_bytes);
    const formats = (d.by_format || []).length;
    const missing = (d.health && d.health.missing_vendor) || 0;
    const t = d.last_scan_at ? Date.parse(d.last_scan_at) : NaN;
    let scanValue = '<span class="dash-value-text">Never</span>';
    let scanSub = 'No scan recorded yet';
    if (!isNaN(t)) {
      const r = relativeParts(t);
      const exact = new Date(t).toLocaleString();
      const inner = r.num ? `${r.num}<span class="dash-unit">${esc(r.unit)}</span>` : `<span class="dash-value-text">${esc(r.text)}</span>`;
      scanValue = `<time datetime="${esc(d.last_scan_at)}" title="${esc(exact)}">${inner}</time>`;
      scanSub = `<span title="${esc(exact)}">${esc(new Date(t).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' }))}</span>`;
    }
    return `
      <div class="dash-kpis">
        <div class="dash-kpi dash-kpi-hero" style="--k:0">
          <div class="dash-label">Plugins</div>
          <div class="dash-value">${countText('total', d.total || 0, num(d.total))}</div>
          <div class="dash-sub">in ${plural(formats, 'format')}</div>
        </div>
        <div class="dash-kpi" style="--k:1">
          <div class="dash-label">On disk</div>
          <div class="dash-value" title="${esc(num(d.total_size_bytes))} bytes">${countText('size', Number(sizeNum), sizeNum, sizeDec)}<span class="dash-unit">${sizeUnit}</span></div>
          <div class="dash-sub">about ${fmtBytes(d.total ? d.total_size_bytes / d.total : 0)} per plugin</div>
        </div>
        <div class="dash-kpi" style="--k:2">
          <div class="dash-label">Vendors</div>
          <div class="dash-value">${countText('vendors', d.vendors_total || 0, num(d.vendors_total))}</div>
          <div class="dash-sub">${missing ? `${plural(missing, 'plugin')} without one` : 'every plugin has one'}</div>
        </div>
        <div class="dash-kpi" style="--k:3">
          <div class="dash-label">Last scan</div>
          <div class="dash-value">${scanValue}</div>
          <div class="dash-sub">${scanSub}
            <button type="button" class="primary dash-scan-btn" data-nav="scan">Go to Scan →</button>
          </div>
        </div>
      </div>`;
  }

  function formatsHtml(d, order) {
    const rows = (d.by_format || []).filter(r => r.count > 0);
    const keys = `
      <span class="dash-keys" aria-hidden="true">
        <span class="dash-key"><i class="dash-key-line is-outer"></i>Plugins</span>
        <span class="dash-key"><i class="dash-key-line is-inner"></i>Disk space</span>
      </span>`;
    const body = rows.length ? `<div class="dash-rings">${rows.map((r, i) => {
      const name = FORMAT_LABELS[r.format] || r.format;
      const pc = d.total ? r.count / d.total : 0;
      const ps = d.total_size_bytes ? (r.size_bytes || 0) / d.total_size_bytes : 0;
      const aria = `${name}: ${plural(r.count, 'plugin')}, ${pctText(r.count, d.total)} of the library; ` +
        `${fmtBytes(r.size_bytes)} on disk, ${pctText(r.size_bytes, d.total_size_bytes)} of the disk space`;
      return `
        <figure class="dash-fmt" tabindex="0" role="img" aria-label="${esc(aria)}" style="--i:${i}"
          ${tip(plural(r.count, 'plugin'), `${name} · ${pctText(r.count, d.total)} of the library · ${fmtBytes(r.size_bytes)}, ${pctText(r.size_bytes, d.total_size_bytes)} of the disk space`)}>
          <div class="dash-ring">${ringSvg(pc, ps)}<span class="dash-ring-num">${num(r.count)}</span></div>
          <figcaption>
            <span class="dash-fmt-name" title="${esc(r.format)}">${esc(name)}</span>
            <span class="dash-fmt-sub">${pctText(r.count, d.total)} · ${fmtBytes(r.size_bytes)}</span>
          </figcaption>
        </figure>`;
    }).join('')}</div>` : '<div class="dash-none">No formats recorded.</div>';
    const b = d.by_bitness || {};
    const bits = [
      { label: '64-bit', count: b['64'] || 0, color: 'c1' },
      { label: '32-bit', count: b['32'] || 0, color: 'c2' },
      { label: 'Unknown', count: b.unknown || 0, color: 'muted' },
    ];
    const mix = `
      <div class="dash-mix">
        <div class="dash-mix-item" role="group" aria-labelledby="dash-h-type">
          <h4 class="dash-label" id="dash-h-type">Type</h4>
          ${strip('Plugin type', typeSegments(d))}
        </div>
        <div class="dash-mix-item" role="group" aria-labelledby="dash-h-bits">
          <h4 class="dash-label" id="dash-h-bits">Architecture</h4>
          ${strip('Architecture', bits)}
        </div>
      </div>`;
    return block('formats', 'Formats', rows.length ? keys : '', body + mix, order);
  }

  // Effect and instrument keep fixed colors so they never swap; any custom
  // types take the next slots, unknown is always the neutral gray.
  function typeSegments(d) {
    const rows = (d.by_type || []).filter(r => r.count > 0);
    const isUnknown = (t) => !t || t === 'unknown';
    const rank = (t) => t === 'effect' ? 0 : (t === 'synth' || t === 'instrument') ? 1 : 2;
    const known = rows.filter(r => !isUnknown(r.type)).sort((a, b) => rank(a.type) - rank(b.type) || b.count - a.count);
    const spare = ['c3', 'c4'];
    const segs = [];
    let other = 0;
    for (const r of known) {
      const k = rank(r.type);
      const color = k === 0 ? 'c1' : k === 1 ? 'c2' : spare.shift();
      if (!color) { other += r.count; continue; }
      const label = TYPE_LABELS[r.type] || (r.type.charAt(0).toUpperCase() + r.type.slice(1));
      const same = segs.find(s => s.label === label);
      if (same) same.count += r.count;
      else segs.push({ label, count: r.count, color });
    }
    if (other) segs.push({ label: 'Other types', count: other, color: 'c4' });
    const unknown = rows.filter(r => isUnknown(r.type)).reduce((a, r) => a + r.count, 0);
    if (unknown) segs.push({ label: 'Unknown', count: unknown, color: 'muted' });
    return segs;
  }

  // Metadata coverage: one ring per field, the share of the plugins that have it. A ring is
  // a button into the review that fills that field.
  const COVERAGE = [
    ['missing_vendor', 'Vendor', 'have a vendor'],
    ['missing_type', 'Type', 'have a type'],
    ['without_categories', 'Categories', 'have a category'],
    ['missing_links', 'Links', 'have store, KVR and Equipboard settled'],
  ];

  function metadataHtml(d, order) {
    const h = d.health || {};
    const total = d.total || 0;
    const rings = COVERAGE.map(([key, label, what], i) => {
      const missing = Math.min(total, h[key] || 0);
      const have = total - missing;
      const p = total ? have / total : 0;
      // Never round an incomplete field up to 100%.
      const pct = !missing ? '100%' : `${Math.min(99, Math.floor(p * 100))}%`;
      const sub = missing ? `${num(missing)} missing` : 'Complete';
      const aria = `${label}: ${num(have)} of ${num(total)} plugins ${what} (${pct}). ${missing ? `${num(missing)} missing. ` : ''}Open the review.`;
      return `
        <button type="button" class="dash-fmt dash-cov${missing ? '' : ' is-complete'}" data-nav="${key}" aria-label="${esc(aria)}" style="--i:${i}"
          ${tip(sub, `${label} · ${num(have)} of ${num(total)} plugins ${what}`)}>
          <span class="dash-ring">${ringSvg(p, null)}<span class="dash-ring-num">${pct}</span></span>
          <span class="dash-fmt-cap">
            <span class="dash-fmt-name">${esc(label)}</span>
            <span class="dash-fmt-sub">${esc(sub)}</span>
          </span>
        </button>`;
    }).join('');
    const decisions = `
      <div class="dash-list dash-decisions">
        ${navRow('inferred_vendor', 'Inferred vendors to confirm', h.inferred_vendor || 0, 'note', 'Vendor guessed from the name or path; worth a quick check', '', total, 4)}
        ${navRow('ambiguous_vendor', 'Ambiguous vendors to decide', h.ambiguous_vendor || 0, 'danger', 'More than one vendor could match; needs a decision', '', total, 5)}
      </div>`;
    return block('metadata', 'Metadata', 'Share of the plugins with each field', `<div class="dash-rings dash-cov-rings">${rings}</div>${decisions}`, order);
  }

  // What needs doing: the Cleanup and Organize lists, and the queue.
  function todoHtml(d, order) {
    const h = d.health || {};
    const q = d.queue || {};
    const total = d.total || 0;
    const files = [
      ['flagged', 'Flagged files', 'danger', 'Files the scan marked as corrupted or needing review'],
      ['dedup_candidates', 'Superseded duplicates', 'warn', 'Copies made redundant by a better format or bitness of the same plugin'],
      ['unorganized', 'Needs a vendor folder', 'warn', "Plugins sitting loose instead of inside their vendor's subfolder"],
    ];
    const clear = files.every(([key]) => !h[key]) && !q.pending_actions;
    const size = q.quarantine_size_bytes ? ` <span class="dash-muted">· ${fmtBytes(q.quarantine_size_bytes)}</span>` : '';
    let i = 0;
    const body = `
      <div class="dash-group-label">Files</div>
      <div class="dash-list">${files.map(([key, label, tone, hint]) => navRow(key, label, h[key] || 0, tone, hint, '', total, i++)).join('')}</div>
      <div class="dash-group-label">Queue</div>
      <div class="dash-list">
        ${navRow('pending_actions', 'Pending actions', q.pending_actions || 0, 'warn', 'Proposed moves and quarantines waiting for your confirmation', '', -1, i++)}
        ${navRow('quarantine', 'In quarantine', q.quarantined || 0, 'note', 'Files set aside; restore them or delete them for good', size, -1, i++)}
      </div>
      ${d.out_of_scope ? `<p class="dash-note">${plural(d.out_of_scope, 'file')} found outside the scan scope. Review them in Configuration, Out of scope.</p>` : ''}`;
    const meta = clear ? '<span class="dash-clear"><i aria-hidden="true"></i>All clear</span>' : '';
    return block('todo', 'To do', meta, body, order);
  }

  function playgroundHtml(d) {
    const p = d.playground || {};
    const hostable = p.hostable || 0;
    const link = '<button type="button" class="dash-link" data-nav="playground">Open Playground →</button>';
    return block('playground', 'Playground', link, `
      <div class="dash-stat"><span class="dash-stat-num">${countText('hostable', hostable, num(hostable))}</span>
        <span class="dash-stat-text">${hostable === 1 ? 'plugin' : 'plugins'} ready to test <span class="dash-muted">(VST3, VST2, CLAP, Waves)</span></span></div>
      ${hostable ? `<div class="dash-gauges">${gauge('With a picture', p.with_image || 0, p.catalog || hostable, 0)}</div>` : ''}`, null);
  }

  function flHtml(d) {
    const fl = d.fl || {};
    if (!fl.installed) return '';
    const link = '<button type="button" class="dash-link" data-nav="flsync">FL Studio Sync →</button>';
    return block('fl', 'FL Studio', link, `
      <div class="dash-gauges">${gauge('Known to FL Studio', fl.detected || 0, d.total, 1)}</div>
      <div class="dash-kv"><span>FL Cloud plugins</span><strong>${num(fl.fl_cloud)}</strong></div>`, null);
  }

  function categoriesHtml(d, order) {
    const all = (d.by_category || []).filter(c => c.count > 0);
    const rows = showAllCategories ? all : all.slice(0, CATEGORY_PREVIEW);
    const max = Math.max(0, ...all.map(c => c.count));
    const toggle = all.length > CATEGORY_PREVIEW
      ? `<button type="button" class="dash-link" data-toggle="categories" aria-expanded="${showAllCategories}">${showAllCategories ? 'Show fewer' : `Show all ${all.length}`}</button>`
      : '';
    const body = rows.length ? `
      <div class="dash-bars">${rows.map((c, i) => `
        <div class="dash-brow" role="img" aria-label="${esc(`${c.name}: ${plural(c.count, 'plugin')}`)}" style="--i:${i}"
          ${tip(plural(c.count, 'plugin'), `${c.name} · ${pctText(c.count, d.total)} of the library`)}>
          <span class="dash-rlabel">${esc(c.name)}</span>
          ${bar(c.count, max, num(c.count))}
        </div>`).join('')}
      </div>
      <p class="dash-note">A plugin can sit in more than one category.</p>` : '<div class="dash-none">No categories assigned yet.</div>';
    return block('categories', 'Top categories', toggle, body, order);
  }

  function vendorsHtml(d, order) {
    const rows = d.by_vendor || [];
    const max = Math.max(0, ...rows.map(v => v.count || 0));
    const meta = rows.length && d.vendors_total > rows.length ? `Top ${rows.length} of ${num(d.vendors_total)}` : '';
    const body = rows.length ? `
      <div class="dash-vrow dash-colhead" aria-hidden="true"><span></span><span>Plugins</span><span>Size</span></div>
      <div class="dash-bars">${rows.map((v, i) => {
        const p = max > 0 ? Math.min(1, (v.count || 0) / max) : 0;
        return `
        <div class="dash-vrow" role="img" aria-label="${esc(`${v.name}: ${plural(v.count || 0, 'plugin')}, ${fmtBytes(v.size_bytes)} on disk`)}" style="--i:${i}"
          ${tip(plural(v.count || 0, 'plugin'), `${v.name} · ${fmtBytes(v.size_bytes)} on disk`)}>
          <span class="dash-rlabel">${esc(v.name)}</span>
          <div class="dash-lolli" aria-hidden="true">
            <span class="dash-stem" style="--p:${p.toFixed(4)}"></span><span class="dash-pip"></span><span class="dash-bar-value">${num(v.count)}</span>
          </div>
          <span class="dash-vsize" aria-hidden="true">${fmtBytes(v.size_bytes)}</span>
        </div>`;
      }).join('')}
      </div>` : '<div class="dash-none">No vendors recorded yet.</div>';
    return block('vendors', 'Top vendors', meta, body, order);
  }

  function emptyHtml(d) {
    const scanned = d && d.last_scan_at && !isNaN(Date.parse(d.last_scan_at));
    const title = scanned ? 'The last scan found no plugins' : 'No plugins cataloged yet';
    const text = scanned
      ? 'Check the scan locations in Configuration, or turn on the full C: scan, then scan again.'
      : 'Scan this PC to catalog your VST3, VST2, CLAP, Waves and FL Studio plugins. Scanning only reads; nothing is moved or deleted without your confirmation.';
    return `
      <div class="dash-empty">
        <div class="dash-label">Library</div>
        <div class="dash-empty-title">${title}</div>
        <p>${text}</p>
        <button type="button" class="primary dash-scan-btn" data-nav="scan">Go to Scan →</button>
      </div>`;
  }

  function errorHtml(msg) {
    return `
      <div class="dash-empty">
        <div class="dash-label">Dashboard</div>
        <div class="dash-empty-title">Could not load the overview</div>
        <p>${esc(msg)}</p>
        <button type="button" data-retry>Try again</button>
      </div>`;
  }

  // ---------- rendering and motion ----------

  // enter: play the first-render choreography (fx on). from: the data shown before, so
  // changed numbers count from their old value (a refresh).
  function render({ enter = false, from = null } = {}) {
    if (!root || !data) return;
    const d = data;
    clearTimeout(enterTimer);
    hideTip();
    if (!d.total) { root.innerHTML = emptyHtml(d); shown = d; return; }
    root.innerHTML = `
      <div class="dash${enter ? ' dash-enter' : ''}">
        ${kpisHtml(d)}
        <div class="dash-grid">
          <div class="dash-col">${formatsHtml(d, 0)}${categoriesHtml(d, 4)}</div>
          <div class="dash-col">${metadataHtml(d, 1)}${todoHtml(d, 3)}</div>
          <div class="dash-col"><div class="dash-stack" style="--b:2">${playgroundHtml(d)}${flHtml(d)}</div>${vendorsHtml(d, 5)}</div>
        </div>
      </div>`;
    if (V.fx) {
      const before = enter ? null : (from && from.total ? countsOf(from) : null);
      root.querySelectorAll('.dash-count').forEach((el, i) => {
        const to = Number(el.dataset.to);
        const dec = Number(el.dataset.dec) || 0;
        const fmt = (n) => (dec ? n.toFixed(dec) : Math.round(n).toLocaleString('en-US'));
        if (enter) V.fx.countUp(el, to, { from: 0, format: fmt, delay: 80 + i * 60 });
        else if (before && before[el.dataset.count] != null && before[el.dataset.count] !== to) {
          V.fx.countUp(el, to, { from: before[el.dataset.count], format: fmt, duration: 600 });
        }
      });
    }
    // The entrance plays once; later re-renders (Show all, refresh) don't replay it.
    if (enter) {
      const dash = root.querySelector('.dash');
      enterTimer = setTimeout(() => { if (dash) dash.classList.remove('dash-enter'); }, 2200);
    }
    shown = d;
  }

  // The numbers that count up, by their data-count key, for a given payload.
  function countsOf(d) {
    const size = sizeParts(d.total_size_bytes);
    return {
      total: d.total || 0,
      size: Number(size[0]),
      vendors: d.vendors_total || 0,
      hostable: (d.playground && d.playground.hostable) || 0,
    };
  }

  // ---------- hover / focus: tooltip and highlight ----------

  let tipEl = null;
  function ensureTip() {
    if (tipEl && tipEl.isConnected) return tipEl;
    tipEl = document.createElement('div');
    tipEl.className = 'dash-tip';
    tipEl.setAttribute('role', 'tooltip');
    tipEl.setAttribute('aria-hidden', 'true');
    tipEl.innerHTML = '<strong></strong><span></span>';
    document.body.appendChild(tipEl);
    return tipEl;
  }

  function showTip(target, x, y) {
    const el = ensureTip();
    el.firstChild.textContent = target.dataset.tipV || '';
    el.lastChild.textContent = target.dataset.tipL || '';
    el.classList.add('is-shown');
    placeTip(target, x, y);
  }

  function placeTip(target, x, y) {
    if (!tipEl) return;
    const pad = 12;
    const w = tipEl.offsetWidth;
    const h = tipEl.offsetHeight;
    let left;
    let top;
    if (x == null) {
      // Keyboard focus: above the element, centered.
      const r = target.getBoundingClientRect();
      left = r.left + r.width / 2 - w / 2;
      top = r.top - h - 8;
      if (top < 8) top = r.bottom + 8;
    } else {
      left = x + pad;
      top = y - h - pad;
      if (top < 8) top = y + pad + 4;
      if (left + w > window.innerWidth - 8) left = x - w - pad;
    }
    left = Math.max(8, Math.min(left, window.innerWidth - w - 8));
    top = Math.max(8, Math.min(top, window.innerHeight - h - 8));
    tipEl.style.transform = `translate(${Math.round(left)}px, ${Math.round(top)}px)`;
  }

  function hideTip() {
    if (tipEl) tipEl.classList.remove('is-shown');
    if (root) root.querySelectorAll('.dash-part[data-hl]').forEach(p => delete p.dataset.hl);
  }

  function highlight(target) {
    const part = target.closest('.dash-part');
    const hl = target.closest('[data-hl]');
    if (!part) return;
    if (hl && part.contains(hl)) part.dataset.hl = hl.dataset.hl;
    else delete part.dataset.hl;
  }

  // ---------- section lifecycle ----------

  function bind(el) {
    if (bound.has(el)) return;
    bound.add(el);
    el.addEventListener('click', (e) => {
      const nav = e.target.closest('[data-nav]');
      if (nav && el.contains(nav)) {
        const [section, params] = NAV[nav.dataset.nav] || [];
        if (section) V.navigate(section, params || {});
        return;
      }
      if (e.target.closest('[data-toggle="categories"]')) {
        showAllCategories = !showAllCategories;
        render();
        const t = el.querySelector('[data-toggle="categories"]');
        if (t) t.focus();
        // The rows that just appeared rise in (fx on).
        if (showAllCategories && V.fx) {
          const rows = [...el.querySelectorAll('.dash-brow')].slice(CATEGORY_PREVIEW);
          V.fx.enter(rows, { stagger: 18 });
        }
        return;
      }
      if (e.target.closest('[data-retry]')) load(el);
    });
    el.addEventListener('pointerover', (e) => {
      const t = e.target.closest('[data-tip-v]');
      highlight(e.target);
      if (t && el.contains(t)) showTip(t, e.clientX, e.clientY);
    });
    el.addEventListener('pointermove', (e) => {
      if (tipEl && tipEl.classList.contains('is-shown')) {
        const t = e.target.closest('[data-tip-v]');
        if (t) placeTip(t, e.clientX, e.clientY);
      }
    });
    el.addEventListener('pointerout', (e) => {
      const to = e.relatedTarget;
      const from = e.target.closest('[data-tip-v]');
      if (from && (!to || !from.contains(to))) {
        if (!to || !to.closest || !to.closest('[data-tip-v]')) hideTip();
      }
      if (!to || !el.contains(to)) hideTip();
    });
    el.addEventListener('focusin', (e) => {
      const t = e.target.closest('[data-tip-v]');
      highlight(e.target);
      if (t) showTip(t);
    });
    el.addEventListener('focusout', () => hideTip());
    const main = el.closest('main');
    if (main) main.addEventListener('scroll', hideTip, { passive: true });
  }

  async function load(el, params = {}) {
    root = el;
    bind(el);
    const seq = ++requestSeq;
    const refresh = !!(params && params.refresh);
    // A refetch keeps the previous render, dimmed, instead of flashing a loader.
    const keep = data && el.querySelector('.dash, .dash-empty');
    if (keep) el.classList.add('dash-busy');
    else el.innerHTML = '<div class="dash-loading">Loading…</div>';
    try {
      const d = await V.api('/api/dashboard');
      if (seq !== requestSeq) return;
      const prev = shown;
      data = d;
      // The choreography plays when the section is opened (not on a refresh in place).
      render({ enter: !refresh && motion(), from: prev });
    } catch (err) {
      if (seq !== requestSeq) return;
      const msg = (err && err.message) || String(err);
      V.toast('Could not load the dashboard: ' + msg, true);
      if (!el.querySelector('.dash')) el.innerHTML = errorHtml(msg);
    } finally {
      if (seq === requestSeq) el.classList.remove('dash-busy');
    }
  }

  V.registerSection('dashboard', {
    load(el, params) { return load(el, params); },
    unload() {
      requestSeq++; // a response still in flight no longer renders
      clearTimeout(enterTimer);
      // Left before the entrance ended: the kept page must not replay it when shown again.
      const entering = root && root.querySelector('.dash-enter');
      if (entering) entering.classList.remove('dash-enter');
      hideTip();
      if (root) root.classList.remove('dash-busy');
    },
  });
})();
