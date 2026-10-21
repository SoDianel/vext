// The desktop shell (desktop/) bundles this file into Tauri's own origin
// (https://tauri.localhost) rather than loading it from the FastAPI server
// directly — loading an external http:// URL as the main window content hit
// real Tauri v2 bugs around remote-origin windows (confirmed: JS silently
// failed to execute at all — see AGENTS.md). That means API calls are
// cross-origin there, to the address the shell gives in window.__VEXT__
// (http://127.0.0.1:8000 unless overridden). When the page is served
// by the FastAPI server itself (a plain browser tab, on any port), the API is
// that same origin, so a test server on another port works too.
//
// Sections from /static/js/*.js plug in through window.VEXT (defined below,
// synchronously). The shell's own start waits for DOMContentLoaded, so every
// module script has registered its section before the first one loads.
//
// Guard against double execution in the same global scope: Tauri's `tauri dev`
// injects an auto-reload script that calls window.location.reload() as soon as
// it sees frontendDist change, which happens right at startup because
// beforeDevCommand (copy-frontend.js) rewrites that whole directory after the
// dev server has already started watching it. On WebView2 this self-triggered
// reload has been observed to re-run this classic <script> in the still-live
// global scope instead of a fresh one, which throws "Identifier ... has
// already been declared" on the first top-level const/let/function below and
// aborts the whole file. Wrapping everything in an IIFE behind this flag makes
// a second execution a harmless no-op instead of a crash.
if (!window.__VEXT_APP_LOADED__) {
window.__VEXT_APP_LOADED__ = true;
// window.__VEXT__ = { apiBase, token } is set before this script runs: by the desktop
// shell (an initialization script, per launch) or by the backend when it serves this
// page (injected into index.html). Without it, the old guess still applies.
const API_BASE = (() => {
  const given = window.__VEXT__ && window.__VEXT__.apiBase;
  if (given) return String(given).replace(/\/+$/, '');
  const tauri = !!window.__TAURI__ || /(^|\.)tauri\.localhost$/i.test(location.hostname);
  return !tauri && /^https?:$/.test(location.protocol) && location.host ? location.origin : 'http://localhost:8000';
})();

// The backend refuses every write (any method but GET) that doesn't carry this
// launch's token, so a web page open in the in-app browser, or anywhere else on
// this PC, can't drive the local API. Reads stay token-free (<img> needs that).
const apiToken = () => (window.__VEXT__ && window.__VEXT__.token) || '';

const api = async (path, opts) => {
  const method = String((opts && opts.method) || 'GET').toUpperCase();
  if (method !== 'GET' && method !== 'HEAD' && apiToken()) {
    const headers = new Headers((opts && opts.headers) || {});
    headers.set('X-VEXT-Token', apiToken());
    opts = { ...opts, headers };
  }
  const res = await fetch(API_BASE + path, opts);
  // Any write to a setting makes the cached GET /api/settings stale (see settings()).
  if (opts && opts.method && opts.method !== 'GET' && path.startsWith('/api/settings')) settingsCache = null;
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    const err = new Error(body.detail || res.statusText);
    err.status = res.status;
    // A move / delete Windows refused names the plugin folder VEXT may ask access to (handleActionError).
    if (body.folder) err.folder = body.folder;
    throw err;
  }
  return res.status === 204 ? null : res.json();
};

// GET /api/settings, cached: the sidebar, the plugin modal and the module sections all
// ask whether FL Studio is installed. force=true fetches again.
let settingsCache = null;
function settings(force = false) {
  if (force || !settingsCache) {
    const p = api('/api/settings');
    settingsCache = p;
    p.catch(() => { if (settingsCache === p) settingsCache = null; });
  }
  return settingsCache;
}

// Visual effects (js/fx.js, loaded after this file): VEXT.fx once it's there, else null.
// Every call site below does the plain thing without it, or when effects are off.
const fx = () => (window.VEXT && window.VEXT.fx) || null;
// Rewrites a list; with effects on, its items glide to their new places (FLIP).
const fxFlip = (el, mutate, opts) => { const f = fx(); if (f) f.flip(el, mutate, opts); else mutate(); };
// Lets a row or card leave before its list is reloaded.
const fxLeave = (el) => { const f = fx(); return f ? f.leave(el) : Promise.resolve(); };

const toast = (msg, isError = false) => {
  const root = document.getElementById('toast-root');
  const el = document.createElement('div');
  el.className = 'toast' + (isError ? ' error' : '');
  el.textContent = msg;
  root.appendChild(el);
  setTimeout(() => { const f = fx(); if (f) f.dismiss(el); else el.remove(); }, 4000);
};

// Every "Are you sure?" in this app used to be a native window.confirm().
// Replaced with this in-app modal after a real bug: in the Tauri desktop
// shell (WebView2), a confirm() call can leave the renderer stuck waiting on
// the dialog with no working way to answer it — the whole window then looks
// like every button silently stopped responding (confirmed by hand: the same
// freeze reproduced against this app's own confirm() calls; see AGENTS.md).
// A same-origin in-app modal can't hit that failure mode at all — it's just
// normal DOM/JS, nothing synchronous or native-dialog-based involved.
// In-app text prompt (native prompt()/confirm() freeze inside the desktop shell; see confirmDialog).
// Resolves to the trimmed text, or null when cancelled.
function promptDialog(message, placeholder = '') {
  return new Promise((resolve) => {
    const root = document.getElementById('confirm-root');
    root.innerHTML = `
      <div class="modal-backdrop" id="confirm-backdrop">
        <form class="modal" id="prompt-form">
          <p>${escHtml(message)}</p>
          <input type="text" id="prompt-input" placeholder="${escHtml(placeholder)}" style="width:100%; margin-bottom:14px">
          <div class="actions-row">
            <button type="button" id="confirm-cancel">Cancel</button>
            <button type="submit" class="primary">OK</button>
          </div>
        </form>
      </div>`;
    const input = document.getElementById('prompt-input');
    const finish = (result) => {
      root.innerHTML = '';
      document.removeEventListener('keydown', onKey);
      resolve(result);
    };
    const onKey = (e) => { if (e.key === 'Escape') finish(null); };
    document.addEventListener('keydown', onKey);
    document.getElementById('prompt-form').onsubmit = (e) => { e.preventDefault(); finish(input.value.trim() || null); };
    document.getElementById('confirm-cancel').onclick = () => finish(null);
    document.getElementById('confirm-backdrop').onclick = (e) => { if (e.target.id === 'confirm-backdrop') finish(null); };
    input.focus();
  });
}

function confirmDialog(message) {
  return new Promise((resolve) => {
    const root = document.getElementById('confirm-root');
    const esc = String(message).replace(/&/g, '&amp;').replace(/</g, '&lt;');
    root.innerHTML = `
      <div class="modal-backdrop" id="confirm-backdrop">
        <div class="modal">
          <p>${esc}</p>
          <div class="actions-row">
            <button id="confirm-cancel">Cancel</button>
            <button class="danger" id="confirm-ok">OK</button>
          </div>
        </div>
      </div>
    `;
    const finish = (result) => {
      root.innerHTML = '';
      document.removeEventListener('keydown', onKey);
      resolve(result);
    };
    const onKey = (e) => { if (e.key === 'Escape') finish(false); };
    document.addEventListener('keydown', onKey);
    document.getElementById('confirm-ok').onclick = () => finish(true);
    document.getElementById('confirm-cancel').onclick = () => finish(false);
    document.getElementById('confirm-backdrop').onclick = (e) => {
      if (e.target.id === 'confirm-backdrop') finish(false);
    };
  });
}

// In-app crop: the picture whole, a frame drawn on it (drag to draw, drag inside to move, the
// handles to resize; arrow keys nudge it, Shift by 10). Resolves to {x, y, width, height} in the
// picture's own pixels, or null when cancelled. It sits in confirm-root, above the plugin sheet
// and the picture picker (which leave their keys to a dialog there).
function cropDialog(src, title = 'Crop the picture') {
  return new Promise((resolve) => {
    const root = document.getElementById('confirm-root');
    root.innerHTML = `
      <div class="modal-backdrop crop-backdrop" id="confirm-backdrop">
        <div class="modal crop-modal" role="dialog" aria-modal="true" aria-labelledby="crop-title">
          <h2 id="crop-title">${escHtml(title)}</h2>
          <p class="hint">Drag over the picture to keep only that part. Drag inside the frame to move it, its corners and
            edges to resize it. Arrow keys nudge it (Shift: 10 px).</p>
          <div class="crop-stage" id="crop-stage">
            <img id="crop-img" src="${escHtml(src)}" alt="" draggable="false">
            <div class="crop-sel" id="crop-sel" tabindex="0" aria-label="Crop frame">
              ${['nw', 'n', 'ne', 'e', 'se', 's', 'sw', 'w'].map(h => `<span class="crop-h" data-h="${h}"></span>`).join('')}
            </div>
          </div>
          <div class="actions-row">
            <span class="crop-size" id="crop-size"></span>
            <button type="button" id="crop-reset" title="The whole picture again">Reset</button>
            <span class="grow"></span>
            <button type="button" id="crop-cancel">Cancel</button>
            <button type="button" class="primary" id="crop-ok" disabled>Crop</button>
          </div>
        </div>
      </div>`;
    const img = document.getElementById('crop-img');
    const stage = document.getElementById('crop-stage');
    const selEl = document.getElementById('crop-sel');
    const sizeEl = document.getElementById('crop-size');
    let W = 0, H = 0;               // the picture's own size
    let r = null;                   // the frame, in picture pixels: {x, y, w, h}
    let drag = null;

    const scale = () => (img.clientWidth ? W / img.clientWidth : 1);
    const whole = () => !!r && r.x === 0 && r.y === 0 && r.w === W && r.h === H;
    const clampRect = (q) => {
      const w = Math.max(1, Math.min(Math.round(q.w), W));
      const h = Math.max(1, Math.min(Math.round(q.h), H));
      return { x: Math.max(0, Math.min(Math.round(q.x), W - w)), y: Math.max(0, Math.min(Math.round(q.y), H - h)), w, h };
    };
    const paint = () => {
      if (!r) return;
      const k = scale();
      Object.assign(selEl.style, {
        left: `${img.offsetLeft + r.x / k}px`, top: `${img.offsetTop + r.y / k}px`,
        width: `${r.w / k}px`, height: `${r.h / k}px`,
      });
      sizeEl.textContent = `${r.w} × ${r.h} px${whole() ? ' (whole picture)' : ` of ${W} × ${H}`}`;
      document.getElementById('crop-ok').disabled = whole();
    };
    // A pointer position, in picture pixels (clamped to the picture).
    const at = (e) => {
      const b = img.getBoundingClientRect();
      const k = scale();
      return { x: Math.max(0, Math.min((e.clientX - b.left) * k, W)), y: Math.max(0, Math.min((e.clientY - b.top) * k, H)) };
    };

    const ready = () => {
      W = img.naturalWidth; H = img.naturalHeight;
      r = { x: 0, y: 0, w: W, h: H };
      paint();
      selEl.focus({ preventScroll: true });
    };
    if (img.complete && img.naturalWidth) ready(); else img.addEventListener('load', ready);
    img.addEventListener('error', () => { sizeEl.textContent = 'The picture could not be loaded.'; });
    const onResize = () => paint();
    window.addEventListener('resize', onResize);

    stage.addEventListener('pointerdown', (e) => {
      if (!W || e.button !== 0) return;
      e.preventDefault();
      const p = at(e);
      const handle = e.target.closest('.crop-h');
      if (handle) drag = { mode: handle.dataset.h, start: p, from: { ...r } };
      else if (e.target.closest('.crop-sel') && !whole()) drag = { mode: 'move', start: p, from: { ...r } };
      else drag = { mode: 'new', start: p, from: { x: p.x, y: p.y, w: 1, h: 1 } };
      stage.setPointerCapture(e.pointerId);
    });
    stage.addEventListener('pointermove', (e) => {
      if (!drag) return;
      const p = at(e);
      const f = drag.from;
      const dx = p.x - drag.start.x, dy = p.y - drag.start.y;
      if (drag.mode === 'move') { r = clampRect({ ...f, x: f.x + dx, y: f.y + dy }); paint(); return; }
      if (drag.mode === 'new') {
        r = clampRect({ x: Math.min(p.x, drag.start.x), y: Math.min(p.y, drag.start.y),
          w: Math.abs(p.x - drag.start.x), h: Math.abs(p.y - drag.start.y) });
        paint();
        return;
      }
      // A handle: its edges follow the pointer, the opposite ones stay.
      let x1 = f.x, y1 = f.y, x2 = f.x + f.w, y2 = f.y + f.h;
      if (drag.mode.includes('w')) x1 = Math.min(f.x + dx, x2 - 1);
      if (drag.mode.includes('e')) x2 = Math.max(f.x + f.w + dx, x1 + 1);
      if (drag.mode.includes('n')) y1 = Math.min(f.y + dy, y2 - 1);
      if (drag.mode.includes('s')) y2 = Math.max(f.y + f.h + dy, y1 + 1);
      x1 = Math.max(0, x1); y1 = Math.max(0, y1); x2 = Math.min(W, x2); y2 = Math.min(H, y2);
      r = clampRect({ x: x1, y: y1, w: x2 - x1, h: y2 - y1 });
      paint();
    });
    const endDrag = () => {
      if (drag && drag.mode === 'new' && r.w < 4 && r.h < 4) r = { x: 0, y: 0, w: W, h: H }; // a click, not a drag
      drag = null;
      paint();
    };
    stage.addEventListener('pointerup', endDrag);
    stage.addEventListener('pointercancel', endDrag);

    const finish = (result) => {
      document.removeEventListener('keydown', onKey, true);
      window.removeEventListener('resize', onResize);
      root.innerHTML = '';
      resolve(result);
    };
    const ok = () => {
      if (!r || whole()) return;
      finish({ x: r.x, y: r.y, width: r.w, height: r.h });
    };
    const onKey = (e) => {
      if (!document.getElementById('crop-stage')) { document.removeEventListener('keydown', onKey, true); return; }
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); finish(null); return; }
      if (e.key === 'Enter' && !e.target.closest('button')) { e.preventDefault(); e.stopPropagation(); ok(); return; }
      const step = e.shiftKey ? 10 : 1;
      const d = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] }[e.key];
      if (d && r && !e.target.closest('button')) {
        e.preventDefault();
        e.stopPropagation();
        r = clampRect({ ...r, x: r.x + d[0], y: r.y + d[1] });
        paint();
      }
    };
    document.addEventListener('keydown', onKey, true);
    document.getElementById('crop-ok').onclick = ok;
    document.getElementById('crop-cancel').onclick = () => finish(null);
    document.getElementById('crop-reset').onclick = () => { if (W) { r = { x: 0, y: 0, w: W, h: H }; paint(); } };
    document.getElementById('confirm-backdrop').addEventListener('click', (e) => {
      if (e.target.id === 'confirm-backdrop' && !drag) finish(null);
    });
  });
}

// A failed file action's error is shown as a plain toast by default. When
// Windows refused it because of the folder's permissions (the backend names
// the folder: folder_access.py), VEXT offers to be allowed to manage that one
// folder: Windows asks for permission once, and from then on moves and
// deletes there work without running as Administrator (owner's choice,
// 2026-10-19: ask per folder, never all at once, never by itself). Declined,
// or not that case, the older offer stays: relaunch the whole app as
// Administrator (the same as right-clicking "Run as Administrator").
async function handleActionError(err) {
  const msg = err?.message || String(err);
  if (err && err.folder) {
    const ask = `${msg}\n\nAllow VEXT to manage ${err.folder}? Windows asks for permission once; after that, VEXT `
      + 'can move, organize and delete plugins there without running as Administrator. (Other programs you run '
      + 'could then change files there too.)';
    if (await confirmDialog(ask)) {
      try {
        const r = await api('/api/system/folder-access', { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ folder: err.folder }) });
        toast(r.ok ? `${r.message} Try the action again.` : r.message, !r.ok);
      } catch (e) {
        toast(e.message || String(e), true);
      }
      return;
    }
  }
  const isPermissionError = !!(err && err.folder) || /permission denied|access is denied|read-only attribute|restrictive ACL/i.test(msg);
  if (isPermissionError && isTauri()) {
    if (await confirmDialog(msg + '\n\nRelaunch this app as Administrator and try again?')) {
      window.__TAURI__.core.invoke('relaunch_elevated').catch(e => toast('Could not relaunch elevated: ' + e, true));
      return;
    }
  }
  toast(msg, true);
}

const fmtSize = (n) => {
  if (!n) return '-';
  const units = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i === 0 ? 0 : 1)}${units[i]}`;
};

const escHtml = (v) => String(v ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

const statusBadge = (status) => {
  const cls = status === 'ok' ? 'ok' : status === 'corrupted' ? 'danger' : 'warn';
  return `<span class="badge ${cls}">${status}</span>`;
};

// "Open in Explorer" is best-effort: a file:// link to the containing folder
// (not the file itself — linking straight to a .dll/.vst3/.exe risks the browser
// trying to execute it). Some browsers hand file:// off to the real Windows
// Explorer, others (Chrome, most of the time) just render their own directory
// listing instead — there is no way from a web page to guarantee launching the
// native explorer.exe. Copy path is the reliable fallback: paste it into
// Explorer's address bar yourself.
function hostPathToFolderFileUrl(hostPath) {
  const parts = hostPath.split('\\').filter(Boolean);
  parts.pop(); // drop the plugin's own filename/bundle name, keep the containing folder
  return 'file:///' + parts.map(encodeURIComponent).join('/') + '/';
}

// Running inside the Tauri desktop shell (see /desktop) gives us a REAL "reveal
// in Explorer, file selected" via the opener plugin — window.__TAURI__ is only
// present there (withGlobalTauri: true in tauri.conf.json), so the plain browser
// version of this same page still falls back to the best-effort file:// link.
const isTauri = () => typeof window !== 'undefined' && !!window.__TAURI__;

async function revealInExplorer(hostPath) {
  await window.__TAURI__.core.invoke('plugin:opener|reveal_item_in_dir', { paths: [hostPath] });
}

// A plain <a target="_blank"> doesn't hand off to the OS's real default
// browser inside the Tauri desktop shell the way it does in an actual
// browser tab — real bug reported: clicking a vendor link did nothing
// visible (Tauri's webview has no "open a new tab" concept to fall back to).
// Route through the opener plugin's open_url command instead (already
// covered by the `opener:default` capability — see
// desktop/src-tauri/capabilities — no config change needed).
//
// Registered on `document` with capture:true, not bubble-phase on `body`:
// vendor links also carry `onclick="event.stopPropagation()"` (so clicking
// one inside a Browse-table row doesn't also open that row's plugin-detail
// modal) — stopPropagation() blocks bubble-phase listeners on ancestors like
// `body`, but a capture-phase listener on `document` runs BEFORE the click
// even reaches the link, so it can't be blocked by anything the link's own
// handler does later. Plain browser tab mode is untouched: target="_blank"
// already works correctly there, so this only intercepts when isTauri().
document.addEventListener('click', async (e) => {
  const vendorLink = e.target.closest('.vendor-link');
  if (!vendorLink || !isTauri()) return;
  e.preventDefault();
  try {
    await window.__TAURI__.core.invoke('plugin:opener|open_url', { url: vendorLink.href });
  } catch (err) {
    toast('Could not open link: ' + err, true);
  }
}, true);

function pathActions(hostPath) {
  const esc = hostPath.replace(/"/g, '&quot;');
  const openBtn = isTauri()
    ? `<button type="button" class="path-btn" data-reveal-path="${esc}">📂 Open in Explorer</button>`
    : `<a class="path-btn" href="${hostPathToFolderFileUrl(hostPath)}" target="_blank" rel="noopener noreferrer" ` +
      `title="Best-effort: opens the containing folder. If this just shows a page instead of Windows Explorer, use Copy path and paste it into Explorer's address bar (Win+E), or run the desktop app (see /desktop) for a real Explorer reveal.">📂 Open folder</a>`;
  return `<span class="path-actions">${openBtn}` +
    `<button type="button" class="path-btn" data-copy-path="${esc}">📋 Copy path</button>` +
    `</span>`;
}

document.body.addEventListener('click', async (e) => {
  const revealBtn = e.target.closest('[data-reveal-path]');
  if (revealBtn) {
    try {
      await revealInExplorer(revealBtn.dataset.revealPath);
    } catch (err) {
      toast('Could not open Explorer: ' + err, true);
    }
    return;
  }
  const btn = e.target.closest('[data-copy-path]');
  if (!btn) return;
  try {
    await navigator.clipboard.writeText(btn.dataset.copyPath);
    toast('Path copied.');
  } catch {
    toast('Could not copy automatically — path: ' + btn.dataset.copyPath, true);
  }
});

// Shows what's already going on for a plugin so the UI never lets you propose
// a second action on top of an unresolved one without saying so.
const actionStatusBadge = (actionStatus) => {
  if (!actionStatus) return '';
  if (!actionStatus.applied) return ` <span class="badge warn" title="${actionStatus.reason || ''}">${actionStatus.action} proposed</span>`;
  return ` <span class="badge ok" title="${actionStatus.reason || ''}">${actionStatus.action} applied</span>`;
};

// Vendor name, optionally a clickable link to their site, optionally with a
// red-dotted-circle warning badge (hover for why) when the vendor/product line
// is deprecated or down.
const vendorCell = (r) => {
  if (!r.vendor) return '<span class="badge muted">unknown</span>';
  const label = r.vendor_url
    ? `<a class="vendor-link" href="${r.vendor_url}" target="_blank" rel="noopener noreferrer" onclick="event.stopPropagation()">${r.vendor}</a>`
    : r.vendor;
  const inferredTag = r.vendor_source === 'inferred' ? ' <span class="tag">inferred</span>' : '';
  let warnBadge = '';
  if (r.vendor_status === 'deprecated' || r.vendor_status === 'down') {
    warnBadge = `<span class="vendor-warn-badge" data-warn-vendor="${r.vendor.replace(/"/g, '&quot;')}" ` +
      `data-warn-status="${r.vendor_status}" data-warn-note="${(r.vendor_status_note || '').replace(/"/g, '&quot;')}"></span>`;
  }
  return label + inferredTag + warnBadge;
};

// FL Studio actually queries each plugin for its real name, which catches
// cases our own static metadata reading gets wrong (e.g. a shared white-label
// engine reporting the same generic name for every product built on it — see
// AGENTS.md). Shown as a small confirmation badge when FL's own plugin
// database also knows this plugin; nothing shown when it doesn't (FL simply
// never having scanned a format like Waves/AAX isn't itself notable).
// Small shopping-bag icon before a plugin's name that opens the plugin's own store/product page.
// Nothing at all when the plugin has no store link. The vendor's name keeps its own link to the
// vendor's website (see vendorCell), so the two are visibly different.
const storeIcon = (r) => {
  if (!r.store_url) return '';
  const url = String(r.store_url).replace(/"/g, '&quot;');
  return `<a class="vendor-link store-icon" href="${url}" target="_blank" rel="noopener noreferrer" ` +
    `title="Open this plugin's store page&#10;${url}" aria-label="Open store page for ${String(r.name).replace(/"/g, '&quot;')}" ` +
    `onclick="event.stopPropagation()"><svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" fill="none" ` +
    `stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round">` +
    `<path d="M2.5 5.5h11l-.8 8h-9.4z"/><path d="M5.5 5.5V4a2.5 2.5 0 0 1 5 0v1.5"/></svg></a>`;
};
const bitBadge = (r) => r.bitness
  ? ` <span class="tag bits" title="${r.bitness}-bit binary (read from the PE header).">x${r.bitness === 64 ? '64' : '86'}</span>`
  : '';
const flCloudBadge = (r) => r.fl_cloud
  ? ` <span class="tag fl-cloud" title="FL Cloud plugin — pinned to its install path; never moved, deduped or quarantined.">☁ FL Cloud</span>`
  : '';
const flBadge = (r) => r.fl_detected
  ? ` <span class="tag fl-detected" title="Also found in FL Studio's own plugin database${r.fl_name ? ' as “' + r.fl_name + '”' : ''}.">FL</span>`
  : '';

// Every row, tile or card that stands for ONE plugin carries data-plugin-id and ends
// with this button. js/contextmenu.js listens on the document for both (right-click
// on the row, click on the button) and opens the plugin's menu. A row's own click
// handler must ignore clicks inside .row-menu-btn.
const rowMenuBtn = (id) =>
  `<button type="button" class="row-menu-btn" data-plugin-id="${id}" aria-label="Actions" title="Actions">⋯</button>`;

// Without the context-menu module the ⋯ button still does something useful: open the details.
document.addEventListener('click', (e) => {
  const btn = e.target.closest && e.target.closest('.row-menu-btn');
  if (!btn || (window.VEXT && window.VEXT.contextMenu)) return;
  const id = parseInt(btn.dataset.pluginId, 10);
  if (id) showPluginModal(id);
});

// ---------- sections ----------
// One section is visible at a time. The shell's own sections load through
// SHELL_SECTIONS; the module sections (Dashboard, Playground, from /static/js)
// register through VEXT.registerSection and get load(root, params) every time
// they're shown and unload() when they're left. Configuration is a section too,
// opened from the button at the foot of the sidebar.
const SECTION_TITLES = {
  dashboard: 'Dashboard', playground: 'Playground', analyzer: 'Analyzer', scan: 'Scan', database: 'Database',
  actions: 'Actions', quarantine: 'Quarantine', flsync: 'FL Studio Sync', config: 'Configuration', logs: 'Log',
};
const SHELL_SECTIONS = {
  scan: (params) => loadScanSection(params),
  database: () => loadReference(),
  actions: (params) => loadActionsSection(params),
  quarantine: () => loadQuarantine(),
  // Only exists while FL Studio is installed (the sidebar hides it otherwise, see applyFlStudioPresence).
  // The page itself is js/flsync.js (Organize) and js/flsync-check.js (Check & fix).
  flsync: () => settings().then(s => (s.fl_studio_installed
    ? (window.VEXT.flSync ? window.VEXT.flSync.load() : null) : navigate('dashboard'))),
  config: (params) => showConfigPane(params.pane || configPane),
  logs: () => loadLogsSection(),
};
const moduleSections = {};
let activeSection = null;
let activeParams = {};
let previousSection = null;
let booted = false;

function registerSection(name, impl) {
  moduleSections[name] = impl || {};
  // A module that registers after the shell started still shows up where it belongs.
  if (!booted) return;
  if (name === activeSection) loadSection(name, activeParams);
  else if (name === 'config-audio' && activeSection === 'config' && AUDIO_MODULE_PANES[configPane]) showConfigPane(configPane);
}

// Calls a module's hook without letting a broken module take the shell down with it.
function callModule(name, hook, ...args) {
  const m = moduleSections[name];
  if (!m || typeof m[hook] !== 'function') return;
  try {
    const r = m[hook](...args);
    if (r && typeof r.catch === 'function') r.catch(e => console.error(`[${name}] ${hook} failed:`, e));
  } catch (e) { console.error(`[${name}] ${hook} failed:`, e); }
}

// A module section whose script hasn't registered (yet) shows a quiet placeholder.
function moduleRootOrPlaceholder(name) {
  const root = document.getElementById(name + '-root');
  if (root && !moduleSections[name] && !root.childElementCount) {
    root.innerHTML = '<div class="empty-state module-missing">Nothing to show here yet.</div>';
  }
  return root;
}

function loadSection(section, params = {}) {
  if (moduleSections[section]) {
    callModule(section, 'load', document.getElementById(section + '-root'), params);
    return;
  }
  if (!SHELL_SECTIONS[section]) { moduleRootOrPlaceholder(section); return; }
  // Synchronously, so a pane is set up (or left) in the same order navigate() was called.
  const fail = (e) => toast((e && e.message) || String(e), true);
  try {
    const r = SHELL_SECTIONS[section](params);
    if (r && typeof r.catch === 'function') r.catch(fail);
  } catch (e) { fail(e); }
}

function leaveSection(section, next) {
  if (!section) return;
  callModule(section, 'unload', next);
  if (section === 'config') leaveAudioPane();
  if (section === 'logs' && logsPollTimer) { clearInterval(logsPollTimer); logsPollTimer = null; }
  if (section === 'scan') {
    // ~690 rows kept in a hidden section still cost memory and a relayout on return; the
    // Plug-ins tab builds them again when it shows.
    document.getElementById('browse-tbody').innerHTML = '';
  }
}

function navigate(section, params = {}) {
  if (!SECTION_TITLES[section] || !document.getElementById('panel-' + section)) section = 'dashboard';
  if (section !== activeSection) {
    leaveSection(activeSection, section);
    previousSection = activeSection;
  }
  activeSection = section;
  activeParams = params || {};
  // For section-specific layout in CSS (the Playground fills the window and doesn't scroll).
  document.documentElement.dataset.section = section;
  const panel = document.getElementById('panel-' + section);
  document.querySelectorAll('main > .panel').forEach(p => p.classList.toggle('active', p === panel));
  document.querySelectorAll('.sidebar button[data-tab]').forEach(b => {
    b.classList.toggle('active', b.dataset.tab === section);
    if (b.dataset.tab === section) b.setAttribute('aria-current', 'page'); else b.removeAttribute('aria-current');
  });
  const gear = document.getElementById('config-btn');
  gear.classList.toggle('active', section === 'config');
  gear.setAttribute('aria-pressed', String(section === 'config'));
  closeSidebarOverlay();
  document.getElementById('page-title').textContent = SECTION_TITLES[section];
  document.querySelector('main').scrollTop = 0;
  hideTooltip();
  // For modules that hold something open over the page (the context menu closes on it).
  document.dispatchEvent(new CustomEvent('vext:sectionchange', { detail: { section, params: activeParams } }));
  loadSection(section, activeParams);
}

// Reloads the sidebar counts and the visible section in place (no unload: the section
// wasn't left). Modules get {refresh: true}, so their load() can keep what's selected.
function refresh() {
  loadStats();
  if (activeSection) loadSection(activeSection, { refresh: true });
}

document.querySelectorAll('.sidebar button[data-tab]').forEach(btn => {
  btn.addEventListener('click', () => navigate(btn.dataset.tab));
});
// The gear opens Configuration; pressed again, it goes back to where you were.
document.getElementById('config-btn').addEventListener('click', () => {
  if (activeSection === 'config') navigate(previousSection && previousSection !== 'config' ? previousSection : 'dashboard');
  else navigate('config');
});

// ---------- Actions: groups (Pending / Cleanup / Organize / Metadata) ----------
// The old Actions, Cleanup review, Flagged, Organize and review tabs, re-homed as
// groups of one section. navigate('actions', {group, chip}) opens a given group and,
// for Metadata, a given chip. Without one, the last group and chip used stay open
// (remembered across restarts), else Pending. {focus: 'duplicates'|'flagged'} scrolls
// Cleanup to that subsection.
const ACTION_GROUPS = ['pending', 'cleanup', 'organize', 'metadata'];
const METADATA_CHIPS = ['vendor', 'inferred', 'ambiguous', 'type', 'categories', 'links', 'picture'];
const storedPick = (key, allowed, fallback) => {
  try { const v = localStorage.getItem(key); return allowed.includes(v) ? v : fallback; } catch { return fallback; }
};
let actionsGroup = storedPick('pa-actions-group', ACTION_GROUPS, 'pending');
let metadataChip = storedPick('pa-metadata-chip', METADATA_CHIPS, 'vendor');

function loadActionsSection(params = {}) {
  if (METADATA_CHIPS.includes(params.chip)) { metadataChip = params.chip; actionsGroup = 'metadata'; }
  if (ACTION_GROUPS.includes(params.group)) actionsGroup = params.group;
  try {
    localStorage.setItem('pa-actions-group', actionsGroup);
    localStorage.setItem('pa-metadata-chip', metadataChip);
  } catch { /* storage unavailable */ }
  document.querySelectorAll('#actions-groups button[data-group]').forEach(b => {
    const on = b.dataset.group === actionsGroup;
    b.classList.toggle('active', on);
    b.setAttribute('aria-selected', String(on));
  });
  document.querySelectorAll('#panel-actions .actions-group').forEach(g => g.classList.toggle('active', g.dataset.group === actionsGroup));
  document.querySelectorAll('#metadata-chips button[data-chip]').forEach(b => {
    const on = b.dataset.chip === metadataChip;
    b.classList.toggle('active', on);
    b.setAttribute('aria-selected', String(on));
  });
  document.querySelectorAll('#panel-actions .metadata-pane').forEach(p => p.classList.toggle('active', p.dataset.chip === metadataChip));

  if (actionsGroup === 'pending') return loadActions();
  if (actionsGroup === 'cleanup') {
    return Promise.all([loadCleanup(), loadFlagged()]).then(() => {
      const to = { duplicates: 'cleanup-duplicates', flagged: 'cleanup-flagged' }[params.focus];
      if (to) document.getElementById(to).scrollIntoView({ block: 'start' });
    });
  }
  if (actionsGroup === 'organize') return loadOrganize();
  return metadataChip === 'ambiguous' ? loadAmbiguous() : loadReview(metadataChip);
}

document.querySelectorAll('#actions-groups button[data-group]').forEach(b => {
  b.addEventListener('click', () => navigate('actions', { group: b.dataset.group }));
});
document.querySelectorAll('#metadata-chips button[data-chip]').forEach(b => {
  b.addEventListener('click', () => navigate('actions', { group: 'metadata', chip: b.dataset.chip }));
});

// ---------- configuration panes ----------
// Configuration is split into panes so one long list doesn't have to be scrolled
// past to reach the rest. Each pane loads when it's shown. Audio comes from
// js/audio-settings.js (registered as 'config-audio'): load() when its pane is
// shown, unload() when the pane or Configuration is left.
const CONFIG_PANES = ['general', 'locations', 'daws', 'audio', 'midi', 'performance', 'ai', 'ignored', 'outofscope', 'registry', 'storage'];
// Panes drawn by js/audio-settings.js ('config-audio'): one part each, in its own root.
const AUDIO_MODULE_PANES = { audio: 'config-audio-root', midi: 'config-midi-root', performance: 'config-performance-root' };
const CONFIG_LOADERS = {
  general: () => Promise.all([loadAutoPropose(), loadConfigSettings()]),
  locations: () => Promise.all([loadConfigScanRoots(), loadConfigRemovedRoots()]),
  daws: () => loadDaws(),
  ai: () => Promise.all([loadAi(), initBatchPanel()]),
  ignored: () => loadConfigIgnored(),
  outofscope: () => Promise.all([loadOutOfScope(), loadScopePaths()]),
  // js/config-storage.js: caches and backups, measured, cleared.
  storage: () => (window.VEXT.configStorage ? window.VEXT.configStorage.load(document.getElementById('config-storage-root')) : null),
};
let configPane = 'general';
let audioPaneShown = false;

function leaveAudioPane() {
  if (!audioPaneShown) return;
  audioPaneShown = false;
  callModule('config-audio', 'unload');
}

function showConfigPane(pane) {
  if (!CONFIG_PANES.includes(pane)) pane = 'general';
  leaveAudioPane();
  configPane = pane;
  document.querySelectorAll('#config-tabs button[data-pane]').forEach(b => {
    const on = b.dataset.pane === pane;
    b.classList.toggle('active', on);
    b.setAttribute('aria-selected', String(on));
  });
  document.querySelectorAll('.config-pane').forEach(p => p.classList.toggle('active', p.dataset.pane === pane));
  if (AUDIO_MODULE_PANES[pane]) {
    const root = document.getElementById(AUDIO_MODULE_PANES[pane]);
    if (moduleSections['config-audio']) {
      audioPaneShown = true;
      callModule('config-audio', 'load', root, { part: pane });
    } else if (!root.childElementCount) {
      root.innerHTML = '<div class="empty-state module-missing">Audio settings aren\'t available yet.</div>';
    }
    return;
  }
  return CONFIG_LOADERS[pane] ? CONFIG_LOADERS[pane]() : undefined;
}

document.querySelectorAll('#config-tabs button[data-pane]').forEach(btn => {
  btn.addEventListener('click', () => navigate('config', { pane: btn.dataset.pane }));
});

// ---------- FL Studio present? ----------
// FL Studio Sync (sidebar) and the FL Cloud options only make sense with FL Studio installed.
async function applyFlStudioPresence(force = false) {
  let s;
  try { s = await settings(force); } catch { return; }
  document.getElementById('nav-group-daw').hidden = !s.fl_studio_installed;
  if (!s.fl_studio_installed && activeSection === 'flsync') navigate('dashboard');
}

// ---------- view modes, per section ----------
// Each section with lists has its own view selector, placed in the section right above the
// list it reshapes (Scan: the Browse bar; Actions: every group; Quarantine: above its list;
// Playground: the plugins header, rendered by playground.js from viewToggleHtml()). Each
// section remembers its own mode ('pa-view-<section>'). The mode is data-view on the
// section's view root, which style.css keys on ([data-tiles][data-view="grid"] …);
// modules read VEXT.view(section) and listen for 'vext:viewchange' {section, view}.
//   list    rows of text (Browse: the table)
//   thumbs  "Small images": compact rows that start with the plugin's picture
//   grid    tiles
const VIEW_LABELS = { list: 'List', thumbs: 'Small images', grid: 'Tiles' };
const VIEW_ICONS = {
  list: '<path d="M2.5 4h11M2.5 8h11M2.5 12h11" stroke-width="1.7" stroke-linecap="round"/>',
  thumbs: '<rect x="1.75" y="2.25" width="4.5" height="4.5" rx="1.1" stroke-width="1.4"/><rect x="1.75" y="9.25" width="4.5" height="4.5" rx="1.1" stroke-width="1.4"/><path d="M8.75 4.5h5.5M8.75 11.5h5.5" stroke-width="1.7" stroke-linecap="round"/>',
  grid: '<path d="M2.5 2.5h4.5v4.5H2.5zM9 2.5h4.5v4.5H9zM2.5 9H7v4.5H2.5zM9 9h4.5v4.5H9z" stroke-width="1.5" stroke-linejoin="round"/>',
};
const VIEW_SECTIONS = {
  scan: { modes: ['list', 'thumbs', 'grid'], names: { list: 'Table' }, label: 'Catalog view', root: 'browse' },
  actions: { modes: ['list', 'thumbs', 'grid'], label: 'Actions view', root: 'panel-actions' },
  quarantine: { modes: ['list', 'thumbs', 'grid'], label: 'Quarantine view', root: 'panel-quarantine' },
  // The Playground's list always shows pictures, so its two modes are Small images and Tiles.
  playground: { modes: ['thumbs', 'grid'], label: 'Plugins view', root: 'panel-playground' },
};
const viewModes = {};

// The saved mode, else the old global one ('pa-view': list | grid, before 2026-10-15),
// else the section's first mode.
function storedView(section) {
  const modes = VIEW_SECTIONS[section].modes;
  let v = null;
  try {
    v = localStorage.getItem('pa-view-' + section);
    if (!modes.includes(v)) {
      const old = localStorage.getItem('pa-view');
      v = old === 'grid' ? 'grid' : modes[0];
    }
  } catch { /* storage unavailable */ }
  return modes.includes(v) ? v : modes[0];
}

// VEXT.view(section): that section's mode. Without a section, the visible section's (or
// 'list' where there is no selector), as the old global toggle answered.
function currentView(section) {
  const s = section === undefined ? activeSection : section;
  const cfg = VIEW_SECTIONS[s];
  if (!cfg) return 'list';
  return viewModes[s] || storedView(s);
}

function setView(section, view, opts = {}) {
  const cfg = VIEW_SECTIONS[section];
  if (!cfg || !cfg.modes.includes(view)) return;
  const root = document.getElementById(cfg.root);
  const changed = viewModes[section] !== view;
  const update = () => {
    viewModes[section] = view;
    if (root) root.dataset.view = view;
    document.querySelectorAll(`.view-toggle[data-view-for="${section}"] button[data-view-mode]`)
      .forEach(b => b.setAttribute('aria-pressed', String(b.dataset.viewMode === view)));
    try { localStorage.setItem('pa-view-' + section, view); } catch { /* storage unavailable */ }
    if (changed) document.dispatchEvent(new CustomEvent('vext:viewchange', { detail: { section, view } }));
  };
  // With effects on, the items in view morph into their new shape (rows <-> tiles).
  const f = fx();
  if (f && changed && opts.animate !== false && root && root.getClientRects().length) f.swap(update, { morph: viewListEl(root) });
  else update();
}

// The list a section's view reshapes (Browse rows, the open group's cards, the Playground
// list): the visible one with the most items; they carry data-plugin-id / data-id.
function viewListEl(root) {
  const lists = [...root.querySelectorAll('#browse-tbody, .card-list, .pg-list')].filter(el => el.getClientRects().length);
  return lists.sort((a, b) => b.childElementCount - a.childElementCount)[0] || null;
}

// The selector: one small segmented control, the same in every section.
function viewToggleHtml(section) {
  const cfg = VIEW_SECTIONS[section];
  if (!cfg) return '';
  const cur = currentView(section);
  return `<div class="view-toggle" role="group" aria-label="${cfg.label}" data-view-for="${section}">` + cfg.modes.map((m) => {
    const name = (cfg.names && cfg.names[m]) || VIEW_LABELS[m];
    return `<button type="button" data-view-mode="${m}" aria-pressed="${m === cur}" aria-label="${name}" title="${name}">` +
      `<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" fill="none" stroke="currentColor">${VIEW_ICONS[m]}</svg></button>`;
  }).join('') + '</div>';
}

document.addEventListener('click', (e) => {
  const b = e.target.closest && e.target.closest('.view-toggle[data-view-for] button[data-view-mode]');
  if (b) setView(b.closest('.view-toggle').dataset.viewFor, b.dataset.viewMode);
});
(function initViews() {
  Object.keys(VIEW_SECTIONS).forEach(s => setView(s, storedView(s), { animate: false }));
  try { localStorage.removeItem('pa-view'); } catch { /* storage unavailable */ }
  document.querySelectorAll('.view-toggle[data-view-for]').forEach((el) => {
    if (!el.childElementCount) el.outerHTML = viewToggleHtml(el.dataset.viewFor);
  });
})();

// ---------- overlays ----------
// The Playground's web browser is a native webview laid over this page, so it
// covers any HTML drawn where it sits. While a modal, a confirmation, the agent
// picker or the context menu is open, the Playground hides it. Watched here, in
// one place for every overlay: 'vext:overlay' {open} on document when it changes,
// and VEXT.overlayOpen() for the current state.
let overlayIsOpen = false;
const overlayOpen = () => !!(document.getElementById('modal-root').childElementCount
  || document.getElementById('confirm-root').childElementCount
  || document.querySelector('#imgsearch-root > *')
  || document.querySelector('body > .cm'));
// Whether an overlay covers the rectangle r ({x, y, width, height}, window coordinates).
// Modals, confirmations and the image search fill the window; the context menu only covers
// where it sits, so one opened over the plugin list leaves the browser showing. A menu still
// pending hasn't been placed yet (contextmenu.js announces it again once it is). offset* are
// unaffected by the menu's open animation (a scale), unlike getBoundingClientRect.
const overlayCovers = (r) => {
  if (document.getElementById('modal-root').childElementCount
    || document.getElementById('confirm-root').childElementCount
    || document.querySelector('#imgsearch-root > *')) return true;
  return [...document.querySelectorAll('body > .cm:not(.is-pending)')].some((m) => (
    m.offsetLeft < r.x + r.width && m.offsetLeft + m.offsetWidth > r.x
    && m.offsetTop < r.y + r.height && m.offsetTop + m.offsetHeight > r.y));
};
const overlayWatch = new MutationObserver(() => {
  const open = overlayOpen();
  if (open === overlayIsOpen) return;
  overlayIsOpen = open;
  document.dispatchEvent(new CustomEvent('vext:overlay', { detail: { open } }));
});
overlayWatch.observe(document.getElementById('modal-root'), { childList: true });
overlayWatch.observe(document.getElementById('confirm-root'), { childList: true });
overlayWatch.observe(document.body, { childList: true }); // the context menu (.cm) is appended to body

// ---------- runtime contract for /static/js modules ----------
// See the shared UI spec: modules use only this object and the DOM.
window.VEXT = {
  API_BASE,
  api,
  token: apiToken,
  overlayOpen,
  overlayCovers,
  toast,
  escHtml,
  fmtSize,
  confirmDialog,
  handleActionError,
  isTauri,
  showPluginModal,
  showPluginEditForm,
  closeModal,
  proposeAction,
  revealInExplorer,
  pickAgent,
  // openPicturePicker(pluginId, {selected?, onDone?}): the picture picker popup (pictures found online).
  openPicturePicker,
  settings,
  refresh,
  navigate,
  registerSection,
  currentSection: () => activeSection,
  // View modes (per section): view(section) → 'list' | 'thumbs' | 'grid'; setView(section, mode);
  // viewToggleHtml(section) = the shared selector, for a module that renders its own header.
  view: currentView,
  setView,
  viewToggleHtml,
};

// ---------- sidebar: expanded / collapsed to its icons ----------
// The logo is the button. The choice is remembered per user ('pa-sidebar':
// 'collapsed' | 'expanded'; the old 'hidden' reads as collapsed). Narrow windows
// are always collapsed (html.sidebar-rail) and the logo opens the full sidebar
// over the page instead (html.sidebar-overlay) until a section is picked, a
// click lands outside it, or Escape.
const narrowWindow = window.matchMedia('(max-width: 880px)');
let sidebarCollapsed = (() => {
  try { return ['collapsed', 'hidden'].includes(localStorage.getItem('pa-sidebar')); } catch { return false; }
})();
let sidebarOverlay = false;

function setSidebar() {
  const root = document.documentElement;
  const narrow = narrowWindow.matches;
  if (!narrow) sidebarOverlay = false;
  const rail = narrow || sidebarCollapsed;
  root.classList.toggle('sidebar-rail', rail);
  root.classList.toggle('sidebar-overlay', narrow && sidebarOverlay);
  const open = !rail || sidebarOverlay;
  const btn = document.getElementById('sidebar-toggle');
  const label = open ? 'Collapse the sidebar' : 'Expand the sidebar';
  btn.setAttribute('aria-expanded', String(open));
  btn.setAttribute('aria-label', label);
  btn.title = label;
  // Collapsed, the names only show as tooltips.
  document.querySelectorAll('.sidebar button[data-tab], #config-btn, #about-btn').forEach(b => {
    const name = b.querySelector('.nav-text');
    if (open) b.removeAttribute('title'); else if (name) b.title = name.textContent;
  });
}
function closeSidebarOverlay() {
  if (!sidebarOverlay) return;
  sidebarOverlay = false;
  setSidebar();
}
document.getElementById('sidebar-toggle').addEventListener('click', () => {
  if (narrowWindow.matches) {
    sidebarOverlay = !sidebarOverlay;
  } else {
    sidebarCollapsed = !sidebarCollapsed;
    try { localStorage.setItem('pa-sidebar', sidebarCollapsed ? 'collapsed' : 'expanded'); } catch { /* storage unavailable */ }
  }
  setSidebar();
});
narrowWindow.addEventListener('change', setSidebar);
document.addEventListener('pointerdown', (e) => {
  if (sidebarOverlay && !e.target.closest('.sidebar')) closeSidebarOverlay();
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && sidebarOverlay) closeSidebarOverlay();
});
setSidebar();

