// The Scan button's 3D halo, mounted by js/fx.js only while visual effects are on and the
// Scan section is shown. Pure decoration: the real <button> sits on top and keeps focus,
// label and disabled state; this canvas is aria-hidden and ignores the pointer.
//
// A 3D point scene (a shell of dots around the button and three tilted orbits), projected
// by hand and drawn on a 2D canvas: no WebGL, no library, runs on the CPU alone.
// Its size follows the first screen (owner, 2026-10-15: "too small"): the stage is ~58% of
// the content height (css --scan-stage-h), the orbits grow with it while the shell keeps
// hugging the (also larger) button, and the dots grow a little.
// Kept cheap on purpose (owner, 2026-10-15: "better if it even can be run with CPU only"):
// ~900 points, one pre-rendered soft dot per colour, typed arrays filled once (nothing is
// allocated per frame), a backing store capped at 1.5x the CSS size and ~1.3 M pixels, at
// most 30 frames a second, and no frames at all when nothing moves. Hidden window or stage
// scrolled away: no frames either. It also moves the hero's picture wall (opts.wall, a
// transform): a little parallax with the pointer, a slow sway while scanning.
//
// Three moments:
//   entrance  the section opens: the dots gather into a shell, the orbits sweep in;
//   scanning  the shell tightens and turns, a scan line passes through it, the orbits
//             speed up as the scan runs towards (and past) the length of the last one;
//   finish    success: a ripple runs outwards and the orbits lift; error: a short
//             shiver tinted with the danger colour. Then it settles and stops drawing.
// Colours come from the CSS variables at runtime, so light and dark both work.

const SHELL_R = 1.3;      // world units; ~62 px per unit on a 220 px tall stage (0.28 x its height)
const RING_A_R = 2.3;
const RING_B_R = 1.9;
const RING_C_R = 2.95;    // a wide, faint outer orbit: the scene fills the larger stage
const CAM_Z = 9;
const FOV = 22.5;         // degrees, vertical
const FRAME_MS = 33;      // 30 fps at most
const MAX_DPR = 1.5;
const MAX_PIXELS = 1.3e6; // backing store cap: each frame clears it, the compositor blends it
const WALL_MAX = 44;      // px the wall behind the hero may shift (style.css gives it 48 px of bleed)

const clamp01 = (v) => (v < 0 ? 0 : v > 1 ? 1 : v);
const approach = (v, target, rate, dt) => v + (target - v) * (1 - Math.exp(-rate * dt));
const outCubic = (t) => { t = clamp01(t); const u = 1 - t; return 1 - u * u * u; };

