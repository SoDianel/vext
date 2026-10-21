"""The Playground's Signal tab: test signals made from a patch (oscillators with unison,
pan and modulation, noise, impulses, a tone-burst gate, a level staircase), rendered to a
32-bit float WAV that the Audio tab plays like any file. Plugins, Record, Capture and the
Analyzer work on it unchanged (AGENTS.md "Playground: Browser | Audio | Signal").

Oscillators are band-limited wavetables. Each waveform is a stack of 11 mip levels: level k
keeps harmonics 1..1024 >> k of a 2048-sample cycle. A voice reads the level whose top
harmonic stays under 0.45 x the rate, so high notes don't alias. Everything is numpy,
vectorized over voices and frames (no per-sample Python loop). Frames are rendered in
chunks, so a 60-second file never holds a full-length array per voice. The random
generator is seeded from the patch's hash: the same patch gives the same bytes.

The endpoints are in app/web.py (POST /api/playground/signal, its tables and wavetable
routes).
"""

import hashlib
import json
import math
import os
import re
import struct
import threading
from pathlib import Path

from app import host_worker
from app.config import PROJECT_ROOT

CACHE_DIR = PROJECT_ROOT / "cache" / "playground"
N = 2048  # samples per wavetable cycle
LEVELS = 11  # mip levels: level k keeps harmonics 1..(N // 2) >> k
TABLE_FRAMES = 64  # frames of each built-in wavetable
MAX_USER_FRAMES = 256
PREVIEW_FRAMES = 4096
PREVIEW_AT_S = 0.5
KEEP_SIGNALS = 6  # rendered signal files kept in cache/playground/signals
WORK_LIMIT = 40e6  # voices x frames; above it the unison is cut (say so in info.note)
CHUNK = 1 << 13  # frames per numpy pass: small enough to stay in cache (16k and up measured 2x slower)
MAX_OSCILLATORS = 4
RATES = (44100, 48000, 88200, 96000)
CHANNEL_MODES = ("stereo", "mono", "left", "right", "invert")
WAVES = ("sine", "triangle", "saw", "square", "pulse", "wavetable")
WAVE_LABEL = {"sine": "Sine", "triangle": "Triangle", "saw": "Saw", "square": "Square", "pulse": "Pulse",
              "wavetable": "Table"}
NOISE_ALPHA = {"white": 0.0, "pink": -1.0, "brown": -2.0, "blue": 1.0, "violet": 2.0}  # power ~ f^alpha
BUILTIN_TABLES = (("basic", "Basic"), ("harmonics", "Harmonics"), ("pwm", "PWM"), ("formant", "Formant"),
                  ("sync", "Sync"), ("fm", "FM"), ("bitcrush", "Bitcrush"))
BUILTIN_NAMES = {name for name, _ in BUILTIN_TABLES}
_NAME = re.compile(r"[\w .()-]{1,80}")  # a table name, which is also its file name in wavetables/

_cache: dict = {}  # built mip stacks, by key (a waveform, or a table with its file's stamp)
_cache_lock = threading.Lock()


def _signals_dir() -> Path:
    return CACHE_DIR / "signals"


def _wavetables_dir() -> Path:
    return CACHE_DIR / "wavetables"


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


def _name(v, default: str) -> str:
    text = v.strip()[:80] if isinstance(v, str) else ""
    return text or default


def _obj(v) -> dict:
    return v if isinstance(v, dict) else {}