// ---------- appearance (light / dark / system) ----------
// "system" stores nothing and removes data-theme, so the stylesheet follows the
// OS. Light and dark are stored per-user in localStorage. A storage failure
// just means the choice doesn't persist; the page still renders correctly.
function applyTheme(choice) {
  const root = document.documentElement;
  if (choice === 'light' || choice === 'dark') root.dataset.theme = choice;
  else delete root.dataset.theme;
  try {
    if (choice === 'light' || choice === 'dark') localStorage.setItem('pa-theme', choice);
    else localStorage.removeItem('pa-theme');
  } catch { /* storage unavailable */ }
  document.querySelectorAll('[data-theme-choice]').forEach(b => {
    const on = b.dataset.themeChoice === choice;
    b.setAttribute('aria-checked', String(on));
    b.classList.toggle('active', on);
  });
  // For anything drawn with colours read from the stylesheet (the 3D Scan button).
  document.dispatchEvent(new CustomEvent('vext:themechange', { detail: { choice } }));
}
document.querySelectorAll('[data-theme-choice]').forEach(b => {
  b.addEventListener('click', () => applyTheme(b.dataset.themeChoice));
});
applyTheme((() => {
  try { return localStorage.getItem('pa-theme') || 'system'; } catch { return 'system'; }
})());

// ---------- counts: sidebar badges and the Actions group counts ----------
// The stats row that used to sit above every section is gone (the Dashboard has the
// numbers). What's left are small counts: sidebar badges (Actions: pending actions +
// open issues, Quarantine: items) and one per Actions group and Metadata chip. The
// review lists count from /api/review-summary, so a chip matches its own list.
let lastStats = null;
let statsShown = false; // the counts were painted once (later changes may animate)

function setNavBadge(id, n, attention, title) {
  const el = document.getElementById(id);
  if (!el) return;
  const before = el.hidden ? '' : el.textContent;
  el.hidden = !n;
  el.textContent = n > 999 ? '999+' : String(n || 0);
  el.classList.toggle('attn', !!attention);
  el.title = title;
  // A count that changes (not the first one shown) gives a small bump.
  if (statsShown && n && before !== el.textContent && fx()) fx().pulse(el);
}

async function loadStats() {
  let s, rs;
  try {
    [s, rs] = await Promise.all([api('/api/stats'), api('/api/review-summary').catch(() => null)]);
  } catch { return null; }
  lastStats = s;
  const listed = (kind, fallback) => (rs && rs[kind] ? rs[kind].total : fallback);
  const c = {
    pending: s.pending_actions,
    dedup: s.dedup_candidates,
    flagged: s.flagged,
    organize: s.unorganized,
    vendor: listed('vendor', s.missing_vendor),
    inferred: listed('inferred_vendor', s.inferred_vendor),
    ambiguous: s.ambiguous_vendor,
    type: listed('type', s.missing_type),
    categories: listed('categories', s.without_categories),
    links: listed('links', null),
    picture: listed('picture', null),
  };
  c.cleanup = c.dedup + c.flagged;
  // A missing picture is cosmetic, not a gap to fix: it has its chip count only, outside the
  // Metadata total and the sidebar badge.
  c.metadata = METADATA_CHIPS.reduce((n, k) => n + (k === 'picture' ? 0 : (c[k] || 0)), 0);
  document.querySelectorAll('#panel-actions [data-count]').forEach(el => {
    const v = c[el.dataset.count];
    const text = v == null ? '' : String(v);
    const changed = statsShown && el.textContent !== '' && el.textContent !== text;
    el.textContent = text;
    el.classList.toggle('zero', !v);
    if (changed && fx()) fx().pulse(el);
  });
  const issues = c.cleanup + c.organize + c.metadata;
  setNavBadge('nav-badge-actions', c.pending + issues, c.pending > 0,
    `${c.pending} pending · ${c.cleanup} to clean up · ${c.organize} to organize · ${c.metadata} metadata gaps`);
  setNavBadge('nav-badge-quarantine', s.quarantined, false, `${s.quarantined} in quarantine`);
  statsShown = true;
  loadPictureIndex();
  return s;
}

// ---------- browse (inside Scan) ----------
let allPlugins = [];
let sortState = { key: 'name', dir: 1 };

// Checkbox-dropdown multi-select: closed by default (shows a count), opens a
// scrollable checkbox panel on click. Built by hand rather than a native
// <select multiple> — the owner found that always-visible "slider" of options
// awkward compared to a normal combo box.
function createMultiSelect(containerId, label, onChange) {
  const el = document.getElementById(containerId);
  el.innerHTML = `
    <button type="button" class="ms-btn">${label}</button>
    <div class="ms-panel">
      <input type="text" class="ms-search" placeholder="Filter…">
      <div class="ms-options"></div>
      <div class="ms-actions"><button type="button" class="ms-clear">Clear</button></div>
    </div>
  `;
  const btn = el.querySelector('.ms-btn');
  const panel = el.querySelector('.ms-panel');
  const optionsEl = el.querySelector('.ms-options');
  const searchEl = el.querySelector('.ms-search');
  let allOptions = [];
  const selected = new Set();

  function renderOptions() {
    const q = searchEl.value.trim().toLowerCase();
    const visible = q ? allOptions.filter(o => o.label.toLowerCase().includes(q)) : allOptions;
    optionsEl.innerHTML = visible.length ? visible.map(o => `
      <label class="ms-option"><input type="checkbox" value="${o.value.replace(/"/g, '&quot;')}" ${selected.has(o.value) ? 'checked' : ''}> ${o.label}</label>
    `).join('') : `<div class="ms-empty">No matches.</div>`;
  }
  function updateButton() {
    btn.textContent = selected.size === 0 ? label : `${label}: ${selected.size} selected`;
    btn.classList.toggle('has-selection', selected.size > 0);
  }
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    const wasOpen = panel.classList.contains('open');
    document.querySelectorAll('.ms-panel.open').forEach(p => p.classList.remove('open'));
    if (!wasOpen) { panel.classList.add('open'); searchEl.value = ''; renderOptions(); searchEl.focus(); }
  });
  panel.addEventListener('click', (e) => e.stopPropagation());
  searchEl.addEventListener('input', renderOptions);
  optionsEl.addEventListener('change', (e) => {
    if (e.target.type !== 'checkbox') return;
    if (e.target.checked) selected.add(e.target.value); else selected.delete(e.target.value);
    updateButton();
    onChange();
  });
  el.querySelector('.ms-clear').addEventListener('click', () => {
    selected.clear();
    renderOptions();
    updateButton();
    onChange();
  });

  return {
    setOptions(opts) { allOptions = opts; renderOptions(); },
    getSelected: () => [...selected],
  };
}
document.addEventListener('click', () => document.querySelectorAll('.ms-panel.open').forEach(p => p.classList.remove('open')));

const msFilters = {};

async function initFilters() {
  const [vendors, cats, types, stats] = await Promise.all([
    api('/api/vendors'), api('/api/categories'), api('/api/types'), api('/api/stats'),
  ]);
  const statusSel = document.getElementById('f-status');
  const vsSel = document.getElementById('f-vendor-source');

  if (!statusSel.dataset.populated) {
    statusSel.dataset.populated = '1';
    ['ok', 'needs_review', 'corrupted'].forEach(s => {
      statusSel.innerHTML += `<option value="${s}">${s}</option>`;
    });
    ['moduleinfo', 'binary', 'installer', 'inferred', 'default'].forEach(v => {
      vsSel.innerHTML += `<option value="${v}">${v}</option>`;
    });
  }

  const onChange = debounce(loadBrowse, 250);
  msFilters.vendor = msFilters.vendor || createMultiSelect('f-vendor', 'All vendors', onChange);
  msFilters.format = msFilters.format || createMultiSelect('f-format', 'All formats', onChange);
  msFilters.type = msFilters.type || createMultiSelect('f-type', 'All types', onChange);
  msFilters.category = msFilters.category || createMultiSelect('f-category', 'All categories', onChange);

  // "" is the "(No vendor)" / "(No category)" filter bucket — a plugin
  // orphaned by a deleted vendor/category (see db.py:delete_vendor_reference,
  // delete_category_reference) has a blank vendor / zero tags, same as one
  // that was simply never resolved. Backed by a real, selectable option
  // instead of only showing up in the read-only "Missing vendor"/"Without
  // categories" stat tiles — see app/web.py's list_plugins() for the "" match.
  const categoryOptions = cats.sort((a, b) => a.name.localeCompare(b.name)).map(c => ({ value: c.name, label: c.name }));
  if (stats.without_categories) {
    categoryOptions.unshift({ value: '', label: `(No category) (${stats.without_categories})` });
  }
  const vendorOptions = vendors.filter(v => v.vendor).sort((a, b) => a.vendor.localeCompare(b.vendor))
    .map(v => ({ value: v.vendor, label: `${v.vendor} (${v.count})` }));
  const missingVendor = vendors.find(v => !v.vendor);
  if (missingVendor) {
    vendorOptions.unshift({ value: '', label: `(No vendor) (${missingVendor.count})` });
  }

  msFilters.format.setOptions(['CLAP', 'VST3', 'VST2', 'WAVES', 'AAX', 'FL-NATIVE'].map(f => ({ value: f, label: f })));
  msFilters.type.setOptions(types.map(t => ({ value: t.type, label: `${t.type} (${t.count})` })));
  msFilters.category.setOptions(categoryOptions);
  msFilters.vendor.setOptions(vendorOptions);
}

let browseSeq = 0; // the latest Browse request; an older answer arriving late is dropped
async function loadBrowse() {
  if (!msFilters.vendor) await initFilters();
  const q = document.getElementById('f-search').value;
  const status = document.getElementById('f-status').value;
  const vendor_source = document.getElementById('f-vendor-source').value;
  const params = new URLSearchParams({ q, status, vendor_source });
  for (const [t, st] of Object.entries(browseTags)) params.append('tag', st === 'exclude' ? '-' + t : t);
  msFilters.vendor.getSelected().forEach(v => params.append('vendor', v));
  msFilters.format.getSelected().forEach(v => params.append('format', v));
  msFilters.type.getSelected().forEach(v => params.append('plugin_type', v));
  msFilters.category.getSelected().forEach(v => params.append('category', v));
  // A search that takes a moment shows a thin running light under the filters (fx.css).
  const sticky = document.getElementById('browse-sticky');
  const seq = ++browseSeq;
  const busy = setTimeout(() => { if (seq === browseSeq) sticky.classList.add('is-loading'); }, 120);
  try {
    const rows = await api('/api/plugins?' + params.toString());
    if (seq !== browseSeq) return; // a newer search is on its way
    allPlugins = rows;
    renderBrowse(sortRows(rows));
  } finally {
    clearTimeout(busy);
    if (seq === browseSeq) sticky.classList.remove('is-loading');
  }
}

// Tag chips: any → include (only plugins with the tag) → exclude (without it) → any.
const browseTags = {};
const reloadBrowseSoon = debounce(() => loadBrowse(), 250);
document.getElementById('f-tags').addEventListener('click', (e) => {
  const b = e.target.closest('[data-tag]');
  if (!b) return;
  const t = b.dataset.tag;
  const next = { undefined: 'include', include: 'exclude', exclude: undefined }[browseTags[t]];
  if (next) browseTags[t] = next; else delete browseTags[t];
  b.dataset.state = next || '';
  b.setAttribute('aria-pressed', next ? 'true' : 'false');
  b.setAttribute('aria-label', `${b.textContent.trim()}: ${next === 'include' ? 'only these' : next === 'exclude' ? 'without these' : 'any'}`);
  reloadBrowseSoon();
});

['f-search', 'f-status', 'f-vendor-source'].forEach(id => {
  const el = document.getElementById(id);
  el.addEventListener(el.tagName === 'SELECT' ? 'change' : 'input', debounce(loadBrowse, 250));
});

function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

// ---------- sorting ----------
function sortValue(r, key) {
  if (key === 'categories') return (r.categories || []).map(c => c.name).join(', ');
  return r[key];
}

function sortRows(rows) {
  const { key, dir } = sortState;
  return [...rows].sort((a, b) => {
    let va = sortValue(a, key), vb = sortValue(b, key);
    if (va == null) va = '';
    if (vb == null) vb = '';
    if (typeof va === 'number' && typeof vb === 'number') return (va - vb) * dir;
    return String(va).localeCompare(String(vb), undefined, { numeric: true }) * dir;
  });
}

// The order is set from a column header (table) or from the Sort bar (Small images and
// Tiles have no headers); both show the same state.
function setBrowseSort(key, dir) {
  sortState = { key, dir };
  document.querySelectorAll('#browse th[data-key]').forEach(h => {
    h.classList.toggle('sorted', h.dataset.key === key);
    h.querySelector('.sort-arrow')?.remove();
    if (h.dataset.key === key) h.insertAdjacentHTML('beforeend', `<span class="sort-arrow">${dir === 1 ? '▲' : '▼'}</span>`);
  });
  const sel = document.getElementById('browse-sort-key');
  if (sel) sel.value = key;
  const dirBtn = document.getElementById('browse-sort-dir');
  if (dirBtn) {
    const label = key === 'id' ? (dir === 1 ? 'Oldest first' : 'Newest first') : (dir === 1 ? 'Ascending order' : 'Descending order');
    dirBtn.classList.toggle('desc', dir !== 1);
    dirBtn.setAttribute('aria-label', label);
    dirBtn.title = label;
  }
  renderBrowse(sortRows(allPlugins));
}
document.querySelectorAll('#browse th[data-key]').forEach(th => {
  th.addEventListener('click', () => {
    const key = th.dataset.key;
    setBrowseSort(key, sortState.key === key ? -sortState.dir : 1);
  });
});
// Scanned in is the plugin's id (numbered as a scan first finds it; scanned_at is rewritten by
// every scan): it starts with the newest, any other order with A to Z.
document.getElementById('browse-sort-key').addEventListener('change', (e) => setBrowseSort(e.target.value, e.target.value === 'id' ? -1 : 1));
document.getElementById('browse-sort-dir').addEventListener('click', () => setBrowseSort(sortState.key, -sortState.dir));

// With effects on, rows that stay glide to their new place (search, filters, sorting)
// and new ones rise in (fx.flip).
function renderBrowse(rows) {
  const tbody = document.getElementById('browse-tbody');
  fxFlip(tbody, () => fillBrowse(tbody, rows));
  document.getElementById('browse-count').textContent = `${rows.length.toLocaleString('en-US')} ${rows.length === 1 ? 'plugin' : 'plugins'}`;
}

// A row's categories; tile view shows the first three and "+N" (the rest in its tooltip).
function browseCats(r) {
  const cats = r.categories || [];
  if (!cats.length) return '<span class="none">-</span>';
  const more = cats.length > 3
    ? `<span class="tag more" title="${escHtml(cats.slice(3).map(c => c.name).join(', '))}">+${cats.length - 3}</span>` : '';
  return cats.map(c => `<span class="tag ${c.source === 'heuristic' ? 'heuristic' : ''}">${escHtml(c.name)}</span>`).join('') + more;
}

