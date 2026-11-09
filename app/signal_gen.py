"""The Playground's Signal tab: test signals made from a patch (one oscillator with unison
and pan, noise, impulses, a master level), rendered to a 32-bit float WAV that the Audio tab
plays like any file. Plugins, Record, Capture and the Analyzer work on it unchanged (AGENTS.md
"Playground: Browser | Signal | Audio").

The oscillator is a band-limited wavetable. Each waveform (sine, triangle, saw, square, pulse)
is a stack of 11 mip levels: level k keeps harmonics 1..1024 >> k of a 2048-sample cycle. A
voice reads the level whose top harmonic stays under 0.45 x the rate, so high notes don't alias.
Everything is numpy, vectorized over voices and frames (no per-sample Python loop). Frames are
rendered in chunks, so a 60-second file never holds a full-length array per voice. The random
generator is seeded from the patch's hash: the same patch gives the same bytes.

The endpoints are in app/web.py (POST /api/playground/signal).
"""

import hashlib
import json
import math
import os
import threading
from pathlib import Path

from app import host_worker
from app.config import PROJECT_ROOT

CACHE_DIR = PROJECT_ROOT / "cache" / "playground"
N = 2048  # samples per oscillator cycle
LEVELS = 11  # mip levels: level k keeps harmonics 1..(N // 2) >> k
PREVIEW_FRAMES = 4096
PREVIEW_AT_S = 0.5
KEEP_SIGNALS = 6  # rendered signal files kept in cache/playground/signals
WORK_LIMIT = 40e6  # voices x frames; above it the unison is cut (say so in info.note)
CHUNK = 1 << 13  # frames per numpy pass: small enough to stay in cache (16k and up measured 2x slower)
RATES = (44100, 48000, 88200, 96000)
CHANNEL_MODES = ("stereo", "mono", "left", "right", "invert")
WAVES = ("sine", "triangle", "saw", "square", "pulse")
WAVE_LABEL = {"sine": "Sine", "triangle": "Triangle", "saw": "Saw", "square": "Square", "pulse": "Pulse"}
NOISE_ALPHA = {"white": 0.0, "pink": -1.0, "brown": -2.0, "blue": 1.0, "violet": 2.0}  # power ~ f^alpha
MASTER_DB = 24  # the master level: -24..+24 dB

_cache: dict = {}  # built mip stacks, by waveform (and pulse width)
_cache_lock = threading.Lock()


def _signals_dir() -> Path:
    return CACHE_DIR / "signals"


# ---------- the patch ----------

def _num(v, lo: float, hi: float, default: float) -> float:
    """A number clamped to [lo, hi]; anything that isn't a finite number gives the default."""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(x):
        return float(default)
    return float(min(hi, max(lo, x)))


def _flag(v, default: bool) -> bool:
    return v if isinstance(v, bool) else default


def _pick(v, options: tuple, default: str) -> str:
    return v if isinstance(v, str) and v in options else default


def _obj(v) -> dict:
    return v if isinstance(v, dict) else {}


def _osc(o: dict) -> dict:
    return {
        "on": _flag(o.get("on"), True),
        "wave": _pick(o.get("wave"), WAVES, "sine"),  # an unknown wave (wavetable included) is a sine
        "width": _num(o.get("width"), 0.01, 0.99, 0.5),
        "freq": _num(o.get("freq"), 1, 24000, 1000),
        "semitones": _num(o.get("semitones"), -48, 48, 0),
        "detune": _num(o.get("detune"), -100, 100, 0),
        "phase": _num(o.get("phase"), 0, 360, 0),
        "unison": int(_num(o.get("unison"), 1, 8, 1)),
        "unison_detune": _num(o.get("unison_detune"), 0, 100, 15),
        "unison_spread": _num(o.get("unison_spread"), 0, 1, 0.5),
        "random_phase": _flag(o.get("random_phase"), False),
        "level_db": _num(o.get("level_db"), -60, 0, 0),
        "pan": _num(o.get("pan"), -1, 1, 0),
        "invert": _flag(o.get("invert"), False),
    }


