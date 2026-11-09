// The toolbar's metrics (top right): VEXT's memory and the MIDI input indicator.
// Memory: GET /api/system/memory every 2.5 s (the whole app's private bytes, app/sysmem.py).
// MIDI: GET /api/midi/activity?since=<seq> 4x a second (app/midi_monitor.py); the icon
// lights up on each message, the popover shows the latest one.
// Both poll only while the window is visible.
(() => {
  const MEM_MS = 2500;
  const MIDI_MS = 250;
  const memEl = document.getElementById('tb-mem');
  const midiBtn = document.getElementById('tb-midi');
  const pop = document.getElementById('tb-midi-pop');
  const V = window.VEXT;
  if (!memEl || !midiBtn || !pop || !V) return;
  const memVal = memEl.querySelector('.tb-value');
  const midiLast = midiBtn.querySelector('.tb-value');

  const fmtBytes = (b) => {
    const mb = b / 1048576;
    return mb >= 1024 ? (mb / 1024).toFixed(mb >= 10240 ? 0 : 1) + ' GB' : Math.round(mb) + ' MB';
  };
  const PART_LABELS = { app: 'App & browser views', backend: 'Backend', plugins: 'Plugin hosts' };

  let memTimer = null;
  let memBusy = false;
  let midiBusy = false;
  async function pollMem() {
    memTimer = null;
    memBusy = true;
    try {
      const m = await V.api('/api/system/memory');
      memVal.textContent = fmtBytes(m.total);
      const lines = m.parts.filter(p => p.processes).map(p =>
        `${PART_LABELS[p.key] || p.key}: ${fmtBytes(p.bytes)} (${p.processes} process${p.processes === 1 ? '' : 'es'})`);
      memEl.title = `VEXT memory: ${fmtBytes(m.total)} (private bytes, all ${m.processes} processes)\n` + lines.join('\n');
      memEl.classList.remove('is-off');
    } catch (e) {
      memEl.classList.add('is-off');
    }
    memBusy = false;
    if (document.visibilityState === 'visible') memTimer = setTimeout(pollMem, MEM_MS);
  }

  let midiTimer = null;
  let seq = 0;
  let latest = null; // the last message seen (the popover shows only this one)
  let state = null;
  let fadeTimer = null;
  let first = true;

  const esc = (s) => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const shortText = (e) => e.text.replace(/^Note (On|Off) · /, '').replace(/ · ch \d+$/, '');
  const selectedNone = () => !state || !(state.selected === '*' || (Array.isArray(state.selected) && state.selected.length));

  function blink() {
    midiBtn.classList.remove('is-hit');
    void midiBtn.offsetWidth; // restart the animation
    midiBtn.classList.add('is-hit');
  }

  function render() {
    const none = selectedNone();
    midiBtn.classList.toggle('is-off', none);
    const failed = (state && state.failed) || [];
    const open = (state && state.open) || [];
    if (none) {
      midiBtn.title = 'No MIDI input selected (Configuration › MIDI)';
    } else if (!open.length && failed.length) {
      midiBtn.title = 'MIDI input unavailable: ' + failed.map(f => `${f.id} (${f.error})`).join(', ');
    } else {
      midiBtn.removeAttribute('title');
    }
    midiBtn.classList.toggle('is-warn', !none && !open.length && failed.length > 0);
    if (pop.hidden) return;
    const owner = state && state.owner === 'plugin' ? 'through the open plugin' : 'monitoring';
    const head = none
      ? '<div class="tb-pop-empty">No MIDI input selected. Choose one in Configuration › MIDI.</div>'
      : `<div class="tb-pop-head"><strong>${esc(open.join(', ') || 'No input open')}</strong>` +
        `<span>${open.length ? owner : ''}</span></div>` +
        failed.map(f => `<div class="tb-pop-fail">${esc(f.id)}: ${esc(f.error)}</div>`).join('');
    let body = '';
    if (!none) {
      if (latest) {
        const flags = (latest.flags || []).includes('filtered') ? '<span class="tb-flag">other channel</span>' : '';
        const hex = latest.bytes.map(b => b.toString(16).toUpperCase().padStart(2, '0')).join(' ');
        body = `<div class="tb-pop-last"><span class="tb-ev-text">${esc(latest.text)}</span>${flags}` +
          `<span class="tb-ev-meta">${esc(latest.device || '')} · ${hex}</span></div>`;
      } else {
        body = '<div class="tb-pop-empty">Play your MIDI device: messages show here.</div>';
      }
    }
    pop.innerHTML = head + body;
  }

  async function pollMidi() {
    midiTimer = null;
    midiBusy = true;
    try {
      const a = await V.api('/api/midi/activity?since=' + seq);
      const fresh = (a.seq < seq) ? [] : a.events; // the backend restarted: start over
      if (a.seq < seq) latest = null; // the backend restarted: start over
      seq = a.seq;
      state = a;
      if (fresh.length) {
        const last = fresh[fresh.length - 1];
        latest = last;
        if (!first) {
          blink();
          midiLast.textContent = shortText(last);
          midiBtn.classList.add('has-text');
          clearTimeout(fadeTimer);
          fadeTimer = setTimeout(() => midiBtn.classList.remove('has-text'), 2500);
        }
      }
      first = false;
      render();
    } catch (e) { /* the backend is restarting: try again */ }
    midiBusy = false;
    if (document.visibilityState === 'visible') midiTimer = setTimeout(pollMidi, MIDI_MS);
  }

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') {
      if (!memTimer && !memBusy) memTimer = setTimeout(pollMem, 0);
      if (!midiTimer && !midiBusy) midiTimer = setTimeout(pollMidi, 0);
    } else {
      clearTimeout(memTimer); memTimer = null;
      clearTimeout(midiTimer); midiTimer = null;
    }
  });

  // The popover: shown on hover, pinned by a click; Escape or a click elsewhere closes it.
  let pinned = false;
  let hoverTimer = null;
  const show = () => { pop.hidden = false; midiBtn.setAttribute('aria-expanded', 'true'); render(); };
  const hide = () => { if (pinned) return; pop.hidden = true; midiBtn.setAttribute('aria-expanded', 'false'); };
  const wrap = midiBtn.parentElement;
  wrap.addEventListener('mouseenter', () => { clearTimeout(hoverTimer); if (!selectedNone()) show(); });
  wrap.addEventListener('mouseleave', () => { clearTimeout(hoverTimer); hoverTimer = setTimeout(hide, 200); });
  midiBtn.addEventListener('click', () => {
    if (selectedNone()) {
      pinned = false; pop.hidden = true;
      V.navigate('config', { pane: 'midi' });
      return;
    }
    pinned = !pinned;
    if (pinned) show(); else { pop.hidden = true; midiBtn.setAttribute('aria-expanded', 'false'); }
  });
  document.addEventListener('click', (e) => {
    if (pinned && !wrap.contains(e.target)) { pinned = false; hide(); }
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !pop.hidden) { pinned = false; hide(); }
  });

  pollMem();
  pollMidi();
})();