// One cell per column in all three views, so switching is CSS alone (style.css, "Browse:
// the picture column, Small images and the tiles"): Table = these cells as columns; Small
// images = a compact row like the Playground list (picture, name over vendor · categories,
// format · bitness · type, status, size, ⋯); Tiles = a card (picture on top, name and ⋯,
// vendor, format · bitness · type · size, a few categories, the status over the picture
// only when it isn't plain "ok").
function fillBrowse(tbody, rows) {
  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="9" class="empty-state">No plugins match these filters.</td></tr>`;
    return;
  }
  tbody.innerHTML = rows.map(r => `
    <tr data-id="${r.id}" data-plugin-id="${r.id}">
      <td class="b-pic">${listPictureHtml(r)}</td>
      <td class="b-name">${storeIcon(r)}<span class="b-title">${escHtml(r.name)}</span>${flBadge(r)}${flCloudBadge(r)}</td>
      <td class="b-vendor">${vendorCell(r)}</td>
      <td class="b-format"><span class="badge">${escHtml(r.format)}</span>${bitBadge(r)}</td>
      <td class="b-type">${escHtml(r.plugin_type) || '<span class="none">-</span>'}</td>
      <td class="b-cats">${browseCats(r)}</td>
      <td class="b-status">${r.status === 'ok' ? '<span class="badge ok st-ok">ok</span>' : statusBadge(r.status)}${actionStatusBadge(r.action_status)}</td>
      <td class="b-size">${fmtSize(r.size_bytes)}</td>
      <td class="row-menu-cell">${rowMenuBtn(r.id)}</td>
    </tr>
  `).join('');
  labelCells(tbody);
  tbody.querySelectorAll('tr').forEach(tr => {
    tr.addEventListener('click', (e) => {
      if (e.target.closest('.row-menu-btn')) return;
      showPluginModal(parseInt(tr.dataset.id));
    });
    tr.addEventListener('mouseenter', (e) => showTooltip(e, parseInt(tr.dataset.id)));
    tr.addEventListener('mousemove', positionTooltip);
    tr.addEventListener('mouseleave', hideTooltip);
  });
  tbody.querySelectorAll('.vendor-warn-badge').forEach(badge => {
    badge.addEventListener('mouseenter', (e) => {
      e.stopPropagation();
      showVendorWarnTooltip(e, badge.dataset.warnVendor, badge.dataset.warnStatus, badge.dataset.warnNote);
    });
    badge.addEventListener('mousemove', (e) => { e.stopPropagation(); positionTooltip(e); });
    badge.addEventListener('mouseleave', (e) => { e.stopPropagation(); hideTooltip(); });
  });
}

// ---------- hover tooltip (Spanish short description) ----------
const tooltipEl = () => document.getElementById('hover-tooltip');

function showTooltip(e, id) {
  const p = allPlugins.find(r => r.id === id);
  if (!p) return;
  const desc = p.description_es || p.description;
  tooltipEl().innerHTML = `<div class="tt-title">${p.name}</div>` +
    (desc ? `<div>${desc}</div>` : `<div class="tt-empty">Sin descripción todavía.</div>`);
  tooltipEl().style.display = 'block';
  positionTooltip(e);
}
function positionTooltip(e) {
  const tt = tooltipEl();
  const pad = 16;
  let x = e.clientX + pad, y = e.clientY + pad;
  if (x + 330 > window.innerWidth) x = e.clientX - 330 - pad;
  if (y + 100 > window.innerHeight) y = e.clientY - 100 - pad;
  tt.style.left = x + 'px';
  tt.style.top = y + 'px';
}
function hideTooltip() { tooltipEl().style.display = 'none'; }

function showVendorWarnTooltip(e, vendor, status, note) {
  const label = status === 'down' ? 'Sitio caído / no disponible' : 'Descontinuado / adquirido';
  tooltipEl().innerHTML = `<div class="tt-warn">⚠ ${vendor} — ${label}</div>` +
    (note ? `<div>${note}</div>` : `<div class="tt-empty">Sin más detalles.</div>`);
  tooltipEl().style.display = 'block';
  positionTooltip(e);
}

// ---------- plugin detail ----------
// One fixed-size sheet: a header (name, vendor, badges), tabs, a body that scrolls on its
// own and a footer. General, Categories and Links are edited in place and saved together
// with one Save (the same PATCH + categories PUT + other-vendors PUT the old edit form
// used). Files, AI research and Actions act immediately, as before.
const PM_TABS = [
  ['general', 'General'], ['categories', 'Categories'], ['links', 'Links'],
  ['files', 'Files'], ['research', 'AI research'], ['actions', 'Actions'],
];
let pmTab = 'general';   // the last tab used: reopening after Save / Link stays there
let pmDirty = false;
let pmCatSel = new Set();

async function showPluginModal(id, opts = {}) {
  if (opts.tab) pmTab = opts.tab;
  const [p, s] = await Promise.all([api('/api/plugins/' + id), settings().catch(() => ({}))]);
  if (!window._refCache) await loadReference();
  const { vendors, types } = window._refCache;
  // FL Cloud only exists for FL Studio, so it's only offered when FL Studio is installed.
  const flStudio = !!s.fl_studio_installed;
  const busy = p.action_status ? 'Resolve the pending/unresolved action first' : '';
  const pinned = p.fl_cloud ? 'FL Cloud plugin — must stay at its install path' : '';
  const q = (v) => escHtml(v || '');
  pmDirty = false;
  pmCatSel = new Set((p.categories || []).map(c => c.slug));
  const guessed = new Set((p.categories || []).filter(c => c.source === 'heuristic').map(c => c.slug));
  const linked = (p.linked || []).map(l => `<li>${escHtml(l.name)} <span class="muted">${escHtml(l.format)}${l.bitness ? ' x' + (l.bitness === 64 ? '64' : '86') : ''}</span><div class="pm-path muted">${escHtml(l.host_path)}</div></li>`).join('');
  const actionRow = (btn, title, text) => `<div class="pm-action"><div class="pm-action-text"><div class="pm-action-title">${title}</div><p class="hint">${text}</p></div>${btn}</div>`;
  // The hero: the saved picture whole, on a blurred copy of itself (pictureHtml), which also
  // tints the whole header; then the name, the key facts and the way to open it.
  const stamp = Date.now();
  const heroFacts = [
    ['Type', p.plugin_type], ['Version', p.version], ['Size', p.size_bytes ? fmtSize(p.size_bytes) : ''],
  ].filter(([, v]) => v);

  const root = document.getElementById('modal-root');
  root.innerHTML = `
    <div class="modal-backdrop" id="modal-backdrop">
      <div class="modal plugin-modal" role="dialog" aria-modal="true" aria-labelledby="pm-title">
        <header class="pm-hero"${p.has_image ? ` style="--pic-backdrop: url('${escHtml(pluginPictureUrl(p.id, 'backdrop', stamp))}')"` : ''}>
          ${pictureHtml(p, 'pm-hero-pic', stamp)}
          <div class="pm-hero-text">
            <h2 id="pm-title">${storeIcon(p)}${escHtml(p.name)}</h2>
            <div class="pm-sub">${p.vendor ? vendorCell(p) : '<span class="muted">No vendor</span>'}<span class="pm-dot">·</span>${escHtml(p.format)}${bitBadge(p)}${flBadge(p)}${flCloudBadge(p)}${p.status !== 'ok' ? ' ' + statusBadge(p.status) : ''}${p.action_status ? ' ' + actionStatusBadge(p.action_status) : ''}</div>
            ${heroFacts.length ? `<dl class="pm-hero-facts">${heroFacts.map(([k, v]) => `<div><dt>${k}</dt><dd>${q(v)}</dd></div>`).join('')}</dl>` : ''}
            <div class="pm-hero-actions" id="pm-preview"></div>
          </div>
          <button type="button" class="close-x" id="modal-close" aria-label="Close">✕</button>
        </header>
        <nav class="segmented pm-tabs" role="tablist" aria-label="Plugin sections">
          ${PM_TABS.map(([k, label]) => `<button type="button" role="tab" data-pm-tab="${k}">${label}</button>`).join('')}
        </nav>
        <div class="pm-body">

          <section class="pm-pane" data-pm-pane="general">
            ${p.status !== 'ok' || p.action_status ? `<dl class="pm-facts pm-general-facts">
              ${p.status !== 'ok' ? `<div><dt>Status</dt><dd>${statusBadge(p.status)} ${q(p.status_reason)}</dd></div>` : ''}
              ${p.action_status ? `<div><dt>Action</dt><dd>${actionStatusBadge(p.action_status)} ${q(p.action_status.reason)}</dd></div>` : ''}
            </dl>` : ''}
            <div class="edit-form pm-form">
              <div>
                <label for="ef-name">Name</label>
                <input type="text" id="ef-name" value="${q(p.name)}">
                <p class="hint">Protected from rescans and FL Studio name corrections once edited. Type the original name back to undo.</p>
              </div>
              <div>
                <label for="ef-vendor">Vendor ${p.vendor_source ? `<span class="tag">${q(p.vendor_source)}</span>` : ''}</label>
                <input type="text" id="ef-vendor" list="ef-vendor-list" value="${q(p.vendor)}">
                <datalist id="ef-vendor-list">${vendors.map(v => `<option value="${q(v.name)}">`).join('')}</datalist>
                ${p.vendor_note ? `<p class="hint">${q(p.vendor_note)}</p>` : ''}
                ${p.vendor_status_note ? `<p class="hint">${q(p.vendor_status_note)}</p>` : ''}
                <div class="fast-add-row">
                  <input type="text" id="ef-vendor-fastadd" data-pm-aux placeholder="New vendor name…">
                  <button type="button" id="ef-vendor-fastadd-btn">Add vendor</button>
                </div>
                <div id="ef-others-block"></div>
              </div>
              <div>
                <label for="ef-type">Type</label>
                <!-- Fixed types (effect, synth): one per plugin, never added or renamed. -->
                <select id="ef-type">
                  ${p.plugin_type && types.some(t => t.name === p.plugin_type) ? '' : '<option value="" selected>Not set</option>'}
                  ${types.map(t => `<option value="${q(t.name)}" title="${q(t.description)}" ${t.name === p.plugin_type ? 'selected' : ''}>${q(t.name.charAt(0).toUpperCase() + t.name.slice(1))}</option>`).join('')}
                </select>
              </div>
              <div>
                <label for="ef-description-es">Descripción (Spanish, shown on hover)</label>
                <textarea id="ef-description-es">${q(p.description_es)}</textarea>
              </div>
            </div>
          </section>

          <section class="pm-pane" data-pm-pane="categories">
            <p class="hint">Tick every category that fits. A dashed one was guessed from the name by a keyword rule; saving turns your choice into a confirmed one.</p>
            <input type="search" id="ef-cat-filter" data-pm-aux class="pm-filter" placeholder="Filter categories…" aria-label="Filter categories">
            <div class="category-picker pm-cat-picker" id="ef-categories"></div>
            <h3 class="pm-h3">New category</h3>
            <div class="fast-add-row">
              <input type="text" id="ef-cat-fastadd-name" data-pm-aux placeholder="Name…">
              <input type="text" id="ef-cat-fastadd-desc" data-pm-aux placeholder="Description…">
              <button type="button" id="ef-cat-fastadd-btn">Add category</button>
            </div>
          </section>

          <section class="pm-pane" data-pm-pane="links">
            <div class="edit-form pm-form">
              <div>
                <label>Store (the official, current product page, never a reseller)</label>
                ${linkInputsHtml([{ field: 'store_url', label: 'Store', hint: 'https://…', flag: 'no_store', value: p.store_url, none: !p.store_url && !!p.no_store }]).replace('data-field="store_url"', 'id="ef-store-url" data-field="store_url"')}
              </div>
              <div>
                <label>KVR Audio page</label>
                ${linkInputsHtml([{ field: 'kvr_url', label: 'KVR', hint: 'https://www.kvraudio.com/product/…', flag: 'no_kvr', value: p.kvr_url, none: !p.kvr_url && !!p.no_kvr }]).replace('data-field="kvr_url"', 'id="ef-kvr-url" data-field="kvr_url"')}
              </div>
              <div>
                <label>Equipboard page</label>
                ${linkInputsHtml([{ field: 'equipboard_url', label: 'Equipboard', hint: 'https://equipboard.com/items/…', flag: 'no_equipboard', value: p.equipboard_url, none: !p.equipboard_url && !!p.no_equipboard }]).replace('data-field="equipboard_url"', 'id="ef-equipboard-url" data-field="equipboard_url"')}
              </div>
            </div>
            <p class="hint">"No page" means someone looked and the site has none, so the link is settled rather than missing.
              The vendor's own website and status are shared by all its plugins: edit them in Database › Vendors.</p>
            <a class="vendor-link" href="https://www.kvraudio.com/search?searchwords=${encodeURIComponent(p.name)}" target="_blank" rel="noopener noreferrer">Search “${q(p.name)}” on KVR Audio ↗</a>
          </section>

          <section class="pm-pane" data-pm-pane="files">
            <dl class="pm-facts pm-facts-wide">
              <div><dt>Path</dt><dd><span class="pm-path">${q(p.host_path)}</span> ${pathActions(p.host_path)}</dd></div>
              <div><dt>Size</dt><dd>${fmtSize(p.size_bytes)}</dd></div>
              <div><dt>Status</dt><dd>${statusBadge(p.status)} ${q(p.status_reason)}</dd></div>
            </dl>
            <h3 class="pm-h3">Same plugin in other formats or bitness</h3>
            ${linked ? `<ul class="pm-linked">${linked}</ul>` : '<p class="hint">Not linked to any other copy.</p>'}
            <p class="hint">Linking tells VEXT two files are the same plugin when it can't pair them by name and vendor. It only changes grouping; no file moves.</p>
            <div class="fast-add-row">
              <input type="text" id="link-input" data-pm-aux list="link-list" placeholder="Type a plugin name to link…">
              <datalist id="link-list"></datalist>
              <button type="button" id="link-btn">Link</button>
              ${p.link_group ? '<button type="button" id="unlink-btn">Unlink</button>' : ''}
            </div>
          </section>

          <section class="pm-pane" data-pm-pane="research">
            <p class="hint">An AI agent looks up this plugin's vendor, type, categories, links and a short description.
              Its answer waits here for your review; nothing is applied until you press Apply.</p>
            <button type="button" id="modal-research">Research with AI</button>
            <div id="research-panel"></div>
          </section>

          <section class="pm-pane" data-pm-pane="actions">
            <p class="hint">Proposals don't touch any file: they wait in Actions › Pending until you apply them.</p>
            ${actionRow(`<button type="button" id="modal-propose-reorg" ${busy || pinned ? 'disabled' : ''} title="${q(pinned || busy)}">Propose</button>`,
              'Move to its vendor folder', `Moves it into a “${q(p.vendor || 'Unknown')}” subfolder of its format folder.`)}
            ${actionRow(`<button type="button" class="danger" id="modal-propose-quarantine" ${busy || pinned ? 'disabled' : ''} title="${q(pinned || busy)}">Propose</button>`,
              'Quarantine', 'Moves the file to quarantine, where it can be restored or deleted for good later.')}
            ${flStudio ? actionRow(`<button type="button" id="modal-fl-cloud" ${!p.fl_cloud && busy ? 'disabled' : ''}>${p.fl_cloud ? 'Unmark' : 'Mark'}</button>`,
              p.fl_cloud ? 'FL Cloud plugin' : 'Mark as FL Cloud', p.fl_cloud ? 'Pinned to its install path: never moved, deduplicated or quarantined.' : 'FL Cloud plugins must stay where they were installed. Marking keeps VEXT from ever moving it.') : ''}
            ${actionRow(`<button type="button" class="danger" id="modal-ignore" ${busy ? 'disabled' : ''} title="${q(busy)}">Ignore…</button>`,
              'Ignore', 'Removes it from the catalog and skips it on future scans. The file itself is never touched.')}
          </section>
        </div>
        <footer class="pm-foot">
          <span class="pm-dirty" id="pm-dirty" aria-live="polite"></span>
          <button type="button" id="pm-revert" disabled>Revert</button>
          <button type="button" class="primary" id="ef-save" disabled>Save changes</button>
        </footer>
      </div>
    </div>`;

  const $ = (sel) => root.querySelector(sel);
  const markDirty = (on = true) => {
    pmDirty = on;
    $('#ef-save').disabled = !on;
    $('#pm-revert').disabled = !on;
    $('#pm-dirty').textContent = on ? 'Unsaved changes' : '';
  };
  // Typing in a helper field (filters, "new … name", link search) isn't an edit.
  root.querySelectorAll('[data-pm-pane="general"], [data-pm-pane="categories"], [data-pm-pane="links"]').forEach(pane => {
    ['input', 'change'].forEach(type => pane.addEventListener(type, (e) => { if (!e.target.closest('[data-pm-aux]')) markDirty(); }));
    pane.addEventListener('click', (e) => { if (e.target.closest('.chip-x, #ef-other-add-btn')) markDirty(); });
  });

  const showTab = (k) => {
    if (!PM_TABS.some(([key]) => key === k)) k = 'general';
    pmTab = k;
    root.querySelectorAll('[data-pm-tab]').forEach(b => {
      const on = b.dataset.pmTab === k;
      b.classList.toggle('active', on);
      b.setAttribute('aria-selected', String(on));
    });
    root.querySelectorAll('[data-pm-pane]').forEach(sec => { sec.hidden = sec.dataset.pmPane !== k; });
    $('.pm-body').scrollTop = 0;
  };
  root.querySelectorAll('[data-pm-tab]').forEach(b => b.onclick = () => showTab(b.dataset.pmTab));
  showTab(pmTab);

  const requestClose = async () => {
    if (pmDirty && !(await confirmDialog('Discard the unsaved changes to this plugin?'))) return;
    closeModal();
  };
  $('#modal-close').onclick = requestClose;
  $('#modal-backdrop').onclick = (e) => { if (e.target.id === 'modal-backdrop') requestClose(); };
  $('.plugin-modal').addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !document.querySelector('#confirm-root .modal')) { e.preventDefault(); requestClose(); }
  });
  root.querySelectorAll('.vendor-warn-badge').forEach(badge => {
    badge.addEventListener('mouseenter', (e) => showVendorWarnTooltip(e, badge.dataset.warnVendor, badge.dataset.warnStatus, badge.dataset.warnNote));
    badge.addEventListener('mousemove', positionTooltip);
    badge.addEventListener('mouseleave', hideTooltip);
  });

  // Categories: a filterable picker; the selection survives filtering and re-rendering.
  const renderCats = () => {
    const f = $('#ef-cat-filter').value.trim().toLowerCase();
    const cats = window._refCache.categories.filter(c => !f || c.name.toLowerCase().includes(f) || (c.description || '').toLowerCase().includes(f));
    $('#ef-categories').innerHTML = cats.map(c => `
      <label class="${guessed.has(c.slug) ? 'heuristic' : ''}" title="${q(c.description)}">
        <input type="checkbox" value="${q(c.slug)}" ${pmCatSel.has(c.slug) ? 'checked' : ''}> ${escHtml(c.name)}
      </label>`).join('') || '<span class="muted">No category matches.</span>';
  };
  $('#ef-categories').addEventListener('change', (e) => {
    const box = e.target.closest('input[type="checkbox"]');
    if (!box) return;
    if (box.checked) pmCatSel.add(box.value); else pmCatSel.delete(box.value);
  });
  $('#ef-cat-filter').addEventListener('input', renderCats);
  renderCats();

  $('#ef-vendor-fastadd-btn').onclick = async () => {
    const name = $('#ef-vendor-fastadd').value.trim();
    if (!name) return;
    try {
      await api('/api/reference/vendors', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) });
      await loadReference();
      $('#ef-vendor').value = name;
      $('#ef-vendor-fastadd').value = '';
      markDirty();
      toast('Vendor added and selected.');
    } catch (e) { handleActionError(e); }
  };
  $('#ef-cat-fastadd-btn').onclick = async () => {
    const name = $('#ef-cat-fastadd-name').value.trim();
    const description = $('#ef-cat-fastadd-desc').value.trim();
    if (!name || !description) { toast('A category needs a name and a description.', true); return; }
    try {
      const res = await api('/api/categories', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name, description }) });
      await loadReference();
      pmCatSel.add(res.slug);
      $('#ef-cat-fastadd-name').value = '';
      $('#ef-cat-fastadd-desc').value = '';
      renderCats();
      markDirty();
      toast('Category added and ticked.');
    } catch (e) { handleActionError(e); }
  };

  $('#pm-revert').onclick = () => showPluginModal(p.id);
  $('#ef-save').onclick = async () => {
    const btn = $('#ef-save');
    btn.disabled = true;
    btn.textContent = 'Saving…';
    try {
      await api(`/api/plugins/${p.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: $('#ef-name').value.trim(),
          vendor: $('#ef-vendor').value.trim() || null,
          store_url: $('#ef-store-url').value.trim() || null,
          kvr_url: $('#ef-kvr-url').value.trim() || null,
          equipboard_url: $('#ef-equipboard-url').value.trim() || null,
          no_store: $('[data-flag="no_store"]').checked,
          no_kvr: $('[data-flag="no_kvr"]').checked,
          no_equipboard: $('[data-flag="no_equipboard"]').checked,
          plugin_type: $('#ef-type').value.trim() || null,
          description_es: $('#ef-description-es').value.trim() || null,
        }),
      });
      await saveEfOthers(p.id);
      await api(`/api/plugins/${p.id}/categories`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ slugs: [...pmCatSel] }),
      });
      toast('Saved.');
      refresh();
      showPluginModal(p.id);
    } catch (e) {
      toast(e.message, true);
      btn.disabled = false;
      btn.textContent = 'Save changes';
    }
  };

  $('#modal-propose-reorg').onclick = () => proposeAction(p.id, 'reorganize', `Move into a "${p.vendor || 'Unknown'}" subfolder`);
  $('#modal-propose-quarantine').onclick = () => proposeAction(p.id, 'quarantine', 'Manually flagged for quarantine from plugin detail');
  const flCloudBtn = $('#modal-fl-cloud');
  if (flCloudBtn) flCloudBtn.onclick = async () => {
    try {
      await api(`/api/plugins/${p.id}/fl-cloud`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled: !p.fl_cloud }) });
      toast(p.fl_cloud ? 'Unmarked FL Cloud.' : 'Marked FL Cloud — it will stay where it is installed.');
      closeModal();
      refresh();
    } catch (e) { handleActionError(e); }
  };
  $('#modal-ignore').onclick = async () => {
    if (!(await confirmDialog(
      `Ignore "${p.name}"? It'll disappear from the catalog now, and future scans will skip this exact file entirely — not cataloged, not FL-matched, not linked. The file itself is never touched. Undo any time from Configuration > Ignored, "Ignored plugins".`
    ))) return;
    try {
      await api(`/api/plugins/${p.id}/ignore`, { method: 'POST' });
      toast('Ignored — won\'t be scanned again.');
      closeModal();
      refresh();
    } catch (e) { handleActionError(e); }
  };
  // Also pressed from the ⋯ menu ("Research with AI"), so it brings its own tab forward.
  $('#modal-research').onclick = async () => {
    showTab('research');
    const btn = $('#modal-research');
    btn.disabled = true;
    btn.textContent = 'Researching… (can take a couple of minutes)';
    try {
      const agent = await pickAgent();
      if (!agent) return;
      const suggestion = await api(`/api/plugins/${p.id}/research?agent=${encodeURIComponent(agent)}`, { method: 'POST' });
      renderResearchPanel(p, suggestion);
    } catch (e) {
      handleActionError(e);
    } finally {
      if (btn.isConnected) { btn.disabled = false; btn.textContent = 'Research with AI'; }
    }
  };
  api(`/api/plugins/${p.id}/research`).then(existing => { if (existing) renderResearchPanel(p, existing); });
  loadEfOthers(p.id).catch(handleActionError);
  setupLinkControls(p);
  setupHostControls(p);
  if (opts.focus) setTimeout(() => { const el = $(opts.focus); if (el) el.focus(); }, 0);
}

// A plugin's saved picture: the PNG, or variant 'backdrop' (a tiny blurred copy the
// backend makes once; scaled up it fills what the picture's aspect ratio leaves empty).
const pluginPictureUrl = (id, variant = '', stamp = '') =>
  `${API_BASE}/api/plugins/${id}/image?${new URLSearchParams({ ...(variant ? { variant } : {}), ...(stamp ? { t: stamp } : {}) })}`;

// The picture whole (never cropped) on its blurred backdrop (.plugin-pic, style.css), or
// the logo when there's none. A picture that fails to load falls back to the logo too.
function pictureHtml(p, cls, stamp) {
  if (!p.has_image) {
    return `<div class="${cls} plugin-pic is-empty"><svg class="logo-mark" aria-hidden="true"><use href="/static/logo-mark.svg#mark"/></svg></div>`;
  }
  return `<div class="${cls} plugin-pic" style="--pic-backdrop: url('${escHtml(pluginPictureUrl(p.id, 'backdrop', stamp))}')">` +
    `<img src="${escHtml(pluginPictureUrl(p.id, '', stamp))}" alt="Picture of ${escHtml(p.name)}" ` +
    `onerror="this.parentElement.classList.add('is-empty'); this.remove()"></div>`;
}

// ---------- pictures in lists ----------
// Browse rows and tiles show the picture whole on its blurred backdrop (.plugin-pic) using the
// small 'thumb' variant, lazy-loaded. Its URLs carry the picture's stamp (image_stamp), so the
// backend lets the browser keep them and re-rendering the list on every search costs nothing.
// The backdrop is only asked for once the picture itself has loaded (the load listener below),
// so a long list never fires hundreds of requests at once. No picture: the muted VEXT mark.
// The placeholder is the clean VEXT mark (logo-mark.svg, its letters in the theme's text
// colour), muted.
const PIC_MARK = '<svg class="logo-mark" aria-hidden="true"><use href="/static/logo-mark.svg#mark"/></svg>';

function listPictureHtml(r) {
  if (!r.has_image) return `<div class="plugin-pic is-mark">${PIC_MARK}</div>`;
  const t = r.image_stamp || '';
  return `<div class="plugin-pic"><img src="${escHtml(pluginPictureUrl(r.id, 'thumb', t))}" ` +
    `data-backdrop="${escHtml(pluginPictureUrl(r.id, 'backdrop', t))}" alt="" loading="lazy" decoding="async"></div>`;
}
// load / error don't bubble, so these listen in the capture phase, for every list at once.
document.addEventListener('load', (e) => {
  const img = e.target;
  if (img instanceof HTMLImageElement && img.dataset.backdrop) {
    img.parentElement.style.setProperty('--pic-backdrop', `url("${img.dataset.backdrop}")`);
  }
}, true);
document.addEventListener('error', (e) => {
  const img = e.target;
  if (!(img instanceof HTMLImageElement) || !img.dataset.backdrop) return;
  const box = img.parentElement;
  img.remove();
  box.classList.add('is-mark');
  box.insertAdjacentHTML('beforeend', PIC_MARK);
}, true);

// The Actions and Quarantine cards stand for one plugin each (data-plugin-id), but their lists
// don't carry picture fields. In the Small images and Tiles views a card shows its plugin's
// thumbnail (a CSS ::before, so the List view requests nothing) from this index, {plugin id:
// picture stamp}; refreshed with the counts (loadStats).
// The index is also how the lists learn a picture changed (linked, picked, captured, found by a
// batch: every path refreshes it). Plugins whose stamp differs from the last index are announced
// as 'vext:pictures' {ids}; the Plug-ins list reloads here (with its filters, so "Picture" is
// applied again) and the Playground reloads its own list (js/playground.js).
let pictureIndex = null;
async function loadPictureIndex() {
  let next;
  try { next = await api('/api/pictures/index'); } catch { return; }
  const prev = pictureIndex;
  pictureIndex = next;
  paintCardPictures(document);
  if (!prev) return;
  const ids = [...new Set([...Object.keys(prev), ...Object.keys(next)])]
    .filter(id => String(prev[id] || '') !== String(next[id] || ''));
  if (!ids.length) return;
  if (activeSection === 'scan' && scanTab === 'plugins') loadBrowse().catch(() => {});
  document.dispatchEvent(new CustomEvent('vext:pictures', { detail: { ids: ids.map(Number) } }));
}
function paintCardPictures(root) {
  if (!pictureIndex || !root.querySelectorAll) return;
  root.querySelectorAll('[data-tiles] .candidate-card[data-plugin-id]').forEach(card => {
    const id = card.dataset.pluginId;
    const t = pictureIndex[id];
    const had = card.dataset.picStamp || '';
    if (String(t || '') === had) return;
    card.dataset.picStamp = t ? String(t) : '';
    card.classList.toggle('has-pic', !!t);
    if (t) {
      card.style.setProperty('--card-pic', `url("${pluginPictureUrl(id, 'thumb', t)}")`);
      card.style.setProperty('--card-backdrop', `url("${pluginPictureUrl(id, 'backdrop', t)}")`);
    } else {
      card.style.removeProperty('--card-pic');
      card.style.removeProperty('--card-backdrop');
    }
  });
}
const cardPictureWatch = new MutationObserver((records) => {
  for (const rec of records) paintCardPictures(rec.target);
});
document.querySelectorAll('[data-tiles] .card-list').forEach(list => cardPictureWatch.observe(list, { childList: true }));

// A picture shown big, over everything; a click or Escape closes it.
function showPictureLightbox(src, caption) {
  const root = document.getElementById('modal-root');
  root.innerHTML = `
    <div class="modal-backdrop pic-lightbox" id="pic-lightbox" role="dialog" aria-modal="true" aria-label="Picture">
      <figure><img src="${escHtml(src)}" alt="">${caption ? `<figcaption>${caption}</figcaption>` : ''}</figure>
      <button type="button" class="close-x" aria-label="Close">✕</button>
    </div>`;
  const close = () => { document.removeEventListener('keydown', onKey); closeModal(); };
  const onKey = (e) => { if (e.key === 'Escape') close(); };
  document.addEventListener('keydown', onKey);
  root.querySelector('#pic-lightbox').addEventListener('click', (e) => { if (!e.target.closest('a')) close(); });
}

// The way to open the plugin (and why not). Opening happens in one place only, the
// Playground: it opens the editor in VEXT's plugin frame, plays audio through it, and
// its Capture menu saves the picture shown in the hero. 64-bit VST3, VST2, CLAP and
// Waves; p.host_issue says why a plugin can't open ({code, reason, retry, at?}).
function setupHostControls(p) {
  const box = document.getElementById('pm-preview');
  const issue = p.host_issue;
  // No picture: an AI can look for one on the web (Actions › Metadata › Missing picture).
  // Pictures found online and waiting: "Choose picture…" opens the picker over the sheet.
  const waitingPics = p.picture_candidates || 0;
  const cropPic = p.has_image ? '<button type="button" class="pm-find-pic" id="pm-crop-pic" title="Keep only part of this picture (the plugin’s window, without whatever surrounds it)">Crop…</button>' : '';
  const findBtn = cropPic + (waitingPics ? `<button type="button" class="pm-find-pic primary" id="pm-choose-pic" title="Compare the pictures found online, large, and pick one">Choose picture… (${waitingPics})</button>` : '')
    + `<button type="button" class="pm-find-pic" id="pm-link-pic" title="Paste the address of an image on the internet: VEXT downloads it and shows it in the picker${p.has_image ? '; applying it replaces the current picture' : ''}">${p.has_image ? 'Replace via link…' : 'Image link…'}</button>`
    + `<button type="button" class="pm-find-pic" id="pm-search-pic" title="Opens an image search (Google Images) here in VEXT: point at an image and press Use this image${p.has_image ? '; applying one replaces the current picture' : ''}">Search images…</button>`
    + (p.has_image ? '' : '<button type="button" class="pm-find-pic" id="pm-find-pic" title="VEXT reads its store and KVR pages, and an AI looks on the web if they have none. What it finds waits for your choice (Choose picture…, or Actions › Metadata › Missing picture).">Find a picture online</button>');
  const webNote = p.picture && p.picture.source === 'web' && (p.picture.page_url || p.picture.url)
    ? `Picture from <a class="vendor-link" href="${escHtml(p.picture.page_url || p.picture.url)}" target="_blank" rel="noopener noreferrer">${escHtml(linkHost(p.picture.page_url || p.picture.url))}</a>.` : '';
  if (!p.hostable && !(issue && issue.retry)) {
    box.innerHTML = `${findBtn}<p class="pm-hero-note">Can't be opened in the Playground: ${escHtml(issue ? issue.reason : 'unknown reason')}.${webNote ? ' ' + webNote : ''}</p>`;
  } else {
    const lastTry = issue ? `${issue.at ? `Last try ${escHtml(new Date(issue.at).toLocaleString())}` : 'Last check'}: ${escHtml(issue.reason)}.` : '';
    box.innerHTML = `
      <button class="primary pm-play" id="host-playground" title="Opens the plugin in the Playground, where you can play audio through it">
        <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><path fill="currentColor" d="M5 2.9v10.2a.6.6 0 0 0 .92.5l8-5.1a.6.6 0 0 0 0-1l-8-5.1A.6.6 0 0 0 5 2.9z"/></svg>
        ${issue ? 'Try again in Playground' : 'Open in Playground'}
      </button>${findBtn}
      <p class="pm-hero-note">${lastTry || webNote
        || (p.has_image ? '' : 'No picture yet: open it, then use Capture › Set as plugin picture in its window.')}</p>`;
    document.getElementById('host-playground').onclick = () => {
      closeModal();
      navigate('playground', { pluginId: p.id });
    };
  }
  const find = document.getElementById('pm-find-pic');
  if (find) find.onclick = () => findPictureOnline(p);
  const search = document.getElementById('pm-search-pic');
  if (search) search.onclick = () => openImageSearch(p);
  const link = document.getElementById('pm-link-pic');
  if (link) link.onclick = async () => {
    const url = await promptDialog('Paste the address of an image on the internet. VEXT downloads it and shows it in the picker.', 'https://…');
    if (!url) return;
    try {
      const r = await api(`/api/plugins/${p.id}/picture/link`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url }),
      });
      openPicturePicker(p.id, { selected: r.id, onDone: () => showPluginModal(p.id) });
    } catch (e) { toast(e.message, true); }
  };
  const cropBtn = document.getElementById('pm-crop-pic');
  if (cropBtn) cropBtn.onclick = async () => {
    const box = await cropDialog(pluginPictureUrl(p.id, '', Date.now()), `Crop the picture of ${p.name}`);
    if (!box) return;
    try {
      const res = await api(`/api/plugins/${p.id}/picture/crop`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(box),
      });
      toast(`Picture cropped to ${res.width} × ${res.height}.`);
      loadPictureIndex();
      showPluginModal(p.id);
    } catch (e) { handleActionError(e); }
  };
  const choose = document.getElementById('pm-choose-pic');
  if (choose) choose.onclick = () => openPicturePicker(p.id, { onDone: () => showPluginModal(p.id) });
}

// From the plugin sheet: starts the web search for this one plugin and shows its row in
// Actions › Metadata › Missing picture, where the result will wait.
async function findPictureOnline(p) {
  const agent = await pickAgent({
    noAi: true, noAiLabel: 'No AI: search images in VEXT',
    noAiSub: 'Google Images here in VEXT: you browse and choose the image.',
    noAiToast: 'No AI is set up: showing a web image search instead. Configuration > AI sets one up.',
  });
  if (agent === null) return;
  if (agent === '') { openImageSearch(p); return; }
  closeModal();
  reviewFilter.picture = p.name;
  picShow = 'all';
  navigate('actions', { group: 'metadata', chip: 'picture' });
  startReviewBatch('image', [p.id], 'plugin', agent);
}

// In-app image search (no AI). In the desktop app: the image browser (Google Images in a native
// webview, openImageBrowser). In a plain browser tab, which can't hold a native webview: the
// fallback below, where the backend reads Bing's image results and proxies the thumbnails, 5 at a
// time. Either way choosing one downloads it like a pasted link (same safety rules) and opens the
// picker with it selected; Apply sets it (replacing the current picture, as "Replace via link" does).
function openImageSearch(p, opts = {}) {
  if (isTauri()) openImageBrowser(p, opts);
  else openWebImageSearch(p, opts);
}

// The image browser: a large modal with a search box, Back / Forward / Reload, and a native
// WebView2 (label "imgsearch", desktop/src-tauri/src/imgsearch.rs) laid over its reserved
// rectangle, showing Google Images. The webview's injected script puts "Use this image" over the
// image under the mouse and sends {image_url, page_url} as the event imgsearch://pick; here it
// goes to /picture/link and the picker opens over the modal (the webview hides meanwhile: a native
// webview covers HTML). The modal stays open to pick another; closing it destroys the webview.
// #imgsearch-root is created per opening and removed on close, so the overlay watch (body
// childList) sees it: the Playground's browser hides while it's open.
function openImageBrowser(p, opts = {}) {
  const T = window.__TAURI__;
  const invoke = (cmd, args) => T.core.invoke(cmd, args);
  if (document.getElementById('imgsearch-root')) return;
  const root = document.createElement('div');
  root.id = 'imgsearch-root';
  let query = [p.vendor, p.name, 'plugin'].filter(Boolean).join(' ');
  root.innerHTML = `
    <div class="modal-backdrop imgb-backdrop" id="imgb">
      <div class="modal imgb" role="dialog" aria-modal="true" aria-labelledby="imgb-title">
        <div class="imgb-head">
          <h2 id="imgb-title">Images for ${escHtml(p.name)}</h2>
          <button type="button" class="close-x" data-ib="close" aria-label="Close" title="Close (Esc)">✕</button>
        </div>
        <form class="imgb-bar" data-ib="form">
          <button type="button" class="imgb-nav" data-ib="back" aria-label="Back" title="Back" disabled>←</button>
          <button type="button" class="imgb-nav" data-ib="forward" aria-label="Forward" title="Forward" disabled>→</button>
          <button type="button" class="imgb-nav" data-ib="reload" aria-label="Reload" title="Reload">↻</button>
          <input type="search" id="imgb-q" value="${escHtml(query)}" aria-label="Search images for" spellcheck="false" autocomplete="off">
          <button type="submit" class="primary">Search</button>
        </form>
        <p class="hint imgb-status" id="imgb-status" role="status"></p>
        <div class="imgb-view" id="imgb-view"><span class="imgb-loading"><span class="spinner" aria-hidden="true"></span> Opening Google Images…</span></div>
      </div>
    </div>`;
  document.body.appendChild(root);
  const HINT = 'Point at an image and press <b>Use this image</b>. On Google Images VEXT takes the full-size original when the preview shows it.';
  const view = root.querySelector('#imgb-view');
  const status = root.querySelector('#imgb-status');
  const input = root.querySelector('#imgb-q');
  const backBtn = root.querySelector('[data-ib="back"]');
  const fwdBtn = root.querySelector('[data-ib="forward"]');
  const setStatus = (html) => { status.innerHTML = html; };
  setStatus(HINT);
  let open = true, created = false, sent = '', wanted = null, pumping = false, raf = 0, picking = false;
  const unlisten = [];
  // Covered: the picker, a confirmation or the context menu is open above the modal.
  const covered = () => !!(document.querySelector('#picker-root > *')
    || document.getElementById('confirm-root').childElementCount || document.querySelector('body > .cm'));
  const rect = () => {
    if (!open || covered()) return null;
    const r = view.getBoundingClientRect();
    if (r.width < 40 || r.height < 40) return null;
    return { x: Math.round(r.left), y: Math.round(r.top), width: Math.round(r.width), height: Math.round(r.height) };
  };
  const place = () => {
    cancelAnimationFrame(raf);
    raf = 0;
    if (!open) return;
    const r = rect();
    if (!r && !created && !pumping) return;
    wanted = { ...(r || { x: 0, y: 0, width: 100, height: 100 }), visible: !!r };
    if (!pumping) pump();
  };
  const schedule = () => { if (!raf) raf = requestAnimationFrame(place); };
  // One call at a time, newest wins (as the Playground's browser): a show never lands after a newer hide.
  async function pump() {
    pumping = true;
    try {
      while (wanted && open) {
        const w = wanted;
        wanted = null;
        const key = JSON.stringify(w);
        if (key === sent) continue;
        try {
          await invoke('imgsearch_place', { ...w, query });
          sent = key;
          if (w.visible) created = true;
        } catch (e) {
          sent = '';
          if (open) setStatus(`The image browser couldn't open: ${escHtml(String(e))}`);
          break;
        }
      }
    } finally { pumping = false; }
    if (!open) invoke('imgsearch_close').catch(() => {});
  }
  const onState = (s) => {
    if (!open || !s) return;
    backBtn.disabled = !s.can_go_back;
    fwdBtn.disabled = !s.can_go_forward;
    // A search typed in Google's own box shows up here too.
    try {
      const u = new URL(s.url);
      const q = /(^|\.)google\.[a-z.]+$/i.test(u.hostname) && u.pathname === '/search' ? u.searchParams.get('q') : null;
      if (q && document.activeElement !== input) { input.value = q; query = q; }
    } catch { /* about:blank */ }
  };
  const onPick = async (pick) => {
    if (!open || !pick || !pick.image_url || picking) return;
    picking = true;
    const from = pick.image_url.startsWith('data:') ? 'Google (thumbnail)' : linkHost(pick.image_url);
    setStatus(`<span class="spinner" aria-hidden="true"></span> Downloading the image from ${escHtml(from)}…`);
    try {
      const res = await api(`/api/plugins/${p.id}/picture/link`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: pick.image_url, page_url: pick.page_url || null }),
      });
      if (!open) return;
      setStatus(pick.thumbnail
        ? 'Only Google’s thumbnail was available for that one. For a bigger copy, open the result’s page and use the image there.'
        : HINT);
      const done = opts.onDone || (() => showPluginModal(p.id));
      await openPicturePicker(p.id, {
        selected: res.id,
        onDone: () => {
          // After Apply (or Discard all): the picker said which with a toast.
          if (open) setStatus('Done. Pick another image to replace it, or close this window.');
          done();
        },
      });
      // The picker root may have just been created: watch it too.
      const pr = document.getElementById('picker-root');
      if (pr && open) coverWatch.observe(pr, { childList: true });
      place();
    } catch (e) {
      if (open) { const m = String(e.message || e).trim(); setStatus(`${escHtml(m)}${/[.!?]$/.test(m) ? '' : '.'} Try another one.`); }
    } finally { picking = false; }
  };
  const close = () => {
    if (!open) return;
    open = false;
    cancelAnimationFrame(raf);
    document.removeEventListener('keydown', onKey, true);
    window.removeEventListener('resize', schedule);
    coverWatch.disconnect();
    resizeWatch.disconnect();
    unlisten.forEach(f => { try { f(); } catch { /* gone */ } });
    root.remove();
    if (!pumping) invoke('imgsearch_close').catch(() => {});
  };
  const onKey = (e) => {
    if (e.key !== 'Escape' || covered()) return;
    e.stopPropagation();
    close();
  };
  document.addEventListener('keydown', onKey, true);
  window.addEventListener('resize', schedule);
  const resizeWatch = new ResizeObserver(schedule);
  resizeWatch.observe(view);
  // The picker, a confirmation or the context menu above: the webview hides at once, and shows after.
  const coverWatch = new MutationObserver(() => { if (covered()) place(); else schedule(); });
  coverWatch.observe(document.body, { childList: true });
  coverWatch.observe(document.getElementById('confirm-root'), { childList: true });
  const pickerRoot = document.getElementById('picker-root');
  if (pickerRoot) coverWatch.observe(pickerRoot, { childList: true });
  root.querySelector('[data-ib="form"]').onsubmit = (e) => {
    e.preventDefault();
    const q = input.value.trim();
    if (!q) return;
    query = q;
    if (created) invoke('imgsearch_go', { action: 'search', query: q }).catch(err => setStatus(escHtml(String(err))));
    else schedule();
  };
  backBtn.onclick = () => invoke('imgsearch_go', { action: 'back' }).catch(() => {});
  fwdBtn.onclick = () => invoke('imgsearch_go', { action: 'forward' }).catch(() => {});
  root.querySelector('[data-ib="reload"]').onclick = () => invoke('imgsearch_go', { action: 'reload' }).catch(() => {});
  root.querySelectorAll('[data-ib="close"]').forEach(b => { b.onclick = close; });
  root.querySelector('#imgb').addEventListener('click', (e) => { if (e.target.id === 'imgb') close(); });
  if (T.event && typeof T.event.listen === 'function') {
    const keep = (f) => { if (open) unlisten.push(f); else f(); };
    T.event.listen('imgsearch://state', (e) => onState(e.payload)).then(keep);
    T.event.listen('imgsearch://pick', (e) => onPick(e.payload)).then(keep);
  }
  // After the modal's entrance (it rises into place), so the webview lands where it ends.
  setTimeout(schedule, 200);
}
// Reloading the page while the image browser was open would leave its webview behind.
if (isTauri()) window.__TAURI__.core.invoke('imgsearch_close').catch(() => { /* an older shell */ });

