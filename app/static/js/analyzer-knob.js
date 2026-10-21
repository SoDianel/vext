// A knob for the Analyzer's fixed steps (VEXT.AnalyzerKnob). A 270-degree dial over a short list
// of values: drag up or down (one step per 6 px), the wheel, or the arrow keys move it between
// the steps; double-click (or Enter) types a value, which is clamped to min..max and kept as it
// is (a value between two steps is drawn at the place it falls). The caller decides what a change
// means: onChange gets the value, and the caller calls set() with the value it actually kept.
(function () {
  const V = window.VEXT;
  if (!V || V.AnalyzerKnob) return;

  const SWEEP = 270;          // degrees from the minimum to the maximum
  const DRAG_PX = 6;          // pixels of drag per step
  const CX = 12;              // the SVG's centre and radius (viewBox 24)
  const R = 9;

  // ISO 266 third-octave series, 10 Hz .. 20 kHz.
  const ISO = [
    10, 12.5, 16, 20, 25, 31.5, 40, 50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 630, 800,
    1000, 1250, 1600, 2000, 2500, 3150, 4000, 5000, 6300, 8000, 10000, 12500, 16000, 20000,
  ];
  const range = [];
  for (let v = -120; v <= 18; v += 6) range.push(v);
  const level = [];
  for (let v = -60; v <= 0; v += 3) level.push(v);
  const STEPS = {
    ISO,
    freqLo: [0, ...ISO.filter((f) => f <= 16000)],          // 0 = from DC
    freqHi: [...ISO.filter((f) => f >= 20), 22000, 24000, 30000],
    range,
    level,
    tone: ISO.filter((f) => f >= 20 && f <= 20000),
  };

  const minus = () => (V.AnalyzerDSP && V.AnalyzerDSP.MINUS) || '−';
  const round = (v, d) => {
    const f = 10 ** d;
    return Math.round(v * f) / f;
  };

  // Hz: 0, 31.5, 630, 1k, 1.25k, 20k (the unit is in the field's label, not in each value).
  function fmtFreq(f) {
    if (!(f >= 1000)) return String(round(f, 1));
    return `${round(f / 1000, 3)}k`;
  }

  // dB: a true minus sign; a whole number as it is, one decimal for a typed fraction.
  function fmtDb(v) {
    if (Number.isInteger(v)) return v < 0 ? `${minus()}${-v}` : String(v);
    const s = Math.abs(round(v, 1)).toFixed(1);
    return v < 0 && s !== '0.0' ? `${minus()}${s}` : s;
  }

  // Typed text to a number: a comma or a dot, a true or an ASCII minus, a trailing k or kHz (x 1000).
  function parseText(text) {
    const s = String(text).trim().replace(/−/g, '-').replace(',', '.');
    const n = parseFloat(s);
    if (!Number.isFinite(n)) return NaN;
    return /k(hz)?$/i.test(s) ? Number((n * 1000).toPrecision(12)) : n;
  }

  function create(o) {
    const values = o.values;
    const last = values.length - 1;
    const min = Number.isFinite(o.min) ? o.min : values[0];
    const max = Number.isFinite(o.max) ? o.max : values[last];
    const format = o.format || String;
    const parse = o.parse || parseText;
    const name = o.ariaLabel || o.label || 'Value';
    let onChange = o.onChange;
    let value = Number.isFinite(o.value) ? o.value : values[0];
    let disabled = false;
    let destroyed = false;
    let editor = null;
    let cancelEdit = null;
    let drag = null;

    const el = document.createElement('div');
    el.className = 'ak';
    el.setAttribute('role', 'slider');
    el.tabIndex = 0;
    el.title = 'Drag up or down, or use the wheel or the arrow keys. Double-click to type a value.';
    el.setAttribute('aria-label', name);
    el.setAttribute('aria-valuemin', String(min));
    el.setAttribute('aria-valuemax', String(max));
    el.innerHTML = `<svg class="ak-svg" viewBox="0 0 24 24" aria-hidden="true"><circle class="ak-body" cx="${CX}" cy="${CX}" r="6"/><path class="ak-track"/><path class="ak-arc"/><line class="ak-tick" x1="${CX}" y1="${CX}" x2="${CX}" y2="4"/></svg><span class="ak-val"></span><span class="ak-label"></span>`;
    const trackEl = el.querySelector('.ak-track');
    const arcEl = el.querySelector('.ak-arc');
    const tickEl = el.querySelector('.ak-tick');
    const valEl = el.querySelector('.ak-val');
    el.querySelector('.ak-label').textContent = o.label || '';
    trackEl.setAttribute('d', arcPath(-SWEEP / 2, SWEEP / 2));

    function arcPath(from, to) {
      if (to - from < 0.5) return '';
      const pt = (deg) => {
        const a = ((deg - 90) * Math.PI) / 180;
        return `${(CX + R * Math.cos(a)).toFixed(2)} ${(CX + R * Math.sin(a)).toFixed(2)}`;
      };
      return `M ${pt(from)} A ${R} ${R} 0 ${to - from > 180 ? 1 : 0} 1 ${pt(to)}`;
    }

    // The place of a value among the steps: 0 at the first, `last` at the last, between them fractional.
    function posOf(v) {
      if (v <= values[0]) return 0;
      if (v >= values[last]) return last;
      let i = 0;
      while (values[i + 1] < v) i++;
      const span = values[i + 1] - values[i];
      return span > 0 ? i + (v - values[i]) / span : i;
    }

    function draw() {
      const f = last > 0 ? posOf(value) / last : 0;
      const angle = -SWEEP / 2 + f * SWEEP;
      arcEl.setAttribute('d', arcPath(-SWEEP / 2, angle));
      tickEl.setAttribute('transform', `rotate(${angle.toFixed(1)} ${CX} ${CX})`);
      const text = format(value);
      valEl.textContent = text;
      el.setAttribute('aria-valuenow', String(value));
      el.setAttribute('aria-valuetext', text);
    }

    // A value the user chose: shown, then handed to the caller (which may correct it with set()).
    function commit(v) {
      value = v;
      draw();
      if (onChange) onChange(v);
    }

    function goTo(n) {
      const i = Math.max(0, Math.min(last, n));
      if (values[i] !== value) commit(values[i]);
    }

    // k steps up (k > 0) or down from where the value sits; a value between steps goes to the next one.
    function stepBy(k) {
      const pos = posOf(value);
      let n;
      if (k > 0) n = value < values[0] ? k - 1 : Math.floor(pos) + k;
      else n = value > values[last] ? last + k + 1 : Math.ceil(pos) + k;
      goTo(n);
    }

    // Typing: the value text becomes an input with the value selected. Enter or blur keeps it,
    // Escape drops it. Keys in the input never reach the knob or the section.
    function startEdit() {
      if (editor || disabled || destroyed) return;
      const input = document.createElement('input');
      input.type = 'text';
      input.className = 'ak-edit';
      input.inputMode = 'decimal';
      input.autocomplete = 'off';
      input.spellcheck = false;
      input.value = format(value);
      input.setAttribute('aria-label', `${name}: type a value`);
      editor = input;
      valEl.replaceWith(input);
      input.focus();
      input.select();
      let done = false;
      // refocus: the knob takes the focus back (Enter, Escape); a blur leaves the focus where it went.
      const finish = (apply, refocus) => {
        if (done) return;
        done = true;
        const text = input.value;
        editor = null;
        cancelEdit = null;
        if (input.isConnected) input.replaceWith(valEl);
        if (refocus) el.focus({ preventScroll: true });
        const v = apply ? parse(text) : NaN;
        // An unreadable entry is ignored: the old value shows again.
        if (Number.isFinite(v)) {
          const kept = Math.max(min, Math.min(max, v));
          if (kept !== value) commit(kept);
          else draw();
        } else draw();
      };
      cancelEdit = () => finish(false, false);
      input.addEventListener('keydown', (e) => {
        e.stopPropagation();
        if (e.key === 'Enter') { e.preventDefault(); finish(true, true); }
        else if (e.key === 'Escape') { e.preventDefault(); finish(false, true); }
      });
      input.addEventListener('blur', () => finish(true, false));
    }

    // Drag: one step per DRAG_PX from where the press started; a typed value between steps snaps once it moves.
    el.addEventListener('pointerdown', (e) => {
      if (disabled || editor || drag || e.button !== 0) return;
      e.preventDefault();
      el.focus({ preventScroll: true });
      try { el.setPointerCapture(e.pointerId); } catch { /* the pointer is gone already */ }
      const start = posOf(value);
      drag = { id: e.pointerId, y: e.clientY, start, n: Math.round(start) };
      el.classList.add('is-drag');
    });
    el.addEventListener('pointermove', (e) => {
      if (!drag || drag.id !== e.pointerId) return;
      const n = Math.max(0, Math.min(last, Math.round(drag.start + (drag.y - e.clientY) / DRAG_PX)));
      if (n !== drag.n) {
        drag.n = n;
        commit(values[n]);
      }
    });
    const endDrag = (e) => {
      if (drag && drag.id === e.pointerId) { drag = null; el.classList.remove('is-drag'); }
    };
    el.addEventListener('pointerup', endDrag);
    el.addEventListener('pointercancel', endDrag);
    el.addEventListener('lostpointercapture', endDrag);

    el.addEventListener('wheel', (e) => {
      if (disabled || editor || !e.deltaY) return;
      e.preventDefault();   // the wheel steps the knob, it does not scroll the panel
      stepBy(e.deltaY < 0 ? 1 : -1);
    }, { passive: false });

    el.addEventListener('dblclick', (e) => {
      if (disabled) return;
      e.preventDefault();
      startEdit();
    });

    const KEY_STEPS = { ArrowUp: 1, ArrowRight: 1, ArrowDown: -1, ArrowLeft: -1, PageUp: 3, PageDown: -3 };
    el.addEventListener('keydown', (e) => {
      if (disabled || editor || e.altKey || e.ctrlKey || e.metaKey) return;
      // Space is not taken here: it still plays and pauses the Playground (the section's own key).
      if (Object.prototype.hasOwnProperty.call(KEY_STEPS, e.key)) { e.preventDefault(); stepBy(KEY_STEPS[e.key]); }
      else if (e.key === 'Home') { e.preventDefault(); goTo(0); }
      else if (e.key === 'End') { e.preventDefault(); goTo(last); }
      else if (e.key === 'Enter') { e.preventDefault(); startEdit(); }
    });

    // The value as the caller knows it (no onChange): the knob follows the parameters.
    function set(v) {
      if (destroyed || !Number.isFinite(v)) return;
      value = v;
      draw();
    }

    function setDisabled(b) {
      disabled = !!b;
      el.classList.toggle('is-disabled', disabled);
      el.setAttribute('aria-disabled', String(disabled));
      el.tabIndex = disabled ? -1 : 0;
      if (disabled) {
        if (cancelEdit) cancelEdit();
        drag = null;
        el.classList.remove('is-drag');
      }
    }

    function destroy() {
      destroyed = true;
      drag = null;
      onChange = null;
      el.remove();
    }

    draw();
    return { el, set, setDisabled, destroy };
  }

  V.AnalyzerKnob = { create, STEPS, fmtFreq, fmtDb, parseText };
})();