def _osc(o: dict) -> dict:
    sweep, vib, trem = _obj(o.get("sweep")), _obj(o.get("vibrato")), _obj(o.get("tremolo"))
    return {
        "on": _flag(o.get("on"), True),
        "wave": _pick(o.get("wave"), WAVES, "sine"),
        "table": _name(o.get("table"), "basic"),
        "position": _num(o.get("position"), 0, 1, 0),
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
        "sweep": {"on": _flag(sweep.get("on"), False), "to": _num(sweep.get("to"), 1, 24000, 20000),
                  "law": _pick(sweep.get("law"), ("log", "linear"), "log")},
        "vibrato": {"rate": _num(vib.get("rate"), 0, 20, 0), "depth": _num(vib.get("depth"), 0, 1200, 0)},
        "tremolo": {"rate": _num(trem.get("rate"), 0, 20, 0), "depth": _num(trem.get("depth"), 0, 1, 0)},
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
    raw = patch.get("oscillators")
    if raw is None:
        raw = [{}]  # missing: one sine at 1 kHz
    elif not isinstance(raw, list):
        raw = []
    oscs = [_osc(o) for o in raw if isinstance(o, dict)][:MAX_OSCILLATORS]
    noise, imp = _obj(patch.get("noise")), _obj(patch.get("impulse"))
    gate, steps = _obj(patch.get("gate")), _obj(patch.get("steps"))
    return {
        "seconds": _num(patch.get("seconds"), 0.5, 60, 10),
        "rate": rate,
        "level_db": _num(patch.get("level_db"), -80, 0, -18),
        "normalize": _flag(patch.get("normalize"), True),
        "channels": _pick(patch.get("channels"), CHANNEL_MODES, "stereo"),
        "seam_ms": _num(patch.get("seam_ms"), 0, 200, 20),
        "oscillators": oscs,
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
        "gate": {
            "on": _flag(gate.get("on"), False),
            "on_ms": _num(gate.get("on_ms"), 1, 5000, 200),
            "off_ms": _num(gate.get("off_ms"), 1, 5000, 800),
            "attack_ms": _num(gate.get("attack_ms"), 0.1, 1000, 1),
            "release_ms": _num(gate.get("release_ms"), 0.1, 1000, 1),
        },
        "steps": {
            "on": _flag(steps.get("on"), False),
            "from_db": _num(steps.get("from_db"), -80, 0, -60),
            "to_db": _num(steps.get("to_db"), -80, 0, 0),
            "step_db": _num(steps.get("step_db"), 1, 10, 6),
            "step_ms": _num(steps.get("step_ms"), 1, 5000, 500),
        },
        "dc": _num(patch.get("dc"), -0.5, 0.5, 0),
    }


def patch_key(clean: dict) -> str:
    """SHA-1 of the clean patch as sorted JSON: names the file and seeds the noise."""
    return hashlib.sha1(json.dumps(clean, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


# ---------- wavetables ----------

def _cached(key, make):
    """A built stack, kept in memory (a few dozen at most: the cache is cleared when it grows)."""
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
    """One naive cycle of a basic waveform, (1, N). The mip levels band-limit it later."""
    import numpy as np

    x = np.arange(N) / N
    if wave == "sine":
        y = np.sin(2 * np.pi * x)
    elif wave == "triangle":
        y = 1 - 4 * np.abs(np.mod(x + 0.25, 1) - 0.5)
    elif wave == "saw":
        y = 2 * x - 1
    elif wave == "square":
        y = np.where(x < 0.5, 1.0, -1.0)
    else:  # pulse
        y = np.where(x < width, 1.0, -1.0)
    return y[None, :]


def _synth(amps):
    """Frames from harmonic amplitudes (F, N // 2 + 1), each frame peak-normalized."""
    import numpy as np

    spec = np.zeros(amps.shape, dtype=np.complex128)
    spec[:, 1:] = -1j * amps[:, 1:] * (N / 2)  # the bin of a unit sine (rfft of sin is -i N/2)
    return _peak_norm(np.fft.irfft(spec, n=N, axis=1))


def _builtin(name: str):
    """A built-in table's frames, (TABLE_FRAMES, N)."""
    import numpy as np

    x = np.arange(N) / N
    k = np.arange(TABLE_FRAMES)
    if name == "basic":  # sine -> triangle -> saw -> square, morphed
        shapes = np.concatenate([_cycle(w) for w in ("sine", "triangle", "saw", "square")])
        p = k / (TABLE_FRAMES - 1) * 3
        seg = np.minimum(np.floor(p).astype(int), 2)
        w = (p - seg)[:, None]
        return (1 - w) * shapes[seg] + w * shapes[seg + 1]
    if name == "harmonics":  # frame k holds harmonics 1..k+1 at 1/n (an organ build-up)
        n = np.arange(N // 2 + 1)
        keep = (n[None, :] >= 1) & (n[None, :] <= (k + 1)[:, None])
        return _synth(np.where(keep, 1.0 / np.maximum(n, 1)[None, :], 0.0))
    if name == "pwm":  # pulse width 50 % -> 2 %
        width = 0.5 + (0.02 - 0.5) * k / (TABLE_FRAMES - 1)
        return np.where(x[None, :] < width[:, None], 1.0, -1.0)
    if name == "formant":  # A E I O U as formant pairs (F1, F2 in Hz), morphed, on a 110 Hz cycle
        vowels = [(700, 1220), (530, 1840), (270, 2290), (570, 840), (300, 870)]
        hz = np.arange(N // 2 + 1) * 110.0
        shapes = np.array([sum(np.exp(-0.5 * ((hz - f) / 120.0) ** 2) for f in pair) for pair in vowels])
        shapes[:, 0] = 0.0  # no DC
        p = k / (TABLE_FRAMES - 1) * 4
        seg = np.minimum(np.floor(p).astype(int), 3)
        w = (p - seg)[:, None]
        return _synth((1 - w) * shapes[seg] + w * shapes[seg + 1])
    if name == "sync":  # a saw whose slave ratio goes 1 -> 8 across the frames
        ratio = 1 + 7 * k / (TABLE_FRAMES - 1)
        return 2 * np.mod(ratio[:, None] * x[None, :], 1.0) - 1
    if name == "fm":  # 2-operator FM at a 1:1 ratio, index 0 -> 10
        index = 10 * k / (TABLE_FRAMES - 1)
        s = np.sin(2 * np.pi * x)[None, :]
        return np.sin(2 * np.pi * x[None, :] + index[:, None] * s)
    if name == "bitcrush":  # a sine quantized from 16 levels down to 2
        steps = np.round(16 - 14 * k / (TABLE_FRAMES - 1))[:, None] - 1
        s = np.sin(2 * np.pi * x)[None, :]
        return np.round((s + 1) / 2 * steps) / steps * 2 - 1
    raise ValueError(f"There's no wavetable named {name}.")


def _safe_name(stem: str) -> str:
    name = re.sub(r"[^\w .()-]", "_", stem).strip(" .")[:60]
    return name or "Wavetable"


def _user_path(name: str):
    """The .npy of a user table, or None (a bad name, or no such file)."""
    if not _NAME.fullmatch(name or ""):
        return None
    path = _wavetables_dir() / f"{name}.npy"
    return path if path.is_file() else None


def _table_key(name: str):
    if name in BUILTIN_NAMES:
        return ("table", name)
    path = _user_path(name)
    if path is None:
        raise ValueError(f"There's no wavetable named {name}.")
    st = path.stat()
    return ("table", name, st.st_mtime_ns, st.st_size)  # a changed file is built again


def _table_frames(name: str):
    import numpy as np

    if name in BUILTIN_NAMES:
        return _builtin(name)
    path = _user_path(name)
    if path is None:
        raise ValueError(f"There's no wavetable named {name}.")
    return np.load(path, allow_pickle=False).astype(np.float32)


def _mips(frames):
    """(LEVELS, frames, N): each frame at every band limit, from its spectrum."""
    import numpy as np

    spec = np.fft.rfft(frames, axis=1)
    out = np.empty((LEVELS, frames.shape[0], N), dtype=np.float32)
    for k in range(LEVELS):
        keep = (N // 2) >> k  # level 0 keeps every bin, so it is the frame itself
        part = np.zeros_like(spec)
        part[:, :keep + 1] = spec[:, :keep + 1]
        out[k] = np.fft.irfft(part, n=N, axis=1)
    return out


def _stack(o: dict):
    """The mip stack of an oscillator's waveform (LEVELS, frames, N)."""
    wave = o["wave"]
    if wave == "wavetable":
        name = o["table"]
        return _cached(_table_key(name), lambda: _mips(_table_frames(name)))
    if wave == "pulse":
        return _cached(("pulse", round(o["width"], 4)), lambda: _mips(_cycle("pulse", o["width"])))
    return _cached((wave,), lambda: _mips(_cycle(wave)))


def tables() -> list[dict]:
    """The built-in tables, then the user's (cache/playground/wavetables/*.npy)."""
    import numpy as np

    out = [{"name": n, "label": label, "frames": TABLE_FRAMES, "user": False} for n, label in BUILTIN_TABLES]
    folder = _wavetables_dir()
    if folder.is_dir():
        for path in sorted(folder.glob("*.npy")):
            try:
                arr = np.load(path, mmap_mode="r")
            except (OSError, ValueError):
                continue
            if arr.ndim == 2 and arr.shape[1] == N:
                out.append({"name": path.stem, "label": path.stem, "frames": int(arr.shape[0]), "user": True})
    return out


def _wav_format(body: bytes):
    """(format code, channels, bits) of a WAV's fmt chunk."""
    if len(body) < 16:
        raise ValueError("The WAV format is broken.")
    code, channels, _rate, _bps, _align, bits = struct.unpack("<HHIIHH", body[:16])
    if code == 0xFFFE and len(body) >= 26:  # WAVE_FORMAT_EXTENSIBLE: the real code leads the sub-format
        code = struct.unpack("<H", body[24:26])[0]
    return code, channels, bits


def _read_first_channel(path: Path):
    """The first channel of a WAV as float32, at most MAX_USER_FRAMES cycles long. PCM 8, 16, 24
    and 32-bit, and 32-bit float. Raises ValueError for anything else."""
    import numpy as np

    with open(path, "rb") as f:
        head = f.read(12)
        if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
            raise ValueError("That isn't a WAV file.")
        fmt = None
        while True:
            hdr = f.read(8)
            if len(hdr) < 8:
                raise ValueError("The WAV file has no audio data.")
            cid, size = hdr[:4], struct.unpack("<I", hdr[4:])[0]
            if cid == b"data":
                break
            if cid == b"fmt ":
                fmt = _wav_format(f.read(size))
            else:
                f.seek(size, 1)
            if size % 2:
                f.seek(1, 1)  # chunks are padded to an even size
        if fmt is None:
            raise ValueError("The WAV file has no format chunk.")
        code, channels, bits = fmt
        if channels < 1 or not ((code == 1 and bits in (8, 16, 24, 32)) or (code == 3 and bits == 32)):
            raise ValueError("Only PCM (8, 16, 24 or 32-bit) and 32-bit float WAV files can be imported.")
        width = bits // 8
        frame = channels * width
        raw = f.read(min(size, MAX_USER_FRAMES * N * frame))
    count = len(raw) // frame
    if count == 0:
        raise ValueError("The WAV file has no samples.")
    first = np.ascontiguousarray(np.frombuffer(raw, dtype=np.uint8, count=count * frame)
                                 .reshape(count, frame)[:, :width])
    if code == 3:
        x = first.view("<f4")[:, 0]
    elif width == 1:
        x = (first[:, 0].astype(np.float32) - 128) / 128
    elif width == 2:
        x = first.view("<i2")[:, 0] / 32768
    elif width == 3:
        v = (first[:, 0].astype(np.int32) | (first[:, 1].astype(np.int32) << 8)
             | (first[:, 2].astype(np.int32) << 16))
        x = np.where(v & 0x800000, v - 0x1000000, v) / 8388608
    else:
        x = first.view("<i4")[:, 0] / 2147483648
    return np.asarray(x, dtype=np.float32)


def import_wavetable(path: str) -> list[dict]:
    """A WAV as a user wavetable (cache/playground/wavetables/<name>.npy): its first channel cut into
    2048-sample frames, at most 256. A file shorter than one cycle is one frame, stretched to 2048
    samples. Raises ValueError for a file that can't be read. Answers the table list."""
    import numpy as np

    src = Path((path or "").strip().strip('"'))
    if not src.is_file():
        raise ValueError("That file doesn't exist.")
    x = _read_first_channel(src)
    if x.size < N:
        frames = np.interp(np.linspace(0, x.size - 1, N), np.arange(x.size), x)[None, :]
    else:
        count = min(MAX_USER_FRAMES, x.size // N)
        frames = x[:count * N].reshape(count, N)
    name = _safe_name(src.stem)
    if name in BUILTIN_NAMES:
        name += " (user)"
    folder = _wavetables_dir()
    folder.mkdir(parents=True, exist_ok=True)
    np.save(folder / f"{name}.npy", frames.astype(np.float32))
    return tables()


# ---------- rendering ----------

def _read(flat, frames: int, lev, j, frac, position: float):
    """Samples from a mip stack: level `lev`, cycle index j (wrapped), fraction `frac` to the next
    sample. Wavetable frames morph linearly; `position` (0..1) runs across the table."""
    jn = (j + 1) & (N - 1)
    if frames == 1:
        picks = [(0, 1.0)]
    else:
        p = position * (frames - 1)
        i0 = min(int(p), frames - 2)
        picks = [(i0, 1.0 - (p - i0)), (i0 + 1, p - i0)]
    out = None
    for idx, w in picks:
        if w <= 0:
            continue
        row = lev * (frames * N) + idx * N
        a = flat[row + j]
        term = a + (flat[row + jn] - a) * frac
        if w < 1.0:
            term = term * w
        out = term if out is None else out + term
    return out


def _add_oscillator(out, o: dict, rate: int, total: int, secs: float, rng) -> None:
    """Adds one oscillator (its unison voices, pan, level, tremolo) to `out` (2, total).
    The voices are one (V, frames) array per chunk of frames; the pitch is split into a
    per-voice ratio (detune, unison, semitones) and a per-frame part (sweep, vibrato)."""
    import numpy as np

    V = o["unison"]
    share = np.linspace(-0.5, 0.5, V) if V > 1 else np.zeros(1)  # the outer voices sit at +-1/2
    cents = share * o["unison_detune"]
    pans = np.clip(o["pan"] + share * 2 * o["unison_spread"], -1.0, 1.0)
    ang = (pans + 1) * np.pi / 4
    gains = np.stack([np.cos(ang), np.sin(ang)]) * math.sqrt(2)  # equal power, unity at the centre
    scale = 10 ** (o["level_db"] / 20) / math.sqrt(V) * (-1.0 if o["invert"] else 1.0)
    phase = rng.random(V) if o["random_phase"] else np.full(V, o["phase"] / 360)
    ratio = np.exp2((o["semitones"] + (o["detune"] + cents) / 100) / 12)  # (V,)
    # log2(f * (N/2) / (0.45 rate)): the mip level is ceil of it (see the level below)
    lev_const = np.log2(ratio * (N // 2) / (0.45 * rate))[:, None]
    stack = _stack(o)
    frames = stack.shape[1]
    flat = stack.reshape(-1)
    sw, vib, trem = o["sweep"], o["vibrato"], o["tremolo"]
    f0 = o["freq"]
    for start in range(0, total, CHUNK):
        stop = min(total, start + CHUNK)
        t = np.arange(start, stop) / rate
        pitch = np.full(stop - start, f0)  # Hz, before the voice ratios
        if sw["on"]:
            x = t / secs
            pitch = f0 * (sw["to"] / f0) ** x if sw["law"] == "log" else f0 + (sw["to"] - f0) * x
        if vib["depth"] and vib["rate"]:
            pitch = pitch * np.exp2(vib["depth"] * np.sin(2 * np.pi * vib["rate"] * t) / 1200)  # cents
        inc = ratio[:, None] * pitch / rate  # (V, n) cycles per frame
        cs = np.cumsum(inc, axis=1)
        ph = phase[:, None] + cs - inc  # a voice's first frame is at its start phase
        phase = np.mod(phase + cs[:, -1], 1.0)
        ph -= np.floor(ph)
        x = ph * N
        i = np.floor(x)
        j = i.astype(np.int64) & (N - 1)
        # the lowest level whose top harmonic (N/2 >> k times f) is under 0.45 x the rate
        lev = np.clip(np.ceil(lev_const + np.log2(np.maximum(pitch, 1e-6))), 0, LEVELS - 1).astype(np.int64)
        v = _read(flat, frames, lev, j, x - i, o["position"])
        amp = 1.0
        if trem["depth"] and trem["rate"]:
            amp = 1.0 - trem["depth"] * (0.5 + 0.5 * np.sin(2 * np.pi * trem["rate"] * t))
        out[:, start:stop] += (gains @ v) * (scale * amp)


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


def _gate(p: dict, rate: int, total: int):
    """The burst envelope: `on_ms` open, `off_ms` closed, linear attack and release ramps."""
    import numpy as np

    on = p["on_ms"] / 1000 * rate
    period = (p["on_ms"] + p["off_ms"]) / 1000 * rate
    attack = p["attack_ms"] / 1000 * rate
    release = p["release_ms"] / 1000 * rate
    u = np.mod(np.arange(total, dtype=np.float64), period)
    env = np.minimum(np.minimum(u / attack, (on - u) / release), 1.0)
    return np.clip(env, 0.0, 1.0) * (u < on)


def _steps(p: dict, rate: int, total: int):
    """The level staircase: from `from_db` towards `to_db` by `step_db` every `step_ms`, then held."""
    import numpy as np

    every = p["step_ms"] / 1000 * rate
    j = np.floor(np.arange(total) / every)
    span = abs(p["to_db"] - p["from_db"])
    sign = 1.0 if p["to_db"] >= p["from_db"] else -1.0
    db = p["from_db"] + sign * np.minimum(j * p["step_db"], span)
    return 10 ** (db / 20)


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
    name = f"Table {o['table']}" if o["wave"] == "wavetable" else WAVE_LABEL[o["wave"]]
    if o["sweep"]["on"]:
        text = f"{name} sweep {_hz(o['freq'])}–{_hz(o['sweep']['to'])}"
    else:
        text = f"{name} {_hz(o['freq'])}"
    return text + (f" ×{o['unison']}" if o["unison"] > 1 else "")


def _summary(p: dict) -> str:
    """A short name for the signal, like "Saw 110 Hz ×7" or "2 oscillators + pink noise"."""
    oscs = [o for o in p["oscillators"] if o["on"]]
    parts = []
    if len(oscs) == 1:
        parts.append(_osc_name(oscs[0]))
    elif oscs:
        parts.append(f"{len(oscs)} oscillators")
    if p["noise"]["on"]:
        parts.append(f"{p['noise']['color']} noise")
    if p["impulse"]["on"]:
        parts.append("impulses")
    if not parts:
        return "Silence"
    text = " + ".join(parts)
    if p["gate"]["on"]:
        text += " burst"
    if p["steps"]["on"]:
        text += " staircase"
    return text[0].upper() + text[1:]


def render(clean: dict):
    """(audio, info). audio is float32 (2, frames) at the patch's rate, `seconds` long and clipped
    to [-1, 1]; info is what the Signal tab shows. Raises ValueError for a table that doesn't exist.

    Order: oscillators and noise, normalized with the impulses (the mix's peak is level_db), then
    the gate on the oscillators and noise only, then the impulses are added, then the staircase,
    the channel mode, the seam, DC and the clip."""
    import numpy as np

    rate = clean["rate"]
    length = round(clean["seconds"] * rate)
    secs = length / rate
    oscs = [o for o in clean["oscillators"] if o["on"]]
    sweeping = any(o["sweep"]["on"] for o in oscs)
    steps_on = clean["steps"]["on"]
    seam = 0 if (sweeping or steps_on) else round(clean["seam_ms"] / 1000 * rate)
    total = length + seam
    note = ""
    work = sum(o["unison"] for o in oscs) * total
    if work > WORK_LIMIT:
        cut = WORK_LIMIT / work
        oscs = [dict(o, unison=max(1, int(o["unison"] * cut))) for o in oscs]
        note = f"Unison cut to {sum(o['unison'] for o in oscs)} voices to keep the render quick."

    rng = np.random.default_rng(int(patch_key(clean)[:16], 16))
    A = np.zeros((2, total), dtype=np.float32)  # oscillators and noise: what the gate acts on
    for o in oscs:
        _add_oscillator(A, o, rate, total, secs, rng)
    if clean["noise"]["on"]:
        A += _noise(clean["noise"], rate, total, rng)
    I = _impulses(clean["impulse"], rate, total)
    gain = 10 ** (clean["level_db"] / 20)
    if clean["normalize"]:
        peak = float(np.abs(A + I).max())
        gain = gain / peak if peak > 1e-9 else 1.0
    A *= gain
    I *= gain
    if clean["gate"]["on"]:
        A *= _gate(clean["gate"], rate, total)
    out = A + I
    if steps_on:
        out *= _steps(clean["steps"], rate, total)
    _channels(out, clean["channels"])
    out = _seam(out, length, seam)
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