function openWebImageSearch(p, opts = {}) {
  let root = document.getElementById('picker-root');
  if (!root) { root = document.createElement('div'); root.id = 'picker-root'; document.body.appendChild(root); }
  const PAGE = 5;
  let query = [p.vendor, p.name, 'plugin'].filter(Boolean).join(' ');
  let offset = 0, results = [], more = false, busy = false, seq = 0;
  root.innerHTML = `
    <div class="modal-backdrop pic-picker-backdrop" id="img-search">
      <div class="modal pic-picker img-search" role="dialog" aria-modal="true" aria-labelledby="img-search-title">
        <div class="pic-picker-head">
          <h2 id="img-search-title">Search images for ${escHtml(p.name)}</h2>
          <button type="button" class="close-x" data-is="close" aria-label="Close">✕</button>
        </div>
        <form class="img-search-bar" data-is="form">
          <input type="search" id="img-search-q" value="${escHtml(query)}" aria-label="Search for" spellcheck="false">
          <button type="submit" class="primary">Search</button>
        </form>
        <p class="hint" id="img-search-status" role="status"></p>
        <div class="img-search-grid" role="list" aria-label="Images found"></div>
        <div class="actions-row">
          <button data-is="prev">Previous 5</button>
          <button data-is="next">Next 5</button>
          <span class="grow"></span>
          <button data-is="close">Close</button>
        </div>
      </div>
    </div>`;
  const grid = root.querySelector('.img-search-grid');
  const status = root.querySelector('#img-search-status');
  const prevBtn = root.querySelector('[data-is="prev"]');
  const nextBtn = root.querySelector('[data-is="next"]');
  const finish = () => { document.removeEventListener('keydown', onKey, true); root.innerHTML = ''; };
  const onKey = (e) => {
    if (e.key === 'Escape' && root.querySelector('#img-search') && !document.querySelector('#confirm-root .modal')) {
      e.stopPropagation(); finish();
    }
  };
  document.addEventListener('keydown', onKey, true);
  const thumb = (r) => `${API_BASE}/api/pictures/search/thumb?u=${encodeURIComponent(r.thumb_url)}`;
  const render = () => {
    prevBtn.disabled = busy || offset === 0;
    nextBtn.disabled = busy || !more;
    if (busy) return;
    grid.innerHTML = results.map((r, i) => `
      <div class="img-search-item" role="listitem">
        <button type="button" class="img-search-pick" data-i="${i}" title="Use this image (${escHtml(r.title || r.image_url)})">
          <img src="${escHtml(thumb(r))}" alt="${escHtml(r.title || `Result ${offset + i + 1}`)}" loading="lazy" decoding="async">
        </button>
        <span class="img-search-meta"><span class="img-search-site">${escHtml(r.site)}</span>${r.title ? `<span class="muted img-search-title">${escHtml(r.title)}</span>` : ''}</span>
      </div>`).join('');
    grid.querySelectorAll('img').forEach(img => { img.onerror = () => img.closest('.img-search-item')?.classList.add('is-broken'); });
    status.textContent = results.length
      ? `Results ${offset + 1}–${offset + results.length}. Click one to use it.` : 'No images found. Try other words.';
  };
  const load = async () => {
    const mine = ++seq;
    busy = true; render();
    grid.innerHTML = '';
    status.innerHTML = '<span class="spinner" aria-hidden="true"></span> Searching…';
    let r = null, err = null;
    try { r = await api(`/api/pictures/search?q=${encodeURIComponent(query)}&offset=${offset}&count=${PAGE}`); } catch (e) { err = e; }
    if (mine !== seq) return;
    busy = false;
    results = r ? r.results || [] : [];
    more = r ? !!r.more : false;
    render();
    if (err) status.textContent = `${err.message || err} You can also paste an image link (Image link…).`;
  };
  const choose = async (r, btn) => {
    if (busy || !r) return;
    busy = true; render(); btn.classList.add('is-loading');
    status.innerHTML = `<span class="spinner" aria-hidden="true"></span> Downloading the image from ${escHtml(linkHost(r.image_url))}…`;
    try {
      const res = await api(`/api/plugins/${p.id}/picture/link`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: r.image_url, page_url: r.page_url }),
      });
      finish();
      openPicturePicker(p.id, { selected: res.id, onDone: opts.onDone || (() => showPluginModal(p.id)) });
    } catch (e) {
      busy = false; render();
      status.textContent = `${e.message || e} Try another one.`;
    }
  };
  root.querySelector('[data-is="form"]').onsubmit = (e) => {
    e.preventDefault();
    const q = root.querySelector('#img-search-q').value.trim();
    if (q) { query = q; offset = 0; load(); }
  };
  prevBtn.onclick = () => { if (offset > 0) { offset = Math.max(0, offset - PAGE); load(); } };
  nextBtn.onclick = () => { if (more) { offset += PAGE; load(); } };
  root.querySelectorAll('[data-is="close"]').forEach(b => { b.onclick = finish; });
  root.querySelector('#img-search').onclick = (e) => { if (e.target.id === 'img-search') finish(); };
  grid.onclick = (e) => {
    const btn = e.target.closest('.img-search-pick');
    if (btn) choose(results[Number(btn.dataset.i)], btn);
  };
  load();
}

const linkHost = (url) => { try { return new URL(url).hostname.replace(/^www\./, ''); } catch { return url; } };

// Review UI for an AI research suggestion (app/codex_research.py) — never
// applied automatically; every field is editable here before "Apply" writes
// anything, same propose-then-apply model as every other action in this app.
async function renderResearchPanel(p, s) {
  const panel = document.getElementById('research-panel');
  if (!panel) return;
  if (!s) { panel.innerHTML = ''; return; }
  const researchOthers = Array.isArray(s.secondary_vendors) ? s.secondary_vendors : (() => {
    try { return JSON.parse(s.secondary_vendors || '[]'); } catch { return []; }
  })();
  if (!window._refCache) await loadReference();
  const currentSlugs = new Set(s.categories || []);
  const cats = window._refCache.categories;
  panel.innerHTML = `
    <div class="edit-form" style="margin-top:12px; border-top:1px solid var(--border); padding-top:12px;">
      <p class="hint" style="margin-top:0;">The AI's suggestion (confidence: ${s.confidence || 'unknown'}) — review/edit before applying. Nothing here has touched the plugin yet.</p>
      <div><label>Vendor</label><input type="text" id="rs-vendor" value="${(s.vendor || '').replace(/"/g, '&quot;')}"></div>
      <div><label>Vendor URL</label><input type="text" id="rs-vendor-url" value="${(s.vendor_url || '').replace(/"/g, '&quot;')}"></div>
      <div><label>Vendor status</label>
        <select id="rs-vendor-status">
          <option value="">Unknown</option>
          <option value="active" ${s.vendor_status === 'active' ? 'selected' : ''}>Active</option>
          <option value="deprecated" ${s.vendor_status === 'deprecated' ? 'selected' : ''}>Deprecated</option>
          <option value="down" ${s.vendor_status === 'down' ? 'selected' : ''}>Down</option>
        </select>
      </div>
      <div><label>Vendor status note</label><input type="text" id="rs-vendor-status-note" value="${(s.vendor_status_note || '').replace(/"/g, '&quot;')}"></div>
      <div><label>Other vendors involved (comma-separated, up to 5)</label><input type="text" id="rs-others" value="${researchOthers.join(', ').replace(/"/g, '&quot;')}"></div>
      <div><label>Type</label>
        <select id="rs-type">
          <option value="">Unknown</option>
          <option value="effect" ${s.plugin_type === 'effect' ? 'selected' : ''}>effect</option>
          <option value="synth" ${s.plugin_type === 'synth' ? 'selected' : ''}>synth</option>
        </select>
      </div>
      <div>
        <label>Categories</label>
        <div class="category-picker" id="rs-categories">
          ${cats.map(c => `
            <label title="${c.description.replace(/"/g, '&quot;')}">
              <input type="checkbox" value="${c.slug}" ${currentSlugs.has(c.slug) ? 'checked' : ''}> ${c.name}
            </label>
          `).join('')}
        </div>
      </div>
      <div><label>Descripción (Spanish)</label><textarea id="rs-description-es">${s.description_es || ''}</textarea></div>
      ${s.sources && s.sources.length ? `<div><label>Sources consulted</label><div class="v">${s.sources.map(u => `<div><a class="vendor-link" href="${u}" target="_blank" rel="noopener noreferrer">${u}</a></div>`).join('')}</div></div>` : ''}
      ${s.notes ? `<div><label>Notes from the AI</label><div class="v">${s.notes}</div></div>` : ''}
      <div style="margin-top:12px; display:flex; gap:8px;">
        <button class="primary" id="rs-apply">Apply to plugin</button>
        <button id="rs-discard">Discard suggestion</button>
      </div>
    </div>
  `;
  document.getElementById('rs-apply').onclick = async () => {
    const btn = document.getElementById('rs-apply');
    btn.disabled = true;
    btn.textContent = 'Applying…';
    try {
      await api(`/api/plugins/${p.id}/research/apply`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          vendor: document.getElementById('rs-vendor').value.trim() || null,
          vendor_url: document.getElementById('rs-vendor-url').value.trim() || null,
          vendor_status: document.getElementById('rs-vendor-status').value || null,
          vendor_status_note: document.getElementById('rs-vendor-status-note').value.trim() || null,
          plugin_type: document.getElementById('rs-type').value || null,
          secondary_vendors: document.getElementById('rs-others').value.split(',').map(x => x.trim()).filter(Boolean),
          categories: [...document.querySelectorAll('#rs-categories input:checked')].map(i => i.value),
          description_es: document.getElementById('rs-description-es').value.trim() || null,
        }),
      });
      toast('Applied.');
      refresh();
      showPluginModal(p.id);
    } catch (e) {
      handleActionError(e);
      btn.disabled = false;
      btn.textContent = 'Apply to plugin';
    }
  };
  document.getElementById('rs-discard').onclick = async () => {
    try {
      await api(`/api/plugins/${p.id}/research`, { method: 'DELETE' });
      toast('Discarded.');
      panel.innerHTML = '';
    } catch (e) { handleActionError(e); }
  };
}

// Manual "same plugin" link (see app/dedup.py): for twins the automatic name+vendor
// matcher can't pair. Only affects grouping — nothing on disk changes.
async function setupLinkControls(p) {
  const all = await api('/api/plugins');
  const rows = (all.plugins || all).filter(x => x.id !== p.id);
  const label = (x) => `${x.name} — ${x.format}${x.bitness ? ' x' + (x.bitness === 64 ? '64' : '86') : ''} (#${x.id})`;
  const byLabel = new Map(rows.map(x => [label(x), x]));
  document.getElementById('link-list').innerHTML = rows.map(x => `<option value="${label(x).replace(/"/g, '&quot;')}">`).join('');
  document.getElementById('link-btn').onclick = async () => {
    const hit = byLabel.get(document.getElementById('link-input').value);
    if (!hit) { toast('Pick a plugin from the suggestions list.', true); return; }
    await api(`/api/plugins/${p.id}/link`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ other_id: hit.id }) });
    refresh();
    showPluginModal(p.id);
  };
  const un = document.getElementById('unlink-btn');
  if (un) un.onclick = async () => {
    await api(`/api/plugins/${p.id}/link`, { method: 'DELETE' });
    refresh();
    showPluginModal(p.id);
  };
}

// ---------- plugin edit form ----------
// Editing lives in the detail sheet itself; this opens it on the General tab.
function showPluginEditForm(p) {
  return showPluginModal(p.id, { tab: 'general', focus: '#ef-name' });
}
function closeModal() { document.getElementById('modal-root').innerHTML = ''; }

async function proposeAction(plugin_id, action, reason) {
  try {
    await api('/api/actions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ plugin_id, action, reason }),
    });
    toast('Action proposed — review and apply it from Actions > Pending.');
    closeModal();
    refresh();
  } catch (e) { handleActionError(e); }
}

// A list's "Propose …" button: the card glows once when its list comes back with the
// proposal on it (fx.mark, with effects on).
function proposeMarked(b, action) {
  const id = parseInt(b.dataset.id);
  const f = fx();
  if (f) f.mark(id);
  return proposeAction(id, action, b.dataset.reason);
}

// ---------- cleanup (Actions > Cleanup: duplicates) ----------
// Bulk propose: one confirm naming the count, then fires the individual propose
// calls sequentially (so the duplicate-action check on the server stays correct)
// and reports one summary toast instead of N. Shared by every "propose all
// listed" button regardless of action type (quarantine, reorganize, ...).
async function bulkPropose(targets, action) {
  if (!targets.length) { toast('Nothing to propose — everything listed already has an action.'); return; }
  if (!(await confirmDialog(`Propose ${action} for ${targets.length} plugin(s)? Nothing is applied yet — you still review before Apply.`))) return;
  let ok = 0, fail = 0;
  for (const t of targets) {
    try {
      await api('/api/actions', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ plugin_id: t.id, action, reason: t.reason }),
      });
      ok++;
    } catch (e) { fail++; }
  }
  toast(`Proposed ${ok}${fail ? `, ${fail} failed (likely already had an action)` : ''}.`);
  refresh();
}

async function loadCleanup() {
  const rows = await api('/api/dedup');
  const list = document.getElementById('cleanup-list');
  if (!rows.length) { fxFlip(list, () => { list.innerHTML = `<div class="empty-state">No dedup candidates — nothing superseded right now.</div>`; }); return; }
  fxFlip(list, () => { list.innerHTML = rows.map(c => `
    <div class="candidate-card" data-plugin-id="${c.superseded.id}">
      <div class="info">
        <strong>${c.superseded.name}</strong> — ${c.reason} ${actionStatusBadge(c.superseded.action_status)}
        <div class="path">${c.superseded.host_path} ${pathActions(c.superseded.host_path)}</div>
        <div class="path">kept: ${c.keeper.host_path}</div>
      </div>
      <div class="actions-inline">
        <button class="danger" data-id="${c.superseded.id}" data-reason="${c.reason.replace(/"/g, '&quot;')}" ${c.superseded.action_status ? 'disabled' : ''}>
          ${c.superseded.action_status ? 'Already proposed' : 'Propose quarantine'}
        </button>
        ${rowMenuBtn(c.superseded.id)}
      </div>
    </div>
  `).join(''); });
  list.querySelectorAll('.actions-inline button[data-id]:not([disabled])').forEach(b => b.onclick = () => proposeMarked(b, 'quarantine'));
  document.getElementById('cleanup-propose-all').onclick = () => bulkPropose(
    rows.filter(c => !c.superseded.action_status).map(c => ({ id: c.superseded.id, reason: c.reason })), 'quarantine'
  );
}

// ---------- organize (Actions > Organize: vendor-subfolder gaps) ----------
async function loadOrganize() {
  const rows = await api('/api/unorganized');
  const list = document.getElementById('organize-list');
  if (!rows.length) { fxFlip(list, () => { list.innerHTML = `<div class="empty-state">Everything's already organized into a vendor folder.</div>`; }); return; }
  fxFlip(list, () => { list.innerHTML = rows.map(r => `
    <div class="candidate-card" data-plugin-id="${r.id}">
      <div class="info">
        <strong>${r.name}</strong> — ${r.vendor} ${actionStatusBadge(r.action_status)}
        <div class="path">${r.host_path} ${pathActions(r.host_path)}</div>
      </div>
      <div class="actions-inline">
        <button data-id="${r.id}" data-reason="Loose in the format root, not in its own vendor subfolder" ${r.action_status ? 'disabled' : ''}>
          ${r.action_status ? 'Already proposed' : `Propose: move into "${r.vendor}\\"`}
        </button>
        ${rowMenuBtn(r.id)}
      </div>
    </div>
  `).join(''); });
  list.querySelectorAll('.actions-inline button[data-id]:not([disabled])').forEach(b => b.onclick = () => proposeMarked(b, 'reorganize'));
  document.getElementById('organize-propose-all').onclick = () => bulkPropose(
    rows.filter(r => !r.action_status).map(r => ({ id: r.id, reason: 'Loose in the format root, not in its own vendor subfolder' })), 'reorganize'
  );
}

// ---------- flagged (Actions > Cleanup) ----------
async function loadFlagged() {
  const rows = await api('/api/flagged');
  const list = document.getElementById('flagged-list');
  if (!rows.length) { fxFlip(list, () => { list.innerHTML = `<div class="empty-state">Nothing flagged.</div>`; }); return; }
  fxFlip(list, () => { list.innerHTML = rows.map(r => `
    <div class="candidate-card" data-plugin-id="${r.id}">
      <div class="info">
        <strong>${r.name}</strong> ${statusBadge(r.status)}${flCloudBadge(r)}${actionStatusBadge(r.action_status)}
        <div class="path">${r.host_path} ${pathActions(r.host_path)}</div>
        <div class="path">${r.status_reason || ''}</div>
      </div>
      <div class="actions-inline">
        <button class="danger" data-id="${r.id}" data-reason="${(r.status_reason || r.status).replace(/"/g, '&quot;')}" ${r.action_status || r.fl_cloud ? 'disabled' : ''}>
          ${r.fl_cloud ? 'FL Cloud — pinned' : r.action_status ? 'Already proposed' : 'Propose quarantine'}
        </button>
        ${rowMenuBtn(r.id)}
      </div>
    </div>
  `).join(''); });
  list.querySelectorAll('.actions-inline button[data-id]:not([disabled])').forEach(b => b.onclick = () => proposeMarked(b, 'quarantine'));
  document.getElementById('flagged-propose-all').onclick = () => bulkPropose(
    rows.filter(r => !r.action_status && !r.fl_cloud).map(r => ({ id: r.id, reason: r.status_reason || r.status })), 'quarantine'
  );
}

// FL Cloud has no section of its own any more: a plugin is marked or unmarked from its
// details or its ⋯ menu (only while FL Studio is installed), and Scan > Browse lists the
// marked ones with the "FL Cloud: marked only" filter.

// ---------- ambiguous (Actions > Metadata) ----------
async function openPluginEditor(id) {
  const p = await api('/api/plugins/' + id);
  showPluginEditForm(p);
}

async function loadAmbiguous() {
  const rows = await api('/api/ambiguous');
  const list = document.getElementById('ambiguous-list');
  if (!rows.length) { list.innerHTML = `<div class="empty-state">Nothing ambiguous.</div>`; return; }
  list.innerHTML = rows.map(r => `
    <div class="candidate-card" data-plugin-id="${r.id}">
      <div class="info">
        <strong>${r.name}</strong>
        <div class="path">${r.host_path}</div>
        <div class="path">${r.vendor_note || ''}</div>
      </div>
      <div class="actions-inline">
        <button data-edit-id="${r.id}">✎ Fix vendor</button>
        ${rowMenuBtn(r.id)}
      </div>
    </div>
  `).join('');
  list.querySelectorAll('[data-edit-id]').forEach(b => b.onclick = () => openPluginEditor(parseInt(b.dataset.editId)));
}

// ---------- pending actions (Actions > Pending) ----------
// Only pending (proposed-but-not-applied) actions are ever shown here — once
// an action resolves (applied+restored, applied+deleted-forever, or a one-shot
// reorganize) its action_log row is dropped server-side rather than kept as
// browsable history; the audit trail for that lives in the server's log file
// only (see AGENTS.md).
async function loadActions() {
  const pending = await api('/api/actions?applied=false');
  const pendingList = document.getElementById('actions-pending-list');

  fxFlip(pendingList, () => { pendingList.innerHTML = pending.length ? pending.map(a => `
    <div class="candidate-card" data-plugin-id="${a.plugin_id}">
      <div class="info">
        <strong>${a.plugin_name}</strong> — <span class="badge">${a.action}</span>
        ${a.source_missing ? '<span class="badge danger">source no longer exists</span>' : ''}
        <div class="path">${a.host_path} ${pathActions(a.host_path)}</div>
        <div class="path">${a.source_missing ? 'File appears to have been deleted or moved outside the app — Apply would just fail. Reject this to clean it up.' : a.reason}</div>
      </div>
      <div class="actions-inline">
        <button class="primary" data-id="${a.id}" data-do="apply" ${a.source_missing ? 'disabled title="Source file is gone"' : ''}>Apply now</button>
        <button data-id="${a.id}" data-do="reject">Reject</button>
        ${rowMenuBtn(a.plugin_id)}
      </div>
    </div>
  `).join('') : `<div class="empty-state">No pending actions.</div>`; });

  pendingList.querySelectorAll('.actions-inline button[data-do]').forEach(b => {
    b.onclick = async () => {
      const id = parseInt(b.dataset.id);
      const originalText = b.textContent;
      try {
        if (b.dataset.do === 'apply') {
          if (!(await confirmDialog('Apply this action now? This will actually move the file.'))) return;
          b.disabled = true;
          b.textContent = 'Applying…';
          await api(`/api/actions/${id}/apply`, { method: 'POST' });
          toast('Action applied.');
        } else {
          await api(`/api/actions/${id}`, { method: 'DELETE' });
          toast('Action rejected.');
        }
        // The card leaves first; the list then reloads around the gap.
        await fxLeave(b.closest('.candidate-card'));
        loadActions();
        loadStats();
      } catch (e) {
        handleActionError(e);
        b.disabled = false;
        b.textContent = originalText;
      }
    };
  });

  document.getElementById('actions-apply-all').onclick = async () => {
    const targets = pending.filter(a => !a.source_missing);
    if (!targets.length) { toast('Nothing applicable to apply.'); return; }
    if (!(await confirmDialog(`Apply ${targets.length} action(s) now? This will actually move ${targets.length} file(s) — each one individually, so a failure on one doesn't stop the rest.`))) return;
    let ok = 0, fail = 0;
    for (const a of targets) {
      try {
        await api(`/api/actions/${a.id}/apply`, { method: 'POST' });
        ok++;
      } catch (e) { fail++; }
    }
    toast(`Applied ${ok}${fail ? `, ${fail} failed — check the item(s) individually` : ''}.`);
    loadStats();
    loadActions();
  };
}

// ---------- quarantine ----------
async function loadQuarantine() {
  const rows = await api('/api/quarantine');
  const list = document.getElementById('quarantine-list');

  document.getElementById('quarantine-delete-all').onclick = async () => {
    if (!rows.length) { toast('Quarantine is already empty.'); return; }
    if (!(await confirmDialog(`Permanently delete all ${rows.length} quarantined item(s)? This cannot be undone. Are you sure?`))) return;
    let ok = 0, fail = 0;
    for (const a of rows) {
      try {
        await api(`/api/quarantine/${a.id}/delete-forever`, { method: 'POST' });
        ok++;
      } catch (e) { fail++; }
    }
    toast(`Deleted ${ok}${fail ? `, ${fail} failed — check the item(s) individually` : ''} forever.`);
    loadQuarantine();
    loadStats();
  };

  if (!rows.length) { fxFlip(list, () => { list.innerHTML = `<div class="empty-state">Quarantine is empty.</div>`; }); return; }
  fxFlip(list, () => { list.innerHTML = rows.map(a => `
    <div class="candidate-card" data-plugin-id="${a.plugin_id}">
      <div class="info">
        <strong>${a.plugin_name}</strong> (${a.format}, ${a.vendor || 'unknown vendor'}) — ${fmtSize(a.size_bytes)}
        <div class="path">${a.host_path}</div>
        <div class="path">Quarantined ${new Date(a.ts).toLocaleString()} — ${a.reason}</div>
      </div>
      <div class="actions-inline">
        <button class="primary" data-id="${a.id}" data-name="${a.plugin_name.replace(/"/g, '&quot;')}" data-do="restore">Restore</button>
        <button class="danger" data-id="${a.id}" data-name="${a.plugin_name.replace(/"/g, '&quot;')}" data-do="delete-forever">Delete forever</button>
        ${rowMenuBtn(a.plugin_id)}
      </div>
    </div>
  `).join(''); });
  list.querySelectorAll('.actions-inline button[data-do]').forEach(b => {
    b.onclick = async () => {
      const id = parseInt(b.dataset.id);
      const msg = b.dataset.do === 'restore'
        ? `Restore "${b.dataset.name}" to its original location?`
        : `Permanently delete "${b.dataset.name}"? This cannot be undone. Are you sure?`;
      if (!(await confirmDialog(msg))) return;
      try {
        const res = await api(`/api/quarantine/${id}/${b.dataset.do}`, { method: 'POST' });
        toast(b.dataset.do === 'restore' ? 'Restored.' : 'Permanently deleted.');
        await fxLeave(b.closest('.candidate-card'));
        loadQuarantine();
        loadStats();
        // Real owner-reported case: after deleting a plugin this way, the
        // next installer run still detected it as "already installed" —
        // that's a leftover Windows "Programs and Features" entry, unrelated
        // to the file we just removed. Surface any that look related so it
        // can be cleaned up right here instead of resurfacing at install time.
        if (res && res.registry_hits && res.registry_hits.length) {
          showRegistryCleanupModal(res.registry_hits, b.dataset.name);
        }
      } catch (e) { handleActionError(e); }
    };
  });
}

// See app/registry_scan.py: Windows "Programs and Features" entries are the
// installer's own bookkeeping of what it registered, independent of whether
// the plugin's file is still on disk — deleting the file through this app
// doesn't touch them, so a later installer run can still report the format
// as "already installed". Reviewed and removed one at a time, same
// propose-then-apply model as everything else here; deleting an entry only
// removes that registry bookkeeping, never any files.
function showRegistryCleanupModal(hits, contextLabel) {
  const root = document.getElementById('modal-root');
  function render() {
    root.innerHTML = `
      <div class="modal-backdrop" id="modal-backdrop">
        <div class="modal">
          <button class="close-x" id="modal-close">✕</button>
          <h2>Registry cleanup${contextLabel ? ' — ' + contextLabel : ''}</h2>
          <p class="hint">Windows "Programs and Features" entries that look related by vendor/name. These
            only track what an installer registered — removing one does not touch any files, and does not
            undo whatever the entry's own vendor/version info might still prove useful for.</p>
          <div class="card-list" id="reg-list" style="max-height:400px; overflow-y:auto;">
            ${hits.length ? hits.map((h, i) => `
              <div class="candidate-card">
                <div class="info">
                  <strong>${h.display_name}</strong> ${h.display_version ? `(${h.display_version})` : ''}
                  <div class="path">${h.publisher || 'unknown publisher'} — matched by ${h.match_reason}</div>
                  ${h.install_location ? `<div class="path">${h.install_location}${h.install_location_exists === false ? ' — missing on disk' : h.install_location_exists === true ? ' — still exists on disk' : ''}</div>` : ''}
                  <div class="path">${h.hive}\\${h.key_path}</div>
                </div>
                <div class="actions-inline">
                  <button class="danger" data-idx="${i}" data-do="delete-reg">Remove entry</button>
                </div>
              </div>
            `).join('') : `<div class="empty-state">No related registry entries found.</div>`}
          </div>
          <div style="margin-top:16px;">
            <button id="reg-close">Close</button>
          </div>
        </div>
      </div>
    `;
    document.getElementById('modal-close').onclick = closeModal;
    document.getElementById('reg-close').onclick = closeModal;
    document.getElementById('modal-backdrop').onclick = (e) => { if (e.target.id === 'modal-backdrop') closeModal(); };
    root.querySelectorAll('button[data-do="delete-reg"]').forEach(b => {
      b.onclick = async () => {
        const h = hits[parseInt(b.dataset.idx)];
        if (!(await confirmDialog(`Remove this registry entry?\n\n${h.display_name}\n${h.hive}\\${h.key_path}\n\nThis only deletes the "Programs and Features" bookkeeping entry — no files are touched.`))) return;
        try {
          await api('/api/registry-scan/delete', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ hive: h.hive, key_path: h.key_path }),
          });
          hits.splice(hits.indexOf(h), 1);
          toast('Removed.');
          render();
        } catch (e) { handleActionError(e); }
      };
    });
  }
  render();
}

// ---------- sync (Scan section) ----------
// Lighter than a full rescan: just reconciles quarantine's and out-of-scope's
// registries against what's actually on disk (catches a tracked file the
// owner deleted directly, e.g. in Explorer) and refreshes the counts and the section on screen so
// nothing stale lingers.
document.getElementById('sync-btn').addEventListener('click', async () => {
  const btn = document.getElementById('sync-btn');
  btn.disabled = true;
  btn.textContent = 'Syncing…';
  try {
    const result = await api('/api/sync', { method: 'POST' });
    const parts = [];
    if (result.removed_quarantine > 0) parts.push(`${result.removed_quarantine} quarantined item(s)`);
    if (result.removed_out_of_scope > 0) parts.push(`${result.removed_out_of_scope} out-of-scope item(s)`);
    toast(parts.length
      ? `${parts.join(' and ')} could no longer be found and were removed from tracking. See logs for details.`
      : 'Synced — everything already up to date.');
    refresh();
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Sync';
  }
});

// ---------- out of scope (Configuration > Out of scope) ----------
function parentFolder(hostPath) {
  const parts = hostPath.split('\\');
  parts.pop();
  return parts.join('\\');
}

function guessFormatFromGuess(formatGuess) {
  const g = (formatGuess || '').toUpperCase();
  if (g.startsWith('VST3')) return 'VST3';
  if (g.startsWith('VST2')) return 'VST2';
  if (g.startsWith('CLAP')) return 'CLAP';
  if (g.startsWith('AAX')) return 'AAX';
  return 'VST3';
}

function fillAddScopeForm(hostPath, formatGuess) {
  const form = document.getElementById('form-add-scope-path');
  form.host_path.value = parentFolder(hostPath);
  form.format.value = guessFormatFromGuess(formatGuess);
  form.host_path.focus();
  form.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

async function loadOutOfScope() {
  const includeIgnored = document.getElementById('outofscope-include-ignored').checked;
  const rows = await api('/api/out-of-scope?include_ignored=' + includeIgnored);
  const list = document.getElementById('outofscope-list');
  window._outOfScopeRows = rows;
  if (!rows.length) {
    list.innerHTML = `<div class="empty-state">Nothing found outside the known scan folders${includeIgnored ? '' : ' (or everything found so far is ignored — check "Include ignored" above)'}.</div>`;
  } else {
    list.innerHTML = rows.map(r => `
      <div class="candidate-card">
        <div class="info">
          <strong>${r.name}</strong> — <span class="badge">${r.format_guess || 'unknown format'}</span> ${r.ignored ? '<span class="badge muted">ignored</span>' : ''}
          <div class="path">${r.host_path} ${pathActions(r.host_path)}</div>
          <div class="path">${fmtSize(r.size_bytes)} — found ${new Date(r.found_at).toLocaleString()}</div>
        </div>
        <div class="actions-inline">
          <button data-id="${r.id}" data-path="${r.host_path.replace(/"/g, '&quot;')}" data-format="${(r.format_guess || '').replace(/"/g, '&quot;')}" data-do="scope">Add folder to scope</button>
          <button data-id="${r.id}" data-do="${r.ignored ? 'unignore' : 'ignore'}">${r.ignored ? 'Unignore' : 'Ignore'}</button>
          <button data-path="${r.host_path.replace(/"/g, '&quot;')}" data-do="ignore-folder">Ignore whole folder</button>
          <button class="danger" data-id="${r.id}" data-name="${r.name.replace(/"/g, '&quot;')}" data-do="delete">Delete Forever</button>
        </div>
      </div>
    `).join('');
  }
  list.querySelectorAll('.actions-inline button[data-do="scope"]').forEach(b => {
    b.onclick = () => fillAddScopeForm(b.dataset.path, b.dataset.format);
  });
  list.querySelectorAll('.actions-inline button[data-do="ignore-folder"]').forEach(b => {
    b.onclick = async () => {
      const folder = parentFolder(b.dataset.path);
      if (!(await confirmDialog(`Ignore the whole folder "${folder}"? Nothing currently in it, or added to it later, will show up as out-of-scope again. Manage or undo this from Configuration > Ignored, "Ignored folders".`))) return;
      try {
        await api('/api/ignored-folders', {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ host_path: folder }),
        });
        toast('Folder ignored.');
        loadOutOfScope();
      } catch (e) { handleActionError(e); }
    };
  });
  list.querySelectorAll('.actions-inline button[data-do="ignore"], .actions-inline button[data-do="unignore"]').forEach(b => {
    b.onclick = async () => {
      try {
        await api(`/api/out-of-scope/${b.dataset.id}/${b.dataset.do}`, { method: 'POST' });
        toast(b.dataset.do === 'ignore' ? 'Ignored.' : 'Unignored.');
        loadOutOfScope();
      } catch (e) { handleActionError(e); }
    };
  });
  list.querySelectorAll('.actions-inline button[data-do="delete"]').forEach(b => {
    b.onclick = async () => {
      if (!(await confirmDialog(`Permanently delete "${b.dataset.name}"? This cannot be undone (no quarantine step — it was never tracked). Are you sure?`))) return;
      try {
        await api(`/api/out-of-scope/${b.dataset.id}/delete`, { method: 'POST' });
        toast('Deleted forever.');
        loadOutOfScope();
        loadStats();
      } catch (e) { handleActionError(e); }
    };
  });

  document.getElementById('outofscope-delete-all-aax').onclick = async () => {
    const targets = rows.filter(r => !r.ignored && (r.format_guess || '').toUpperCase().startsWith('AAX'));
    if (!targets.length) { toast('No AAX entries listed right now.'); return; }
    if (!(await confirmDialog(`Permanently delete all ${targets.length} AAX item(s) listed here? This cannot be undone. Are you sure?`))) return;
    let ok = 0, fail = 0;
    for (const r of targets) {
      try { await api(`/api/out-of-scope/${r.id}/delete`, { method: 'POST' }); ok++; }
      catch (e) { fail++; }
    }
    toast(`Deleted ${ok}${fail ? `, ${fail} failed — check the item(s) individually` : ''} forever.`);
    loadOutOfScope();
    loadStats();
  };
}
document.getElementById('outofscope-include-ignored').addEventListener('change', loadOutOfScope);

// ---------- scope paths (Configuration > Out of scope: owner-promoted scan directories) ----------
// Owner-added paths only — built-in roots are managed in Configuration >
// Locations instead (see loadConfigScanRoots there), which also shows this same
// data alongside them.
async function loadScopePaths() {
  const rows = (await api('/api/scan-roots')).filter(r => r.source !== 'builtin');
  const el = document.getElementById('scope-paths-list');
  if (!rows.length) { el.innerHTML = `<p class="hint">No extra paths added yet.</p>`; return; }
  el.innerHTML = rows.map(r => `
    <div class="candidate-card">
      <div class="info"><strong>${r.host_path}</strong> — <span class="badge">${r.format}</span> ${r.enabled ? '' : '<span class="badge muted">disabled</span>'}</div>
      <div class="actions-inline"><button data-id="${r.id}">Remove</button></div>
    </div>
  `).join('');
  el.querySelectorAll('.actions-inline button').forEach(b => {
    b.onclick = async () => {
      if (!(await confirmDialog('Stop scanning this path? Plugins already cataloged from it stay until the next rescan reconciles them.'))) return;
      try {
        await api(`/api/scan-roots/${b.dataset.id}`, { method: 'DELETE' });
        toast('Removed.');
        loadScopePaths();
      } catch (e) { handleActionError(e); }
    };
  });
}
document.getElementById('form-add-scope-path').addEventListener('submit', async (e) => {
  e.preventDefault();
  const form = e.target;
  const host_path = form.host_path.value.trim();
  const format = form.format.value;
  try {
    await api('/api/scan-roots', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ host_path, format }),
    });
    toast('Added — it\'ll be included in the next scan.');
    form.reset();
    loadScopePaths();
  } catch (e) { handleActionError(e); }
});

// ---------- configuration ----------
// Each pane has its own loader (CONFIG_LOADERS, near the top).
const SCAN_FORMATS = ['VST3', 'VST2', 'CLAP', 'AAX', 'WAVES', 'FL-NATIVE'];