def clean_patch(patch) -> dict:
    """The patch with every number clamped, the defaults filled in and unknown keys dropped.
    Only a patch that isn't an object raises (ValueError). The same patch always cleans to the
    same dict: its hash names the file and seeds the noise."""
    if not isinstance(patch, dict):
        raise ValueError("The signal must be an object.")  # noqa: TRY004 - the endpoint answers 409 for it
    rate = int(_num(patch.get("rate"), 0, 1e6, 48000))
    if rate not in RATES:
        rate = 48000
    noise, imp = _obj(patch.get("noise")), _obj(patch.get("impulse"))
    return {
        "seconds": _num(patch.get("seconds"), 0.5, 60, 10),
        "rate": rate,
        "level_db": _num(patch.get("level_db"), -80, 0, -18),
        "normalize": _flag(patch.get("normalize"), True),
        "channels": _pick(patch.get("channels"), CHANNEL_MODES, "stereo"),
        "seam_ms": _num(patch.get("seam_ms"), 0, 200, 20),
        "osc": _osc(_obj(patch.get("osc"))),  # missing: one sine at 1 kHz
        "noise": {
            "on": _flag(noise.get("on"), False),
            "color": _pick(noise.get("color"), tuple(NOISE_ALPHA), "pink"),
            "level_db": _num(noise.get("level_db"), -60, 0, 0),
            "stereo": _pick(noise.get("stereo"), ("same", "independent"), "same"),
        },
        "impulse": {
            "on": _flag(imp.get("on"), False),
            "every_ms": _num(imp.get("every_ms"), 10, 5000, 1000),
            "level_db": _num(imp.get("level_db"), -60, 0, 0),
            "polarity": _pick(imp.get("polarity"), ("positive", "negative", "alternate"), "positive"),
        },
        "master_db": _num(patch.get("master_db"), -MASTER_DB, MASTER_DB, 0),
        "dc": _num(patch.get("dc"), -0.5, 0.5, 0),
    }


