// The Analyzer section's maths and shared pieces (VEXT.AnalyzerDSP) and the registry its views
// register in (VEXT.AnalyzerViews). Nothing here draws or talks to the backend; js/analyzer.js
// (the section, its Spectrum and Spectrogram views) and js/analyzer-stereo.js (Correlation,
// Imager) build on it. It loads before both, so a view file can register before the section runs.
//
// Calibration (the same as the Python side used to have, so readings stay comparable): per
// channel the power of bin k is (2|X_k| / sum(w))^2 (the DC and Nyquist bins without the 2),
// averaged over the channels, dB = 10 log10(P + 1e-20). A full-scale sine on a bin centre reads
// 0.0 dB; its block RMS reads -3.01 dBFS. The true peak of one block is found on the block
// upsampled 4x by FFT zero-padding; the Peak meter's stream uses truePeakStream (a polyphase filter).
//
// The registry: VEXT.AnalyzerViews.register(name, {label, settings, create(pane, ctx)}),
// .get(name) -> the entry (or undefined), .list() -> every entry, {name, ...definition}, in the
// order they registered. See js/analyzer.js for what create() is given and has to return.
(function () {
  const V = window.VEXT;
  if (!V || V.AnalyzerDSP) return;

  const FLOOR_DB = 1e-20;                 // added to the power before the log
  const MINUS = '−';
  const NOTES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B'];

  // ---------- the view registry ----------

  // Never replaces a registry that is already there (a view file may have made one first).
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

  // ---------- parameters: one source of truth ----------

  const DEFAULTS = {
    view: 'spectrum',        // 'spectrum' | 'spectrogram' | 'correlation' | 'imager'
    signal: 'both',          // 'before' | 'after' | 'both'
    layout: 'overlay',       // 'overlay' (one window) | 'split' (Before | After side by side)
    history: 10,             // seconds shown by the spectrogram and the correlation history
    fps: 30,                 // refresh rate
    meter: 'rms',            // 'rms' | 'peak'
    freqLo: 20, freqHi: 20000,
    rangeLo: -78, rangeHi: -18,
    block: 2048,
    avgTime: 0.3,            // seconds
    window: 'hann',          // 'dome' | 'blackman' | 'hann'
  };
  // Freq Hi starts at 2 Hz: the axis starts at max(freqLo, 1) and a log axis needs hi > lo.
  const LIMITS = { freqLo: [0, 30000], freqHi: [2, 30000], rangeLo: [-120, 20], rangeHi: [-120, 20] };
  const BLOCKS = [512, 1024, 2048, 4096, 8192];
  const WINDOWS = [['hann', 'Hann'], ['blackman', 'Blackman'], ['dome', 'Dome']];
  const FPS = [10, 15, 20, 30, 60];
  const AVG_TIMES = [0.05, 0.1, 0.2, 0.3, 0.5, 1, 2, 5];
  const HISTORIES = [5, 10, 20, 30];
  const VIEWS = ['spectrum', 'spectrogram', 'correlation', 'imager'];
  const SIGNALS = ['before', 'after', 'both'];
  const LAYOUTS = ['overlay', 'split'];
  const METERS = ['rms', 'peak'];
  const AVG_RANGE = [0.01, 10];           // any value in between is taken; the menu offers AVG_TIMES

  const num = (v, fallback) => {
    const n = typeof v === 'string' && v.trim() === '' ? NaN : Number(v);
    return Number.isFinite(n) ? n : fallback;
  };
  const clamp = (v, lo, hi) => (v < lo ? lo : v > hi ? hi : v);
  const pick = (v, list, fallback) => (list.includes(v) ? v : fallback);

  // Two ends of a range, `gap` apart at least: the end the user just edited gives way to the other.
  function fitPair(lo, hi, limLo, limHi, gap, editedLo, editedHi) {
    lo = clamp(Math.round(lo), limLo[0], limLo[1]);
    hi = clamp(Math.round(hi), limHi[0], limHi[1]);
    if (lo > hi - gap) {
      if (editedHi && !editedLo) hi = lo + gap;
      else lo = hi - gap;
    }
    // Only at an extreme can an end still be out of reach: push the other one.
    if (hi > limHi[1]) { hi = limHi[1]; lo = Math.min(lo, hi - gap); }
    if (lo < limLo[0]) { lo = limLo[0]; hi = Math.max(hi, lo + gap); }
    return [lo, hi];
  }

  // `partial` over `current` (or the defaults), every value clamped or, when it is not one of
  // the known choices, replaced by the current / default one. Unknown keys are dropped.
  function normalize(partial, current) {
    const sane = (src, base, edited) => {
      const fps = num(src.fps, NaN);
      const block = num(src.block, NaN);
      const history = num(src.history, NaN);
      const [freqLo, freqHi] = fitPair(num(src.freqLo, base.freqLo), num(src.freqHi, base.freqHi),
        LIMITS.freqLo, LIMITS.freqHi, 1, edited.has('freqLo'), edited.has('freqHi'));
      const [rangeLo, rangeHi] = fitPair(num(src.rangeLo, base.rangeLo), num(src.rangeHi, base.rangeHi),
        LIMITS.rangeLo, LIMITS.rangeHi, 6, edited.has('rangeLo'), edited.has('rangeHi'));
      return {
        view: pick(src.view, VIEWS, base.view),
        signal: pick(src.signal, SIGNALS, base.signal),
        layout: pick(src.layout, LAYOUTS, base.layout),
        history: HISTORIES.includes(history) ? history : base.history,
        fps: FPS.includes(fps) ? fps : base.fps,
        meter: pick(src.meter, METERS, base.meter),
        freqLo, freqHi, rangeLo, rangeHi,
        block: BLOCKS.includes(block) ? block : base.block,
        avgTime: clamp(num(src.avgTime, base.avgTime), AVG_RANGE[0], AVG_RANGE[1]),
        window: pick(src.window, WINDOWS.map((w) => w[0]), base.window),
      };
    };
    const base = sane({ ...DEFAULTS, ...(current || {}) }, DEFAULTS, new Set());
    const given = Object.keys(partial || {}).filter((k) => partial[k] !== undefined);
    const merged = { ...base };
    for (const k of given) merged[k] = partial[k];
    return sane(merged, base, new Set(given));
  }

  // ---------- FFT, windows, spectra ----------

  const tableCache = new Map();
  function tables(n) {
    let t = tableCache.get(n);
    if (t) return t;
    if (!(n >= 2) || (n & (n - 1)) !== 0) throw new Error(`FFT size must be a power of two, got ${n}`);
    const half = n >> 1;
    const cos = new Float64Array(half);
    const sin = new Float64Array(half);
    for (let i = 0; i < half; i++) {
      const a = (-2 * Math.PI * i) / n;
      cos[i] = Math.cos(a);
      sin[i] = Math.sin(a);
    }
    const bits = Math.round(Math.log2(n));
    const rev = new Uint32Array(n);
    for (let i = 0; i < n; i++) {
      let r = 0;
      for (let b = 0, x = i; b < bits; b++, x >>= 1) r = (r << 1) | (x & 1);
      rev[i] = r;
    }
    t = { cos, sin, rev };
    tableCache.set(n, t);
    return t;
  }

  // In-place iterative radix-2 complex FFT (forward, e^-i), size n = a power of two.
  // The inverse, unscaled, is fft(im, re, n): swapping the two arrays swaps the parts.
  function fft(re, im, n = re.length) {
    const { cos, sin, rev } = tables(n);
    for (let i = 0; i < n; i++) {
      const j = rev[i];
      if (j > i) {
        let t = re[i]; re[i] = re[j]; re[j] = t;
        t = im[i]; im[i] = im[j]; im[j] = t;
      }
    }
    for (let size = 2; size <= n; size <<= 1) {
      const half = size >> 1;
      const step = n / size;
      for (let start = 0; start < n; start += size) {
        for (let k = 0, tw = 0; k < half; k++, tw += step) {
          const wr = cos[tw];
          const wi = sin[tw];
          const a = start + k;
          const b = a + half;
          const xr = re[b] * wr - im[b] * wi;
          const xi = re[b] * wi + im[b] * wr;
          re[b] = re[a] - xr;
          im[b] = im[a] - xi;
          re[a] += xr;
          im[a] += xi;
        }
      }
    }
  }

  // Symmetric windows (denominator N-1). `.sum` is the sum of the window.
  const windowCache = new Map();
  function windowOf(kind, n) {
    if (kind !== 'dome' && kind !== 'blackman') kind = 'hann';
    const key = `${kind}:${n}`;
    let w = windowCache.get(key);
    if (w) return w;
    w = new Float64Array(n);
    const d = Math.max(1, n - 1);
    let sum = 0;
    for (let i = 0; i < n; i++) {
      const x = (2 * Math.PI * i) / d;
      w[i] = kind === 'hann' ? 0.5 - 0.5 * Math.cos(x)
        : kind === 'blackman' ? 0.42 - 0.5 * Math.cos(x) + 0.08 * Math.cos(2 * x)
          : Math.sin(Math.PI * i / d);
      sum += w[i];
    }
    w.sum = sum;
    windowCache.set(key, w);
    return w;
  }

  const scratch = {};   // re-used work buffers by name and size
  function work(name, n) {
    const key = `${name}${n}`;
    return scratch[key] || (scratch[key] = new Float64Array(n));
  }

  // Linear power per bin 0..N/2 of the windowed blocks (`chans`: one array of N samples per
  // channel), averaged over the channels, into `out` (length >= N/2 + 1).
  function power(chans, n, kind, out) {
    const half = n >> 1;
    const w = windowOf(kind, n);
    const re = work('re', n);
    const im = work('im', n);
    for (let k = 0; k <= half; k++) out[k] = 0;
    const c2 = 4 / (w.sum * w.sum);
    const c1 = 1 / (w.sum * w.sum);
    for (let c = 0; c < chans.length; c++) {
      const x = chans[c];
      for (let i = 0; i < n; i++) { re[i] = x[i] * w[i]; im[i] = 0; }
      fft(re, im, n);
      out[0] += (re[0] * re[0] + im[0] * im[0]) * c1;
      for (let k = 1; k < half; k++) out[k] += (re[k] * re[k] + im[k] * im[k]) * c2;
      out[half] += (re[half] * re[half] + im[half] * im[half]) * c1;
    }
    if (chans.length > 1) for (let k = 0; k <= half; k++) out[k] /= chans.length;
    return out;
  }

  // dB per bin 0..N/2 (the calibration above).
  function powerDb(chans, n, kind, out) {
    const half = n >> 1;
    out = out || new Float64Array(half + 1);
    if (!chans.length) { for (let k = 0; k <= half; k++) out[k] = 10 * Math.log10(FLOOR_DB); return out; }
    const p = work('pw', half + 1);
    power(chans, n, kind, p);
    for (let k = 0; k <= half; k++) out[k] = 10 * Math.log10(p[k] + FLOOR_DB);
    return out;
  }

  // Block RMS over all channels and samples, dBFS.
  function rms(chans) {
    let sum = 0;
    let count = 0;
    for (let c = 0; c < chans.length; c++) {
      const x = chans[c];
      for (let i = 0; i < x.length; i++) sum += x[i] * x[i];
      count += x.length;
    }
    return 10 * Math.log10((count ? sum / count : 0) + FLOOR_DB);
  }

  // True peak, dBTP: the block upsampled 4x by FFT zero-padding (the spectrum of the unwindowed
  // block, padded to 4N, inverse transformed), the maximum over all channels. Two channels share
  // one complex transform. The padding treats the block as periodic, so a jump between its two
  // ends rings; the inter-sample maximum is therefore taken away from the ends (the outer 1/16
  // on each side) and the sample peak of the whole block is added to it.
  function truePeak(chans) {
    const n = chans.length ? chans[0].length : 0;
    let peak = 0;
    for (let c = 0; c < chans.length; c++) {
      const x = chans[c];
      for (let i = 0; i < x.length; i++) { const a = x[i] < 0 ? -x[i] : x[i]; if (a > peak) peak = a; }
    }
    if (n >= 16 && (n & (n - 1)) === 0) {
      const m = n * 4;
      const half = n >> 1;
      const re = work('re', n);
      const im = work('im', n);
      const bre = work('bre', m);
      const bim = work('bim', m);
      const from = m >> 4;
      const to = m - from;
      for (let c = 0; c < chans.length; c += 2) {
        const a = chans[c];
        const b = chans[c + 1] || null;
        for (let i = 0; i < n; i++) { re[i] = a[i]; im[i] = b ? b[i] : 0; }
        fft(re, im, n);
        bre.fill(0);
        bim.fill(0);
        // Split the two channels out of the one transform, place them in the long spectrum.
        for (let k = 0; k <= half; k++) {
          const j = (n - k) & (n - 1);
          const ar = (re[k] + re[j]) / 2;
          const ai = (im[k] - im[j]) / 2;
          const br = (im[k] + im[j]) / 2;
          const bi = -(re[k] - re[j]) / 2;
          if (k === 0) { bre[0] = ar - bi; bim[0] = ai + br; continue; }
          if (k === half) {   // the Nyquist bin is shared between +f and -f
            bre[half] = (ar - bi) / 2; bim[half] = (ai + br) / 2;
            bre[m - half] = (ar + bi) / 2; bim[m - half] = (br - ai) / 2;
            continue;
          }
          bre[k] = ar - bi; bim[k] = ai + br;
          bre[m - k] = ar + bi; bim[m - k] = br - ai;
        }
        fft(bim, bre, m);   // inverse, unscaled: bre / bim are now the two upsampled channels
        for (let i = from; i < to; i++) {
          const x = (bre[i] < 0 ? -bre[i] : bre[i]) / n;
          const y = (bim[i] < 0 ? -bim[i] : bim[i]) / n;
          if (x > peak) peak = x;
          if (y > peak) peak = y;
        }
      }
    }
    return 20 * Math.log10(peak + 1e-10);
  }

  // The true peak of a stream, block by block: every inter-sample maximum of the stream, however
  // the blocks are cut (truePeak above only sees one block, and only its middle). The values
  // between two samples come from a 4x polyphase filter: 24 taps per phase (a Blackman-windowed
  // sinc, each phase scaled to unit DC gain), flat to about 0.1 dB up to 20 kHz. The taps reach
  // TP_HALF samples either side, so a pair is only final once those samples have come: the carry
  // keeps them for the next block. The samples themselves are the caller's (the sample peak).
  // One state per channel.
  const TP_HALF = 12;
  const TP_TAPS = 2 * TP_HALF;
  const TP_CARRY = 2 * TP_HALF;
  const tpPhase = (() => {
    const h = new Float64Array(3 * TP_TAPS);
    for (let p = 1; p <= 3; p++) {
      const f = p / 4;
      let sum = 0;
      for (let k = 1 - TP_HALF; k <= TP_HALF; k++) {
        const d = f - k;
        const w = 0.42 + 0.5 * Math.cos((Math.PI * d) / TP_HALF) + 0.08 * Math.cos((2 * Math.PI * d) / TP_HALF);
        const v = (Math.sin(Math.PI * d) / (Math.PI * d)) * w;
        h[(p - 1) * TP_TAPS + k - 1 + TP_HALF] = v;
        sum += v;
      }
      for (let i = 0; i < TP_TAPS; i++) h[(p - 1) * TP_TAPS + i] /= sum;
    }
    return h;
  })();

  function truePeakStream() {
    const carry = new Float32Array(TP_CARRY);     // the last samples of the stream (zeros at the start)
    let buf = new Float32Array(TP_CARRY + 2048);
    return {
      // One channel (0 = L, 1 = R) of `n` interleaved stereo frames: the largest inter-sample
      // magnitude (linear) among the pairs that became final with this block.
      push(frames, n, ch) {
        if (!(n > 0)) return 0;
        if (buf.length < TP_CARRY + n) buf = new Float32Array(TP_CARRY + n);
        buf.set(carry, 0);
        for (let i = 0, k = ch; i < n; i++, k += 2) buf[TP_CARRY + i] = frames[k];
        let peak = 0;
        // Pairs s (between buf[s] and buf[s + 1]) whose taps, buf[s - TP_HALF + 1 .. s + TP_HALF], are all known.
        for (let s = TP_HALF; s < n + TP_HALF; s++) {
          const b = s - TP_HALF + 1;
          for (let p = 0; p < 3; p++) {
            const o = p * TP_TAPS;
            let y = 0;
            for (let k = 0; k < TP_TAPS; k++) y += buf[b + k] * tpPhase[o + k];
            const a = y < 0 ? -y : y;
            if (a > peak) peak = a;
          }
        }
        carry.set(buf.subarray(n, n + TP_CARRY));
        return peak;
      },
      reset() { carry.fill(0); },
    };
  }

  // Display column -> FFT bins (the log frequency rule): per column of `W`, the bins whose centre
  // frequency falls in it (k0..k1, the maximum is taken), or, with no bin inside (k1 = -2), the
  // linear interpolation in dB between the two bins around the column centre; above the Nyquist
  // the column is empty (k1 = -1).
  function buildMap(W, n, rate, fmin, fhi) {
    const bins = (n >> 1) + 1;
    const nyq = bins - 1;
    const k0 = new Int32Array(W);
    const k1 = new Int32Array(W);
    const frac = new Float32Array(W);
    const ratio = fhi / fmin;
    const binHz = rate / n;
    for (let x = 0; x < W; x++) {
      const f0 = fmin * Math.pow(ratio, x / W);
      const f1 = fmin * Math.pow(ratio, (x + 1) / W);
      const lo = Math.ceil(f0 / binHz);
      const hi = Math.ceil(f1 / binHz) - 1;
      if (lo > nyq) { k0[x] = 0; k1[x] = -1; continue; }
      if (hi >= lo) { k0[x] = lo; k1[x] = Math.min(hi, nyq); continue; }
      const p = Math.sqrt(f0 * f1) / binHz;
      if (p > nyq) { k0[x] = 0; k1[x] = -1; continue; }
      const i = Math.floor(p);
      k0[x] = i; k1[x] = -2; frac[x] = p - i;
    }
    return { W, bins, k0, k1, frac };
  }

  function applyMap(map, src, out) {
    const { k0, k1, frac, bins, W } = map;
    for (let x = 0; x < W; x++) {
      const b = k1[x];
      if (b === -1) { out[x] = NaN; continue; }
      const a = k0[x];
      if (b === -2) {
        const v0 = src[a];
        const v1 = a + 1 < bins ? src[a + 1] : v0;
        out[x] = v0 + (v1 - v0) * frac[x];
        continue;
      }
      let m = src[a];
      for (let k = a + 1; k <= b; k++) if (src[k] > m) m = src[k];
      out[x] = m;
    }
    return out;
  }

  // ---------- text ----------

  function fmtDb(x, digits = 1) {
    if (!(x > -199)) return `${MINUS}∞`;
    const s = Math.abs(x).toFixed(digits);
    if (Number(s) === 0) return (0).toFixed(digits);
    return x < 0 ? `${MINUS}${s}` : `+${s}`;
  }
  const fmtInt = (v) => (v < 0 ? `${MINUS}${-v}` : String(v));
  function fmtHz(f) {
    if (f < 100) return `${f.toFixed(1)} Hz`;
    return `${String(Math.round(f)).replace(/\B(?=(\d{3})+(?!\d))/g, ' ')} Hz`;
  }
  const fmtHzShort = (f) => (f >= 1000 && f % 100 === 0 ? `${f / 1000} kHz` : `${f} Hz`);
  const tickLabel = (f) => (f >= 1000 ? `${f / 1000}k` : String(f));

  // The nearest note and the distance to it ("D#6 -12 ct"), A4 = 440 Hz.
  function noteOf(f) {
    if (!(f > 0)) return '';
    const m = 69 + 12 * Math.log2(f / 440);
    const r = Math.round(m);
    if (r < 0) return '';
    const cents = Math.round((m - r) * 100);
    const c = cents === 0 ? '0' : cents < 0 ? `${MINUS}${-cents}` : `+${cents}`;
    return `${NOTES[r % 12]}${Math.floor(r / 12) - 1} ${c} ct`;
  }

  // 1-2-5 per decade between fmin and fhi, labelled ("20 50 100 ... 20k"). A Freq Lo of 0 is drawn
  // as 1 Hz and labelled 0.
  function freqTicks(fmin, fhi, freqLo) {
    const out = [];
    for (let e = Math.floor(Math.log10(fmin)) - 1; e <= Math.ceil(Math.log10(fhi)); e++) {
      for (const m of [1, 2, 5]) {
        const f = m * 10 ** e;
        if (f >= fmin * 0.9999 && f <= fhi * 1.0001) out.push({ f, label: f === 1 && freqLo === 0 ? '0' : tickLabel(f) });
      }
    }
    return out;
  }

  // ---------- colours ----------

  // Any CSS colour as something a canvas takes (a 1 px probe canvas turns color-mix() and friends into rgba).
  let probeCtx = null;
  function colorOf(css, fallback) {
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
  function rgbaOf(str) {
    const m = /rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)(?:[,\s/]+([\d.]+))?/.exec(str || '');
    return m ? [+m[1], +m[2], +m[3], m[4] === undefined ? 1 : +m[4]] : [136, 136, 136, 1];
  }
  const rgba = (c, k) => `rgba(${c[0]}, ${c[1]}, ${c[2]}, ${(c[3] * k).toFixed(3)})`;

  // The colour ramp: 256 packed RGBA entries, linear in RGB between the stops.
  function buildLut(stops) {
    const lut = new Uint32Array(256);
    for (let i = 0; i < 256; i++) {
      const p = (i / 255) * (stops.length - 1);
      const s = Math.min(stops.length - 2, Math.floor(p));
      const k = p - s;
      const a = stops[s];
      const b = stops[s + 1];
      const r = Math.round(a[0] + (b[0] - a[0]) * k);
      const g = Math.round(a[1] + (b[1] - a[1]) * k);
      const bl = Math.round(a[2] + (b[2] - a[2]) * k);
      lut[i] = ((255 << 24) | (bl << 16) | (g << 8) | r) >>> 0;   // little-endian RGBA
    }
    return lut;
  }

  // ---------- a ring of the newest stereo frames ----------

  // Keeps the newest `cap` (a power of two) frames of one signal, L and R apart. `total` counts
  // every frame ever pushed (an absolute position), so a reader can ask for "the block that ends
  // at frame e". Frames before the start of the stream or older than the ring reads as 0.
  function createRing(cap = 32768) {
    if (!(cap >= 2) || (cap & (cap - 1)) !== 0) throw new Error(`ring size must be a power of two, got ${cap}`);
    const l = new Float32Array(cap);
    const r = new Float32Array(cap);
    const ring = {
      cap, l, r, total: 0,
      // `n` frames of interleaved stereo `frames`, starting at frame `from`. More than `cap` keeps the newest.
      push(frames, n, from = 0) {
        if (n > cap) { from += n - cap; ring.total += n - cap; n = cap; }
        let w = ring.total % cap;
        for (let i = 0, k = from * 2; i < n; i++, k += 2) {
          l[w] = frames[k];
          r[w] = frames[k + 1];
          if (++w === cap) w = 0;
        }
        ring.total += n;
      },
      // The `n` frames that end at absolute frame `end`, into outL / outR (Float32Arrays of length >= n).
      block(end, n, outL, outR) {
        const start = end - n;
        const have0 = Math.max(0, ring.total - cap);           // the oldest frame still held
        const i0 = Math.max(0, Math.min(n, have0 - start));    // output index where held data begins
        const i1 = Math.max(i0, Math.min(n, ring.total - start));
        for (let i = 0; i < i0; i++) { outL[i] = 0; outR[i] = 0; }
        let p = (start + i0) % cap;
        for (let i = i0; i < i1; i++) {
          outL[i] = l[p];
          outR[i] = r[p];
          if (++p === cap) p = 0;
        }
        for (let i = i1; i < n; i++) { outL[i] = 0; outR[i] = 0; }
        return outL;
      },
      latest(n, outL, outR) { return ring.block(ring.total, n, outL, outR); },
      clear() { l.fill(0); r.fill(0); ring.total = 0; },
    };
    return ring;
  }

  V.AnalyzerDSP = {
    fft, windowOf, power, powerDb, rms, truePeak, truePeakStream, buildMap, applyMap,
    noteOf, fmtDb, fmtInt, fmtHz, fmtHzShort, freqTicks,
    colorOf, rgbaOf, rgba, buildLut, createRing,
    DEFAULTS, LIMITS, BLOCKS, WINDOWS, FPS, AVG_TIMES, HISTORIES, VIEWS, SIGNALS, LAYOUTS, METERS, normalize,
    FLOOR_DB, MINUS,
  };
})();