// ---------- DAWs (Configuration > DAWs) ----------
// FL Studio is the only DAW wired up. Empty folder = auto-detect (registry, then
// Program Files); saving a folder pins it, and the FL Studio plugin rows follow it.
// The others render the same row but stay disabled until they are supported.
// Logos are simplified inline marks, so no image files ship with the app.
const DAW_LOGOS = {
  fl: `<svg viewBox="0 0 40 40" aria-hidden="true"><rect width="40" height="40" rx="9" fill="#f58220"/><circle cx="20" cy="23" r="10.5" fill="#fff"/><circle cx="20" cy="23" r="8" fill="#f58220"/><path d="M20 23V15.5M20 23l6.3-3.6M20 23l6.3 3.6M20 23v7.5M20 23l-6.3 3.6M20 23l-6.3-3.6" stroke="#fff" stroke-width="1.4"/><path d="M20 12.5c0-3 2-5 5.5-5.5-.2 3.2-2.3 5.3-5.5 5.5z" fill="#fff"/></svg>`,
  ableton: `<svg viewBox="0 0 40 40" aria-hidden="true"><rect width="40" height="40" rx="9" fill="#1a1a1a"/><g fill="#fff"><rect x="8" y="9" width="2.6" height="22"/><rect x="13" y="9" width="2.6" height="22"/><rect x="18" y="9" width="2.6" height="22"/><rect x="23" y="9" width="2.6" height="22"/><rect x="8" y="14.7" width="24" height="2.6"/><rect x="8" y="22.7" width="24" height="2.6"/></g></svg>`,
  cubase: `<svg viewBox="0 0 40 40" aria-hidden="true"><rect width="40" height="40" rx="9" fill="#d6001c"/><path d="M27 14.5A9.5 9.5 0 1 0 27 25.5" fill="none" stroke="#fff" stroke-width="4" stroke-linecap="round"/></svg>`,
  reaper: `<svg viewBox="0 0 40 40" aria-hidden="true"><rect width="40" height="40" rx="9" fill="#2b2b2b"/><circle cx="20" cy="20" r="11" fill="none" stroke="#fff" stroke-width="2.5"/><path d="M11 20h4l2-6 4 12 2-6h6" fill="none" stroke="#fff" stroke-width="2" stroke-linejoin="round"/></svg>`,
  studioone: `<svg viewBox="0 0 40 40" aria-hidden="true"><rect width="40" height="40" rx="9" fill="#2a8ff7"/><circle cx="20" cy="20" r="10" fill="none" stroke="#fff" stroke-width="3"/><circle cx="20" cy="20" r="3.5" fill="#fff"/></svg>`,
  bitwig: `<svg viewBox="0 0 40 40" aria-hidden="true"><rect width="40" height="40" rx="9" fill="#ff6a13"/><g fill="#fff"><circle cx="10.5" cy="20" r="2.6"/><circle cx="17.5" cy="20" r="2.6"/><circle cx="24.5" cy="20" r="2.6"/><circle cx="31" cy="20" r="2.6"/></g></svg>`,
};

const FUTURE_DAWS = [
  { key: 'ableton', name: 'Ableton Live' },
  { key: 'cubase', name: 'Cubase' },
  { key: 'reaper', name: 'REAPER' },
  { key: 'studioone', name: 'Studio One' },
  { key: 'bitwig', name: 'Bitwig Studio' },
];

function dawRow({ key, name, installed, badgeText, input, status, enabled }) {
  return `
    <div class="candidate-card daw-row" data-daw="${key}">
      <div class="daw-logo">${DAW_LOGOS[key] || ''}</div>
      <div class="info">
        <strong>${name}</strong>
        <span class="badge ${installed ? 'ok' : 'muted'}">${badgeText}</span>
        <div class="fixed-path-edit">
          <input type="text" class="daw-path" ${input} ${enabled ? '' : 'disabled'} aria-label="${name} install folder">
          <button class="primary" data-daw-do="save" ${enabled ? '' : 'disabled'}>Save</button>
          <button data-daw-do="auto" ${enabled ? '' : 'disabled'}>Auto-detect</button>
        </div>
        <div class="path">${status}</div>
      </div>
    </div>`;
}

function loadDaws() {
  return Promise.all([loadFlStudioSetting(), loadFlNativeFolders(), loadConfigSettings('config-daw-settings-list', DAW_SETTINGS)]);
}

async function loadFlStudioSetting() {
  const el = document.getElementById('config-daws');
  if (!el) return;
  const s = await settings(true);
  const via = { registry: 'the Windows registry', folder: 'Program Files', default: 'the default location' }[s.fl_studio_detected_from];
  const placeholder = s.fl_studio_detected ? `Detected: ${s.fl_studio_detected}` : 'Not found. Enter the install folder.';
  const status = s.fl_studio_root
    ? 'Using the folder you set.'
    : (via ? `Auto-detected from ${via}.` : '');

  el.innerHTML = dawRow({
    key: 'fl', name: 'FL Studio', installed: s.fl_studio_installed,
    badgeText: s.fl_studio_installed ? '✓ Installed' : 'Not found',
    input: `id="fl-studio-root" value="${escHtml(s.fl_studio_root)}" placeholder="${escHtml(placeholder)}"`,
    status, enabled: true,
  });
  el.querySelector('[data-daw="fl"] [data-daw-do="save"]').onclick =
    () => saveFlStudioRoot(document.getElementById('fl-studio-root').value.trim());
  el.querySelector('[data-daw="fl"] [data-daw-do="auto"]').onclick = () => saveFlStudioRoot('');

  const future = document.getElementById('config-future-daws');
  if (future) {
    future.innerHTML = FUTURE_DAWS.map(d => dawRow({
      key: d.key, name: d.name, installed: false, badgeText: 'Not supported yet',
      input: 'placeholder="Not available yet"', status: 'Reserved for future support.', enabled: false,
    })).join('');
  }
}

// FL Studio's own stock plugin folders. Read-only here: they follow the FL Studio folder,
// so they aren't listed (or editable) under Locations either.
async function loadFlNativeFolders() {
  const el = document.getElementById('config-fl-native-folders');
  if (!el) return;
  const rows = (await api('/api/scan-roots')).filter(r => (r.location_key || '').startsWith('fl_'));
  el.innerHTML = rows.length ? rows.map(r => `
    <div class="candidate-card">
      <div class="info">
        <strong>${escHtml(r.label || r.host_path)}</strong>
        <span class="badge">${escHtml(r.format)}</span>
        ${r.enabled ? '' : '<span class="badge warn">Off</span>'}
        ${r.exists ? '' : '<span class="badge warn">Folder not found</span>'}
        <div class="path">${escHtml(r.host_path)} ${r.exists ? pathActions(r.host_path) : ''}</div>
      </div>
    </div>`).join('') : '<div class="empty-state">No FL Studio folders. Set the FL Studio folder above.</div>';
}

async function saveFlStudioRoot(value) {
  try {
    await api('/api/settings/fl_studio_root', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ value }),
    });
    toast(value ? 'FL Studio folder saved.' : 'Back to auto-detect.');
    await Promise.all([loadFlStudioSetting(), loadFlNativeFolders()]);
    // Installed or not may have changed with the folder: FL Studio Sync and FL Cloud follow it.
    applyFlStudioPresence();
  } catch (e) { handleActionError(e); }
}

// ---------- scan paths: standard locations + custom paths ----------
// Both lists use the same row layout. Standard rows carry a location_key and
// can be reset to their detected path. Custom rows can't, and can be removed.
async function loadConfigScanRoots() {
  const rows = await api('/api/scan-roots');
  // FL Studio rows follow the FL Studio folder setting above, so they're not listed twice.
  const isFl = (r) => (r.location_key || '').startsWith('fl_');
  renderFixedFolderRows(
    document.getElementById('config-format-folders'),
    rows.filter(r => r.fixed && !isFl(r)),
  );
  renderScanRootRows(
    document.getElementById('config-plugin-locations'),
    rows.filter(r => r.location_key && !r.fixed && !isFl(r)),
    'No other standard locations.',
  );
  renderScanRootRows(
    document.getElementById('config-scan-roots-list'),
    rows.filter(r => !r.location_key),
    'No custom paths yet. Add one above.',
  );
}

// Required format folders, laid out as a grid: one row per format, one column
// per bitness. The path is edited inline and saved explicitly, with no Edit
// dialog and no Remove. Reset appears only when the path differs from what was
// detected. A format with no folder for a bitness (CLAP x32) shows a dash.
const FORMAT_GRID = [
  { label: 'CLAP', key: 'clap' },
  { label: 'VST3', key: 'vst3' },
  { label: 'VST2', key: 'vst2' },
  { label: 'VST', key: 'vst' },
  { label: 'AAX', key: 'aax' },
];

function formatCell(r, bits) {
  if (!r) return `<div class="format-cell na" aria-label="Not applicable">&mdash;</div>`;
  const changed = r.source === 'manual' || r.host_path !== r.detected_path;
  return `
    <div class="format-cell" data-bits="${bits}">
      <input type="text" class="fixed-path" data-id="${r.id}" value="${escHtml(r.host_path)}" aria-label="${escHtml(r.label || 'Folder')} path">
      <div class="format-cell-actions">
        <button class="primary" data-do="save-path" data-id="${r.id}">Save</button>
        ${changed ? `<button data-do="reset" data-id="${r.id}">Reset</button>` : ''}
        <label class="switch-row"><input type="checkbox" class="switch" data-do="enabled" data-id="${r.id}" ${r.enabled ? 'checked' : ''}> <span>Scan</span></label>
        ${r.exists ? '' : '<span class="badge warn">Not found</span>'}
      </div>
      <div class="path">Detected: ${escHtml(r.detected_path) || '(not detected)'}</div>
    </div>`;
}

function renderFixedFolderRows(el, rows) {
  if (!el) return;
  if (!rows.length) {
    el.innerHTML = `<div class="empty-state">Format folders will appear after the first refresh.</div>`;
    return;
  }
  const byKey = new Map(rows.map(r => [r.location_key, r]));
  const shown = new Set();
  const gridRows = FORMAT_GRID.map(f => {
    const x32 = byKey.get(`${f.key}_x32`);
    const x64 = byKey.get(`${f.key}_x64`);
    if (x32) shown.add(x32.location_key);
    if (x64) shown.add(x64.location_key);
    return `
      <div class="format-grid-row">
        <div class="format-name">${f.label}</div>
        ${formatCell(x32, "x32")}
        ${formatCell(x64, "x64")}
      </div>`;
  });
  // Any fixed folder outside the grid still shows up, so nothing becomes unreachable.
  const extra = rows.filter(r => !shown.has(r.location_key)).map(r => `
      <div class="format-grid-row">
        <div class="format-name">${escHtml(r.label || r.format)}</div>
        ${formatCell(r, r.location_key.endsWith("_x32") ? "x32" : "x64")}
        ${formatCell(null)}
      </div>`);
  el.innerHTML = `
    <div class="format-grid">
      <div class="format-grid-head"><div></div><div>x32</div><div>x64</div></div>
      ${gridRows.concat(extra).join('')}
    </div>`;

  el.querySelectorAll('[data-do="save-path"]').forEach(b => {
    b.onclick = async () => {
      const input = el.querySelector(`input.fixed-path[data-id="${b.dataset.id}"]`);
      const value = input.value.trim();
      if (!value) { toast('Enter a folder path first.', true); return; }
      try {
        await api(`/api/scan-roots/${b.dataset.id}`, {
          method: 'PATCH', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ host_path: value }),
        });
        toast('Path saved. Takes effect on the next rescan.');
        loadConfigScanRoots();
      } catch (e) { handleActionError(e); }
    };
  });
  el.querySelectorAll('[data-do="reset"]').forEach(b => {
    b.onclick = async () => {
      try {
        await api(`/api/scan-roots/${b.dataset.id}/reset`, { method: 'POST' });
        toast('Reset to the detected path.');
        loadConfigScanRoots();
      } catch (e) { handleActionError(e); }
    };
  });
  el.querySelectorAll('[data-do="enabled"]').forEach(b => {
    b.onchange = async () => {
      try {
        await api(`/api/scan-roots/${b.dataset.id}/${b.checked ? 'enable' : 'disable'}`, { method: 'POST' });
        loadConfigScanRoots();
      } catch (e) { b.checked = !b.checked; handleActionError(e); }
    };
  });
}

function renderScanRootRows(el, rows, emptyText) {
  if (!el) return;
  if (!rows.length) {
    el.innerHTML = `<div class="empty-state">${emptyText}</div>`;
    return;
  }
  const sourceLabel = (r) => r.location_key
    ? (r.source === 'manual' ? 'Edited' : 'Detected')
    : 'Custom';
  el.innerHTML = rows.map(r => {
    const title = r.label || r.host_path;
    const canReset = r.location_key && (r.source !== 'builtin' || r.host_path !== r.detected_path);
    return `
    <div class="candidate-card">
      <div class="info">
        <strong>${title}</strong>
        <span class="badge">${r.format}</span>
        <span class="badge muted">${sourceLabel(r)}</span>
        ${r.enabled ? '' : '<span class="badge warn">Off</span>'}
        ${r.exists ? '' : '<span class="badge warn">Folder not found</span>'}
        <div class="path">${r.host_path}</div>
        <div class="actions-inline" style="margin-top:8px;">
          <label class="switch-row"><input type="checkbox" class="switch" data-do="enabled" data-id="${r.id}" ${r.enabled ? 'checked' : ''}> <span>Scan</span></label>
          <label class="switch-row"><input type="checkbox" class="switch" data-do="organizable" data-id="${r.id}" ${r.recurse_vendor_dirs ? 'checked' : ''}> <span>Organizable</span></label>
        </div>
      </div>
      <div class="actions-inline">
        <button data-do="edit" data-id="${r.id}">Edit</button>
        ${canReset ? `<button data-do="reset" data-id="${r.id}">Reset</button>` : ''}
        <button class="danger" data-do="delete" data-id="${r.id}" data-source="${r.source}" data-custom="${r.location_key ? '0' : '1'}">Remove</button>
      </div>
    </div>`;
  }).join('');

  const refresh = () => { loadConfigScanRoots(); loadConfigRemovedRoots(); };

  el.querySelectorAll('[data-do="organizable"]').forEach(b => {
    b.onchange = async () => {
      try {
        await api(`/api/scan-roots/${b.dataset.id}`, {
          method: 'PATCH', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ recurse_vendor_dirs: b.checked }),
        });
        toast('Saved. Takes effect on the next rescan.');
      } catch (e) { b.checked = !b.checked; handleActionError(e); }
    };
  });
  el.querySelectorAll('[data-do="enabled"]').forEach(b => {
    b.onchange = async () => {
      const verb = b.checked ? 'enable' : 'disable';
      try {
        await api(`/api/scan-roots/${b.dataset.id}/${verb}`, { method: 'POST' });
        toast(b.checked ? 'Will be scanned on the next rescan.' : 'Skipped on the next rescan.');
        loadConfigScanRoots();
      } catch (e) { b.checked = !b.checked; handleActionError(e); }
    };
  });
  el.querySelectorAll('[data-do="reset"]').forEach(b => {
    b.onclick = async () => {
      try {
        await api(`/api/scan-roots/${b.dataset.id}/reset`, { method: 'POST' });
        toast('Reset to the detected path.');
        refresh();
      } catch (e) { handleActionError(e); }
    };
  });
  el.querySelectorAll('[data-do="delete"]').forEach(b => {
    b.onclick = async () => {
      const msg = b.dataset.custom === '1'
        ? 'Remove this custom path? Plugins already cataloged from it stay until the next rescan reconciles them.'
        : 'Remove this standard location? Plugins already cataloged from it stay until the next rescan reconciles them. You can restore it from "Removed built-in paths" below.';
      if (!(await confirmDialog(msg))) return;
      try {
        await api(`/api/scan-roots/${b.dataset.id}`, { method: 'DELETE' });
        toast('Removed.');
        refresh();
      } catch (e) { handleActionError(e); }
    };
  });
  el.querySelectorAll('[data-do="edit"]').forEach(b => {
    b.onclick = () => showScanRootEditForm(rows.find(r => r.id === parseInt(b.dataset.id)));
  });
}

async function loadConfigRemovedRoots() {
  const rows = await api('/api/removed-scan-roots');
  const el = document.getElementById('config-removed-roots-list');
  if (!el) return;
  if (!rows.length) {
    el.innerHTML = `<p class="hint">No deleted built-in paths.</p>`;
    return;
  }
  el.innerHTML = rows.map(r => `
    <div class="candidate-card">
      <div class="info"><strong>${r.host_path}</strong></div>
      <div class="actions-inline"><button data-path="${r.host_path.replace(/"/g, '&quot;')}">Restore</button></div>
    </div>
  `).join('');
  el.querySelectorAll('button[data-path]').forEach(b => {
    b.onclick = async () => {
      try {
        await api('/api/removed-scan-roots/restore', {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ host_path: b.dataset.path }),
        });
        toast('Restored.');
        loadConfigScanRoots();
        loadConfigRemovedRoots();
      } catch (e) { handleActionError(e); }
    };
  });
}

function showScanRootEditForm(r) {
  const root = document.getElementById('modal-root');
  root.innerHTML = `
    <div class="modal-backdrop" id="modal-backdrop">
      <div class="modal">
        <button class="close-x" id="modal-close">✕</button>
        <h2>Edit scan path</h2>
        <div class="edit-form">
          <div><label>Path</label><input type="text" id="sr-host-path" value="${r.host_path.replace(/"/g, '&quot;')}"></div>
          <div><label>Format</label>
            <select id="sr-format">${SCAN_FORMATS.map(f => `<option value="${f}" ${f === r.format ? 'selected' : ''}>${f}</option>`).join('')}</select>
          </div>
          <div><label>Default type</label><input type="text" id="sr-default-type" value="${(r.default_type || '').replace(/"/g, '&quot;')}"></div>
          <div><label>Extensions (comma-separated, e.g. .vst3 — leave blank for folder-per-plugin formats like FL-NATIVE)</label>
            <input type="text" id="sr-extensions" value="${(r.extensions || '').replace(/"/g, '&quot;')}"></div>
          <div><label><input type="checkbox" id="sr-recurse" ${r.recurse_vendor_dirs ? 'checked' : ''}> Organizable (propose moving loose plugins here into a vendor subfolder)</label></div>
        </div>
        ${r.source === 'builtin' ? '<p class="hint">Editing a built-in path marks it as customized so future app updates never silently overwrite your change.</p>' : ''}
        <div style="margin-top:16px; display:flex; gap:8px;">
          <button class="primary" id="sr-save">Save changes</button>
          <button id="sr-cancel">Cancel</button>
        </div>
      </div>
    </div>
  `;
  document.getElementById('modal-close').onclick = closeModal;
  document.getElementById('sr-cancel').onclick = closeModal;
  document.getElementById('modal-backdrop').onclick = (e) => { if (e.target.id === 'modal-backdrop') closeModal(); };
  document.getElementById('sr-save').onclick = async () => {
    try {
      await api(`/api/scan-roots/${r.id}`, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          host_path: document.getElementById('sr-host-path').value.trim(),
          format: document.getElementById('sr-format').value,
          default_type: document.getElementById('sr-default-type').value.trim() || 'unknown',
          extensions: document.getElementById('sr-extensions').value.split(',').map(s => s.trim()).filter(Boolean),
          recurse_vendor_dirs: document.getElementById('sr-recurse').checked,
        }),
      });
      toast('Saved — takes effect on the next rescan.');
      closeModal();
      loadConfigScanRoots();
    } catch (e) { handleActionError(e); }
  };
}

document.getElementById('form-add-scan-root').addEventListener('submit', async (e) => {
  e.preventDefault();
  const form = e.target;
  try {
    await api('/api/scan-roots', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        host_path: form.host_path.value.trim(), format: form.format.value,
        recurse_vendor_dirs: form.recurse_vendor_dirs.checked,
      }),
    });
    toast('Added — it\'ll be included in the next scan.');
    form.reset();
    loadConfigScanRoots();
  } catch (e) { handleActionError(e); }
});

document.getElementById('form-registry-scan').addEventListener('submit', async (e) => {
  e.preventDefault();
  const form = e.target;
  const vendor = form.vendor.value.trim();
  const name = form.name.value.trim();
  if (!vendor && !name) { toast('Enter a vendor or a plugin name to search for.', true); return; }
  try {
    const params = new URLSearchParams();
    if (vendor) params.set('vendor', vendor);
    if (name) params.set('name', name);
    const hits = await api(`/api/registry-scan?${params}`);
    showRegistryCleanupModal(hits, [vendor, name].filter(Boolean).join(' / '));
  } catch (e) { handleActionError(e); }
});

async function loadConfigIgnored() {
  const [oos, folders, candidates, ignoredPlugins] = await Promise.all([
    api('/api/out-of-scope?include_ignored=true'),
    api('/api/ignored-folders'),
    api('/api/out-of-scope/consolidate-candidates'),
    api('/api/ignored-plugins'),
  ]);
  const ignoredItems = oos.filter(r => r.ignored);

  const candEl = document.getElementById('config-consolidate-candidates');
  candEl.innerHTML = candidates.length ? candidates.map(c => `
      <div class="candidate-card">
        <div class="info"><strong>${c.parent}</strong> — ${c.count} ignored item(s) here</div>
        <div class="actions-inline"><button class="primary" data-parent="${c.parent.replace(/"/g, '&quot;')}">Consolidate into one folder rule</button></div>
      </div>
    `).join('') : `<p class="hint">No folders with multiple ignored items right now.</p>`;
  candEl.querySelectorAll('button[data-parent]').forEach(b => {
    b.onclick = async () => {
      if (!(await confirmDialog(`Turn every ignored item under "${b.dataset.parent}" into one folder-level ignore rule? New files added to that folder later will also be skipped from now on, not just what's ignored today.`))) return;
      try {
        const res = await api('/api/out-of-scope/consolidate', {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ parent: b.dataset.parent }),
        });
        toast(`Consolidated ${res.consolidated} item(s) into one folder rule.`);
        loadConfigIgnored();
      } catch (e) { handleActionError(e); }
    };
  });

  const foldersEl = document.getElementById('config-ignored-folders-list');
  foldersEl.innerHTML = folders.length ? folders.map(f => `
      <div class="candidate-card">
        <div class="info"><strong>${f.host_path}</strong></div>
        <div class="actions-inline"><button data-id="${f.id}">Remove rule</button></div>
      </div>
    `).join('') : `<div class="empty-state">No ignored folders yet.</div>`;
  foldersEl.querySelectorAll('button[data-id]').forEach(b => {
    b.onclick = async () => {
      if (!(await confirmDialog('Remove this ignore rule? The full C: scan will find files under this folder again next time.'))) return;
      try { await api(`/api/ignored-folders/${b.dataset.id}`, { method: 'DELETE' }); toast('Removed.'); loadConfigIgnored(); }
      catch (e) { handleActionError(e); }
    };
  });

  const itemsEl = document.getElementById('config-ignored-items-list');
  itemsEl.innerHTML = ignoredItems.length ? ignoredItems.map(r => `
      <div class="candidate-card">
        <div class="info"><strong>${r.name}</strong><div class="path">${r.host_path}</div></div>
        <div class="actions-inline"><button data-id="${r.id}">Unignore</button></div>
      </div>
    `).join('') : `<div class="empty-state">No individually-ignored items.</div>`;
  itemsEl.querySelectorAll('button[data-id]').forEach(b => {
    b.onclick = async () => {
      try { await api(`/api/out-of-scope/${b.dataset.id}/unignore`, { method: 'POST' }); toast('Unignored.'); loadConfigIgnored(); }
      catch (e) { handleActionError(e); }
    };
  });

  const pluginsEl = document.getElementById('config-ignored-plugins-list');
  pluginsEl.innerHTML = ignoredPlugins.length ? ignoredPlugins.map(r => `
      <div class="candidate-card">
        <div class="info"><strong>${r.name}</strong><div class="path">${r.host_path}</div></div>
        <div class="actions-inline"><button data-path="${r.host_path.replace(/"/g, '&quot;')}">Unignore</button></div>
      </div>
    `).join('') : `<div class="empty-state">No ignored plugins.</div>`;
  pluginsEl.querySelectorAll('button[data-path]').forEach(b => {
    b.onclick = async () => {
      try {
        await api('/api/ignored-plugins/unignore', {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ host_path: b.dataset.path }),
        });
        toast('Unignored — will be re-cataloged on the next rescan.');
        loadConfigIgnored();
      } catch (e) { handleActionError(e); }
    };
  });
}

document.getElementById('form-add-ignored-folder').addEventListener('submit', async (e) => {
  e.preventDefault();
  const form = e.target;
  try {
    await api('/api/ignored-folders', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ host_path: form.host_path.value.trim() }),
    });
    toast('Ignored — the full C: scan will skip this folder from now on.');
    form.reset();
    loadConfigIgnored();
  } catch (e) { handleActionError(e); }
});

// Plain path settings, edited inline: General has the quarantine folder, DAWs the FL
// Studio plugin database.
const GENERAL_SETTINGS = [
  { key: 'quarantine_dir', label: 'Quarantine directory', hint: 'Where quarantined files are moved to. Can\'t be changed while anything is currently in quarantine. Empty Quarantine first.' },
];
const DAW_SETTINGS = [
  { key: 'fl_plugin_db_root', label: 'FL Studio plugin database path', hint: 'Used for the "FL" badge, FL Studio\'s own plugin names and types, plugin organization and FL Studio Sync.' },
];

async function loadConfigSettings(elId = 'config-settings-list', meta = GENERAL_SETTINGS) {
  const values = await settings();
  const el = document.getElementById(elId);
  if (!el) return;
  el.innerHTML = meta.map(s => `
    <div class="candidate-card">
      <div class="info" style="flex:1;">
        <strong>${s.label}</strong>
        <div class="path">${s.hint}</div>
        <input type="text" id="setting-${s.key}" value="${(values[s.key] || '').replace(/"/g, '&quot;')}" style="width:100%; margin-top:6px;">
      </div>
      <div class="actions-inline"><button class="primary" data-key="${s.key}">Save</button></div>
    </div>
  `).join('');
  el.querySelectorAll('button[data-key]').forEach(b => {
    b.onclick = async () => {
      const value = document.getElementById(`setting-${b.dataset.key}`).value.trim();
      try {
        await api(`/api/settings/${b.dataset.key}`, {
          method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ value }),
        });
        toast('Saved.');
      } catch (e) { handleActionError(e); }
    };
  });
}

// ---------- Scan: the scan button, last scan, and the catalog under it ----------
// The big button runs the same pipeline as the CLI (POST /api/rescan). What the last
// run reported is kept in localStorage, so the summary survives a reload; when it was
// run is read from the database (GET /api/dashboard: last_scan_at), so a scan run
// from somewhere else still shows up. The catalog (Browse) sits right under it.
let scanRunning = false;

function storedScan() {
  try { return JSON.parse(localStorage.getItem('pa-last-scan') || 'null'); } catch { return null; }
}

function timeAgo(date) {
  const s = Math.round((Date.now() - date.getTime()) / 1000);
  if (s < 60) return 'just now';
  const units = [['day', 86400], ['hour', 3600], ['minute', 60]];
  for (const [name, n] of units) {
    if (s >= n) { const v = Math.round(s / n); return `${v} ${name}${v === 1 ? '' : 's'} ago`; }
  }
  return '';
}

async function renderScanInfo() {
  if (scanRunning) return;
  const last = document.getElementById('scan-last');
  const summary = document.getElementById('scan-summary');
  const stored = storedScan();
  let at = null;
  try {
    const d = await api('/api/dashboard');
    if (d && d.last_scan_at) at = new Date(d.last_scan_at);
  } catch { /* older backend: fall back to what this window saw */ }
  if (!at && stored && stored.at) at = new Date(stored.at);
  if (scanRunning) return;
  last.textContent = at && !isNaN(at)
    ? `Last scan ${at.toLocaleString()} (${timeAgo(at)})`
    : (lastStats && lastStats.total ? 'When the last scan ran isn\'t known.' : 'No scan yet. Press Scan to catalog every plugin.');

  // The stored result belongs to that scan unless a newer one was recorded since (the
  // minute of slack covers a start-time vs finish-time stamp); otherwise just say what
  // the catalog holds now.
  const sameRun = stored && at && new Date(stored.at).getTime() >= at.getTime() - 60 * 1000;
  const parts = [];
  if (sameRun) {
    parts.push(`${stored.found} plugins found`);
    if (stored.new) parts.push(`${stored.new} new`);
    parts.push(`${stored.flagged} flagged`);
    if (stored.vendor_filled_installer) parts.push(`${stored.vendor_filled_installer} vendors from installers`);
    if (stored.vendor_filled_research) parts.push(`${stored.vendor_filled_research} vendors from research`);
    if (stored.category_heuristic_tagged) parts.push(`${stored.category_heuristic_tagged} categories guessed from names`);
    if (stored.still_missing_vendor) parts.push(`${stored.still_missing_vendor} still without a vendor`);
    if (stored.full) parts.push('full C: scan');
  } else if (lastStats) {
    parts.push(`${lastStats.total} plugins in the catalog`, `${lastStats.flagged} flagged`);
    if (lastStats.out_of_scope) parts.push(`${lastStats.out_of_scope} out of scope`);
  }
  summary.textContent = parts.join(' · ');
}

// Scan has two tabs, Scan (the hero) and Plug-ins (the catalog), like the Playground's.
// The catalog (~690 rows, about a second of layout and paint) is built only while its tab
// shows, so the hero's entrance never waits for it. The last tab used is remembered.
// navigate('scan', {tab: 'plugins'}) opens the catalog; {browse: true} is the old name for it.
const SCAN_TABS = ['scan', 'plugins'];
let scanTab = storedPick('pa-scan-tab', SCAN_TABS, 'scan');
// A scan found new plug-ins while another section showed: Scan opens on them, newest first.
let scanNewestPending = false;

function showScanTab(tab) {
  scanTab = SCAN_TABS.includes(tab) ? tab : 'scan';
  try { localStorage.setItem('pa-scan-tab', scanTab); } catch { /* storage unavailable */ }
  const panel = document.getElementById('panel-scan');
  panel.dataset.scanTab = scanTab;
  panel.querySelectorAll('.scan-tab').forEach(b => {
    const on = b.dataset.scanTab === scanTab;
    b.classList.toggle('active', on);
    b.setAttribute('aria-selected', String(on));
    b.tabIndex = on ? 0 : -1;
  });
  document.getElementById('scan-hero').hidden = scanTab !== 'scan';
  document.getElementById('browse').hidden = scanTab !== 'plugins';
  document.querySelector('main').scrollTop = 0;
  // js/fx.js (the 3D button) and js/scan-wall.js only draw while the Scan tab shows.
  document.dispatchEvent(new CustomEvent('vext:scantab', { detail: { tab: scanTab } }));
}

function loadScanSection(params = {}) {
  if (params.browse) params = { ...params, tab: 'plugins' };
  if (params.tab) showScanTab(params.tab);
  else if (!params.refresh) showScanTab(scanTab);
  renderScanInfo();
  if (scanTab !== 'plugins') {
    document.getElementById('browse-tbody').innerHTML = '';
    return;
  }
  // After a scan that found new plug-ins: newest first, so they're at the top.
  if (params.newest || scanNewestPending) {
    scanNewestPending = false;
    setBrowseSort('id', -1);
  }
  return loadBrowse();
}

document.querySelectorAll('#panel-scan .scan-tab').forEach(b => {
  b.addEventListener('click', () => {
    if (b.dataset.scanTab === 'plugins') document.getElementById('scan-tab-new').hidden = true;
    navigate('scan', { tab: b.dataset.scanTab });
  });
});
// Arrow keys move between the two tabs, like any tab list.
document.querySelector('#panel-scan .scan-tabs').addEventListener('keydown', (e) => {
  if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
  e.preventDefault();
  const next = scanTab === 'scan' ? 'plugins' : 'scan';
  navigate('scan', { tab: next });
  document.getElementById('scan-tab-' + next).focus();
});

// The Scan hero fills the first screen of the content area, with "Browse" at its foot
// (style.css: .scan-hero { min-height: var(--main-h) }; it bleeds over main's padding).
// --main-fold is the same height below main's top padding. Kept current as the window,
// the toolbar or the sidebar change the size of main.
(function trackMainFold() {
  const mainEl = document.querySelector('main');
  if (!mainEl || typeof ResizeObserver !== 'function') return;
  const set = () => {
    const top = parseFloat(getComputedStyle(mainEl).paddingTop) || 0;
    document.documentElement.style.setProperty('--main-h', mainEl.clientHeight + 'px');
    document.documentElement.style.setProperty('--main-fold', Math.max(0, mainEl.clientHeight - top) + 'px');
  };
  new ResizeObserver(set).observe(mainEl);
  set();
})();

document.getElementById('scan-browse-jump').addEventListener('click', () => navigate('scan', { tab: 'plugins' }));

document.getElementById('rescan-btn').addEventListener('click', async () => {
  const btn = document.getElementById('rescan-btn');
  const label = btn.querySelector('.scan-btn-label');
  const fullScan = document.getElementById('full-scan-checkbox').checked;
  // The visual effects (js/fx.js) follow the scan through 'vext:scan' events: start, then
  // end with ok. How long the last scan of this kind took sets how the 3D button ramps up.
  const startedAt = Date.now();
  const prev = storedScan();
  const expectedMs = prev && prev.ms && !!prev.full === fullScan ? prev.ms : 0;
  const scanEvent = (detail) => document.dispatchEvent(new CustomEvent('vext:scan', { detail }));
  let scanOk = false;
  let scanResult = null;
  scanRunning = true;
  btn.disabled = true;
  btn.classList.add('scanning');
  btn.setAttribute('aria-busy', 'true');
  label.textContent = 'Scanning…';
  document.getElementById('scan-last').textContent = fullScan
    ? 'Scanning every known folder, then the whole C: drive. This takes a while.'
    : 'Scanning every known plugin folder…';
  document.getElementById('scan-summary').textContent = '';
  scanEvent({ phase: 'start', at: startedAt, expectedMs, full: fullScan });
  try {
    const summary = await api('/api/rescan?full_scan=' + fullScan, { method: 'POST' });
    const { log, per_root, ...kept } = summary;
    try { localStorage.setItem('pa-last-scan', JSON.stringify({ ...kept, full: fullScan, at: new Date().toISOString(), ms: Date.now() - startedAt })); } catch { /* storage unavailable */ }
    toast(`Scan complete: ${summary.found} plugins${summary.new ? `, ${summary.new} new` : ''}, ${summary.flagged} flagged.`);
    scanOk = true;
    scanResult = summary;
  } catch (e) {
    toast(e.message, true);
    scanResult = { message: e.message };
  } finally {
    scanEvent({ phase: 'end', ok: scanOk, found: scanResult && scanResult.found, flagged: scanResult && scanResult.flagged, message: scanResult && scanResult.message });
    scanRunning = false;
    btn.disabled = false;
    btn.classList.remove('scanning');
    btn.removeAttribute('aria-busy');
    label.textContent = 'Scan';
    // New plug-ins found (summary.new): the Plug-ins tab shows them, newest first, with their
    // count on the tab. Right away if Scan is still open, else the next time it opens.
    const fresh = scanOk && scanResult && scanResult.new > 0 ? scanResult.new : 0;
    const badge = document.getElementById('scan-tab-new');
    badge.textContent = fresh ? `${fresh.toLocaleString('en-US')} new` : '';
    badge.hidden = !fresh;
    // Counts first, so the summary line can use them when it has nothing better. Then the
    // visible section reloads (Scan redraws its own summary), like refresh() does.
    await loadStats();
    if (fresh && activeSection === 'scan') {
      navigate('scan', { tab: 'plugins', newest: true });
    } else {
      if (fresh) {
        scanTab = 'plugins';
        scanNewestPending = true;
        try { localStorage.setItem('pa-scan-tab', scanTab); } catch { /* storage unavailable */ }
      }
      if (activeSection) loadSection(activeSection, { refresh: true });
      if (activeSection !== 'scan') renderScanInfo();
    }
  }
});

// Copies each column header onto its cells (shown as a label in tile view) and the
// full text as a tooltip when asked (list view clips long text to one line).
function labelCells(tbody, withTitles = false) {
  const heads = [...tbody.closest('table').querySelectorAll('thead th')].map(th => th.textContent.trim());
  tbody.querySelectorAll('tr').forEach(tr => [...tr.children].forEach((td, i) => {
    if (td.colSpan > 1) return;
    if (heads[i]) td.dataset.label = heads[i];
    if (withTitles && !td.title && td.textContent.trim()) td.title = td.textContent.trim().replace(/\s+/g, ' ');
  }));
}