// ---------- colours from the stylesheet ----------
function parseColor(s) {
  const nums = (String(s).match(/-?[\d.]+(?:e-?\d+)?/g) || []).map(Number);
  if (/^color\(srgb/i.test(s) && nums.length >= 3) return nums.slice(0, 3).map(v => Math.round(clamp01(v) * 255));
  if (/^rgba?\(/i.test(s) && nums.length >= 3) return nums.slice(0, 3).map(Math.round);
  return null;
}
function readColors(stage) {
  const probe = document.createElement('i');
  probe.style.cssText = 'position:absolute;width:0;height:0;overflow:hidden;visibility:hidden;';
  stage.appendChild(probe);
  const read = (expr, fallback) => {
    probe.style.color = '';
    probe.style.color = expr;
    return parseColor(getComputedStyle(probe).color) || fallback;
  };
  const bg = read('var(--bg-content)', [242, 244, 243]);
  const dark = (0.2126 * bg[0] + 0.7152 * bg[1] + 0.0722 * bg[2]) / 255 < 0.35;
  const c = {
    dark,
    a: dark ? read('var(--accent)', [169, 146, 125]) : read('color-mix(in srgb, var(--accent) 82%, var(--fg))', [148, 128, 108]),
    b: read('var(--fg-3)', [138, 148, 151]),
    hi: read('var(--fg)', dark ? [242, 244, 243] : [10, 9, 8]),
    err: read('var(--danger)', [196, 40, 47]),
  };
  probe.remove();
  return c;
}

// A soft round dot (solid to half its radius, then fading), drawn once per colour.
function dotSprite([r, g, b]) {
  const size = 32;
  const c = document.createElement('canvas');
  c.width = c.height = size;
  const ctx = c.getContext('2d');
  const grad = ctx.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
  grad.addColorStop(0, `rgba(${r},${g},${b},1)`);
  grad.addColorStop(0.5, `rgba(${r},${g},${b},1)`);
  grad.addColorStop(1, `rgba(${r},${g},${b},0)`);
  ctx.fillStyle = grad;
  ctx.fillRect(0, 0, size, size);
  return c;
}

// ---------- the points (built once) ----------
function rand(seed) {
  let s = seed >>> 0;
  return () => { s = (s * 1664525 + 1013904223) >>> 0; return s / 4294967296; };
}

function buildPoints() {
  const r = rand(7);
  const pts = [];
  // The shell: a Fibonacci sphere. [group, x, y, z, seed, tone, dim, size]
  const n = 260;
  const golden = Math.PI * (3 - Math.sqrt(5));
  for (let i = 0; i < n; i++) {
    const y = 1 - ((i + 0.5) * 2) / n;
    const rr = Math.sqrt(1 - y * y);
    const phi = i * golden;
    const rad = SHELL_R * (1 + (r() - 0.5) * 0.05);
    pts.push([0, Math.cos(phi) * rr * rad, y * rad, Math.sin(phi) * rr * rad, r(), r() > 0.8 ? 1 : 0, 0.75 + r() * 0.25, 2.7]);
  }
  // An orbit: a faint full track plus a few bright arcs that taper, so its turning shows.
  const ring = (group, radius, track, arcs, size) => {
    for (let i = 0; i < track; i++) {
      const a = (i / track) * Math.PI * 2;
      pts.push([group, Math.cos(a) * radius, Math.sin(a) * radius, 0, r(), 1, 0.22, size]);
    }
    for (const [start, span, count] of arcs) {
      for (let i = 0; i < count; i++) {
        const t = i / (count - 1);
        const a = start + span * t;
        pts.push([group, Math.cos(a) * radius, Math.sin(a) * radius, 0, r(), r() > 0.85 ? 1 : 0, 0.35 + 0.65 * Math.sin(Math.PI * t), size]);
      }
    }
  };
  // Track points spaced for a big stage (the orbits grow with it; the shell doesn't).
  ring(1, RING_A_R, 120, [[0.2, 2.3, 80], [3.4, 1.4, 50]], 2.3);
  ring(2, RING_B_R, 100, [[1.0, 1.1, 36], [2.9, 0.9, 30], [4.6, 1.0, 34]], 2.0);
  ring(3, RING_C_R, 120, [[0.7, 1.5, 46], [4.0, 1.1, 32]], 1.9);

  const N = pts.length;
  const P = {
    n: N,
    group: new Uint8Array(N), hx: new Float32Array(N), hy: new Float32Array(N), hz: new Float32Array(N),
    len: new Float32Array(N), seed: new Float32Array(N), tone: new Uint8Array(N), dim: new Float32Array(N), size: new Float32Array(N),
    jx: new Float32Array(N), jy: new Float32Array(N), jz: new Float32Array(N),
  };
  pts.forEach(([g, x, y, z, s, t, d, sz], i) => {
    P.group[i] = g; P.hx[i] = x; P.hy[i] = y; P.hz[i] = z;
    P.len[i] = Math.hypot(x, y, z) || 1e-5;
    P.seed[i] = s; P.tone[i] = t; P.dim[i] = d; P.size[i] = sz;
    // Fixed per-point directions for the error shiver.
    P.jx[i] = ((s * 91.7) % 1) - 0.5; P.jy[i] = ((s * 47.3) % 1) - 0.5; P.jz[i] = ((s * 13.1) % 1) - 0.5;
  });
  return P;
}

// ---------- 3x3 rotation matrices, preallocated (row-major) ----------
function setRot(m, ax, ay, az) {
  // An XYZ Euler rotation: Rx * Ry * Rz.
  const cx = Math.cos(ax), sx = Math.sin(ax), cy = Math.cos(ay), sy = Math.sin(ay), cz = Math.cos(az), sz = Math.sin(az);
  m[0] = cy * cz;                 m[1] = -cy * sz;                m[2] = sy;
  m[3] = sx * sy * cz + cx * sz;  m[4] = -sx * sy * sz + cx * cz; m[5] = -sx * cy;
  m[6] = -cx * sy * cz + sx * sz; m[7] = cx * sy * sz + sx * cz;  m[8] = cx * cy;
}
function mul(out, a, b) {
  const a0 = a[0], a1 = a[1], a2 = a[2], a3 = a[3], a4 = a[4], a5 = a[5], a6 = a[6], a7 = a[7], a8 = a[8];
  out[0] = a0 * b[0] + a1 * b[3] + a2 * b[6]; out[1] = a0 * b[1] + a1 * b[4] + a2 * b[7]; out[2] = a0 * b[2] + a1 * b[5] + a2 * b[8];
  out[3] = a3 * b[0] + a4 * b[3] + a5 * b[6]; out[4] = a3 * b[1] + a4 * b[4] + a5 * b[7]; out[5] = a3 * b[2] + a4 * b[5] + a5 * b[8];
  out[6] = a6 * b[0] + a7 * b[3] + a8 * b[6]; out[7] = a6 * b[1] + a7 * b[4] + a8 * b[7]; out[8] = a6 * b[2] + a7 * b[5] + a8 * b[8];
}

export function mount(stage, opts = {}) {
  const button = opts.button || stage.querySelector('button');
  // The hero's picture wall (js/scan-wall.js): shifted a little with the pointer (parallax
  // with the scene's tilt) and drifting slowly while a scan runs. A transform only.
  const wall = opts.wall || null;
  const canvas = document.createElement('canvas');
  canvas.className = 'scan-fx';
  canvas.setAttribute('aria-hidden', 'true');
  stage.insertBefore(canvas, stage.firstChild);
  const ctx = canvas.getContext('2d', { alpha: true });
  if (!ctx) { canvas.remove(); throw new Error('no 2D canvas'); }

  const P = buildPoints();
  // Per group (shell, orbits A, B and C): its own rotation, and rig x own used per point.
  const G = 4;
  const mRig = new Float64Array(9), mTmp = new Float64Array(9), mSpin = new Float64Array(9);
  const mOwn = Array.from({ length: G }, () => new Float64Array(9));
  const mAll = Array.from({ length: G }, () => new Float64Array(9));
  const groupScale = new Float32Array(G);
  const groupIntro = new Float32Array(G);
  const groupAlpha = new Float32Array(G);

  let colors = null;
  let sprites = null;
  let sizeK = 1;
  // The stage follows the first screen (css: --scan-stage-h), so the scene is scaled at
  // each resize: the orbits grow with the stage, the shell keeps hugging the button
  // (shellK = its scale) and the dots grow a little (dotK).
  let shellK = 1, dotK = 1;
  const depthR = new Float32Array([1.6, 1.6, 1.6, 1.6]);
  function applyColors() {
    colors = readColors(stage);
    sprites = { a: dotSprite(colors.a), b: dotSprite(colors.b), hi: dotSprite(colors.hi), err: dotSprite(colors.err) };
    groupAlpha[0] = colors.dark ? 0.8 : 0.9;
    groupAlpha[1] = colors.dark ? 0.85 : 0.85;
    groupAlpha[2] = colors.dark ? 0.75 : 0.78;
    groupAlpha[3] = colors.dark ? 0.62 : 0.66;
    // Dark dots on a light surface read smaller than light ones glowing on dark.
    sizeK = colors.dark ? 1 : 1.18;
  }
  applyColors();

  // ---------- state ----------
  const st = {
    clock: 0, intro: 0,
    scan: 0, scanTarget: 0, scanStart: 0, expected: 0,
    press: 0, pressTarget: 0,
    pulse: -1, pulseKind: 1,
    sweepPhase: 0,
    tiltX: 0, tiltY: 0, tiltTX: 0, tiltTY: 0,
    spinShell: 0, spinA: 0, spinB: 0, spinC: 0,
    drift: 0,
  };
  if (opts.scan && opts.scan.running) {
    st.scanTarget = 1;
    st.scanStart = opts.scan.startedAt || Date.now();
    st.expected = opts.scan.expectedMs || 0;
  }

  let dpr = 1, focal = 1;
  let wallX = 0, wallY = 0;
  let timer = 0, raf = 0, lastDraw = 0, lastTick = 0;
  let frames = 0, drawMsTotal = 0;
  let visible = false, destroyed = false;

  const active = () => !destroyed && visible && !document.hidden;

  function intensity() {
    if (!st.scanStart) return 0.6;
    const elapsed = (Date.now() - st.scanStart) / 1000;
    const expected = st.expected > 0 ? st.expected / 1000 : 25;
    return 0.45 + 0.55 * (1 - Math.exp(-elapsed / expected));
  }

  function update(dt) {
    st.clock += dt;
    if (st.intro < 1) st.intro = Math.min(1, st.intro + dt / 1.4);
    st.scan = approach(st.scan, st.scanTarget, st.scanTarget > st.scan ? 4 : 2.2, dt);
    if (Math.abs(st.scan - st.scanTarget) < 0.003) st.scan = st.scanTarget;
    st.press = approach(st.press, st.pressTarget, 14, dt);
    if (Math.abs(st.press - st.pressTarget) < 0.003) st.press = st.pressTarget;
    st.tiltX = approach(st.tiltX, st.tiltTX, 6, dt);
    st.tiltY = approach(st.tiltY, st.tiltTY, 6, dt);
    if (Math.abs(st.tiltX - st.tiltTX) < 0.001) st.tiltX = st.tiltTX;
    if (Math.abs(st.tiltY - st.tiltTY) < 0.001) st.tiltY = st.tiltTY;

    const k = st.scan * intensity();
    st.spinShell += dt * (0.06 + k * 1.1);
    st.spinA += dt * (0.1 + k * 1.4);
    st.spinB -= dt * (0.08 + k * 1.1);
    st.spinC -= dt * (0.04 + k * 0.7);
    if (st.scan > 0.002) {
      st.sweepPhase += dt / (1.7 - 0.7 * k);
      st.drift += dt * 0.21; // one slow sway of the wall every ~30 s
    }

    let ringScale = 1;
    if (st.pulse >= 0) {
      st.pulse += dt / (st.pulseKind > 0 ? 1.35 : 1.0);
      const bump = Math.sin(Math.PI * Math.min(1, st.pulse));
      ringScale = st.pulseKind > 0 ? 1 + 0.1 * bump : 1 - 0.05 * bump;
      if (st.pulse >= 1) st.pulse = -1;
    }
    groupScale[0] = shellK; groupScale[1] = ringScale; groupScale[2] = ringScale; groupScale[3] = ringScale;
    groupIntro[0] = st.intro;
    groupIntro[1] = clamp01((st.intro * 1.5 - 0.2) / 1.3);
    groupIntro[2] = clamp01((st.intro * 1.5 - 0.3) / 1.2);
    groupIntro[3] = clamp01((st.intro * 1.5 - 0.4) / 1.1);

    setRot(mRig, st.tiltX, st.tiltY, 0);
    setRot(mOwn[0], 0.35, st.spinShell, 0);
    setRot(mTmp, 1.24, 0, -0.2); setRot(mSpin, 0, 0, st.spinA); mul(mOwn[1], mTmp, mSpin);
    setRot(mTmp, 1.1, 0, 0.34); setRot(mSpin, 0, 0, st.spinB); mul(mOwn[2], mTmp, mSpin);
    setRot(mTmp, 1.36, 0, 0.1); setRot(mSpin, 0, 0, st.spinC); mul(mOwn[3], mTmp, mSpin);
    for (let g = 0; g < G; g++) mul(mAll[g], mRig, mOwn[g]);
  }

  // True while anything moves; false = keep showing the last frame, draw nothing.
  function moving() {
    return st.intro < 1 || st.pulse >= 0 || st.scanTarget > 0 || st.scan > 0
      || st.press !== st.pressTarget || st.tiltX !== st.tiltTX || st.tiltY !== st.tiltTY;
  }

  function draw() {
    const t0 = performance.now();
    ctx.globalAlpha = 1;
    ctx.globalCompositeOperation = 'source-over';
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.globalCompositeOperation = colors.dark ? 'lighter' : 'source-over';
    const cx = canvas.width / 2;
    const cy = canvas.height / 2;
    const sweepY = 1.75 * Math.max(0.68, shellK) * Math.cos(st.sweepPhase * Math.PI);
    const sweepAmt = st.scan;
    const pulse = st.pulse;
    const pulseOk = st.pulseKind > 0;
    const shrink = 1 - 0.06 * st.press - 0.07 * st.scan;
    const breatheAmt = 0.02 * st.scan;
    const clock = st.clock;
    const sa = sprites.a, sb = sprites.b, shi = sprites.hi, serr = sprites.err;
    const n = P.n;
    for (let i = 0; i < n; i++) {
      const g = P.group[i];
      const seed = P.seed[i];
      const k = outCubic((groupIntro[g] - seed * 0.4) / 0.6);
      if (k <= 0) continue;
      const m = mAll[g];
      const gs = groupScale[g];
      let x = P.hx[i] * gs, y = P.hy[i] * gs, z = P.hz[i] * gs;
      const len = P.len[i] * gs;
      const dx = x / len, dy = y / len, dz = z / len;
      // Entrance: from scattered to home.
      if (k < 1) {
        const far = len * (1.7 + seed * 1.3);
        const u = 1 - k;
        x += (dx * far - x) * u;
        y += (dy * far + (seed - 0.5) - y) * u;
        z += (dz * far - z) * u;
      }
      const s = shrink + breatheAmt * Math.sin(clock * 2.6 + seed * 6.2831);
      x *= s; y *= s; z *= s;
      // Finish: a wave over the side facing the viewer (a ripple out, or a tinted shiver in).
      let wave = 0;
      if (pulse >= 0) {
        const vz = m[6] * dx + m[7] * dy + m[8] * dz;
        const ang = Math.acos(vz < -1 ? -1 : vz > 1 ? 1 : vz) / Math.PI;
        const d = (ang - (pulse * 1.4 - 0.1)) * 6;
        wave = Math.exp(-d * d) * (1 - pulse);
        if (pulseOk) {
          x += dx * wave * 0.3; y += dy * wave * 0.3; z += dz * wave * 0.3;
        } else {
          x -= dx * wave * 0.12; y -= dy * wave * 0.12; z -= dz * wave * 0.12;
          const j = 0.08 * (1 - pulse) * Math.sin(clock * 55 + seed * 40);
          x += P.jx[i] * j; y += P.jy[i] * j; z += P.jz[i] * j;
        }
      }
      const wx = m[0] * x + m[1] * y + m[2] * z;
      const wy = m[3] * x + m[4] * y + m[5] * z;
      const wz = m[6] * x + m[7] * y + m[8] * z;
      const zc = CAM_Z - wz;
      const persp = focal / zc;
      const sw = (wy - sweepY) * 7;
      const sweep = sweepAmt > 0 ? Math.exp(-sw * sw) * sweepAmt : 0;
      const depth = clamp01((wz / depthR[g] + 1) / 2);
      const hi = clamp01(sweep + (pulseOk ? wave : 0));
      const err = !pulseOk && pulse >= 0 ? clamp01(wave * 1.5 + 0.4 * (1 - pulse)) : 0;
      let alpha = groupAlpha[g] * P.dim[i] * k * (0.25 + 0.75 * depth) * (1 + hi * 0.6);
      if (alpha > 1) alpha = 1;
      if (alpha < 0.01) continue;
      const size = P.size[i] * (0.7 + seed * 0.6) * (1 + sweep * 1.3 + wave * 1.4) * (CAM_Z / zc) * dpr * sizeK * dotK;
      const half = size / 2;
      const px = cx + wx * persp - half;
      const py = cy - wy * persp - half;
      const hiMix = hi * 0.85;
      const base = (1 - hiMix) * (1 - err);
      if (base > 0.02) {
        ctx.globalAlpha = alpha * base;
        ctx.drawImage(P.tone[i] ? sb : sa, px, py, size, size);
      }
      if (hiMix > 0.02) {
        ctx.globalAlpha = alpha * hiMix * (1 - err);
        ctx.drawImage(shi, px, py, size, size);
      }
      if (err > 0.02) {
        ctx.globalAlpha = alpha * err;
        ctx.drawImage(serr, px, py, size, size);
      }
    }
    frames++;
    drawMsTotal += performance.now() - t0;
    if (!canvas.classList.contains('is-ready')) canvas.classList.add('is-ready');
    moveWall();
  }

  // The wall leans against the tilt (parallax) and sways while scanning; half-pixel steps,
  // and only a changed value is written.
  function moveWall() {
    if (!wall) return;
    const sway = st.scan;
    let x = -st.tiltY * 90 + Math.sin(st.drift) * 30 * sway;
    let y = st.tiltX * 70 + Math.sin(st.drift * 0.63 + 1.1) * 12 * sway;
    x = Math.max(-WALL_MAX, Math.min(WALL_MAX, Math.round(x * 2) / 2));
    y = Math.max(-WALL_MAX, Math.min(WALL_MAX, Math.round(y * 2) / 2));
    if (x === wallX && y === wallY) return;
    wallX = x; wallY = y;
    wall.style.transform = x || y ? `translate3d(${x}px, ${y}px, 0)` : '';
  }

  function frame(now) {
    raf = 0;
    if (!active()) return;
    // Pace on the rAF clock: a vsync that comes too soon after the last drawn frame is skipped.
    if (lastDraw && now - lastDraw < FRAME_MS - 4) { raf = requestAnimationFrame(frame); return; }
    const dt = lastTick ? Math.min(0.066, (now - lastTick) / 1000) : 1 / 30;
    lastTick = now;
    lastDraw = now;
    update(dt);
    draw();
    schedule();
  }

  // The next frame, at most 30 a second, only while something moves.
  function schedule() {
    if (!active() || raf || timer) return;
    if (!moving()) { lastTick = 0; lastDraw = 0; return; }
    // Sleep most of the gap, then let frame() line up with a vsync.
    timer = setTimeout(() => { timer = 0; if (active()) raf = requestAnimationFrame(frame); }, FRAME_MS - 20);
  }

  function stop() {
    if (timer) clearTimeout(timer);
    if (raf) cancelAnimationFrame(raf);
    timer = 0; raf = 0; lastTick = 0; lastDraw = 0;
  }

  // One frame now (a resize, a theme change, coming back into view), then on demand.
  function redraw() {
    if (!active()) return;
    if (!raf && !timer) { update(0); draw(); }
    schedule();
  }

  function resize() {
    const w = Math.max(1, canvas.clientWidth);
    const h = Math.max(1, canvas.clientHeight);
    dpr = Math.min(window.devicePixelRatio || 1, MAX_DPR, Math.sqrt(MAX_PIXELS / (w * h)));
    const W = Math.round(w * dpr);
    const H = Math.round(h * dpr);
    if (canvas.width !== W) canvas.width = W;
    if (canvas.height !== H) canvas.height = H;
    focal = (H / 2) / Math.tan((FOV * Math.PI) / 360);
    // CSS px per world unit at the scene's centre.
    const unit = (h / 2) / Math.tan((FOV * Math.PI) / 360) / CAM_Z;
    const btnR = button && button.offsetWidth ? button.offsetWidth / 2 : 64;
    shellK = Math.max(0.42, Math.min(1.1, (btnR * 1.3) / (SHELL_R * unit)));
    depthR[0] = 1.6 * shellK;
    dotK = Math.pow(Math.max(1, h / 220), 0.55);
    redraw();
  }

  // ---------- events ----------
  const io = new IntersectionObserver((entries) => {
    const e = entries[entries.length - 1];
    visible = !!(e && e.isIntersecting);
    if (visible) redraw(); else stop();
  });
  io.observe(stage);
  const ro = new ResizeObserver(() => resize());
  ro.observe(canvas);

  const onVisibility = () => { if (document.hidden) stop(); else redraw(); };
  document.addEventListener('visibilitychange', onVisibility);

  // The pointer tilts the scene a little (quantized, so small moves don't wake it up).
  const onPointerMove = (e) => {
    const r = stage.getBoundingClientRect();
    const nx = Math.max(-1, Math.min(1, (e.clientX - (r.left + r.width / 2)) / (r.width / 2 || 1)));
    const ny = Math.max(-1, Math.min(1, (e.clientY - (r.top + r.height / 2)) / (r.height / 2 || 1)));
    const ty = Math.round(nx * 22) / 100;
    const tx = Math.round(ny * 16) / 100;
    if (ty === st.tiltTY && tx === st.tiltTX) return;
    st.tiltTY = ty;
    st.tiltTX = tx;
    schedule();
  };
  const onPointerLeave = () => { st.tiltTX = 0; st.tiltTY = 0; st.pressTarget = 0; schedule(); };
  const pressOn = () => { if (!button || !button.disabled) { st.pressTarget = 1; schedule(); } };
  const pressOff = () => { st.pressTarget = 0; schedule(); };
  const onKey = (e) => { if (e.key === ' ' || e.key === 'Enter') { pressOn(); setTimeout(pressOff, 140); } };
  stage.addEventListener('pointermove', onPointerMove);
  stage.addEventListener('pointerleave', onPointerLeave);
  if (button) {
    button.addEventListener('pointerdown', pressOn);
    button.addEventListener('pointerup', pressOff);
    button.addEventListener('pointercancel', pressOff);
    button.addEventListener('keydown', onKey);
  }

  const onTheme = () => { applyColors(); redraw(); };
  document.addEventListener('vext:themechange', onTheme);
  const darkQuery = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;
  if (darkQuery && darkQuery.addEventListener) darkQuery.addEventListener('change', onTheme);
  // A GPU reset can drop a 2D canvas too: draw again once it's back.
  canvas.addEventListener('contextrestored', onTheme);

  resize();

  return {
    // {phase: 'start', at, expectedMs} or {phase: 'end', ok}
    scan(detail = {}) {
      if (destroyed) return;
      if (detail.phase === 'start') {
        st.scanTarget = 1;
        st.scanStart = detail.at || Date.now();
        st.expected = detail.expectedMs || 0;
        st.pulse = -1;
      } else if (detail.phase === 'end') {
        st.scanTarget = 0;
        st.scanStart = 0;
        st.pulse = 0;
        st.pulseKind = detail.ok === false ? -1 : 1;
      }
      schedule();
    },
    // For checks: frames drawn, average draw time (ms), whether a frame is scheduled.
    stats() {
      return {
        frames,
        drawMs: frames ? Math.round((drawMsTotal / frames) * 100) / 100 : 0,
        points: P.n,
        looping: !!(raf || timer),
        visible,
        size: `${canvas.width}x${canvas.height}`,
        shellK: Math.round(shellK * 100) / 100,
        wall: wall ? `${wallX},${wallY}` : null,
        phase: st.pulse >= 0 ? 'finish' : st.scanTarget ? 'scanning' : st.intro < 1 ? 'entrance' : 'idle',
      };
    },
    destroy() {
      if (destroyed) return;
      destroyed = true;
      stop();
      io.disconnect();
      ro.disconnect();
      document.removeEventListener('visibilitychange', onVisibility);
      document.removeEventListener('vext:themechange', onTheme);
      if (darkQuery && darkQuery.removeEventListener) darkQuery.removeEventListener('change', onTheme);
      stage.removeEventListener('pointermove', onPointerMove);
      stage.removeEventListener('pointerleave', onPointerLeave);
      if (button) {
        button.removeEventListener('pointerdown', pressOn);
        button.removeEventListener('pointerup', pressOff);
        button.removeEventListener('pointercancel', pressOff);
        button.removeEventListener('keydown', onKey);
      }
      canvas.removeEventListener('contextrestored', onTheme);
      canvas.width = canvas.height = 0; // release the backing store now
      canvas.remove();
      if (wall) wall.style.transform = '';
    },
  };
}
