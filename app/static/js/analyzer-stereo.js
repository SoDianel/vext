// The Correlation and Imager views of the Analyzer section (VEXT.AnalyzerViews; js/analyzer.js
// shows them, js/analyzer-dsp.js owns the registry). Both views get the two signals as
// interleaved stereo blocks and keep their own running sums, so neither waits for the other.
//
// Correlation: r = sum(L R) / sqrt(sum(L^2) sum(R^2)) over exponential sums whose time
// constant is the avg time (each frame weighs d = exp(-1 / (rate * avgTime)) less than the
// one after it). Silence (mean power of both channels under 1e-12, about -120 dBFS RMS) is
// undefined and drawn as an em dash. The history is one sample per 1/60 s of audio, written
// from the feed into a typed ring, so it stops with the audio and freezes with the section.
//
// Imager: a goniometer, one point per frame at x = (R - L) / sqrt(2) (right = R louder) and
// y = (L + R) / sqrt(2) (up = mid), so a full-scale single channel lands on the unit circle.
// The scale is automatic: a peak follower (instant attack, 2 s release) over the largest radius
// sqrt(L^2 + R^2) = sqrt(S^2 + M^2) of the blocks sets a gain of 1 / peak, capped at +24 dB, so
// the circle holds every point: quiet material fills the scope (the boost is shown), a loud
// in-phase one is brought in (a full-scale in-phase pair sits on the circle, -3 dB). Each signal keeps its own offscreen canvas for the persistence: a
// draw removes 15 % of its alpha, then adds the new points. Points are thinned in the feed to
// at most 4096 per signal per draw, so a 60 fps draw stays cheap.
//
// Width = 100 (sum(L^2) + sum(R^2) - 2 sum(LR)) / (sum(L^2) + sum(R^2)): 0 mono, 100 one
// channel alone, 200 out of phase. Balance = 10 log10(R^2 / L^2) of the mean powers.
//
// Colours come from the theme: the signals from ctx.color(sig), the text and grid from the
// pane canvas's own colour, the tint below zero from --danger. A theme switch shows within
// a quarter second. The maths is exposed as V.AnalyzerStereo._math for the node test.
(function () {
  const V = window.VEXT;
  if (!V) return;

  // The registry is normally made by analyzer-dsp.js; this is the same shape in case it loads later.
  if (!V.AnalyzerViews) {
    const entries = [];
    V.AnalyzerViews = {
      register(name, def) {
        const entry = { name, ...def };
        const i = entries.findIndex((e) => e.name === name);
        if (i >= 0) entries[i] = entry;
        else entries.push(entry);
      },
      get(name) { return entries.find((e) => e.name === name); },
      list() { return entries.map((e) => ({ ...e })); },
    };
  }

  const MINUS = '−';
  const SIGNALS = ['before', 'after'];
  const LABEL = { before: 'Before', after: 'After' };
  const FALLBACK = { before: 'rgba(34, 51, 59, 1)', after: 'rgba(169, 146, 125, 1)' };
  const SAMPLE_HZ = 60;           // correlation history: one sample per 1/60 s of audio
  const HIST_CAP = 2048;          // a power of two above 30 s of history (1800 samples)
  const EPS = 1e-12;              // a mean power under this is silence
  const CLOUD_MAX = 4096;         // goniometer points per signal per draw
  const FADE_CUT = 0.15;          // persistence: a draw removes this share of the alpha (keeps 85 %)
  const BOOST_MAX = 10 ** (24 / 20);
  const PEAK_RELEASE_S = 2;
  const COLOUR_MS = 250;          // how often the theme colours are read again
  const SQRT_HALF = Math.SQRT1_2;

  // ---------- the maths (no DOM) ----------

  const clamp = (v, lo, hi) => (v < lo ? lo : v > hi ? hi : v);
  const decayFor = (rate, tau) => Math.exp(-1 / Math.max(1, rate * tau));
  const newAcc = () => ({ ll: 0, rr: 0, lr: 0, w: 0 });
  const newHist = () => ({ ring: new Float32Array(HIST_CAP), head: 0, count: 0, left: 0, hop: 1 });

  // The correlation of the sums, or undefined in silence.
  function corr(s) {
    if (!(s.w > 0)) return undefined;
    if (s.ll / s.w < EPS && s.rr / s.w < EPS) return undefined;
    const den = Math.sqrt(s.ll * s.rr);
    return den > 0 ? clamp(s.lr / den, -1, 1) : 0;   // one channel silent: no shared energy
  }

  // Width in per cent of the energy: 0 mono, 100 one channel alone, 200 out of phase.
  function width(s) {
    if (!(s.w > 0)) return undefined;
    const tot = s.ll + s.rr;
    if (tot / s.w < EPS) return undefined;
    return clamp((100 * (tot - 2 * s.lr)) / tot, 0, 200);
  }

  // 10 log10(R / L) of the mean powers: positive when R is the louder.
  function balanceDb(s) {
    if (!(s.w > 0) || s.ll / s.w < EPS || s.rr / s.w < EPS) return undefined;
    return 10 * Math.log10(s.rr / s.ll);
  }

  function balanceText(s) {
    if (!(s.w > 0)) return '—';
    const left = s.ll / s.w < EPS;
    const right = s.rr / s.w < EPS;
    if (left && right) return '—';
    if (left) return 'R only';
    if (right) return 'L only';
    const d = balanceDb(s);
    return Math.abs(d) < 0.05 ? '0.0 dB' : `${d > 0 ? 'R' : 'L'} ${Math.abs(d).toFixed(1)} dB`;
  }

  // Folds a block of n interleaved stereo frames into the sums. With a history, one correlation
  // sample is written every hist.hop frames.
  function accumulate(s, data, n, d, hist) {
    let { ll, rr, lr, w } = s;
    let left = hist ? hist.left : 0;
    const hop = hist ? hist.hop : 0;
    for (let i = 0, j = 0; i < n; i++, j += 2) {
      const l = data[j];
      const r = data[j + 1];
      ll = ll * d + l * l;
      rr = rr * d + r * r;
      lr = lr * d + l * r;
      w = w * d + 1;
      if (hist && --left <= 0) {
        left += hop;
        s.ll = ll; s.rr = rr; s.lr = lr; s.w = w;
        const c = corr(s);
        hist.ring[hist.head] = c === undefined ? NaN : c;
        hist.head = (hist.head + 1) & (HIST_CAP - 1);
        if (hist.count < HIST_CAP) hist.count++;
      }
    }
    s.ll = ll; s.rr = rr; s.lr = lr; s.w = w;
    if (hist) hist.left = left;
  }

  // Adds the block's goniometer points to the pending list (pts holds x, y pairs; np is how many
  // are there). The block is thinned evenly into the room left, so the list never passes CLOUD_MAX.
  function pushPoints(pts, np, data, n) {
    const room = CLOUD_MAX - np;
    if (room <= 0 || !(n > 0)) return np;
    const step = Math.ceil(n / room);
    let k = np;
    for (let i = 0; i < n; i += step) {
      const l = data[2 * i];
      const r = data[2 * i + 1];
      pts[2 * k] = (r - l) * SQRT_HALF;
      pts[2 * k + 1] = (l + r) * SQRT_HALF;
      k++;
    }
    return k;
  }

  // The largest radius sqrt(L^2 + R^2) of a block: a point of the scope is that far from its centre.
  function blockPeak(data, n) {
    let p = 0;
    for (let i = 0, j = 0; i < n; i++, j += 2) {
      const l = data[j];
      const r = data[j + 1];
      const q = l * l + r * r;
      if (q > p) p = q;
    }
    return Math.sqrt(p);
  }

  // The goniometer gain: 1 / peak, so the loudest point lands on the circle; capped at +24 dB for
  // quiet material, and below 1 (down to -3 dB for full scale in phase) for a loud one. Silence
  // is not scaled.
  function gainOf(peak) {
    if (!(peak > 1e-4)) return 1;
    return Math.min(BOOST_MAX, 1 / peak);
  }

  const fmtR = (r) => (r === undefined ? '—' : `${r < 0 ? MINUS : '+'}${Math.abs(r).toFixed(2)}`);
  const fmtWidth = (w) => (w === undefined ? '—' : `${Math.round(w)} %`);

  // ---------- colours ----------

  // Any CSS colour as something a canvas takes: WaveView's probe, or one of our own (made lazily).
  let probeCtx = null;
  function canvasColorLocal(css, fallback) {
    if (!css) return fallback;
    if (!probeCtx) {
      const probe = document.createElement('canvas');
      probe.width = probe.height = 1;
      probeCtx = probe.getContext('2d', { willReadFrequently: true });
    }
    probeCtx.clearRect(0, 0, 1, 1);
    probeCtx.fillStyle = fallback;
    probeCtx.fillStyle = css;
    probeCtx.fillRect(0, 0, 1, 1);
    const [r, g, b, a] = probeCtx.getImageData(0, 0, 1, 1).data;
    return `rgba(${r}, ${g}, ${b}, ${(a / 255).toFixed(3)})`;
  }
  const colorOf = (css, fallback) => ((V.WaveView && V.WaveView.canvasColor) || canvasColorLocal)(css, fallback);

  function rgbaOf(str) {
    const m = /rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)(?:[,\s/]+([\d.]+))?/.exec(str || '');
    return m ? [+m[1], +m[2], +m[3], m[4] === undefined ? 1 : +m[4]] : [128, 128, 128, 1];
  }
  const rgba = (c, k) => `rgba(${c[0]}, ${c[1]}, ${c[2]}, ${(c[3] * k).toFixed(3)})`;

  function readColors(el, ctx) {
    const cs = getComputedStyle(el);
    const text = rgbaOf(colorOf(cs.color, 'rgba(110, 110, 110, 1)'));
    const danger = rgbaOf(colorOf(getComputedStyle(document.documentElement).getPropertyValue('--danger').trim(),
      'rgba(196, 40, 47, 1)'));
    const sig = {};
    for (const s of SIGNALS) sig[s] = colorOf(ctx.color(s), FALLBACK[s]);
    return {
      font: cs.fontFamily || 'sans-serif',
      text: rgba(text, 1),
      grid: rgba(text, 0.16),
      strong: rgba(text, 0.4),
      zero: rgba(text, 0.6),
      danger: rgba(danger, 0.1),
      sig,
    };
  }

  // A colour set that is read again at most every COLOUR_MS.
  function colourCache(el, ctx) {
    let cur = null;
    let at = -Infinity;
    return () => {
      const t = performance.now();
      if (!cur || t - at > COLOUR_MS) { cur = readColors(el, ctx); at = t; }
      return cur;
    };
  }

  const visible = (list) => (list || []).filter((s, i, a) => SIGNALS.includes(s) && a.indexOf(s) === i);

  // ---------- Correlation ----------

  // Paints the meter and the history at pane coordinates (device px). Returns the history's
  // rectangle for the hover.
  function paintCorr(g, W, H, u, sigs, N, states, col) {
    g.clearRect(0, 0, W, H);
    const pad = 10 * u;
    const topH = Math.round(H * 0.4);
    const tickW = Math.max(1, Math.round(u));
    const font = (size, weight) => { g.font = `${weight ? `${weight} ` : ''}${size * u}px ${col.font}`; };

    // The meter: a bar from -1 to +1, ticks, labels under it, one marker per signal.
    const mx0 = pad;
    const mx1 = W - pad;
    const barH = 8 * u;
    const barY = Math.round(Math.max(topH * 0.62, pad + (sigs.length + 1) * 17 * u));
    const xAt = (r) => mx0 + ((r + 1) / 2) * (mx1 - mx0);
    g.fillStyle = col.grid;
    g.fillRect(mx0, barY - barH / 2, mx1 - mx0, barH);
    for (const t of [-1, -0.5, 0, 0.5, 1]) {
      g.fillStyle = t === 0 ? col.zero : col.strong;
      g.fillRect(Math.round(xAt(t) - tickW / 2), barY - barH / 2 - 4 * u, tickW, barH + 8 * u);
    }
    font(11);
    g.fillStyle = col.text;
    g.textBaseline = 'top';
    const labelY = Math.round(barY + barH / 2 + 6 * u);
    g.textAlign = 'left';
    g.fillText(`${MINUS}1 out of phase`, mx0, labelY);
    g.textAlign = 'center';
    g.fillText('0', xAt(0), labelY);
    g.textAlign = 'right';
    g.fillText('+1 mono', mx1, labelY);
    for (const sig of sigs) {
      const r = corr(states[sig].acc);
      if (r === undefined) continue;
      g.fillStyle = col.sig[sig];
      g.fillRect(Math.round(xAt(r) - 1.5 * u), barY - barH / 2 - 4 * u, 3 * u, barH + 8 * u);
    }
    font(13, '600');
    g.textAlign = 'left';
    sigs.forEach((sig, i) => {
      g.fillStyle = col.sig[sig];
      g.fillText(`${LABEL[sig]}  ${fmtR(corr(states[sig].acc))}`, mx0, pad + i * 17 * u);
    });

    // The history: r over the last N samples, newest at the right edge; below zero tinted.
    const px0 = Math.round(pad + 28 * u);
    const px1 = W - pad;
    const py0 = Math.round(topH + 16 * u);
    const py1 = Math.round(H - 22 * u);
    const geo = { px0, px1, py0, py1, N };
    if (px1 - px0 < 20 || py1 - py0 < 20) return geo;
    const yOf = (v) => py0 + ((1 - v) / 2) * (py1 - py0);
    g.fillStyle = col.danger;
    g.fillRect(px0, Math.round(yOf(0)), px1 - px0, py1 - Math.round(yOf(0)));
    g.fillStyle = col.grid;
    for (const v of [1, 0.5, -0.5, -1]) g.fillRect(px0, Math.round(yOf(v)), px1 - px0, tickW);
    g.fillStyle = col.zero;
    g.fillRect(px0, Math.round(yOf(0)), px1 - px0, tickW);
    font(11);
    g.fillStyle = col.text;
    g.textAlign = 'right';
    g.textBaseline = 'top';
    g.fillText('+1', px0 - 5 * u, py0);
    g.textBaseline = 'middle';
    g.fillText('0', px0 - 5 * u, yOf(0));
    g.textBaseline = 'bottom';
    g.fillText(`${MINUS}1`, px0 - 5 * u, py1);
    g.textBaseline = 'top';
    g.textAlign = 'left';
    g.fillText(`${MINUS}${Math.round(N / SAMPLE_HZ)} s`, px0, py1 + 6 * u);
    g.textAlign = 'right';
    g.fillText('now', px1, py1 + 6 * u);

    const span = px1 - px0;
    g.lineJoin = 'round';
    g.lineWidth = 1.5 * u;
    for (const sig of sigs) {
      const h = states[sig].hist;
      const avail = Math.min(N, h.count);
      g.strokeStyle = col.sig[sig];
      g.beginPath();
      let pen = false;
      for (let age = 0; age < avail; age++) {
        const v = h.ring[(h.head - 1 - age) & (HIST_CAP - 1)];
        if (v !== v) { pen = false; continue; }   // NaN: undefined in silence, a gap
        const x = px1 - (age / (N - 1)) * span;
        const y = yOf(v);
        if (pen) g.lineTo(x, y);
        else { g.moveTo(x, y); pen = true; }
      }
      g.stroke();
    }
    return geo;
  }

  function correlationView(pane, ctx) {
    const canvas = pane.canvas;
    const states = {};
    for (const s of SIGNALS) states[s] = { acc: newAcc(), hist: newHist() };
    const colours = colourCache(canvas, ctx);
    let last = { geo: null, sigs: [] };

    const historyN = () => {
      const h = Math.max(1, Number(ctx.params().history) || 10);
      return Math.min(HIST_CAP, Math.max(2, Math.round(h * SAMPLE_HZ)));
    };

    return {
      feed(sig, frames, n, rate) {
        const s = states[sig];
        if (!s || !(n > 0)) return;
        const tau = Math.max(0.05, Number(ctx.params().avgTime) || 0.3);
        s.hist.hop = Math.max(1, Math.round(rate / SAMPLE_HZ));
        accumulate(s.acc, frames, n, decayFor(rate, tau), s.hist);
      },
      draw() {
        const W = canvas.width;
        const H = canvas.height;
        if (!W || !H) return;
        const sigs = visible(ctx.signals());
        last = { geo: paintCorr(canvas.getContext('2d'), W, H, ctx.dpr(), sigs, historyN(), states, colours()), sigs };
      },
      hover(x, y) {
        const geo = last.geo;
        if (!geo || !last.sigs.length) return null;
        const dpr = ctx.dpr();
        const X = x * dpr;
        const Y = y * dpr;
        if (X < geo.px0 || X > geo.px1 || Y < geo.py0 || Y > geo.py1) return null;
        const age = Math.round(((geo.px1 - X) / (geo.px1 - geo.px0)) * (geo.N - 1));
        const parts = last.sigs.map((sig) => {
          const h = states[sig].hist;
          const v = age < h.count ? h.ring[(h.head - 1 - age) & (HIST_CAP - 1)] : NaN;
          return `${fmtR(v === v ? v : undefined)} (${LABEL[sig]})`;
        });
        const when = age === 0 ? 'now' : `${MINUS}${(age / SAMPLE_HZ).toFixed(1)} s`;
        return [`${when} · r ${parts.join(' · ')}`];
      },
      reset() {
        for (const s of SIGNALS) states[s] = { acc: newAcc(), hist: newHist() };
        last = { geo: null, sigs: [] };
      },
      snapshot(signals, w, h) {
        const c = document.createElement('canvas');
        c.width = w;
        c.height = h;
        paintCorr(c.getContext('2d'), w, h, ctx.dpr(), visible(signals), historyN(), states, colours());
        return c;
      },
      destroy() {
        last = { geo: null, sigs: [] };
      },
    };
  }

  // ---------- Imager ----------

  // The scope: the square of the pane above a strip of one readout line per signal.
  function scopeLayout(W, H, u, rows) {
    const lineH = 15 * u;
    const strip = rows ? rows * lineH + 12 * u : 0;
    const Ah = Math.max(1, H - strip);
    const side = Math.max(1, Math.min(W, Ah) - 20 * u);
    const halfS = side / 2;
    return { W, H, u, cx: W / 2, cy: Ah / 2, halfS, U: 0.7 * halfS, lineH, strip, Ah, dot: Math.max(1, Math.round(1.5 * u)) };
  }

  // Fades one signal's persistence canvas and adds the pending points in its colour.
  function plotCloud(p, geo, pts, np, gain, colour) {
    const g = p.ctx;
    g.globalCompositeOperation = 'destination-out';
    g.fillStyle = `rgba(0, 0, 0, ${FADE_CUT})`;
    g.fillRect(0, 0, geo.W, geo.H);
    g.globalCompositeOperation = 'source-over';
    if (!np) return;
    const k = geo.U * gain;
    const lim = geo.halfS;
    const d = geo.dot;
    g.fillStyle = colour;
    g.beginPath();
    for (let i = 0; i < np; i++) {
      const dx = pts[2 * i] * k;
      const dy = -pts[2 * i + 1] * k;
      if (dx > lim || dx < -lim || dy > lim || dy < -lim) continue;
      g.rect(geo.cx + dx - d / 2, geo.cy + dy - d / 2, d, d);
    }
    g.fill();
  }

  // The scope at pane coordinates: guides, the persisted clouds, the readouts per signal.
  function composeScope(g, geo, sigs, gain, peak, states, col, persist) {
    const { W, H, u, cx, cy, halfS, U } = geo;
    g.clearRect(0, 0, W, H);
    g.lineWidth = u;
    g.strokeStyle = col.grid;
    g.strokeRect(cx - halfS, cy - halfS, 2 * halfS, 2 * halfS);
    g.beginPath();
    g.moveTo(cx, cy - halfS); g.lineTo(cx, cy + halfS);            // M, vertical
    g.moveTo(cx - halfS, cy); g.lineTo(cx + halfS, cy);            // S, horizontal
    g.moveTo(cx, cy); g.lineTo(cx - halfS, cy - halfS);            // L, up-left diagonal
    g.moveTo(cx, cy); g.lineTo(cx + halfS, cy - halfS);            // R, up-right diagonal
    g.stroke();
    g.strokeStyle = col.strong;
    g.beginPath();
    g.arc(cx, cy, U, 0, 2 * Math.PI);                              // one channel at full scale
    g.stroke();
    for (const sig of sigs) {
      const p = persist[sig];
      if (p) g.drawImage(p.canvas, 0, 0, W, H);
    }
    g.font = `${11 * u}px ${col.font}`;
    g.fillStyle = col.text;
    g.textBaseline = 'top';
    g.textAlign = 'left';
    g.fillText('M', cx + 4 * u, cy - halfS + 2 * u);
    g.textAlign = 'right';
    g.fillText('R', cx + halfS - 4 * u, cy - halfS + 2 * u);
    g.textAlign = 'left';
    g.fillText('L', cx - halfS + 4 * u, cy - halfS + 2 * u);
    g.textBaseline = 'bottom';
    g.fillText('S', cx + halfS - 4 * u, cy - 3 * u);

    if (!sigs.length) return;
    g.font = `${12 * u}px ${col.font}`;
    g.textBaseline = 'top';
    g.textAlign = 'left';
    sigs.forEach((sig, i) => {
      const s = states[sig].acc;
      const text = `${LABEL[sig]}   Width ${fmtWidth(width(s))}  ·  Balance ${balanceText(s)}  ·  r ${fmtR(corr(s))}`;
      g.fillStyle = col.sig[sig];
      g.fillText(text, 10 * u, geo.Ah + 6 * u + i * geo.lineH);
    });
    if (gain > 1.01 && peak > 1e-4) {
      g.textAlign = 'right';
      g.fillStyle = col.text;
      g.fillText(`Boost +${Math.round(20 * Math.log10(gain))} dB`, W - 10 * u, geo.Ah + 6 * u);
    }
  }

  function imagerView(pane, ctx) {
    const canvas = pane.canvas;
    const states = {};
    const persist = {};   // signal -> { canvas, ctx }, the pane's size
    const newState = () => ({ acc: newAcc(), peak: 0, pts: new Float32Array(2 * CLOUD_MAX), np: 0 });
    for (const s of SIGNALS) states[s] = newState();
    const colours = colourCache(canvas, ctx);

    function ensurePersist(W, H) {
      for (const sig of SIGNALS) {
        const p = persist[sig];
        if (p && p.canvas.width === W && p.canvas.height === H) continue;
        const c = document.createElement('canvas');
        c.width = W;
        c.height = H;
        persist[sig] = { canvas: c, ctx: c.getContext('2d') };
      }
    }

    return {
      feed(sig, frames, n, rate) {
        const s = states[sig];
        if (!s || !(n > 0)) return;
        const tau = Math.max(0.05, Number(ctx.params().avgTime) || 0.3);
        accumulate(s.acc, frames, n, decayFor(rate, tau), null);
        s.peak = Math.max(s.peak * Math.exp(-n / (Math.max(1, rate) * PEAK_RELEASE_S)), blockPeak(frames, n));
        s.np = pushPoints(s.pts, s.np, frames, n);
      },
      draw() {
        const W = canvas.width;
        const H = canvas.height;
        if (!W || !H) return;
        const u = ctx.dpr();
        const sigs = visible(ctx.signals());
        ensurePersist(W, H);
        const col = colours();
        const peak = Math.max(states.before.peak, states.after.peak);
        const gain = gainOf(peak);
        const geo = scopeLayout(W, H, u, sigs.length);
        for (const sig of SIGNALS) {
          const s = states[sig];
          if (sigs.includes(sig)) plotCloud(persist[sig], geo, s.pts, s.np, gain, col.sig[sig]);
          s.np = 0;   // drained even when not shown, so the list never backs up
        }
        composeScope(canvas.getContext('2d'), geo, sigs, gain, peak, states, col, persist);
      },
      hover() {
        return null;   // the scope has no crosshair; the readouts are always on
      },
      reset() {
        for (const s of SIGNALS) states[s] = newState();
        for (const sig of SIGNALS) {
          const p = persist[sig];
          if (p) p.ctx.clearRect(0, 0, p.canvas.width, p.canvas.height);
        }
      },
      snapshot(signals, w, h) {
        const c = document.createElement('canvas');
        c.width = w;
        c.height = h;
        const g = c.getContext('2d');
        const sigs = visible(signals);
        const W = canvas.width || w;
        const H = canvas.height || h;
        // The pane's own layout, scaled to fit the snapshot (never stretched).
        const k = Math.min(w / W, h / H);
        g.setTransform(k, 0, 0, k, (w - W * k) / 2, (h - H * k) / 2);
        const peak = Math.max(states.before.peak, states.after.peak);
        composeScope(g, scopeLayout(W, H, ctx.dpr(), sigs.length), sigs, gainOf(peak), peak, states, colours(), persist);
        g.setTransform(1, 0, 0, 1, 0, 0);
        return c;
      },
      destroy() {
        for (const sig of SIGNALS) delete persist[sig];
      },
    };
  }

  V.AnalyzerViews.register('correlation', {
    label: 'Correlation',
    settings: ['avgTime', 'history'],
    create: correlationView,
  });
  V.AnalyzerViews.register('imager', {
    label: 'Imager',
    settings: ['avgTime'],
    create: imagerView,
  });

  V.AnalyzerStereo = {
    _math: {
      decayFor, newAcc, newHist, accumulate, corr, width, balanceDb, balanceText,
      pushPoints, blockPeak, gainOf, fmtR, fmtWidth, CLOUD_MAX, HIST_CAP, EPS,
    },
  };
})();