// ---------- reference data (Database: categories / vendors / types) ----------
// The three lists are loaded whole (a few hundred rows at most); the search box, the filters and
// the order work on them in the page, like Scan's, and each tab keeps its own order.
// "Scanned in" is the row's id: rows are numbered as they're first created, so it's the order a
// scan found them in (ascending = oldest first, descending = newest first).
const REF_KINDS_LIST = ['categories', 'vendors', 'types'];
const REF_NOUNS = { categories: ['category', 'categories'], vendors: ['vendor', 'vendors'], types: ['plugin type', 'plugin types'] };
const REF_SORTS = {
  categories: [['name', 'Name'], ['slug', 'Slug'], ['description', 'Description'], ['source', 'Source'], ['id', 'Scanned in']],
  vendors: [['name', 'Name'], ['url', 'Website'], ['status', 'Status'], ['status_note', 'Note'], ['source', 'Source'], ['id', 'Scanned in']],
  types: [['name', 'Name'], ['description', 'Description'], ['source', 'Source'], ['id', 'Scanned in']],
};
const REF_SOURCES = {
  categories: [['builtin', 'Built-in'], ['manual', 'Manual']],
  vendors: [['catalog', 'Catalog'], ['manual', 'Manual']],
  types: [['builtin', 'Built-in'], ['manual', 'Manual']],
};
const REF_SEARCH_FIELDS = {
  categories: ['name', 'slug', 'description'],
  vendors: ['name', 'url', 'kvr_url', 'equipboard_url', 'status', 'status_note'],
  types: ['name', 'description'],
};
const refSort = {
  categories: { key: 'name', dir: 1 }, vendors: { key: 'name', dir: 1 }, types: { key: 'name', dir: 1 },
};
let refData = { categories: [], vendors: [], types: [] };
let refKind = 'categories';

const refSource = (kind, r) => r.source || REF_SOURCES[kind][0][0];

// The rows of one list that pass the search and the filters, in the chosen order.
function refRows(kind) {
  const q = document.getElementById('db-search').value.trim().toLowerCase();
  const source = document.getElementById('db-f-source').value;
  const status = document.getElementById('db-f-status').value;
  const links = document.getElementById('db-f-links').value;
  let rows = refData[kind];
  if (kind === refKind && source) rows = rows.filter(r => refSource(kind, r) === source);
  if (kind === 'vendors' && refKind === 'vendors') {
    if (status) rows = rows.filter(v => (v.status || 'unknown') === status);
    const missing = { 'no-url': ['url', 'no_url'], 'no-kvr': ['kvr_url', 'no_kvr'], 'no-equipboard': ['equipboard_url', 'no_equipboard'] };
    if (missing[links]) rows = rows.filter(v => !v[missing[links][0]] && !v[missing[links][1]]);
    else if (links === 'complete') rows = rows.filter(v => Object.values(missing).every(([u, no]) => v[u] || v[no]));
  }
  if (q) rows = rows.filter(r => REF_SEARCH_FIELDS[kind].some(f => String(r[f] ?? '').toLowerCase().includes(q)));
  const { key, dir } = refSort[kind];
  const value = (r) => (key === 'source' ? refSource(kind, r) : r[key]);
  return [...rows].sort((a, b) => {
    let va = value(a), vb = value(b);
    if (va == null) va = '';
    if (vb == null) vb = '';
    if (typeof va === 'number' && typeof vb === 'number') return (va - vb) * dir;
    return String(va).localeCompare(String(vb), undefined, { numeric: true }) * dir;
  });
}

// The controls follow the open list: its own sources, order options and (vendors) filters.
function syncRefControls(resetFilters) {
  const sourceSel = document.getElementById('db-f-source');
  if (resetFilters) {
    sourceSel.innerHTML = '<option value="">All sources</option>'
      + REF_SOURCES[refKind].map(([v, label]) => `<option value="${v}">${label}</option>`).join('');
    document.getElementById('db-f-status').value = '';
    document.getElementById('db-f-links').value = '';
  }
  document.querySelectorAll('#db-sticky [data-for]').forEach(el => { el.hidden = el.dataset.for !== refKind; });
  const { key, dir } = refSort[refKind];
  const keySel = document.getElementById('db-sort-key');
  keySel.innerHTML = REF_SORTS[refKind].map(([k, label]) => `<option value="${k}">${label}</option>`).join('');
  keySel.value = key;
  const dirBtn = document.getElementById('db-sort-dir');
  const label = key === 'id' ? (dir === 1 ? 'Oldest first' : 'Newest first') : (dir === 1 ? 'Ascending order' : 'Descending order');
  dirBtn.classList.toggle('desc', dir !== 1);
  dirBtn.setAttribute('aria-label', label);
  dirBtn.title = label;
  document.querySelectorAll(`.ref-pane[data-ref-pane="${refKind}"] th[data-key]`).forEach(h => {
    h.classList.toggle('sorted', h.dataset.key === key);
    h.querySelector('.sort-arrow')?.remove();
    if (h.dataset.key === key) h.insertAdjacentHTML('beforeend', `<span class="sort-arrow">${dir === 1 ? '▲' : '▼'}</span>`);
  });
}

function setRefSort(key, dir) {
  refSort[refKind] = { key, dir };
  syncRefControls(false);
  renderReference();
}

async function loadReference() {
  const [categories, vendors, types] = await Promise.all([
    api('/api/categories'), api('/api/reference/vendors'), api('/api/reference/types'),
  ]);
  refData = { categories, vendors, types };
  // cache for the plugin editor's dropdowns/fast-add, refreshed each time this loads
  window._refCache = { categories, vendors, types };
  if (!document.getElementById('db-f-source').childElementCount) syncRefControls(true);
  renderReference();
}

function renderReference() {
  const cats = refRows('categories');
  const vendors = refRows('vendors');
  const types = refRows('types');
  const empty = (kind, cols) => {
    const filtered = refData[kind].length > 0;
    return `<tr><td colspan="${cols}" class="empty-state">${filtered ? 'No matches.' : 'None yet.'}</td></tr>`;
  };
  const attr = (s) => escHtml(s).replace(/"/g, '&quot;');

  document.getElementById('ref-categories-tbody').innerHTML = cats.map(c => `
    <tr>
      <td>${escHtml(c.name)}</td><td><code>${escHtml(c.slug)}</code></td><td>${escHtml(c.description)}</td>
      <td><span class="badge ${c.source === 'manual' ? '' : 'muted'}">${escHtml(refSource('categories', c))}</span></td>
      <td class="actions-inline ref-actions">
        <button type="button" data-edit-category="${attr(c.slug)}">Edit</button>
        <button type="button" class="danger" data-delete-category="${attr(c.slug)}">Delete</button>
      </td>
    </tr>
  `).join('') || empty('categories', 5);

  document.getElementById('ref-vendors-tbody').innerHTML = vendors.map(v => `
    <tr>
      <td>${escHtml(v.name)}</td>
      <td>${v.url ? `<a class="vendor-link" href="${attr(v.url)}" target="_blank" rel="noopener noreferrer">${escHtml(v.url)}</a>` : (v.no_url ? '<span class="tag link-none">No website</span>' : '-')}</td>
      <td>${[['kvr_url', 'KVR', 'no_kvr'], ['equipboard_url', 'Equipboard', 'no_equipboard']].map(([f, label, flag]) => v[f] ? `<a class="tag link-chip vendor-link" href="${attr(v[f])}" target="_blank" rel="noopener noreferrer">${label}</a>` : (v[flag] ? `<span class="tag link-none">No ${label}</span>` : `<span class="tag link-missing">${label} ?</span>`)).join('')}</td>
      <td>${escHtml(v.status) || '-'}</td>
      <td>${escHtml(v.status_note) || '-'}</td>
      <td><span class="badge ${v.source === 'manual' ? '' : 'muted'}">${escHtml(refSource('vendors', v))}</span></td>
      <td class="actions-inline ref-actions">
        <button type="button" data-edit-vendor="${attr(v.name)}">Edit</button>
        <button type="button" class="primary" data-assign-vendor="${attr(v.name)}" title="Assign this vendor to plugin(s)">Assign</button>
        <button type="button" class="danger" data-delete-vendor="${attr(v.name)}">Delete</button>
      </td>
    </tr>
  `).join('') || empty('vendors', 7);

  document.getElementById('ref-types-tbody').innerHTML = types.map(t => `
    <tr>
      <td>${escHtml(t.name)}</td><td>${escHtml(t.description) || '-'}</td>
      <td><span class="badge ${t.source === 'manual' ? '' : 'muted'}">${escHtml(refSource('types', t))}</span></td>
    </tr>
  `).join('') || empty('types', 3);

  REF_KINDS_LIST.forEach(k => labelCells(document.getElementById(`ref-${k}-tbody`), true));

  const shown = { categories: cats, vendors, types }[refKind].length;
  const total = refData[refKind].length;
  const noun = REF_NOUNS[refKind][total === 1 ? 0 : 1];
  document.getElementById('db-count').textContent = shown === total
    ? `${total.toLocaleString('en-US')} ${noun}`
    : `${shown.toLocaleString('en-US')} of ${total.toLocaleString('en-US')} ${noun}`;
}

// The buttons in the rows, one listener per table (the rows are drawn again on every search).
function refDeleted() {
  loadReference();
  initFilters();
  loadStats();
}

document.getElementById('ref-categories-tbody').addEventListener('click', async (e) => {
  const edit = e.target.closest('[data-edit-category]');
  const del = e.target.closest('[data-delete-category]');
  if (edit) showRefModal('category', refData.categories.find(x => x.slug === edit.dataset.editCategory));
  if (!del) return;
  const c = refData.categories.find(x => x.slug === del.dataset.deleteCategory);
  if (!c) return;
  if (!(await confirmDialog(`Delete category "${c.name}"? Any plugin tagged with it just loses that tag (shows up under "Without categories") — nothing else about those plugins changes, and they can be re-tagged afterward.`))) return;
  try {
    const res = await api(`/api/categories/${encodeURIComponent(c.slug)}`, { method: 'DELETE' });
    toast(res.affected ? `Deleted — ${res.affected} plugin(s) lost this tag.` : 'Deleted.');
    refDeleted();
  } catch (err) { handleActionError(err); }
});

document.getElementById('ref-vendors-tbody').addEventListener('click', async (e) => {
  const edit = e.target.closest('[data-edit-vendor]');
  const assign = e.target.closest('[data-assign-vendor]');
  const del = e.target.closest('[data-delete-vendor]');
  if (edit) showRefModal('vendor', refData.vendors.find(x => x.name === edit.dataset.editVendor));
  if (assign) showVendorAssignModal(assign.dataset.assignVendor);
  if (!del) return;
  const name = del.dataset.deleteVendor;
  if (!(await confirmDialog(`Delete vendor "${name}" from the reference list? Plugins carrying this vendor are cleared back to "Missing vendor" (not just unlinked) and can be reassigned to a different vendor afterward.`))) return;
  try {
    const res = await api(`/api/reference/vendors/${encodeURIComponent(name)}`, { method: 'DELETE' });
    toast(res.affected ? `Deleted — ${res.affected} plugin(s) are now orphaned from this vendor.` : 'Deleted.');
    refDeleted();
  } catch (err) { handleActionError(err); }
});

// Search, filters and order.
document.getElementById('db-search').addEventListener('input', debounce(renderReference, 150));
['db-f-source', 'db-f-status', 'db-f-links'].forEach(id => document.getElementById(id).addEventListener('change', renderReference));
document.getElementById('db-sort-key').addEventListener('change', (e) => {
  // Scanned in starts with the newest, any other order with A to Z.
  setRefSort(e.target.value, e.target.value === 'id' ? -1 : 1);
});
document.getElementById('db-sort-dir').addEventListener('click', () => setRefSort(refSort[refKind].key, -refSort[refKind].dir));
document.querySelectorAll('.ref-pane th[data-key]').forEach(th => {
  th.addEventListener('click', () => {
    const key = th.dataset.key;
    const cur = refSort[refKind];
    setRefSort(key, cur.key === key ? -cur.dir : 1);
  });
});


// Reference rows are added and edited in a popup. Each kind says where it saves and
// how the form values become the request body. A category's name is free (its slug is
// the key); a vendor's name is the key, so a changed one is renamed first (its own
// endpoint, which moves the plugins too) and the rest is saved under the new name.
const REF_KINDS = {
  category: {
    noun: 'category', endpoint: '/api/categories',
    body: (v, row) => ({ name: v.name, description: v.description, slug: row ? row.slug : null }),
    fields: (row) => `
      <div><label>Name</label><input type="text" data-k="name" placeholder="e.g. Saturation" value="${escHtml(row?.name)}" required></div>
      <div><label>Description</label><input type="text" data-k="description" value="${escHtml(row?.description)}" required></div>`,
  },
  vendor: {
    noun: 'vendor', endpoint: '/api/reference/vendors',
    body: (v) => ({
      name: v.name, url: v.url || null, status: v.status || null, status_note: v.status_note || null,
      kvr_url: v.kvr_url || '', equipboard_url: v.equipboard_url || '',
      no_url: !!v.no_url && !v.url, no_kvr: !!v.no_kvr && !v.kvr_url, no_equipboard: !!v.no_equipboard && !v.equipboard_url,
    }),
    fields: (row) => `
      <div><label>Vendor name</label><input type="text" data-k="name" value="${escHtml(row?.name)}" required>${row ? '<p class="hint">Renaming moves every plugin from this vendor to the new name.</p>' : ''}</div>
      <div><label>Links</label>${linkInputsHtml([
        { field: 'url', label: 'Website', hint: 'Official website URL', flag: 'no_url', value: row?.url, none: !row?.url && !!row?.no_url },
        { field: 'kvr_url', label: 'KVR', hint: 'KVR Audio page URL', flag: 'no_kvr', value: row?.kvr_url, none: !row?.kvr_url && !!row?.no_kvr },
        { field: 'equipboard_url', label: 'Equipboard', hint: 'Equipboard page URL', flag: 'no_equipboard', value: row?.equipboard_url, none: !row?.equipboard_url && !!row?.no_equipboard },
      ])}</div>
      <div><label>Status</label><select data-k="status">${[['', 'Status unknown'], ['active', 'Active'], ['deprecated', 'Deprecated'], ['down', 'Down']]
        .map(([val, text]) => `<option value="${val}" ${(row?.status || '') === val ? 'selected' : ''}>${text}</option>`).join('')}</select></div>
      <div><label>Status note</label><input type="text" data-k="status_note" placeholder="Optional" value="${escHtml(row?.status_note)}"></div>`,
  },
};

function showRefModal(kind, row) {
  const cfg = REF_KINDS[kind];
  const root = document.getElementById('modal-root');
  root.innerHTML = `
    <div class="modal-backdrop" id="modal-backdrop">
      <div class="modal">
        <button class="close-x" id="modal-close">✕</button>
        <h2>${row ? 'Edit' : 'Add'} ${cfg.noun}</h2>
        <form class="edit-form" id="ref-modal-form">
          ${cfg.fields(row)}
          <div style="margin-top:6px; display:flex; gap:8px;">
            <button type="submit" class="primary">${row ? 'Save changes' : 'Add'}</button>
            <button type="button" id="ref-modal-cancel">Cancel</button>
          </div>
        </form>
      </div>
    </div>`;
  const form = document.getElementById('ref-modal-form');
  document.getElementById('modal-close').onclick = closeModal;
  document.getElementById('ref-modal-cancel').onclick = closeModal;
  document.getElementById('modal-backdrop').onclick = (e) => { if (e.target.id === 'modal-backdrop') closeModal(); };
  form.onsubmit = async (e) => {
    e.preventDefault();
    const values = {};
    form.querySelectorAll('[data-k]').forEach(el => { values[el.dataset.k] = el.value.trim(); });
    form.querySelectorAll('input[data-field]').forEach(el => { values[el.dataset.field] = el.value.trim(); });
    form.querySelectorAll('input[data-flag]').forEach(el => { values[el.dataset.flag] = el.checked; });
    try {
      let renamed = null;
      if (kind === 'vendor' && row && values.name !== row.name) {
        renamed = await api(`${cfg.endpoint}/${encodeURIComponent(row.name)}/rename`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ new_name: values.name }),
        });
      }
      await api(cfg.endpoint, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(cfg.body(values, row)) });
      toast(renamed && renamed.affected ? `Saved. ${renamed.affected} plugin(s) follow the new name.` : 'Saved.');
      closeModal();
      loadReference();
      if (renamed) { initFilters(); loadStats(); }
    } catch (err) { toast(err.message, true); }
  };
  form.querySelector('input:not([readonly]):not([disabled])')?.focus();
}
document.getElementById('ref-add-category').onclick = () => showRefModal('category');
document.getElementById('ref-add-vendor').onclick = () => showRefModal('vendor');

// Lets the owner research a vendor once (Database > Vendors: URL, active/
// deprecated/down status) and then apply it to every plugin from that
// vendor in one action, instead of opening each plugin's edit modal
// individually — the workflow explicitly asked for: research the vendor,
// then "just link the plugin to the vendor."
async function showVendorAssignModal(vendorName) {
  const allPlugins = await api('/api/plugins');
  const selected = new Set();
  const root = document.getElementById('modal-root');
  root.innerHTML = `
    <div class="modal-backdrop" id="modal-backdrop">
      <div class="modal">
        <button class="close-x" id="modal-close">✕</button>
        <h2>Assign "${vendorName}" to plugin(s)</h2>
        <p class="hint">Sets this plugin's vendor (and copies the vendor's URL/status onto it immediately) —
          same as editing it by hand, just for many at once.</p>
        <label class="hint" style="display:flex; align-items:center; gap:6px; margin:10px 0;">
          <input type="checkbox" id="va-missing-only" checked> Only show plugins with no vendor
        </label>
        <input type="text" id="va-search" placeholder="Search plugin name…" style="width:100%; box-sizing:border-box; margin-bottom:8px; padding:6px 8px; background:var(--bg); border:1px solid var(--border); color:var(--text); border-radius:6px;">
        <div class="card-list" id="va-list" style="max-height:320px; overflow-y:auto;"></div>
        <div style="margin-top:16px; display:flex; gap:8px;">
          <button class="primary" id="va-assign">Assign selected</button>
          <button id="va-cancel">Cancel</button>
        </div>
      </div>
    </div>
  `;
  const listEl = document.getElementById('va-list');
  const searchEl = document.getElementById('va-search');
  const missingEl = document.getElementById('va-missing-only');

  function render() {
    const q = searchEl.value.trim().toLowerCase();
    const rows = allPlugins.filter(p =>
      (!missingEl.checked || !p.vendor) && (!q || p.name.toLowerCase().includes(q))
    ).slice(0, 200);
    listEl.innerHTML = rows.length ? rows.map(p => `
      <label class="ms-option" style="white-space:normal;">
        <input type="checkbox" value="${p.id}" ${selected.has(p.id) ? 'checked' : ''}>
        ${p.name} <span class="path">${p.vendor || 'no vendor'} — ${p.format}</span>
      </label>
    `).join('') : `<div class="empty-state">No matches.</div>`;
    listEl.querySelectorAll('input[type="checkbox"]').forEach(cb => {
      cb.onchange = () => {
        const id = parseInt(cb.value);
        if (cb.checked) selected.add(id); else selected.delete(id);
      };
    });
  }
  render();
  searchEl.addEventListener('input', render);
  missingEl.addEventListener('change', render);

  document.getElementById('modal-close').onclick = closeModal;
  document.getElementById('va-cancel').onclick = closeModal;
  document.getElementById('modal-backdrop').onclick = (e) => { if (e.target.id === 'modal-backdrop') closeModal(); };
  document.getElementById('va-assign').onclick = async () => {
    if (!selected.size) { toast('Select at least one plugin.', true); return; }
    try {
      const res = await api(`/api/reference/vendors/${encodeURIComponent(vendorName)}/assign`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ plugin_ids: [...selected] }),
      });
      toast(`Assigned ${res.assigned} plugin(s) to ${vendorName}.`);
      closeModal();
      refresh();
    } catch (e) { handleActionError(e); }
  };
}

// ---------- init ----------
// The desktop app spawns the Python backend as a sidecar process (see
// desktop/src-tauri/src/lib.rs) — it takes a moment to start listening after
// the window itself has already loaded and started running this script, so
// the very first fetch can lose the race and fail with a generic "Failed to
// fetch" (confirmed real via CDP: reloading the page afterwards works fine,
// since by then the backend's had time to come up). Wait for it explicitly
// instead of just letting that first load look broken.
async function waitForBackend(maxAttempts = 20, delayMs = 300) {
  for (let i = 0; i < maxAttempts; i++) {
    try {
      const res = await fetch(API_BASE + '/api/stats');
      if (res.ok) return true;
    } catch { /* not up yet */ }
    await new Promise(r => setTimeout(r, delayMs));
  }
  return false;
}

// Starts on DOMContentLoaded rather than right away: the module scripts load after
// this one, and each must have registered its section before the first one is shown.
// ---------- About ----------
// GET /api/about: the version shows next to the sidebar's logo; the About window has the rest.
let aboutInfo = null;
async function loadAbout() {
  try { aboutInfo = await api('/api/about'); } catch { return; }
  const v = document.getElementById('app-version');
  if (v && aboutInfo.version) v.textContent = `v${aboutInfo.version}`;
}
function showAbout() {
  const a = aboutInfo || { name: 'VEXT', version: '', company: 'Dianel Records', creator: 'SoDianel' };
  const root = document.getElementById('modal-root');
  root.innerHTML = `
    <div class="modal-backdrop" id="modal-backdrop">
      <div class="modal about-modal" role="dialog" aria-modal="true" aria-labelledby="about-title">
        <button type="button" class="close-x" id="modal-close" aria-label="Close">✕</button>
        <svg class="about-mark" aria-hidden="true"><use href="/static/logo-mark.svg#mark"/></svg>
        <h2 id="about-title">${escHtml(a.name)}</h2>
        ${a.version ? `<p class="about-version">Version ${escHtml(a.version)}</p>` : ''}
        <dl class="about-facts">
          <div><dt>Company</dt><dd>${escHtml(a.company)}</dd></div>
          <div><dt>Creator</dt><dd>${escHtml(a.creator)}</dd></div>
        </dl>
        <p class="about-copy">© ${new Date().getFullYear()} ${escHtml(a.company)}</p>
      </div>
    </div>`;
  document.getElementById('modal-close').onclick = closeModal;
  document.getElementById('modal-backdrop').onclick = (e) => { if (e.target.id === 'modal-backdrop') closeModal(); };
  document.getElementById('modal-close').focus();
}
document.getElementById('about-btn').addEventListener('click', showAbout);

// ---------- Log (app/logbuf.py: this session's backend + plugin-host output) ----------
// A sidebar section. The buffer lives in the backend's memory only, so closing VEXT
// clears it. While the section is open it tails GET /api/logs every second; elsewhere a
// slower poll of /api/logs/status lights the red dot on the sidebar button when an error
// arrives that hasn't been seen, and opening the section clears it.
let logsPollTimer = null;
let logsAfter = 0;
let logsSeenError = 0;

function logsLineHtml(l) {
  const cls = l.level === 'error' || l.level === 'critical' ? 'log-error' : l.level === 'warning' ? 'log-warn' : '';
  return `<div class="log-line ${cls}"><span class="log-source">${escHtml(l.time || '')} ${escHtml(l.source)}</span>${escHtml(l.text)}</div>`;
}

function setLogsErrorDot(lastError) {
  const dot = document.getElementById('logs-error-dot');
  if (activeSection === 'logs') logsSeenError = Math.max(logsSeenError, lastError);
  dot.hidden = !(lastError > logsSeenError);
}

async function pollLogs() {
  const el = document.getElementById('logs-console');
  try {
    const r = await api(`/api/logs?after=${logsAfter}`);
    if (r.lines.length) {
      const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
      el.insertAdjacentHTML('beforeend', r.lines.map(logsLineHtml).join(''));
      if (atBottom) el.scrollTop = el.scrollHeight;
    }
    logsAfter = r.latest;
    setLogsErrorDot(r.last_error);
  } catch { /* backend hiccup: retry on the next tick */ }
}

async function pollLogsStatus() {
  if (activeSection === 'logs') return; // the section's own poll covers it
  try { setLogsErrorDot((await api('/api/logs/status')).last_error); } catch { /* retry next tick */ }
}

async function exportLogs() {
  const name = `vext-log-${new Date().toISOString().slice(0, 19).replace(/[T:]/g, '-')}.txt`;
  if (!isTauri()) {
    // Plain browser: the backend serves it as a download.
    const a = document.createElement('a');
    a.href = `${API_BASE}/api/logs/text`;
    a.download = name;
    a.click();
    return;
  }
  const path = await window.__TAURI__.core.invoke('plugin:dialog|save', {
    options: { title: 'Export log', defaultPath: name, filters: [{ name: 'Text file', extensions: ['txt'] }] },
  });
  if (!path) return; // cancelled
  const target = /\.txt$/i.test(path) ? path : `${path}.txt`;
  try {
    await api('/api/logs/export', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ path: target }),
    });
    toast(`Log exported to ${target}`);
  } catch (e) { toast(`Couldn't export the log: ${e.message}`, true); }
}

function loadLogsSection() {
  const el = document.getElementById('logs-console');
  if (!el.dataset.inited) {
    el.dataset.inited = '1';
    document.getElementById('logs-export').addEventListener('click', exportLogs);
    document.getElementById('logs-copy').addEventListener('click', async () => {
      try { await navigator.clipboard.writeText(el.innerText); toast('Copied.'); }
      catch { toast('Could not copy: clipboard access was blocked.', true); }
    });
    document.getElementById('logs-reset').addEventListener('click', async () => {
      try {
        await api('/api/logs', { method: 'DELETE' });
        el.innerHTML = '';
        setLogsErrorDot(0);
        toast('Log reset.');
      } catch (e) { toast(`Couldn't reset the log: ${e.message}`, true); }
    });
  }
  pollLogs();
  if (!logsPollTimer) logsPollTimer = setInterval(pollLogs, 1000);
}
setInterval(pollLogsStatus, 3000);

// ---------- Microsoft's C++ runtime (app/runtime_check.py) ----------
// Plugins share Windows' copy of msvcp140.dll with VEXT (VEXT carries none on
// purpose). Missing, too old (< 14.40) or damaged, plugins crash on open, so the
// window says it plainly at the top, with a Repair that runs the runtime
// installers VEXT ships (Windows asks for permission once).
async function checkRuntime() {
  const el = document.getElementById('runtime-banner');
  if (!el) return;
  let rt;
  try { rt = await api('/api/system/runtime'); } catch { return; /* backend busy: next start checks again */ }
  if (!rt || rt.ok) { el.hidden = true; el.innerHTML = ''; return; }
  const fix = rt.repair
    ? '<button type="button" class="primary" id="rb-repair">Repair</button>'
    : '<a href="https://learn.microsoft.com/cpp/windows/latest-supported-vc-redist" target="_blank" rel="noopener">Get it from Microsoft</a>';
  el.innerHTML = `<span class="rb-text"><strong>Microsoft's Visual C++ runtime needs repair.</strong>
    Plugins will crash on open until it's fixed: ${escHtml(rt.problem)}.</span>
    <span class="rb-actions">${fix}<button type="button" id="rb-log">Details in Log</button></span>`;
  el.hidden = false;
  document.getElementById('rb-log').onclick = () => navigate('logs');
  const btn = document.getElementById('rb-repair');
  if (!btn) return;
  btn.onclick = async () => {
    btn.disabled = true;
    btn.textContent = 'Repairing… (allow it in the Windows prompt)';
    let r;
    try { r = await api('/api/system/runtime/repair', { method: 'POST' }); }
    catch (e) { r = { ok: false, message: e.message }; }
    toast(r.message || (r.ok ? 'Repaired.' : 'The repair failed.'), !r.ok);
    if (r.ok) {
      el.querySelector('.rb-text').innerHTML = `<strong>Repaired.</strong> ${escHtml(r.message)}`;
      btn.remove();
    } else {
      btn.disabled = false;
      btn.textContent = 'Repair';
    }
  };
}

async function init() {
  loadAbout();
  const meta = document.getElementById('toolbar-meta');
  if (isTauri()) {
    if (meta) meta.textContent = 'Starting backend…';
    const ready = await waitForBackend();
    if (meta) meta.textContent = '';
    if (!ready) {
      toast('Backend did not start in time — try reloading (Ctrl+R) in a moment.', true);
    }
  }

  booted = true;
  applyFlStudioPresence();
  loadStats();
  checkRuntime();
  // Dashboard is the landing section, unless something was opened while the backend started.
  navigate(activeSection || 'dashboard', activeParams);
}
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, { once: true });
else setTimeout(init, 0);
// ---------- AI (Configuration > AI: agent setup and research prompt) ----------
// Agents are optional. Configuration > AI checks each one, installs an npm-installable one on an
// explicit click (or links to its own installer), opens its sign-in in a terminal, and edits the
// research prompt. The output format is fixed by the schema in the backend, so the prompt only
// changes how the agent searches. An agent VEXT can't drive yet (Freebuff: no non-interactive
// mode) is still listed, with the reason; one that bills per use (Gemini CLI on an API key) only
// becomes ready after the owner allows it here.
function agentCardHtml(a) {
  const recommended = a.recommended ? ' <span class="badge" title="Free, with light, fast models">Recommended</span>' : '';
  let stateBadges;
  if (!a.supported) stateBadges = `${a.installed ? '<span class="badge ok">Installed</span> ' : ''}<span class="badge muted">Not supported yet</span>`;
  else if (a.installed) stateBadges = `<span class="badge ok">Installed</span> <span class="badge ${a.signed_in ? 'ok' : 'warn'}">${a.signed_in ? 'Signed in' : 'Not signed in'}</span>`
    + (a.billing_note ? ` <span class="badge ${a.allow_billed ? 'warn' : 'muted'}">${a.allow_billed ? 'Billed use allowed' : 'Billed: not allowed'}</span>` : '');
  else stateBadges = '<span class="badge muted">Not installed</span>';
  const install = a.install_url
    ? `<a class="vendor-link" href="${escHtml(a.install_url)}" target="_blank" rel="noopener noreferrer">Installation page ↗</a>` : '';
  const detail = a.installed
    ? [a.version, a.auth_detail].filter(Boolean).map(escHtml).join(' · ')
    : `${escHtml(a.blurb || '')} ${a.supported ? 'Optional.' : ''}`.trim();
  const blurb = a.installed && a.blurb ? `<div class="path">${escHtml(a.blurb)}</div>` : '';
  const unsupported = !a.supported
    ? `<div class="agent-note">Not supported: ${escHtml(a.unsupported_reason)}${a.issue_url
      ? ` <a class="vendor-link" href="${escHtml(a.issue_url)}" target="_blank" rel="noopener noreferrer">Follow the request ↗</a>` : ''}</div>` : '';
  const billing = a.supported && a.installed && a.billing_note ? `
    <div class="agent-billing">
      <div class="agent-note">${escHtml(a.billing_note)}</div>
      <label class="check"><input type="checkbox" data-agent-consent ${a.allow_billed ? 'checked' : ''}>
        Allow VEXT to use ${escHtml(a.label)} this way (each investigation may be billed to your key)</label>
    </div>` : '';
  const model = a.models.find(m => m.id === a.model);
  const modelOpts = a.models.map(m => `<option value="${escHtml(m.id)}" ${m.id === a.model ? 'selected' : ''}>${escHtml(m.label)}${m.label !== m.id ? ` (${escHtml(m.id)})` : ''}</option>`).join('');
  const efforts = model ? model.efforts : [];
  const effortOpts = efforts.map(e => `<option value="${escHtml(e)}" ${e === a.effort ? 'selected' : ''}>${escHtml(e)}</option>`).join('');
  const settings = a.supported && a.installed && a.models.length ? `
    <div class="agent-settings">
      <label>Model <select data-field="model" aria-label="${escHtml(a.label)} model">${modelOpts}</select></label>
      <label ${efforts.length ? '' : 'hidden'}>Effort <select data-field="effort" aria-label="${escHtml(a.label)} effort">${effortOpts}</select></label>
    </div>
    <div class="path">${model && model.description ? escHtml(model.description) + ' · ' : ''}${escHtml(a.models_source)}</div>
    ${a.models_note ? `<div class="agent-note">${escHtml(a.models_note)}</div>` : ''}` : '';
  const buttons = [];
  if (!a.installed && a.npm_installable && a.supported) buttons.push(`<button class="primary" data-agent-act="install">Install ${escHtml(a.label)}</button>`);
  else if (a.installed && a.supported && !a.signed_in) buttons.push(`<button class="primary" data-agent-act="signin">Sign in…</button>`);
  const hint = !a.installed && a.supported && !a.npm_installable && a.install_hint ? `<div class="path">${escHtml(a.install_hint)}</div>` : '';
  return `<div class="candidate-card agent-card${a.supported ? '' : ' is-unsupported'}" data-agent="${escHtml(a.id)}">
    <div class="info"><strong>${escHtml(a.label)}</strong>${recommended} ${stateBadges}
      <div class="path">${detail}${install ? ` · ${install}` : ''}</div>${blurb}${hint}${unsupported}${billing}${settings}</div>
    <div class="actions-inline">${buttons.join('')}</div></div>`;
}

let aiAgentsCache = [];
async function loadAi(refresh = false) {
  const [s, agents] = await Promise.all([api('/api/ai'), api('/api/ai/agents' + (refresh ? '?refresh=true' : ''))]);
  aiAgentsCache = agents.agents;
  document.getElementById('ai-agents').innerHTML = agents.agents.map(agentCardHtml).join('');
  const out = document.getElementById('ai-install-output');
  if (!agents.npm_available && agents.agents.some(a => !a.installed)) {
    out.textContent = 'Installing an agent needs Node.js (npm). Get it from nodejs.org first.';
  }
  const prompt = document.getElementById('ai-prompt');
  prompt.value = s.prompt;
  document.getElementById('ai-prompt-state').textContent = s.customized ? 'Customized' : 'Default';
}

document.getElementById('ai-check').addEventListener('click', () => {
  loadAi(true).then(() => toast('Checked.')).catch(handleActionError);
});

document.getElementById('ai-agents').addEventListener('click', async (e) => {
  const btn = e.target.closest('button[data-agent-act]');
  if (!btn) return;
  const id = btn.closest('.agent-card').dataset.agent;
  const out = document.getElementById('ai-install-output');
  try {
    if (btn.dataset.agentAct === 'install') {
      btn.disabled = true;
      btn.textContent = 'Installing…';
      out.textContent = 'This can take a minute. Keep VEXT open.';
      try {
        const res = await api(`/api/ai/agents/${id}/install`, { method: 'POST' });
        out.textContent = res.version ? `Installed ${res.version}.` : 'Installed.';
        toast('Installed.');
      } finally { loadAi(true); }
    } else if (btn.dataset.agentAct === 'signin') {
      await api(`/api/ai/agents/${id}/signin`, { method: 'POST' });
      toast('Sign-in opened in a terminal window. Finish it there, then click Check again.');
    }
  } catch (err) { out.textContent = err.message; handleActionError(err); }
});

// Picking a model or effort saves at once. Effort choices follow the model: each lists the levels it supports.
document.getElementById('ai-agents').addEventListener('change', async (e) => {
  const card = e.target.closest('.agent-card');
  if (!card) return;
  const agent = aiAgentsCache.find(a => a.id === card.dataset.agent);
  // Allowing an agent that bills per use (Gemini CLI on an API key).
  if (e.target.matches('[data-agent-consent]')) {
    try {
      await api(`/api/ai/agents/${agent.id}/consent`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ allow: e.target.checked }),
      });
      toast(e.target.checked ? `${agent.label} can be used now. Its use may be billed to your key.` : `${agent.label} won't be used.`);
    } catch (err) { handleActionError(err); }
    loadAi(true).catch(handleActionError);
    return;
  }
  const field = e.target.dataset.field;
  if (!field) return;
  const modelSel = card.querySelector('[data-field="model"]');
  const effortSel = card.querySelector('[data-field="effort"]');
  const model = agent.models.find(m => m.id === modelSel.value);
  if (field === 'model') {
    effortSel.innerHTML = model.efforts.map(x => `<option value="${escHtml(x)}">${escHtml(x)}</option>`).join('');
    effortSel.value = model.efforts.includes(agent.effort) ? agent.effort : model.default_effort;
    effortSel.closest('label').hidden = !model.efforts.length;
  }
  try {
    await api(`/api/ai/agents/${agent.id}/settings`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: modelSel.value, effort: effortSel.value || null }),
    });
    agent.model = modelSel.value; agent.effort = effortSel.value;
    toast(`${agent.label}: ${model.label}${effortSel.value ? `, effort ${effortSel.value}` : ''}.`);
  } catch (err) { handleActionError(err); loadAi(); }
});