def patch_key(clean: dict) -> str:
    """SHA-1 of the clean patch as sorted JSON: names the file and seeds the noise."""
    return hashlib.sha1(json.dumps(clean, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


# ---------- oscillator tables ----------

def _cached(key, make):
    """A built stack, kept in memory (a few at most: the cache is cleared when it grows)."""
    with _cache_lock:
        hit = _cache.get(key)
    if hit is None:
        hit = make()
        with _cache_lock:
            if len(_cache) >= 32:
                _cache.clear()
            _cache[key] = hit
    return hit


def _peak_norm(frames):
    import numpy as np

    peak = np.abs(frames).max(axis=1, keepdims=True)
    return frames / np.where(peak > 0, peak, 1.0)


def _cycle(wave: str, width: float = 0.5):
    """One naive cycle of a waveform, (N,). The mip levels band-limit it later."""
    import numpy as np

    x = np.arange(N) / N
    if wave == "sine":
        return np.sin(2 * np.pi * x)
    if wave == "triangle":
        return 1 - 4 * np.abs(np.mod(x + 0.25, 1) - 0.5)
    if wave == "saw":
        return 2 * x - 1
    if wave == "square":
        return np.where(x < 0.5, 1.0, -1.0)
    return np.where(x < width, 1.0, -1.0)  # pulse


def _mips(cycle):
    """(LEVELS, N): the cycle at every band limit, from its spectrum."""
    import numpy as np

    spec = np.fft.rfft(cycle)
    out = np.empty((LEVELS, N), dtype=np.float32)
    for k in range(LEVELS):
        keep = (N // 2) >> k  # level 0 keeps every bin, so it is the cycle itself
        part = np.zeros_like(spec)
        part[:keep + 1] = spec[:keep + 1]
        out[k] = np.fft.irfft(part, n=N)
    return out


def _stack(o: dict):
    """The mip stack of an oscillator's waveform (LEVELS, N)."""
    if o["wave"] == "pulse":
        return _cached(("pulse", round(o["width"], 4)), lambda: _mips(_cycle("pulse", o["width"])))
    return _cached((o["wave"],), lambda: _mips(_cycle(o["wave"])))


# ---------- rendering ----------

def _read(flat, lev, j, frac):
    """Samples from a mip stack (flat): level `lev`, cycle index j (wrapped), linear between j and j + 1
    by `frac`."""
    row = lev * N
    jn = (j + 1) & (N - 1)
    a = flat[row + j]
    return a + (flat[row + jn] - a) * frac


def _add_oscillator(out, o: dict, rate: int, total: int, rng) -> None:
    """Adds the oscillator (its unison voices, pan, level) to `out` (2, total). Each voice has
    one fixed frequency (detune, unison, semitones), so its phase is its start phase plus
    n cycles-per-frame times the frame number: no running sum to carry between chunks."""
    import numpy as np

    V = o["unison"]
    share = np.linspace(-0.5, 0.5, V) if V > 1 else np.zeros(1)  # the outer voices sit at +-1/2
    cents = share * o["unison_detune"]
    pans = np.clip(o["pan"] + share * 2 * o["unison_spread"], -1.0, 1.0)
    ang = (pans + 1) * np.pi / 4
    gains = np.stack([np.cos(ang), np.sin(ang)]) * math.sqrt(2)  # equal power, unity at the centre
    scale = 10 ** (o["level_db"] / 20) / math.sqrt(V) * (-1.0 if o["invert"] else 1.0)
    phase = rng.random(V) if o["random_phase"] else np.full(V, o["phase"] / 360)
    hz = o["freq"] * np.exp2((o["semitones"] + (o["detune"] + cents) / 100) / 12)  # (V,)
    # the lowest mip level whose top harmonic (N/2 >> k times f) stays under 0.45 x the rate
    lev = np.clip(np.ceil(np.log2(hz * (N // 2) / (0.45 * rate))), 0, LEVELS - 1).astype(np.int64)[:, None]
    flat = _stack(o).reshape(-1)
    cycles = (hz / rate)[:, None]  # (V, 1) cycles per frame
    for start in range(0, total, CHUNK):
        stop = min(total, start + CHUNK)
        ph = np.mod(phase[:, None] + np.arange(start, stop)[None, :] * cycles, 1.0)
        x = ph * N
        i = np.floor(x)
        j = i.astype(np.int64) & (N - 1)
        v = _read(flat, lev, j, x - i)
        out[:, start:stop] += (gains @ v) * scale


def _noise(p: dict, rate: int, total: int, rng):
    """Noise (2, total) at its level. FFT method: white noise, its spectrum scaled by f^(alpha/2)
    (DC bin 0), back to time. Each channel is peak-normalized before the level."""
    import numpy as np

    channels = 2 if p["stereo"] == "independent" else 1
    x = rng.uniform(-1.0, 1.0, size=(channels, total))
    alpha = NOISE_ALPHA[p["color"]]
    if alpha:
        f = np.fft.rfftfreq(total, 1 / rate)
        weight = np.zeros_like(f)
        weight[1:] = f[1:] ** (alpha / 2)
        x = np.fft.irfft(np.fft.rfft(x, axis=1) * weight, n=total, axis=1)
    x = _peak_norm(x) * 10 ** (p["level_db"] / 20)
    if channels == 1:
        x = np.repeat(x, 2, axis=0)
    return x.astype(np.float32)


def _impulses(p: dict, rate: int, total: int):
    """Single samples every `every_ms` (not band-limited), both channels alike."""
    import numpy as np

    out = np.zeros((2, total), dtype=np.float32)
    if not p["on"]:
        return out
    every = p["every_ms"] / 1000 * rate
    k = np.arange(int(total / every) + 1)
    pos = np.round(k * every).astype(np.int64)
    keep = pos < total
    k, pos = k[keep], pos[keep]
    if p["polarity"] == "alternate":
        sign = np.where(k % 2 == 0, 1.0, -1.0)
    else:
        sign = np.full(len(k), -1.0 if p["polarity"] == "negative" else 1.0)
    out[:, pos] = sign * 10 ** (p["level_db"] / 20)
    return out


def _channels(out, mode: str) -> None:
    """The channel mode, in place: mono and inverted mono average the two channels."""
    if mode in ("mono", "invert"):
        m = (out[0] + out[1]) / 2
        out[0] = m
        out[1] = -m if mode == "invert" else m
    elif mode == "left":
        out[1] = 0
    elif mode == "right":
        out[0] = 0


def _seam(out, length: int, seam: int):
    """Loops cleanly: the `seam` frames past the end fade into the first `seam` frames, so the
    file's end runs on into its start. Returns the first `length` frames. The fade is linear
    (weights sum to 1), not equal-power: a tone's head and tail are the same wave, so an
    equal-power fade would lift it by up to 3 dB; a linear one can't overshoot the mix's peak."""
    import numpy as np

    if not seam:
        return out[:, :length]
    s = np.linspace(0.0, 1.0, seam)
    head = out[:, :seam].copy()
    tail = out[:, length:length + seam]
    res = out[:, :length].copy()
    res[:, :seam] = tail * (1 - s) + head * s
    return res


def _db(x: float):
    return round(20 * math.log10(x), 1) if x > 0 else None


def _hz(f: float) -> str:
    return f"{round(f / 1000, 2):g} kHz" if f >= 1000 else f"{round(f)} Hz"


def _osc_name(o: dict) -> str:
    return f"{WAVE_LABEL[o['wave']]} {_hz(o['freq'])}" + (f" ×{o['unison']}" if o["unison"] > 1 else "")


def _every(ms: float) -> str:
    return f"{ms / 1000:g} s" if ms >= 1000 else f"{round(ms)} ms"


def _summary(p: dict) -> str:
    """A short name for the signal, like "Saw 110 Hz ×7", "Sine 1 kHz + pink noise" or "Silence"."""
    parts = []
    if p["osc"]["on"]:
        parts.append(_osc_name(p["osc"]))
    if p["noise"]["on"]:
        parts.append(f"{p['noise']['color']} noise")
    if p["impulse"]["on"]:
        parts.append(f"impulses every {_every(p['impulse']['every_ms'])}")
    if not parts:
        return "Silence"
    text = " + ".join(parts)
    return text[0].upper() + text[1:]


def render(clean: dict):
    """(audio, info). audio is float32 (2, frames) at the patch's rate, `seconds` long and clipped
    to [-1, 1]; info is what the Signal tab shows.

    Order: the oscillator and noise, mixed with the impulses; the mix is normalized (its peak is
    level_db), then the channel mode, the seam, the master level, DC and the clip."""
    import numpy as np

    rate = clean["rate"]
    length = round(clean["seconds"] * rate)
    secs = length / rate
    seam = round(clean["seam_ms"] / 1000 * rate)  # a loop seam always, when asked for
    total = length + seam
    osc = dict(clean["osc"])
    note = ""
    work = (osc["unison"] if osc["on"] else 0) * total
    if work > WORK_LIMIT:
        osc["unison"] = max(1, int(osc["unison"] * WORK_LIMIT / work))
        note = f"Unison cut to {osc['unison']} voices to keep the render quick."

    rng = np.random.default_rng(int(patch_key(clean)[:16], 16))
    A = np.zeros((2, total), dtype=np.float32)  # the oscillator and the noise
    if osc["on"]:
        _add_oscillator(A, osc, rate, total, rng)
    if clean["noise"]["on"]:
        A += _noise(clean["noise"], rate, total, rng)
    out = A + _impulses(clean["impulse"], rate, total)
    gain = 10 ** (clean["level_db"] / 20)
    if clean["normalize"]:
        peak = float(np.abs(out).max())
        gain = gain / peak if peak > 1e-9 else 1.0
    out *= gain
    _channels(out, clean["channels"])
    out = _seam(out, length, seam)
    if clean["master_db"]:  # after the mix's level: past 0 dBFS it clips below
        out = out * np.float32(10 ** (clean["master_db"] / 20))
    if clean["dc"]:
        out = out + np.float32(clean["dc"])
    clipped = bool(np.abs(out).max() > 1.0)
    out = np.clip(out, -1.0, 1.0).astype(np.float32)

    peak_db = _db(float(np.abs(out).max()))
    rms_db = _db(float(np.sqrt(np.mean(out * out, dtype=np.float64))))
    info = {"seconds": round(secs, 6), "rate": rate, "peak_db": peak_db, "rms_db": rms_db,
            "crest_db": round(peak_db - rms_db, 1) if peak_db is not None and rms_db is not None else None,
            "clipped": clipped, "summary": _summary(clean)}
    if note:
        info["note"] = note
    return out, info


def preview(audio, rate: int) -> dict:
    """4096 frames from 0.5 s in (from 0 when the file is shorter), 5 decimals: the Scope's Signal view."""
    import numpy as np

    start = int(PREVIEW_AT_S * rate)
    if audio.shape[1] - start < PREVIEW_FRAMES:
        start = 0
    part = audio[:, start:start + PREVIEW_FRAMES].astype(np.float64)
    return {"rate": rate, "l": np.round(part[0], 5).tolist(), "r": np.round(part[1], 5).tolist()}


def save(clean: dict, audio, rate: int) -> Path:
    """Writes the render as cache/playground/signals/signal-<hash>.wav (32-bit float, stereo) unless
    it's there already (then it's touched). Keeps the KEEP_SIGNALS newest files of the folder."""
    import numpy as np

    folder = _signals_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"signal-{patch_key(clean)[:12]}.wav"
    if path.exists():
        os.utime(path)
    else:
        tmp = path.with_suffix(".tmp")
        with open(tmp, "wb") as f:
            f.write(host_worker.float_wav_header(2, rate, audio.shape[1]))
            f.write(np.ascontiguousarray(audio.T, dtype="<f4").tobytes())
        os.replace(tmp, path)
    _prune(folder, path)
    return path


def _prune(folder: Path, keep: Path) -> None:
    """Deletes all but the KEEP_SIGNALS newest signal files. A file a player still holds stays
    (Windows won't delete it), and `keep` is never deleted."""
    def age(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:  # gone meanwhile
            return 0.0

    files = sorted(folder.glob("signal-*.wav"), key=age, reverse=True)
    for old in files[KEEP_SIGNALS:]:
        if old != keep:
            try:
                old.unlink()
            except OSError:
                pass