// ---------- which AI to use ----------
// Every investigation button goes through here. One ready agent: used straight away. Several: a popup
// asks (the last one used is preselected, else the recommended one). Returns the agent id, or null
// when cancelled. With noAi (picture searches), "No AI" is offered too and returns '': VEXT then only
// reads the plugin's store and KVR pages; with no agent ready that is what happens, without asking.
async function pickAgent(opts = {}) {
  // The agent list is cached by the backend (refreshed in the background), so it is usually
  // instant; when it isn't, the popup opens at once with a spinner instead of after the wait.
  const root = document.getElementById('confirm-root');
  let spinner = false;
  const slow = setTimeout(() => {
    spinner = true;
    root.innerHTML = `
      <div class="modal-backdrop" id="agent-backdrop">
        <div class="modal" role="dialog" aria-modal="true" aria-labelledby="agent-pick-title" aria-busy="true">
          <h2 id="agent-pick-title">Which AI should investigate?</h2>
          <p class="hint"><span class="spinner" aria-hidden="true"></span> Checking which AI agents are ready…</p>
        </div>
      </div>`;
  }, 150);
  const done = () => { clearTimeout(slow); if (spinner) root.innerHTML = ''; };
  let data;
  try { data = await api('/api/ai/agents'); } catch (e) { done(); handleActionError(e); return null; }
  clearTimeout(slow);
  aiAgentsCache = data.agents;
  const ready = data.agents.filter(a => a.ready);
  if (!ready.length) {
    done();
    if (opts.noAi) {
      toast(opts.noAiToast || "No AI is set up, so VEXT only reads the plugin's store and KVR pages. Configuration > AI sets one up.");
      return '';
    }
    toast('No AI is set up yet. Open Configuration > AI to install and sign in to one.', true);
    return null;
  }
  if (ready.length === 1 && !opts.noAi) { done(); return ready[0].id; }
  let last = null;
  try { last = localStorage.getItem('pa-last-agent'); } catch { /* storage unavailable */ }
  const chosen = ready.find(a => a.id === last) || ready.find(a => a.recommended) || ready[0];
  const options = ready.map(a => ({
    id: a.id, label: a.label + (a.recommended ? ' (recommended)' : ''),
    sub: `${(a.models.find(m => m.id === a.model) || { label: a.model }).label}${a.effort ? ` · effort ${a.effort}` : ''}`,
  }));
  if (opts.noAi) options.push({ id: '', label: opts.noAiLabel || 'No AI', sub: opts.noAiSub || "Only read the plugin's store and KVR pages for pictures. Free and quick." });
  return new Promise((resolve) => {
    root.innerHTML = `
      <div class="modal-backdrop" id="agent-backdrop">
        <div class="modal" role="dialog" aria-modal="true" aria-labelledby="agent-pick-title">
          <h2 id="agent-pick-title">Which AI should investigate?</h2>
          <div class="agent-pick">${options.map(o => `
            <label><input type="radio" name="agent-pick" value="${escHtml(o.id)}" ${o.id === chosen.id ? 'checked' : ''}>
              <span><strong>${escHtml(o.label)}</strong>
                <span class="sub">${escHtml(o.sub)}</span></span>
            </label>`).join('')}</div>
          <div class="actions-row">
            <button id="agent-cancel">Cancel</button>
            <button class="primary" id="agent-ok">Investigate</button>
          </div>
        </div>
      </div>`;
    const finish = (value) => {
      root.innerHTML = '';
      document.removeEventListener('keydown', onKey);
      resolve(value);
    };
    const onKey = (e) => { if (e.key === 'Escape') finish(null); };
    document.addEventListener('keydown', onKey);
    document.getElementById('agent-ok').onclick = () => {
      const picked = root.querySelector('input[name="agent-pick"]:checked');
      const value = picked ? picked.value : chosen.id;
      if (value) { try { localStorage.setItem('pa-last-agent', value); } catch { /* storage unavailable */ } }
      finish(value);
    };
    document.getElementById('agent-cancel').onclick = () => finish(null);
    document.getElementById('agent-backdrop').onclick = (e) => { if (e.target.id === 'agent-backdrop') finish(null); };
    root.querySelector('input[name="agent-pick"]:checked')?.focus();
  });
}

document.getElementById('ai-prompt-save').addEventListener('click', async () => {
  const value = document.getElementById('ai-prompt').value;
  try {
    await api('/api/ai/prompt', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ value }),
    });
    toast('Prompt saved. It applies to the next research.');
    loadAi();
  } catch (e) { handleActionError(e); }
});

document.getElementById('ai-prompt-reset').addEventListener('click', async () => {
  if (!(await confirmDialog('Replace the research prompt with the default? Your edits will be lost.'))) return;
  try {
    await api('/api/ai/prompt/reset', { method: 'POST' });
    toast('Prompt reset to the default.');
    loadAi();
  } catch (e) { handleActionError(e); }
});

// ---------- reference data: one table at a time ----------
document.querySelectorAll('#reference-tabs button[data-ref-pane]').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('#reference-tabs button[data-ref-pane]').forEach(b => {
      const on = b === btn;
      b.classList.toggle('active', on);
      b.setAttribute('aria-selected', String(on));
    });
    document.querySelectorAll('.ref-pane').forEach(p => p.classList.toggle('active', p.dataset.refPane === btn.dataset.refPane));
    refKind = btn.dataset.refPane;
    syncRefControls(true);
    renderReference();
  });
});

// ---------- quarantine older copies (proposal switch) ----------
async function loadAutoPropose() {
  const s = await api('/api/settings');
  document.getElementById('auto-propose-superseded').checked = s.auto_propose_superseded !== '0';
}
document.getElementById('auto-propose-superseded').addEventListener('change', async (e) => {
  try {
    await api('/api/settings/auto_propose_superseded', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ value: e.target.checked ? '1' : '0' }),
    });
    toast(e.target.checked ? 'Older copies will be proposed after each scan.' : 'Older-copy proposals are off.');
  } catch (err) { e.target.checked = !e.target.checked; handleActionError(err); }
});

// ---------- review lists (Actions > Metadata): Missing vendor / Inferred vendor / Missing type / categories / links ----------
// Each list (a chip; `tab` below is its key) lists the plugins that lack one piece of metadata. The AI can investigate one plugin, or the
// next N plugins (the group size is remembered per list). Its answer waits on the row until it's applied.
const REVIEW = {
  vendor:     { kind: 'vendor',          target: 'vendor',         empty: 'Every plugin has a vendor.', what: 'vendor' },
  inferred:   { kind: 'inferred_vendor', target: 'confirm_vendor', empty: 'No inferred vendors left to confirm.', what: 'vendor check' },
  type:       { kind: 'type',            target: 'types',          empty: 'Every plugin has a known type.', what: 'type' },
  categories: { kind: 'categories',      target: 'categories',     empty: 'Every plugin has a category.', what: 'category suggestion' },
  links:      { kind: 'links',           target: 'links',          empty: 'Every plugin has its store, KVR and Equipboard links.', what: 'links' },
  picture:    { kind: 'picture',         target: 'image',          empty: 'Every plugin has a picture.', what: 'picture' },
};
const REVIEW_CHUNKS = [10, 25, 50, 100, 0]; // 0 = every plugin listed
const reviewData = {};
const reviewFilter = {};
let batchPollTimer = null;
let batchWasRunning = false;

function chunkPref(key) {
  try { const v = parseInt(localStorage.getItem('pa-chunk-' + key), 10); if (REVIEW_CHUNKS.includes(v)) return v; } catch { /* storage unavailable */ }
  return 25;
}
function setChunkPref(key, value) {
  try { localStorage.setItem('pa-chunk-' + key, String(value)); } catch { /* storage unavailable */ }
}
const chunkOptions = (selected) => REVIEW_CHUNKS.map(n =>
  `<option value="${n}" ${n === selected ? 'selected' : ''}>${n ? n + ' at a time' : 'All'}</option>`).join('');

function progressBlock(target) {
  return `<div class="review-progress" data-progress-target="${target}" hidden>
    <div class="review-progress-row">
      <div class="batch-progress" aria-hidden="true"><span></span></div>
      <button class="danger" data-role="stop" title="Stops now: running AI calls are cancelled and queued ones are skipped.">Stop investigation</button>
    </div>
    <p class="hint"></p>
  </div>`;
}

const LINK_LABELS = [['store_url', 'Store'], ['kvr_url', 'KVR'], ['equipboard_url', 'Equipboard']];

const LINK_NONE = { store_url: 'no_store', kvr_url: 'no_kvr', equipboard_url: 'no_equipboard' };
const LINK_NONE_LABEL = { store_url: 'No store', kvr_url: 'No KVR', equipboard_url: 'No Equipboard' };

// One chip per link, three states: a real link; "No KVR" (looked, the site has no page: settled);
// or a dim "KVR ?" (never looked at).
function linkChips(obj) {
  return LINK_LABELS.map(([field, label]) => {
    if (obj[field]) return `<a class="tag link-chip vendor-link" href="${escHtml(obj[field])}" target="_blank" rel="noopener noreferrer" title="${escHtml(obj[field])}">${label}</a>`;
    if (obj[LINK_NONE[field]]) return `<span class="tag link-none" title="Looked: this site has no page for it. Settled, not missing.">${LINK_NONE_LABEL[field]}</span>`;
    return `<span class="tag link-missing" title="Not investigated yet.">${label} ?</span>`;
  }).join('');
}

// Text inputs with a "No page" box beside each: ticking it means "looked, nothing there".
function linkInputsHtml(entries) {
  return entries.map(e => `<div class="link-input">
    <input type="text" data-field="${e.field}" placeholder="${escHtml(e.hint)}" value="${escHtml(e.value || '')}" ${e.none ? 'disabled' : ''} aria-label="${escHtml(e.label)} link">
    <label class="none-check"><input type="checkbox" data-flag="${e.flag}" ${e.none ? 'checked' : ''}> No page</label></div>`).join('');
}

// A ticked box clears and locks its field. Typing in the field unticks the box.
document.addEventListener('change', (e) => {
  const box = e.target.closest && e.target.closest('input[data-flag]');
  if (!box) return;
  const input = box.closest('.link-input').querySelector('input[data-field]');
  input.disabled = box.checked;
  if (box.checked) input.value = '';
});
document.addEventListener('input', (e) => {
  const input = e.target.closest && e.target.closest('.link-input input[data-field]');
  if (!input || !input.value.trim()) return;
  const box = input.closest('.link-input').querySelector('input[data-flag]');
  if (box) box.checked = false;
});

const shortDate = (iso) => { try { return new Date(iso).toLocaleDateString(); } catch { return ''; } };

function reviewSuggestionHtml(tab, r) {
  if (!r.suggestion) return '';
  const cfg = REVIEW[tab];
  let text;
  let agree = false;
  if (cfg.kind === 'inferred_vendor') {
    agree = String(r.suggestion).toLowerCase() === String(r.vendor || '').toLowerCase();
    text = agree ? `The AI confirms <strong>${escHtml(r.suggestion)}</strong>` : `The AI suggests <strong>${escHtml(r.suggestion)}</strong> instead of ${escHtml(r.vendor)}`;
  } else if (cfg.kind === 'categories') {
    text = `The AI suggests: <strong>${escHtml(r.suggestion.join(', '))}</strong>`;
  } else if (cfg.kind === 'links') {
    const sg = r.suggestion;
    text = 'The AI found:'
      + LINK_LABELS.filter(([f]) => sg.found[f]).map(([f, label]) =>
        `<div class="link-found">${label}: <a class="vendor-link" href="${escHtml(sg.found[f])}" target="_blank" rel="noopener noreferrer">${escHtml(sg.found[f])}</a></div>`).join('')
      + sg.none.map(f => `<div class="link-found">${LINK_NONE_LABEL[f]}: no page found. Apply marks it as none.</div>`).join('');
  } else {
    text = `The AI suggests <strong>${escHtml(r.suggestion)}</strong>`;
  }
  const why = [r.suggestion_confidence ? `confidence: ${escHtml(r.suggestion_confidence)}` : '', r.suggestion_notes ? escHtml(r.suggestion_notes) : '']
    .filter(Boolean).join(' · ');
  const src = (r.suggestion_sources || [])[0];
  return `<div class="review-suggestion ${agree ? 'agree' : ''}">${text}
    ${why ? `<div class="why">${why}</div>` : ''}
    ${src ? `<div class="why"><a class="vendor-link" href="${escHtml(src)}" target="_blank" rel="noopener noreferrer">${escHtml(src)}</a></div>` : ''}</div>`;
}

function reviewRowHtml(tab, r) {
  const cfg = REVIEW[tab];
  if (cfg.kind === 'picture') return pictureRowHtml(r);
  const vendorLine = cfg.kind === 'inferred_vendor'
    ? `Inferred vendor: <strong>${escHtml(r.vendor)}</strong>`
    : (r.vendor ? escHtml(r.vendor) : 'unknown vendor');
  const flHint = cfg.kind === 'type'
    ? (r.fl_type ? `<div class="path">FL Studio reports this as: <strong>${escHtml(r.fl_type)}</strong></div>` : '')
    : '';
  const note = cfg.kind === 'vendor' && r.vendor_note ? `<div class="path">${escHtml(r.vendor_note)}</div>` : '';
  const buttons = [];
  if (r.suggestion) {
    buttons.push(`<button class="primary" data-act="apply" data-id="${r.id}">Apply</button>`);
    buttons.push(`<button data-act="discard" data-id="${r.id}">Discard</button>`);
  }
  if (cfg.kind === 'inferred_vendor') buttons.push(`<button data-act="confirm" data-id="${r.id}">Confirm ${escHtml(r.vendor)}</button>`);
  if (cfg.kind === 'type') {
    if (r.fl_type) buttons.push(`<button class="primary" data-act="settype" data-id="${r.id}" data-type="${escHtml(r.fl_type)}">Set type: ${escHtml(r.fl_type)}</button>`);
    buttons.push(`<button data-act="settype" data-id="${r.id}" data-type="effect">Effect</button>`);
    buttons.push(`<button data-act="settype" data-id="${r.id}" data-type="synth">Synth</button>`);
  } else if (cfg.kind === 'categories') {
    buttons.push(`<button data-act="edit" data-id="${r.id}">✎ Edit</button>`);
  }
  if (cfg.kind !== 'type' && cfg.kind !== 'categories') {
    buttons.push(`<button data-act="manual" data-id="${r.id}">Enter manually</button>`);
  }
  buttons.push(`<button data-act="investigate" data-id="${r.id}">${r.suggestion ? 'Investigate again' : 'Investigate'}</button>`);
  buttons.push(`<button data-act="details" data-id="${r.id}">Details</button>`);
  buttons.push(rowMenuBtn(r.id));
  return `
    <div class="candidate-card" data-row-target="${cfg.target}" data-row-id="${r.id}" data-plugin-id="${r.id}">
      <div class="info">
        <span class="review-row-mark"></span>${storeIcon(r)}<strong>${escHtml(r.name)}</strong> <span class="badge">${escHtml(r.format)}</span>${flCloudBadge(r)} — ${vendorLine}
        <div class="path">${escHtml(r.host_path)}</div>
        ${cfg.kind === 'links' ? `<div class="path">${linkChips(r)}</div>` : ''}
        ${flHint}${note}
        ${!r.suggestion && r.investigated_at ? `<div class="path">Investigated ${escHtml(shortDate(r.investigated_at))}: nothing more found. Investigate again to retry.</div>` : ''}
        ${reviewSuggestionHtml(tab, r)}
        ${manualFormHtml(cfg, r)}
        <div class="review-row-error" hidden></div>
      </div>
      <div class="actions-inline">${buttons.join('')}</div>
    </div>`;
}

// Inline form to type the value yourself instead of (or after) asking the AI.
function manualFormHtml(cfg, r) {
  if (cfg.kind === 'type' || cfg.kind === 'categories') return '';
  const isVendor = cfg.kind === 'vendor' || cfg.kind === 'inferred_vendor';
  let inputs;
  if (isVendor) {
    inputs = `<input type="text" data-field="vendor" list="review-vendor-names" placeholder="Vendor name" value="${escHtml(r.vendor || (r.suggestion || ''))}" aria-label="Vendor name">`;
  } else {
    const hints = { store_url: 'Official store page (not a reseller)', kvr_url: 'KVR Audio page', equipboard_url: 'Equipboard page' };
    const sg = r.suggestion || { found: {}, none: [] };
    inputs = linkInputsHtml(LINK_LABELS.map(([field, label]) => ({
      field, label, hint: 'https://… ' + hints[field], flag: LINK_NONE[field],
      value: r[field] || sg.found[field] || '', none: !r[field] && !sg.found[field] && (!!r[LINK_NONE[field]] || sg.none.includes(field)),
    })));
  }
  return `<div class="manual-edit" hidden>${inputs}
    <button class="primary" data-act="save-manual" data-id="${r.id}">Save</button>
    <button data-act="manual" data-id="${r.id}">Cancel</button></div>`;
}

let reviewVendorNamesLoaded = false;
async function ensureVendorNames() {
  if (reviewVendorNamesLoaded) return;
  reviewVendorNamesLoaded = true;
  try {
    const vendors = await api('/api/reference/vendors');
    let dl = document.getElementById('review-vendor-names');
    if (!dl) { dl = document.createElement('datalist'); dl.id = 'review-vendor-names'; document.body.appendChild(dl); }
    dl.innerHTML = vendors.map(v => `<option value="${escHtml(v.name)}">`).join('');
  } catch { reviewVendorNamesLoaded = false; }
}

function toggleManualForm(card) {
  const form = card.querySelector('.manual-edit');
  if (!form) return;
  form.hidden = !form.hidden;
  if (!form.hidden) { ensureVendorNames(); form.querySelector('input,select')?.focus(); }
}

function reviewVisibleRows(tab) {
  const q = (reviewFilter[tab] || '').trim().toLowerCase();
  let rows = reviewData[tab] || [];
  if (tab === 'picture') rows = rows.filter(PIC_SHOW[picShow].test);
  return q ? rows.filter(r => `${r.name} ${r.vendor || ''} ${r.host_path}`.toLowerCase().includes(q)) : rows;
}

function renderReviewList(tab) {
  const list = document.getElementById(`review-${tab}-list`);
  const all = reviewData[tab] || [];
  const rows = reviewVisibleRows(tab);
  // Rows that were applied leave; the rest glide up into place (fx.flip, with effects on).
  if (!all.length) { fxFlip(list, () => { list.innerHTML = `<div class="empty-state">${REVIEW[tab].empty}</div>`; }); return; }
  fxFlip(list, () => {
    list.innerHTML = rows.length ? rows.map(r => reviewRowHtml(tab, r)).join('') : `<div class="empty-state">No plugin matches that search.</div>`;
  });
  refreshBatch();
}

function renderReviewToolbar(tab) {
  const cfg = REVIEW[tab];
  if (cfg.kind === 'picture') { renderPictureToolbar(); return; }
  const rows = reviewData[tab] || [];
  const waiting = rows.filter(r => r.suggestion).length;
  const bar = document.getElementById(`review-${tab}-toolbar`);
  bar.innerHTML = `
    <input type="text" class="grow" data-role="filter" placeholder="Search this list…" value="${escHtml(reviewFilter[tab] || '')}" aria-label="Search this list">
    <span class="badge muted">${rows.length} listed · ${waiting} suggestion${waiting === 1 ? '' : 's'} waiting</span>
    <select data-role="chunk" aria-label="How many to investigate at a time">${chunkOptions(chunkPref(tab))}</select>
    <button class="primary" data-role="investigate-chunk">Investigate with AI</button>
    ${cfg.kind === 'inferred_vendor' ? '<button data-role="confirm-all">Confirm all as they are</button>' : ''}
    <button class="primary" data-role="apply-all" ${waiting ? '' : 'disabled'}>Apply all suggestions (${waiting})</button>
    ${progressBlock(cfg.target)}`;
}

async function loadReview(tab) {
  const cfg = REVIEW[tab];
  const list = document.getElementById(`review-${tab}-list`);
  if (!reviewData[tab]) list.innerHTML = '<div class="empty-state">Loading…</div>';
  try {
    reviewData[tab] = await api(`/api/review/${cfg.kind}`);
  } catch (e) { handleActionError(e); return; }
  if (tab === 'picture') await loadCaptureStatus();
  renderReviewToolbar(tab);
  renderReviewList(tab);
  if (tab === 'vendor') loadVendorSites();
}

// `picked`: an agent id already chosen ('' = no AI, pictures only). `extra`: more batch options
// (ask_ai for pictures).
async function startReviewBatch(target, ids, label, picked = null, extra = {}) {
  const agent = picked !== null ? picked : await pickAgent({ noAi: target === 'image' });
  if (agent === null || (agent === '' && target !== 'image')) return;
  try {
    await api('/api/ai/batch/start', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ target, ids, agent, ...extra }),
    });
    const who = agent ? ((aiAgentsCache.find(a => a.id === agent) || {}).label || 'The AI') : 'VEXT';
    toast(`${who} is ${target === 'image' ? 'looking for pictures of' : 'investigating'} ${ids.length} ${label}.`);
    refreshBatch();
  } catch (e) { handleActionError(e); }
}

async function investigateChunk(tab) {
  const cfg = REVIEW[tab];
  const isPic = cfg.kind === 'picture';
  const pending = reviewVisibleRows(tab).filter(r => !r.suggestion && (!r.investigated_at || (isPic && pictureWorthRetry(r))));
  if (!pending.length) { toast('Nothing new to investigate here. Everything listed already has a suggestion or was already tried. Use a row\'s Investigate again to retry one.'); return; }
  const chunk = chunkPref(tab);
  const ids = (chunk ? pending.slice(0, chunk) : pending).map(r => r.id);
  const n = ids.length;
  const ask = isPic
    ? `Look for pictures of ${n} plugin${n === 1 ? '' : 's'}?\n\nVEXT first reads each one's store and KVR pages itself (no AI cost). The AI is only asked for the ones where that finds nothing: at most ${n} AI call${n === 1 ? '' : 's'}. Pictures wait here for your choice. Nothing is applied until you confirm it.`
    : `Investigate ${n} plugin${n === 1 ? '' : 's'} with AI?\n\nThat is ${n} AI call${n === 1 ? '' : 's'}. Results wait here for your review. Nothing is applied until you confirm it.`;
  if (!(await confirmDialog(ask))) return;
  startReviewBatch(cfg.target, ids, `plugin${ids.length === 1 ? '' : 's'}`);
}

async function stopInvestigation() {
  try {
    await api('/api/ai/batch/stop', { method: 'POST' });
    toast('Investigation stopped. Results already finished are kept.');
    refreshBatch();
  } catch (e) { handleActionError(e); }
}

async function reviewPost(path, body) {
  return api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
}

async function reviewRowAction(tab, act, btn) {
  const cfg = REVIEW[tab];
  const id = parseInt(btn.dataset.id, 10);
  if (cfg.kind === 'picture' && await pictureRowAction(act, btn, id)) return;
  try {
    if (act === 'investigate') { await startReviewBatch(cfg.target, [id], 'plugin'); return; }
    if (act === 'details') { showPluginModal(id); return; }
    if (act === 'edit') { openPluginEditor(id); return; }
    if (act === 'manual') { toggleManualForm(btn.closest('.candidate-card')); return; }
    if (act === 'save-manual') {
      const form = btn.closest('.manual-edit');
      const body = {};
      form.querySelectorAll('input[data-field]').forEach(i => { if (i.value.trim()) body[i.dataset.field] = i.value.trim(); });
      form.querySelectorAll('input[data-flag]').forEach(b => { body[b.dataset.flag] = b.checked; });
      const hasValue = Object.entries(body).some(([k, v]) => (typeof v === 'string') || v === true);
      if (!hasValue) { toast('Type a link, or tick "No page", first.', true); return; }
      await api(`/api/plugins/${id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      toast('Saved.');
    }
    else if (act === 'apply') { await reviewPost(`/api/review/${cfg.kind}/apply`, { ids: [id] }); toast('Applied.'); }
    else if (act === 'discard') { await reviewPost(`/api/review/${cfg.kind}/discard`, { ids: [id] }); toast('Suggestion discarded.'); }
    else if (act === 'confirm') { await reviewPost('/api/review/inferred_vendor/confirm', { ids: [id] }); toast('Vendor confirmed.'); }
    else if (act === 'settype') {
      await api(`/api/plugins/${id}`, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ plugin_type: btn.dataset.type }),
      });
      toast(`Set to ${btn.dataset.type}.`);
    }
    await loadReview(tab);
    loadStats();
  } catch (e) { handleActionError(e); }
}

async function reviewToolbarAction(tab, role) {
  const cfg = REVIEW[tab];
  try {
    if (role === 'investigate-chunk') { investigateChunk(tab); return; }
    if (role === 'stop') { await stopInvestigation(); return; }
    if (role === 'apply-all' && cfg.kind === 'picture') {
      const rows = (reviewData[tab] || []).filter(r => r.suggestion);
      const n = rows.length;
      if (!n || !(await confirmDialog(`Use the chosen picture for all ${n} plugin${n === 1 ? '' : 's'} with pictures waiting?\n\nEach one uses the picture marked ✓ on its row. No AI cost.`))) return;
      const res = await reviewPost('/api/review/picture/apply', { ids: rows.map(r => r.id), choices: pictureChoices(rows) });
      toast(`Applied ${res.applied}.${res.skipped && res.skipped.length ? ` ${res.skipped.length} skipped: ${res.skipped[0].reason}` : ''}`, !!(res.skipped && res.skipped.length));
      loadPictureIndex();
    } else if (role === 'apply-all') {
      const n = (reviewData[tab] || []).filter(r => r.suggestion).length;
      if (!n || !(await confirmDialog(`Apply all ${n} waiting suggestion${n === 1 ? '' : 's'}?\n\nThis changes ${n} plugin${n === 1 ? '' : 's'}. No AI cost.`))) return;
      const res = await reviewPost(`/api/review/${cfg.kind}/apply`, { ids: null });
      toast(`Applied ${res.applied}.`);
    } else if (role === 'confirm-all') {
      const n = (reviewData[tab] || []).length;
      if (!n || !(await confirmDialog(`Confirm all ${n} inferred vendors as they are?\n\nNo AI is used. They stop being inferred and rescans keep them.`))) return;
      const res = await reviewPost('/api/review/inferred_vendor/confirm', { ids: null });
      toast(`Confirmed ${res.confirmed}.`);
    }
    await loadReview(tab);
    loadStats();
  } catch (e) { handleActionError(e); }
}

Object.keys(REVIEW).forEach(tab => {
  document.getElementById(`review-${tab}-list`).addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-act]');
    if (btn) reviewRowAction(tab, btn.dataset.act, btn);
  });
  const bar = document.getElementById(`review-${tab}-toolbar`);
  bar.addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-role]');
    if (btn) reviewToolbarAction(tab, btn.dataset.role);
  });
  bar.addEventListener('input', (e) => {
    if (e.target.dataset.role !== 'filter') return;
    reviewFilter[tab] = e.target.value;
    renderReviewList(tab);
  });
  bar.addEventListener('change', (e) => {
    if (e.target.dataset.role === 'chunk') setChunkPref(tab, parseInt(e.target.value, 10));
    if (e.target.dataset.role === 'pic-show') {
      picShow = PIC_SHOW[e.target.value] ? e.target.value : 'all';
      try { localStorage.setItem('pa-pic-show', picShow); } catch { /* storage unavailable */ }
      renderReviewList(tab);
      renderCaptureBlock();
    }
  });
});

// ---------- Missing picture (Actions › Metadata) ----------
// Plugins without a picture, and two ways to give them one. Both end as the plugin's picture
// file, which Browse, the Playground and the plugin sheet all show.
//  - Capture: the automatic capture job opens each plugin that can open here and saves a
//    picture of its window (backend: the plugin host's /api/pictures/capture, see below).
//  - Find online: the AI proposes up to three picture addresses; the backend downloads and
//    checks each one and keeps the good ones as candidates (app/pictures.py). The owner picks
//    one (✓) and presses Apply. Nothing becomes a plugin's picture before that.
const PIC_SHOW = {
  all:       { label: 'All', test: () => true },
  waiting:   { label: 'Pictures found', test: (r) => !!r.suggestion },
  cant_open: { label: "Can't open here", test: (r) => !!r.host_issue },
  failed:    { label: 'Capture failed', test: (r) => { const c = captureFor(r); return !!c && !c.ok; } },
  can_open:  { label: 'Can be captured', test: (r) => !r.host_issue },
};
let picShow = storedPick('pa-pic-show', Object.keys(PIC_SHOW), 'all');
const picChoice = new Map();   // plugin id -> the candidate (suggestion id) its Apply uses

const sugUrl = (sid, variant = '') =>
  `${API_BASE}/api/pictures/suggestions/${sid}/image${variant ? `?variant=${variant}` : ''}`;

function chosenCandidate(r) {
  const list = r.suggestion || [];
  const want = picChoice.get(r.id);
  return list.find(c => c.id === want) || list[0] || null;
}

function pictureChoices(rows) {
  const out = {};
  rows.forEach(r => { const c = chosenCandidate(r); if (c) out[r.id] = c.id; });
  return out;
}

const PIC_CHECK = '<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true"><path d="M3.5 8.5l3 3 6-7" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';

// A candidate's label: "Likely" when the AI could tell it shows the plugin's own interface,
// "Possible: <reason>" otherwise (pages VEXT read itself, news headers, store images, videos).
function pictureTier(c) {
  if (c.tier === 'verified') return { cls: 'is-likely', text: 'Likely', title: c.note || 'The AI could tell it shows this plugin' };
  const why = (c.note || '').trim();
  return { cls: 'is-possible', text: why ? `Possible: ${why}` : 'Possible', title: why || 'Likely shows it, not confirmed' };
}

// A row tried before that a group run should try again: the plugin got a store or KVR page the
// last search didn't read (the backend also retries one searched without an AI when an AI is used).
function pictureWorthRetry(r) {
  const s = r.search;
  if (!s || s.found) return false;
  const read = s.pages || [];
  return [r.store_url, r.kvr_url].some(u => u && !read.includes(u));
}

function pictureCandidatesHtml(r) {
  if (!r.suggestion) return '';
  const chosen = chosenCandidate(r);
  const n = r.suggestion.length;
  const s = r.search || {};
  const why = [s.confidence ? `confidence: ${escHtml(s.confidence)}` : '', s.notes ? escHtml(s.notes) : ''].filter(Boolean).join(' · ');
  const likely = r.suggestion.filter(c => c.tier === 'verified').length;
  const cands = r.suggestion.map(c => {
    const on = !!chosen && c.id === chosen.id;
    const src = c.page_url || c.image_url;
    const tier = pictureTier(c);
    return `<figure class="pic-cand${on ? ' is-chosen' : ''}">
      <button type="button" class="pic-cand-pick" data-act="pick" data-id="${r.id}" data-sid="${c.id}" aria-pressed="${on}" title="Use this picture">
        <span class="plugin-pic" style="--pic-backdrop: url('${escHtml(sugUrl(c.id, 'backdrop'))}')"><img src="${escHtml(sugUrl(c.id, 'thumb'))}" alt="Picture ${c.n} found for ${escHtml(r.name)}" loading="lazy" decoding="async"></span>
        <span class="pic-cand-check">${PIC_CHECK}</span>
      </button>
      <span class="pic-tier ${tier.cls}" title="${escHtml(tier.title)}">${escHtml(tier.text)}</span>
      <figcaption>
        <a class="vendor-link" href="${escHtml(src)}" target="_blank" rel="noopener noreferrer" title="Found on ${escHtml(src)}">${escHtml(linkHost(src))}</a>
        <span class="muted">${c.width}×${c.height}${c.kind && !['interface', 'other'].includes(c.kind) ? ' · ' + escHtml(c.kind) : ''}</span>
        <button type="button" class="pic-cand-zoom" data-act="zoom" data-id="${r.id}" data-sid="${c.id}" title="View larger" aria-label="View picture ${c.n} larger">
          <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M9.5 2.5h4v4M13.5 2.5L9 7M6.5 13.5h-4v-4M2.5 13.5L7 9"/></svg>
        </button>
      </figcaption>
    </figure>`;
  }).join('');
  return `<div class="review-suggestion pic-suggestion">
    <div class="pic-sug-title">Found ${n === 1 ? 'one picture' : `${n} pictures`}${likely ? ` (${likely} likely)` : ''}. Choose one with <strong>Choose picture…</strong>, or mark one (✓) and Apply.</div>
    <div class="pic-cands">${cands}</div>
    ${why ? `<div class="why">${why}</div>` : ''}
  </div>`;
}

function pictureRowHtml(r) {
  const issue = r.host_issue;
  const cap = captureFor(r);
  const flags = [];
  if (issue) flags.push(`<span class="tag pic-flag is-warn">Can't open here: ${escHtml(issue.reason)}</span>`);
  if (cap && !cap.ok) flags.push(`<span class="tag pic-flag is-bad">Capture failed${cap.reason ? `: ${escHtml(cap.reason)}` : ''}</span>`);
  let searched = '';
  if (!r.suggestion && r.search) {
    const rej = r.search.rejected || [];
    const how = r.search.ai_used ? '' : (r.search.pages && r.search.pages.length ? ' (store / KVR pages only, no AI)' : '');
    const retry = pictureWorthRetry(r) ? ' It has a new store or KVR page since, so a group search will try it again.' : '';
    searched = `<div class="path">Searched ${escHtml(shortDate(r.search.at))}${how}: no usable picture.${r.search.notes ? ' ' + escHtml(r.search.notes) : ''}${retry}</div>` +
      (rej.length ? `<details class="pic-rejected"><summary>${rej.length} proposed, not kept</summary><ul>${rej.map(x =>
        `<li><a class="vendor-link" href="${escHtml(x.url)}" target="_blank" rel="noopener noreferrer" title="${escHtml(x.url)}">${escHtml(linkHost(x.url))}</a>: ${escHtml(x.reason)}</li>`).join('')}</ul></details>` : '');
  } else if (!r.suggestion && r.investigated_at) {
    searched = `<div class="path">Searched ${escHtml(shortDate(r.investigated_at))}: nothing found.</div>`;
  }
  const running = !!(capture.status && capture.status.running);
  const buttons = [];
  if (r.suggestion) {
    buttons.push(`<button class="primary" data-act="choose" data-id="${r.id}" title="Compare the pictures found, large, and pick one">Choose picture…</button>`);
    buttons.push(`<button data-act="apply" data-id="${r.id}" title="Use the picture marked ✓">Apply</button>`);
    buttons.push(`<button data-act="discard" data-id="${r.id}">Discard</button>`);
  }
  if (!issue) {
    buttons.push(`<button data-act="capture" data-id="${r.id}" ${capture.available === false || running ? 'disabled' : ''} ` +
      `title="Opens it in the background and saves a picture of its window">Capture</button>`);
  }
  const again = !!(r.suggestion || r.investigated_at);
  buttons.push(`<button data-act="investigate" data-id="${r.id}" title="${again
    ? 'Reads its store and KVR pages again and asks the AI to look on the web'
    : 'Reads its store and KVR pages; the AI looks on the web if they have no picture'}">${again ? 'Find online again' : 'Find online'}</button>`);
  buttons.push(`<button data-act="details" data-id="${r.id}">Details</button>`);
  buttons.push(rowMenuBtn(r.id));
  return `
    <div class="candidate-card pic-row" data-row-target="image" data-row-id="${r.id}" data-plugin-id="${r.id}">
      <div class="info">
        <span class="review-row-mark"></span><span class="pic-cap-mark"></span>${storeIcon(r)}<strong>${escHtml(r.name)}</strong> <span class="badge">${escHtml(r.format)}</span>${flCloudBadge(r)} — ${r.vendor ? escHtml(r.vendor) : 'unknown vendor'}
        ${flags.length ? `<div class="pic-flags">${flags.join('')}</div>` : ''}
        ${searched}
        ${pictureCandidatesHtml(r)}
        <div class="review-row-error" hidden></div>
      </div>
      <div class="actions-inline">${buttons.join('')}</div>
    </div>`;
}

function renderPictureToolbar() {
  const rows = reviewData.picture || [];
  const waiting = rows.filter(r => r.suggestion).length;
  document.getElementById('review-picture-toolbar').innerHTML = `
    <input type="text" class="grow" data-role="filter" placeholder="Search this list…" value="${escHtml(reviewFilter.picture || '')}" aria-label="Search this list">
    <select data-role="pic-show" aria-label="Which plugins to list">${Object.entries(PIC_SHOW).map(([k, v]) =>
      `<option value="${k}" ${k === picShow ? 'selected' : ''}>${v.label} (${rows.filter(v.test).length})</option>`).join('')}</select>
    <select data-role="chunk" aria-label="How many to look up at a time">${chunkOptions(chunkPref('picture'))}</select>
    <button class="primary" data-role="investigate-chunk" title="Reads the store and KVR pages of the next plugins listed that weren't looked up yet; the AI looks on the web for the ones without a picture there">Find pictures online</button>
    <button data-role="apply-all" ${waiting ? '' : 'disabled'} title="Each plugin gets the picture marked ✓ on its row">Apply chosen pictures (${waiting})</button>
    ${progressBlock('image')}`;
}

// Row buttons only this list has. Returns true when it handled the click.
async function pictureRowAction(act, btn, id) {
  const row = (reviewData.picture || []).find(r => r.id === id);
  if (act === 'pick') {
    picChoice.set(id, parseInt(btn.dataset.sid, 10));
    btn.closest('.pic-cands').querySelectorAll('.pic-cand').forEach(fig => {
      const on = fig.contains(btn);
      fig.classList.toggle('is-chosen', on);
      fig.querySelector('.pic-cand-pick').setAttribute('aria-pressed', String(on));
    });
    return true;
  }
  if (act === 'choose') {
    const c = row && chosenCandidate(row);
    openPicturePicker(id, { selected: c ? c.id : null });
    return true;
  }
  if (act === 'zoom') {
    const sid = parseInt(btn.dataset.sid, 10);
    const c = row && (row.suggestion || []).find(x => x.id === sid);
    const src = c ? (c.page_url || c.image_url) : '';
    showPictureLightbox(sugUrl(sid), c ? `${escHtml(row.name)} · ${c.width}×${c.height} · from ` +
      `<a class="vendor-link" href="${escHtml(src)}" target="_blank" rel="noopener noreferrer">${escHtml(linkHost(src))}</a>` : '');
    return true;
  }
  if (act === 'capture') { await startCapture([id]); return true; }
  if (act === 'investigate') {
    // A row's search always runs. Again = the AI is asked even if the pages give pictures.
    const again = !!(row && (row.suggestion || row.investigated_at));
    await startReviewBatch('image', [id], 'plugin', null, again ? { ask_ai: true } : {});
    return true;
  }
  if (act === 'apply' && row) {
    const c = chosenCandidate(row);
    if (!c) return true;
    try {
      const res = await reviewPost('/api/review/picture/apply', { ids: [id], choices: { [id]: c.id } });
      if (res.skipped && res.skipped.length) { toast(res.skipped[0].reason, true); return true; }
      toast(`Picture set for ${row.name}.`);
      picChoice.delete(id);
      await fxLeave(btn.closest('.candidate-card'));
      await loadReview('picture');
      loadStats();
      loadPictureIndex();
    } catch (e) { handleActionError(e); }
    return true;
  }
  return false;
}

// ---------- the picture picker (Missing picture rows and the plugin sheet) ----------
// Every candidate found for one plugin, large and whole on its blurred backdrop, with its label
// ("Likely" / "Possible: <reason>"), the site it came from and its size. Select one (click or the
// arrow keys), then Apply (Enter) makes it the plugin's picture, or Discard drops them all. Esc
// closes without changing anything. `opts.selected`: the candidate to start on; `opts.onDone()`:
// runs after Apply or Discard (the sheet uses it to refresh itself).
async function openPicturePicker(pluginId, opts = {}) {
  let data;
  try { data = await api(`/api/pictures/candidates/${pluginId}`); } catch (e) { handleActionError(e); return; }
  const cands = data.suggestion || [];
  if (!cands.length) { toast('No pictures are waiting for this plugin. Use Find online first.'); return; }
  let index = Math.max(0, cands.findIndex(c => c.id === (opts.selected ?? picChoice.get(pluginId))));
  // Its own layer, above the plugin sheet and below confirm dialogs (Discard asks first).
  let root = document.getElementById('picker-root');
  if (!root) { root = document.createElement('div'); root.id = 'picker-root'; document.body.appendChild(root); }
  const s = data.search || {};
  root.innerHTML = `
    <div class="modal-backdrop pic-picker-backdrop" id="pic-picker">
      <div class="modal pic-picker" role="dialog" aria-modal="true" aria-labelledby="pic-picker-title">
        <div class="pic-picker-head">
          <h2 id="pic-picker-title">Choose a picture for ${escHtml(data.name)}</h2>
          <button type="button" class="close-x" data-pp="close" aria-label="Close">✕</button>
        </div>
        <p class="hint">${cands.length === 1 ? 'One picture was found.' : `${cands.length} pictures were found.`} Select one and press Apply (Enter). Arrow keys move between them, Esc closes.
          ${data.has_picture ? ' This plugin got a picture since (a capture?), and Apply never replaces one: discard these, or delete that picture first.' : ''}</p>
        <div class="pic-picker-grid" role="listbox" aria-label="Pictures found">${cands.map((c, i) => {
          const tier = pictureTier(c);
          const src = c.page_url || c.image_url;
          return `<div class="pic-pick-item" role="option" tabindex="-1" data-i="${i}" aria-selected="false">
            <span class="plugin-pic" style="--pic-backdrop: url('${escHtml(sugUrl(c.id, 'backdrop'))}')"><img src="${escHtml(sugUrl(c.id))}" alt="Picture ${i + 1} found for ${escHtml(data.name)}" decoding="async"></span>
            <span class="pic-cand-check">${PIC_CHECK}</span>
            <div class="pic-pick-meta">
              <span class="pic-tier ${tier.cls}" title="${escHtml(tier.title)}">${escHtml(tier.text)}</span>
              <span class="pic-pick-line"><a class="vendor-link" href="${escHtml(src)}" target="_blank" rel="noopener noreferrer" title="${escHtml(src)}">${escHtml(linkHost(src))}</a>
                <span class="muted">${c.width}×${c.height}${c.note === 'Cropped' ? ' · cropped' : c.origin === 'page' ? ' · read by VEXT' : (c.origin === 'ai' ? ' · found by the AI' : (c.origin === 'link' ? (c.note === 'From the image search' ? ' · image search' : ' · your link') : ''))}</span></span>
            </div>
          </div>`;
        }).join('')}</div>
        ${s.notes ? `<p class="hint pic-picker-notes">${escHtml(s.notes)}</p>` : ''}
        <div class="actions-row">
          <button data-pp="discard" title="Drops every picture found for this plugin">Discard all</button>
          <span class="grow"></span>
          <button data-pp="crop" title="Keep only part of the selected picture (the plugin's window, without the rest of the page). The original stays.">Crop…</button>
          <button data-pp="close">Cancel</button>
          <button class="primary" data-pp="apply">Apply</button>
        </div>
      </div>
    </div>`;
  const items = [...root.querySelectorAll('.pic-pick-item')];
  const select = (i, focus = true) => {
    index = (i + items.length) % items.length;
    items.forEach((el, k) => { el.classList.toggle('is-chosen', k === index); el.setAttribute('aria-selected', String(k === index)); el.tabIndex = k === index ? 0 : -1; });
    if (focus) { items[index].focus({ preventScroll: true }); items[index].scrollIntoView({ block: 'nearest' }); }
  };
  const finish = () => { document.removeEventListener('keydown', onKey, true); root.innerHTML = ''; };
  const afterChange = async () => {
    picChoice.delete(pluginId);
    loadStats();
    loadPictureIndex();
    if (reviewData.picture) await loadReview('picture');
    if (opts.onDone) opts.onDone();
  };
  const apply = async () => {
    const c = cands[index];
    try {
      const res = await reviewPost('/api/review/picture/apply', { ids: [pluginId], choices: { [pluginId]: c.id } });
      if (res.skipped && res.skipped.length) { toast(res.skipped[0].reason, true); return; }
      finish();
      toast(`Picture set for ${data.name}.`);
      await afterChange();
    } catch (e) { handleActionError(e); }
  };
  // The selected picture cropped: a new candidate beside the original, selected in the picker again.
  const crop = async () => {
    const c = cands[index];
    const box = await cropDialog(sugUrl(c.id), `Crop the picture for ${data.name}`);
    if (!box) return;
    try {
      const res = await api(`/api/pictures/suggestions/${c.id}/crop`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(box),
      });
      finish();
      toast(`Cropped to ${res.width} × ${res.height}. The original is still there to compare.`);
      openPicturePicker(pluginId, { ...opts, selected: res.id });
    } catch (e) { handleActionError(e); }
  };
  const discard = async () => {
    if (!(await confirmDialog(`Discard the ${cands.length === 1 ? 'picture' : `${cands.length} pictures`} found for ${data.name}?`))) return;
    try {
      await reviewPost('/api/review/picture/discard', { ids: [pluginId] });
      finish();
      toast('Discarded.');
      await afterChange();
    } catch (e) { handleActionError(e); }
  };
  const onKey = (e) => {
    if (!root.querySelector('#pic-picker')) { document.removeEventListener('keydown', onKey, true); return; }
    if (document.getElementById('confirm-backdrop')) return;  // a confirm on top answers its own keys
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); finish(); return; }
    if (e.key === 'ArrowRight' || e.key === 'ArrowDown') { e.preventDefault(); select(index + 1); return; }
    if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') { e.preventDefault(); select(index - 1); return; }
    if (e.key === 'Enter' && !e.target.closest('button, a')) { e.preventDefault(); apply(); }
  };
  document.addEventListener('keydown', onKey, true);
  root.querySelector('#pic-picker').addEventListener('click', (e) => {
    if (e.target.id === 'pic-picker') { finish(); return; }
    const act = e.target.closest('[data-pp]');
    if (act) {
      if (act.dataset.pp === 'close') finish();
      else if (act.dataset.pp === 'apply') apply();
      else if (act.dataset.pp === 'discard') discard();
      else if (act.dataset.pp === 'crop') crop();
      return;
    }
    if (e.target.closest('a')) return;
    const item = e.target.closest('.pic-pick-item');
    if (item) select(parseInt(item.dataset.i, 10));
  });
  root.querySelector('#pic-picker').addEventListener('dblclick', (e) => {
    const item = e.target.closest('.pic-pick-item');
    if (item && !e.target.closest('a')) { select(parseInt(item.dataset.i, 10)); apply(); }
  });
  select(index);
}

// ---------- automatic capture: the plugin host's job ----------
// POST /api/pictures/capture {ids?, only_missing: true} starts it (or answers with the job already
// running); GET polls it: {running, total, done, ok, failed, current, started_at, finished_at,
// cancelled, results: [{id, name, ok, reason}]}; DELETE cancels it. A 404 means this build has no
// capture job: the panel then says so instead of offering it.
const capture = { available: null, status: null, timer: null, wasRunning: false, results: new Map() };
const picPaneOpen = () => activeSection === 'actions' && actionsGroup === 'metadata' && metadataChip === 'picture';

// A plugin's capture result: from the plugin itself when the backend reports it, else from the last job.
function captureFor(r) {
  return r.picture_capture || capture.results.get(r.id) || null;
}

function applyCaptureStatus(s) {
  if (!s || typeof s !== 'object' || !('running' in s)) return;
  capture.status = s;
  capture.results = new Map((s.results || []).map(x => [x.id, x]));
  // Rows show how their capture went as it happens, without redrawing the whole list.
  document.querySelectorAll('#review-picture-list .pic-row').forEach(row => {
    const res = capture.results.get(parseInt(row.dataset.rowId, 10));
    const mark = row.querySelector('.pic-cap-mark');
    if (mark) mark.innerHTML = !res ? '' : res.ok
      ? '<span class="tag pic-flag is-ok">Captured</span>' : '<span class="tag pic-flag is-bad">Capture failed</span>';
    const btn = row.querySelector('button[data-act="capture"]');
    if (btn) btn.disabled = !!s.running || capture.available === false;
  });
}

async function loadCaptureStatus() {
  clearTimeout(capture.timer);
  try {
    applyCaptureStatus(await api('/api/pictures/capture'));
    capture.available = true;
  } catch (e) {
    if (e.status === 404 || e.status === 405) capture.available = false;
  }
  const s = capture.status;
  if (s && s.running) {
    capture.wasRunning = true;
    if (picPaneOpen()) capture.timer = setTimeout(loadCaptureStatus, 1500);
  } else if (capture.wasRunning) {
    capture.wasRunning = false;
    if (s) toast(`${s.cancelled ? 'Capture cancelled' : 'Capture finished'}: ${s.ok || 0} picture${s.ok === 1 ? '' : 's'} saved${s.failed ? `, ${s.failed} failed` : ''}.`);
    loadPictureIndex();
    loadStats();
    if (picPaneOpen()) { loadReview('picture'); return; }
  }
  renderCaptureBlock();
}

async function startCapture(ids) {
  try {
    const body = ids ? { ids, only_missing: true } : { only_missing: true };
    const s = await api('/api/pictures/capture', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    capture.available = true;
    applyCaptureStatus(s);
    if (s && s.running) {
      toast(`Capturing ${s.total} plugin${s.total === 1 ? '' : 's'}, one at a time. Their windows may flash on screen.`);
    } else {
      toast('Nothing to capture: every plugin listed already has a picture or can\'t open here.');
    }
  } catch (e) {
    if (e.status === 404 || e.status === 405) {
      capture.available = false;
      renderCaptureBlock();
      toast('Automatic capture isn\'t available in this version of VEXT yet.', true);
      return;
    }
    handleActionError(e);
  }
  loadCaptureStatus();
}

// The job's end (loadCaptureStatus) says how it went: "Capture cancelled: N pictures saved…".
async function cancelCapture() {
  try {
    applyCaptureStatus(await api('/api/pictures/capture', { method: 'DELETE' }));
  } catch (e) { handleActionError(e); }
  loadCaptureStatus();
}

function renderCaptureBlock() {
  const el = document.getElementById('pic-capture');
  if (!el) return;
  const rows = reviewData.picture || [];
  const capturable = rows.filter(r => !r.host_issue);
  const listed = reviewVisibleRows('picture').filter(r => !r.host_issue);
  const s = capture.status;
  const running = !!(s && s.running);
  const off = capture.available === false;
  const allBtn = `<button class="primary" data-cap="all" ${off || running || !capturable.length ? 'disabled' : ''}>Capture all that can open (${capturable.length})</button>`;
  const listedBtn = listed.length && listed.length !== capturable.length
    ? `<button data-cap="listed" ${off || running ? 'disabled' : ''}>Capture the ${listed.length} listed</button>` : '';
  let progress = '';
  if (s && s.total && (s.running || s.finished_at)) {
    const done = s.done || 0;
    const line = s.running
      ? `${done} of ${s.total} · ${s.ok || 0} captured · ${s.failed || 0} failed${s.current ? ` · now: ${escHtml(s.current)}` : ''}`
      : `${s.cancelled ? 'Cancelled' : 'Finished'}${s.finished_at ? ' ' + escHtml(new Date(s.finished_at).toLocaleString()) : ''}: ` +
        `${s.ok || 0} captured, ${s.failed || 0} failed${s.cancelled && s.total > done ? `, ${s.total - done} not tried` : ''}.`;
    progress = `<div class="pic-cap-progress">
      <div class="review-progress-row"><div class="batch-progress" aria-hidden="true"><span style="width:${Math.round(done / s.total * 100)}%"></span></div>
        ${running ? '<button class="danger" data-cap="cancel" title="Stops after the plugin being captured now">Cancel</button>' : ''}</div>
      <p class="hint" aria-live="polite">${line}</p></div>`;
  }
  const failed = s ? (s.results || []).filter(x => !x.ok) : [];
  const failures = failed.length ? `<details class="pic-cap-failed" ${running ? '' : 'open'}>
      <summary>${failed.length} couldn't be captured. An AI can look for their pictures online instead.</summary>
      <ul>${failed.map(x => `<li><span class="pic-cap-name">${escHtml(x.name)}</span><span class="muted">${escHtml(x.reason || 'no reason given')}</span>
        <button data-cap="find" data-id="${x.id}">Find online</button></li>`).join('')}</ul>
      ${failed.length > 1 ? `<button data-cap="find-all">Find all ${failed.length} online</button>` : ''}
    </details>` : '';
  el.innerHTML = `
    <div class="pic-cap-head">
      <div class="pic-cap-text">
        <h3>Capture automatically</h3>
        <p class="hint">${off
          ? 'Automatic capture isn\'t available in this version of VEXT yet. A plugin can still be captured by hand: open it in the Playground, then use Capture › Set as plugin picture in its window.'
          : 'VEXT opens each plugin that can open here, one at a time, saves a picture of its window and closes it. Their windows may flash on screen while it works. A plugin that can\'t be captured is listed with the reason, so an AI can look for its picture online instead.'}</p>
      </div>
      <div class="pic-cap-actions">${allBtn}${listedBtn}</div>
    </div>
    ${progress}${failures}`;
}

document.getElementById('pic-capture').addEventListener('click', async (e) => {
  const b = e.target.closest('button[data-cap]');
  if (!b) return;
  const what = b.dataset.cap;
  if (what === 'all') {
    const n = (reviewData.picture || []).filter(r => !r.host_issue).length;
    if (!(await confirmDialog(`Capture ${n} plugin${n === 1 ? '' : 's'} automatically?\n\nVEXT opens them one at a time and saves a picture of each window. Their windows may flash on screen while it works.`))) return;
    startCapture(null);
  } else if (what === 'listed') {
    const ids = reviewVisibleRows('picture').filter(r => !r.host_issue).map(r => r.id);
    if (!(await confirmDialog(`Capture the ${ids.length} listed plugin${ids.length === 1 ? '' : 's'} automatically?\n\nTheir windows may flash on screen while it works.`))) return;
    startCapture(ids);
  } else if (what === 'cancel') {
    cancelCapture();
  } else if (what === 'find') {
    startReviewBatch('image', [parseInt(b.dataset.id, 10)], 'plugin');
  } else if (what === 'find-all') {
    const ids = ((capture.status && capture.status.results) || []).filter(x => !x.ok).map(x => x.id);
    if (!(await confirmDialog(`Look for pictures of ${ids.length} plugins online?\n\nThat is ${ids.length} AI calls. What they find waits here until you apply it.`))) return;
    startReviewBatch('image', ids, 'plugins');
  }
});

// ---------- vendor websites and status (inside Missing vendor) ----------
let vendorSites = { pending: [], missing: [] };

function vendorSiteFormHtml(v, kind) {
  const name = escHtml(v.vendor);
  const status = v.status || '';
  const opt = (val, label) => `<option value="${val}" ${status === val ? 'selected' : ''}>${label}</option>`;
  const inputs = linkInputsHtml([
    { field: 'url', label: 'Website', hint: "https://… the vendor's official website", flag: 'no_url', value: v.url, none: !v.url && !!v.no_url },
    { field: 'kvr_url', label: 'KVR', hint: 'https://… KVR Audio page', flag: 'no_kvr', value: v.kvr_url, none: !v.kvr_url && !!v.no_kvr },
    { field: 'equipboard_url', label: 'Equipboard', hint: 'https://… Equipboard page', flag: 'no_equipboard', value: v.equipboard_url, none: !v.equipboard_url && !!v.no_equipboard },
  ]);
  return `<div class="manual-edit" hidden>${inputs}
    <select data-field="status" aria-label="Vendor status">${opt('', 'Status: unknown')}${opt('active', 'Active')}${opt('deprecated', 'Deprecated')}${opt('down', 'Down')}</select>
    <button class="primary" data-site-act="save-manual" data-vendor="${name}">Save</button>
    <button data-site-act="manual" data-vendor="${name}">Cancel</button></div>`;
}

const VENDOR_LINKS = [['url', 'Website', 'no_url'], ['kvr_url', 'KVR', 'no_kvr'], ['equipboard_url', 'Equipboard', 'no_equipboard']];

// Same three states as a plugin's links. `pending` shows an AI answer that hasn't been applied: a link it left
// empty will become a "none" mark on Apply (unless the vendor already had that link or mark).
function vendorChips(v, pending = false) {
  const cur = pending ? (v.current || {}) : v;
  return VENDOR_LINKS.map(([f, label, flag]) => {
    const url = (pending ? v[f] : v[f]) || (pending ? cur[f] : null);
    if (url) return `<a class="tag link-chip vendor-link" href="${escHtml(url)}" target="_blank" rel="noopener noreferrer" title="${escHtml(url)}">${label}</a>`;
    if (cur[flag]) return `<span class="tag link-none" title="Looked: this site has no page. Settled, not missing.">No ${label}</span>`;
    if (pending) return `<span class="tag link-none" title="The AI looked and found none. Apply marks it as none.">No ${label} (on apply)</span>`;
    return `<span class="tag link-missing" title="Not investigated yet.">${label} ?</span>`;
  }).join('') + ` <span class="tag">${escHtml(v.status || (pending ? (v.current || {}).status : '') || 'status unknown')}</span>`;
}

function vendorSiteRowHtml(v, kind) {
  const name = escHtml(v.vendor);
  const plugins = `${v.plugins} plugin${v.plugins === 1 ? '' : 's'}`;
  if (kind === 'pending') {
    const cur = v.current || {};
    const form = {
      vendor: v.vendor, status: v.status || cur.status,
      url: v.url || cur.url, kvr_url: v.kvr_url || cur.kvr_url, equipboard_url: v.equipboard_url || cur.equipboard_url,
      no_url: cur.no_url, no_kvr: cur.no_kvr, no_equipboard: cur.no_equipboard,
    };
    return `<div class="candidate-card" data-row-target="vendors" data-row-id="${name}">
      <div class="info"><span class="review-row-mark"></span><strong>${name}</strong> <span class="badge muted">${plugins}</span>
        <div class="review-suggestion">The AI found: ${vendorChips(v, true)}${v.notes ? `<div class="why">${escHtml(v.notes)}</div>` : ''}</div>
        ${vendorSiteFormHtml(form, kind)}
        <div class="review-row-error" hidden></div></div>
      <div class="actions-inline">
        <button class="primary" data-site-act="apply" data-vendor="${name}">Apply</button>
        <button data-site-act="discard" data-vendor="${name}">Discard</button>
        <button data-site-act="manual" data-vendor="${name}">Enter manually</button>
        <button data-site-act="investigate" data-vendor="${name}">Investigate again</button>
      </div></div>`;
  }
  return `<div class="candidate-card" data-row-target="vendors" data-row-id="${name}">
    <div class="info"><span class="review-row-mark"></span><strong>${name}</strong> <span class="badge muted">${plugins}</span>
      <div class="path">${vendorChips(v)}</div>
      ${v.investigated_at ? `<div class="path">Investigated ${escHtml(shortDate(v.investigated_at))}: nothing more found. Investigate again to retry.</div>` : ''}
      ${vendorSiteFormHtml(v, kind)}
      <div class="review-row-error" hidden></div></div>
    <div class="actions-inline">
      <button data-site-act="manual" data-vendor="${name}">Enter manually</button>
      <button data-site-act="investigate" data-vendor="${name}">${v.investigated_at ? 'Investigate again' : 'Investigate'}</button></div></div>`;
}

async function loadVendorSites() {
  try { vendorSites = await api('/api/review/vendor_sites'); } catch (e) { handleActionError(e); return; }
  const bar = document.getElementById('review-vendor-sites-toolbar');
  const list = document.getElementById('review-vendor-sites-list');
  const w = vendorSites.pending.length;
  bar.innerHTML = `
    <span class="badge muted">${vendorSites.missing.filter(v => !v.investigated_at).length} to investigate · ${vendorSites.missing.length} incomplete · ${w} waiting</span>
    <select data-role="chunk" aria-label="How many vendors to investigate at a time">${chunkOptions(chunkPref('vendor-sites'))}</select>
    <button class="primary" data-role="investigate-chunk">Investigate with AI</button>
    <button class="primary" data-role="apply-all" ${w ? '' : 'disabled'}>Apply all (${w})</button>
    ${progressBlock('vendors')}`;
  const rows = [...vendorSites.pending.map(v => vendorSiteRowHtml(v, 'pending')), ...vendorSites.missing.map(v => vendorSiteRowHtml(v, 'missing'))];
  list.innerHTML = rows.length ? rows.join('') : '<div class="empty-state">Every vendor has a website, a status and both database pages.</div>';
  refreshBatch();
}

document.getElementById('review-vendor-sites-toolbar').addEventListener('change', (e) => {
  if (e.target.dataset.role === 'chunk') setChunkPref('vendor-sites', parseInt(e.target.value, 10));
});
document.getElementById('review-vendor-sites-toolbar').addEventListener('click', async (e) => {
  const btn = e.target.closest('button[data-role]');
  if (!btn) return;
  try {
    if (btn.dataset.role === 'stop') { await stopInvestigation(); return; }
    if (btn.dataset.role === 'investigate-chunk') {
      const todo = vendorSites.missing.filter(v => !v.investigated_at);
      if (!todo.length) { toast('Nothing new to investigate here. Use a row\'s Investigate again to retry one.'); return; }
      const chunk = chunkPref('vendor-sites');
      const names = (chunk ? todo.slice(0, chunk) : todo).map(v => v.vendor);
      if (!(await confirmDialog(`Investigate ${names.length} vendor${names.length === 1 ? '' : 's'} with AI?\n\nThat is ${names.length} AI call${names.length === 1 ? '' : 's'}. Results wait here for your review.`))) return;
      startReviewBatch('vendors', names, `vendor${names.length === 1 ? '' : 's'}`);
    } else if (btn.dataset.role === 'apply-all') {
      const n = vendorSites.pending.length;
      if (!n || !(await confirmDialog(`Apply all ${n} vendor website${n === 1 ? '' : 's'}?\n\nEach updates every plugin of that vendor. No AI cost.`))) return;
      const res = await reviewPost('/api/investigations/apply-all', { target: 'vendors' });
      toast(`Applied ${res.applied}.`);
      loadVendorSites(); loadStats();
    }
  } catch (err) { handleActionError(err); }
});
document.getElementById('review-vendor-sites-list').addEventListener('click', async (e) => {
  const btn = e.target.closest('button[data-site-act]');
  if (!btn) return;
  const vendor = btn.dataset.vendor;
  try {
    if (btn.dataset.siteAct === 'investigate') { await startReviewBatch('vendors', [vendor], 'vendor'); return; }
    if (btn.dataset.siteAct === 'manual') { toggleManualForm(btn.closest('.candidate-card')); return; }
    if (btn.dataset.siteAct === 'save-manual') {
      const form = btn.closest('.manual-edit');
      const val = (f) => form.querySelector(`[data-field="${f}"]`).value.trim();
      const flag = (f) => form.querySelector(`[data-flag="${f}"]`).checked;
      const url = val('url'), kvr = val('kvr_url'), eqb = val('equipboard_url');
      const status = form.querySelector('[data-field="status"]').value;
      const marks = { no_url: flag('no_url') && !url, no_kvr: flag('no_kvr') && !kvr, no_equipboard: flag('no_equipboard') && !eqb };
      if (!url && !kvr && !eqb && !status && !Object.values(marks).some(Boolean)) { toast('Enter a link or a status, or tick "No page", first.', true); return; }
      for (const link of [url, kvr, eqb]) {
        if (link && !/^https?:\/\//i.test(link)) { toast('Links must start with http:// or https://', true); return; }
      }
      const known = [...vendorSites.missing, ...vendorSites.pending].find(v => v.vendor === vendor) || {};
      await reviewPost('/api/reference/vendors', {
        name: vendor, url: url || null, status: status || null, status_note: known.status_note || null,
        kvr_url: kvr, equipboard_url: eqb,   // "" clears, so emptying a field removes the link
        ...marks,
      });
      // Typed by hand, so any researched answer still waiting for this vendor is now moot.
      await api(`/api/vendor-suggestions/${encodeURIComponent(vendor)}`, { method: 'DELETE' });
      toast('Vendor saved.');
      loadVendorSites(); loadStats();
      return;
    }
    if (btn.dataset.siteAct === 'apply') {
      await api(`/api/vendor-suggestions/${encodeURIComponent(vendor)}/apply`, { method: 'POST' });
      toast('Vendor updated.');
    } else {
      await api(`/api/vendor-suggestions/${encodeURIComponent(vendor)}`, { method: 'DELETE' });
    }
    loadVendorSites(); loadStats();
  } catch (err) { handleActionError(err); }
});

// ---------- AI batch progress, shown on whichever list started it ----------
function batchSummaryLine(s) {
  const c = s.counts || {};
  const finished = (c.done || 0) + (c.failed || 0) + (c.skipped || 0);
  return `${finished} of ${s.total} finished · ${c.running || 0} researching · ${c.queued || 0} queued · ${c.failed || 0} failed`;
}

// Did this finished item actually find anything? (If it did and the row shows no suggestion any more, the
// owner already applied or discarded it, so there is nothing to explain.)
function batchFoundSomething(target, sm) {
  if (!sm) return false;
  if (target === 'links') return true;   // a links run always leaves a suggestion: found links and/or "none" marks
  if (target === 'categories') return !!(sm.categories && sm.categories.length);
  if (target === 'types') return !!sm.plugin_type;
  if (target === 'vendor' || target === 'confirm_vendor') return !!sm.vendor;
  if (target === 'image') return !!sm.found;
  return true;
}

function paintBatch(s) {
  document.querySelectorAll('.review-progress').forEach(box => {
    const mine = s.total > 0 && s.target === box.dataset.progressTarget && (s.running || s.finished_at);
    box.hidden = !mine;
    if (!mine) return;
    const c = s.counts || {};
    const finished = (c.done || 0) + (c.failed || 0) + (c.skipped || 0);
    box.querySelector('.batch-progress span').style.width = (s.total ? Math.round(finished / s.total * 100) : 0) + '%';
    box.querySelector('.hint').textContent = batchSummaryLine(s);
  });
  document.querySelectorAll('.review-progress button[data-role="stop"]').forEach(b => { b.hidden = !s.running; b.disabled = !!s.stop_requested; b.textContent = s.stop_requested ? 'Stopping…' : 'Stop investigation'; });
  document.querySelectorAll('.review-toolbar button[data-role="investigate-chunk"], .section-actions button[data-role="investigate-chunk"]')
    .forEach(b => { b.disabled = !!s.running; });
  const byId = new Map((s.items || []).map(it => [String(it.id), it]));
  document.querySelectorAll(`[data-row-target="${s.target}"]`).forEach(row => {
    const it = byId.get(row.dataset.rowId);
    const mark = row.querySelector('.review-row-mark');
    const err = row.querySelector('.review-row-error');
    let html = '';
    if (it) {
      if (it.state === 'running') html = '<span class="spinner" role="img" aria-label="Researching"></span>';
      else if (it.state === 'queued') html = '<span class="state-dot queued" aria-label="Queued"></span>';
      else if (it.state === 'done') html = '<span class="state-mark ok" aria-hidden="true">✓</span>';
      else if (it.state === 'failed') html = '<span class="state-mark danger" aria-hidden="true">!</span>';
    }
    mark.innerHTML = html;
    // A finished run that left no suggestion on the row found nothing it could stand behind.
    // Say so, otherwise a green check with no Apply button looks like a bug.
    const noResult = !!it && it.state === 'done' && !row.querySelector('.review-suggestion') && !batchFoundSomething(s.target, it.summary);
    err.hidden = !(it && it.state === 'failed') && !noResult;
    if (it && it.state === 'failed') err.textContent = it.error || 'The AI could not research this item.';
    else if (noResult && s.target === 'image') {
      const sm = it.summary || {};
      err.textContent = `No usable picture found${sm.rejected ? ` (${sm.rejected} proposed, none could be used)` : ''}.${sm.notes ? ' ' + sm.notes : ''}`;
    } else if (noResult) {
      const notes = it.summary && it.summary.notes ? ' ' + it.summary.notes : '';
      err.textContent = 'The AI finished but found nothing reliable to apply.' + notes + ' Try Investigate again, another AI, or Enter manually.';
    } else err.textContent = '';
  });
}

async function refreshBatch() {
  clearTimeout(batchPollTimer);
  let s;
  try { s = await api('/api/ai/batch'); } catch { return; }
  paintBatch(s);
  if (s.running) {
    batchWasRunning = true;
    batchPollTimer = setTimeout(refreshBatch, 1200);
  } else if (batchWasRunning) {
    batchWasRunning = false;
    const failed = (s.counts || {}).failed || 0;
    toast(`${s.agent ? 'The AI' : 'VEXT'} finished: ${(s.counts || {}).done || 0} done${failed ? `, ${failed} failed` : ''}. Review the suggestions.`);
    // Reload the Metadata list on screen, if one is (the run may have started elsewhere).
    if (activeSection === 'actions' && actionsGroup === 'metadata' && REVIEW[metadataChip]) loadReview(metadataChip);
    loadStats();
  }
}

document.getElementById('ai-concurrency').addEventListener('change', async (e) => {
  const value = Math.min(8, Math.max(1, parseInt(e.target.value, 10) || 5));
  e.target.value = value;
  try {
    await api('/api/settings/ai_concurrency', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ value: String(value) }),
    });
  } catch (err) { handleActionError(err); }
});

// Opens with Configuration > AI: just the parallel-requests setting. The investigations live in Actions > Metadata.
async function initBatchPanel() {
  const s = await api('/api/settings');
  document.getElementById('ai-concurrency').value = s.ai_concurrency || 5;
}

// ---------- multivendor: a plugin's other vendors (max 5) ----------
// The main vendor stays the one that organizes the folder. These are the other companies
// involved (earlier developers, licensors, the company that bought it).
let efMain = null;
let efOthers = [];
let efMax = 5;

function renderEfOthers() {
  const el = document.getElementById('ef-others-block');
  if (!el) return;
  el.innerHTML = `
    <label>Other vendors involved (up to ${efMax})</label>
    <p class="hint">Main vendor: <strong>${efMain || 'none yet'}</strong>. It sets the vendor folder. Add the other companies involved in this plugin's history.</p>
    <div class="vendor-chips">${efOthers.map((v, i) => `
      <span class="tag vendor-chip">${v}<button type="button" class="chip-x" data-i="${i}" aria-label="Remove ${v.replace(/"/g, '&quot;')}">×</button></span>`).join('')}</div>
    <div class="fast-add-row">
      <input type="text" id="ef-other-add" placeholder="Another vendor…">
      <button type="button" id="ef-other-add-btn" ${efOthers.length >= efMax ? 'disabled' : ''}>Add</button>
    </div>`;
  el.querySelectorAll('.chip-x').forEach(b => b.onclick = () => { efOthers.splice(+b.dataset.i, 1); renderEfOthers(); });
  document.getElementById('ef-other-add-btn').onclick = () => {
    const input = document.getElementById('ef-other-add');
    const v = input.value.trim();
    if (!v) return;
    if (efOthers.length >= efMax) { toast(`Up to ${efMax} other vendors.`, true); return; }
    if (efMain && v.toLowerCase() === efMain.toLowerCase()) { toast('That is already the main vendor.', true); return; }
    efOthers.push(v);
    renderEfOthers();
    document.getElementById('ef-other-add').focus();
  };
}

async function loadEfOthers(pid) {
  const d = await api(`/api/plugins/${pid}/vendors`);
  efMain = d.main; efOthers = d.others; efMax = d.max;
  renderEfOthers();
}

async function saveEfOthers(pid) {
  await api(`/api/plugins/${pid}/vendors`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ vendors: efOthers }),
  });
}


// Browse: the column header pins right under the sticky filter block. Its height is measured
// so the offset stays right when the hint wraps or the window is resized.
(function pinBrowseHeader() {
  const block = document.getElementById('browse-sticky');
  if (!block || !window.ResizeObserver) return;
  const update = () => document.documentElement.style.setProperty('--browse-head-top', block.offsetHeight + 'px');
  new ResizeObserver(update).observe(block);
  update();
})();

} // end __VEXT_APP_LOADED__ guard
