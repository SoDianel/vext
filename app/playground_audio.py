"""The Playground's Audio tab: one audio file at a time, its waveform, small
edits, and the recording of what comes out of the plugin (AGENTS.md
"Playground: Browser | Audio tabs").

Decoding: FFmpeg's own libraries, through PyAV (`av`, already bundled for
playback), read every format FFmpeg reads. No separate FFmpeg install and no
ffmpeg.exe: nothing here needs the command-line tool (decoding_info() says
which FFmpeg it is). A file is decoded once, at its own sample rate (mono stays
mono, more than two channels are mixed down to stereo), into a 32-bit float
WAV in cache/playground. The plugin's worker plays that copy (exact sample
positions, fast seeks) and the waveform reads its samples. The original file
is never touched.

Peaks: a pyramid of min/max per channel, BUCKET frames per peak at level 0,
FACTOR times coarser per level, built from the decoded copy (a 10-minute file:
about 110,000 level-0 peaks). peaks() answers a view (first frame, end frame,
pixels) from the finest level that has at most one peak per pixel, from the
samples themselves when zoomed in further, and with the samples one by one
when there are fewer samples than pixels (drawn as dots).

Edits (Fade in, Fade out, Normalize) are a list of gain ops
(host_worker.envelope_gain): the worker applies them to what it plays, the
peaks are drawn with them, Undo drops the last one. Nothing is rewritten.

The recording: the worker writes what comes out of the plugin into
recording-<token>.wav at the file's own time positions (host_worker._Recorder),
so both waveforms share one time axis. plugin_host passes its news here
(on_record): the parts written are re-peaked when the view asks.
"""

import json
import os
import shutil
import struct
import threading
import time
import uuid
from pathlib import Path

from app import host_worker
from app.config import PROJECT_ROOT

CACHE_DIR = PROJECT_ROOT / "cache" / "playground"
STATE_FILE = CACHE_DIR / "state.json"
BUCKET = 256  # frames per level-0 peak
FACTOR = 4  # each pyramid level is this much coarser
CHUNK_FRAMES = 1 << 20  # frames read at once while building peaks / normalizing
MAX_DATA_BYTES = 0xFFFFFFFF - host_worker.WAV_FLOAT_HEADER  # one RIFF file
MAX_PEAK_COLUMNS = 8192
NORMALIZE_DBFS = -1.0
MAX_EDITS = 100
MUSIC_FOLDER = "{4BD8D571-6D19-48D3-BE97-422220080E43}"  # FOLDERID_Music

# What the file dialog offers (FFmpeg reads many more; any file can be tried).
EXTENSIONS = ["wav", "mp3", "flac", "ogg", "oga", "opus", "m4a", "aac", "aif", "aiff", "aifc", "wma", "wv", "ape",
              "alac", "caf", "w64", "mka", "webm", "ac3", "dts", "mp2", "amr", "au", "snd", "mp4", "mov", "mkv"]
FORMAT_NOTE = ("WAV, AIFF, FLAC, MP3, AAC / M4A / ALAC, Ogg Vorbis, Opus, WMA, WavPack, Monkey's Audio, CAF, AC-3, "
               "DTS, and the audio of video files")

_HEADER = struct.Struct("<4sIII")  # b"VXPK", kind, channels, count
KIND_EMPTY, KIND_SAMPLES, KIND_PEAKS = 0, 1, 2

_lock = threading.RLock()
_job = {"token": None, "cancel": None}
_state: dict = {"status": "empty", "error": "", "progress": 0.0, "path": "", "name": "", "token": None,
                "info": None, "edits": [], "version": 0}
_source = None  # _Track of the decoded copy, once ready
_recording = None  # _Track of the recording, once it exists
_rec = {"armed": False, "version": 0}
_restored = False


class _Track:
    """A float WAV (host_worker.float_wav_header) and its peak pyramid."""

    def __init__(self, path: Path, layout: dict):
        import numpy as np

        self.path = Path(path)
        self.channels, self.rate = layout["channels"], layout["rate"]
        self.frames, self.offset = layout["frames"], layout["offset"]
        self.level0 = np.zeros((max(1, -(-self.frames // BUCKET)), self.channels, 2), dtype=np.float32)
        self.levels: list | None = None  # coarser levels, rebuilt when level 0 changed
        self.dirty: list | None = None  # [first, end) frames to re-peak

    @property
    def duration(self) -> float:
        return self.frames / self.rate if self.rate else 0.0

    def read(self, start: int, end: int):
        """Frames [start, end) as an (n, channels) float32 array."""
        import numpy as np

        start, end = max(0, start), min(self.frames, end)
        if end <= start:
            return np.zeros((0, self.channels), dtype=np.float32)
        with open(self.path, "rb") as f:
            f.seek(self.offset + start * self.channels * 4)
            data = np.fromfile(f, dtype="<f4", count=(end - start) * self.channels)
        n = data.size // self.channels
        return data[:n * self.channels].reshape(n, self.channels)

    def build(self, start: int = 0, end: int | None = None) -> None:
        """Level 0 for frames [start, end) (whole buckets around them)."""
        end = self.frames if end is None else min(self.frames, end)
        first = max(0, start) // BUCKET
        pos = first * BUCKET
        while pos < end:
            stop = min(end, pos + CHUNK_FRAMES)
            stop = min(self.frames, -(-stop // BUCKET) * BUCKET)
            data = self.read(pos, stop)
            if not len(data):
                break
            peaks = _minmax(data, BUCKET)
            b = pos // BUCKET
            self.level0[b:b + len(peaks)] = peaks
            pos += len(data)
        self.levels = None

    def mark(self, lo: int, hi: int) -> None:
        self.dirty = [min(self.dirty[0], lo), max(self.dirty[1], hi)] if self.dirty else [lo, hi]

    def _level(self, k: int):
        if self.dirty:
            lo, hi = self.dirty
            self.dirty = None
            self.build(lo, hi)
        if self.levels is None:
            self.levels = [self.level0]
        while len(self.levels) <= k:
            prev = self.levels[-1]
            if len(prev) <= 1:
                return prev
            self.levels.append(_reduce(prev, FACTOR))
        return self.levels[k]

    def query(self, start: int, end: int, width: int, ops=None) -> bytes:
        import numpy as np

        start = max(0, min(int(start), self.frames))
        end = max(start, min(int(end), self.frames))
        width = max(1, min(int(width), MAX_PEAK_COLUMNS))
        n = end - start
        if n <= 0:
            return _HEADER.pack(b"VXPK", KIND_EMPTY, self.channels, 0)
        spp = n / width
        if spp <= 1.0:
            data = self.read(start, end)
            if ops:
                t = (start + np.arange(len(data), dtype=np.float64)) / self.rate
                data = data * host_worker.envelope_gain(ops, t)[:, None]
            return _HEADER.pack(b"VXPK", KIND_SAMPLES, self.channels, len(data)) + data.astype("<f4").tobytes()
        edges = start + np.floor(np.arange(width + 1, dtype=np.float64) * spp).astype(np.int64)
        edges[-1] = end
        if spp < BUCKET:
            data = self.read(start, end)
            idx = np.minimum(edges[:-1] - start, max(0, len(data) - 1))
            out = np.empty((width, self.channels, 2), dtype=np.float32)
            if len(data):
                out[:, :, 0] = np.minimum.reduceat(data, idx, axis=0)
                out[:, :, 1] = np.maximum.reduceat(data, idx, axis=0)
            else:
                out[:] = 0
        else:
            k, size = 0, BUCKET
            while size * FACTOR <= spp:
                k, size = k + 1, size * FACTOR
            level = self._level(k)
            first, last = int(edges[0] // size), int(-(-edges[-1] // size))
            part = level[first:max(first + 1, min(last, len(level)))]
            idx = np.minimum(edges[:-1] // size - first, len(part) - 1)
            out = np.empty((width, self.channels, 2), dtype=np.float32)
            out[:, :, 0] = np.minimum.reduceat(part[:, :, 0], idx, axis=0)
            out[:, :, 1] = np.maximum.reduceat(part[:, :, 1], idx, axis=0)
        if ops:
            t = (start + (np.arange(width, dtype=np.float64) + 0.5) * spp) / self.rate
            out *= host_worker.envelope_gain(ops, t)[:, None, None]
        return _HEADER.pack(b"VXPK", KIND_PEAKS, self.channels, width) + out.astype("<f4").tobytes()

    def info(self) -> dict:
        return {"rate": self.rate, "channels": self.channels, "frames": self.frames,
                "duration_s": round(self.duration, 6)}


def _minmax(data, size: int):
    """(n, ch) samples -> (ceil(n/size), ch, 2) min/max per `size` frames."""
    import numpy as np

    n, ch = data.shape
    full = n // size
    out = np.empty((-(-n // size), ch, 2), dtype=np.float32)
    if full:
        d = data[:full * size].reshape(full, size, ch)
        out[:full, :, 0] = d.min(axis=1)
        out[:full, :, 1] = d.max(axis=1)
    if n % size:
        rest = data[full * size:]
        out[full, :, 0] = rest.min(axis=0)
        out[full, :, 1] = rest.max(axis=0)
    return out


def _reduce(level, factor: int):
    """A peak level `factor` times coarser."""
    import numpy as np

    n, ch, _ = level.shape
    full = n // factor
    out = np.empty((-(-n // factor), ch, 2), dtype=np.float32)
    if full:
        d = level[:full * factor].reshape(full, factor, ch, 2)
        out[:full, :, 0] = d[:, :, :, 0].min(axis=1)
        out[:full, :, 1] = d[:, :, :, 1].max(axis=1)
    if n % factor:
        rest = level[full * factor:]
        out[full, :, 0] = rest[:, :, 0].min(axis=0)
        out[full, :, 1] = rest[:, :, 1].max(axis=0)
    return out


# ---------- decoding ----------

def decoding_info() -> dict:
    """Configuration > Audio's "Audio decoding" line."""
    try:
        import av

        version = getattr(av, "ffmpeg_version_info", "") or ""
        libs = getattr(av, "library_versions", {}) or {}
        codec = libs.get("libavcodec")
        return {"ok": True, "engine": "FFmpeg (built in via PyAV)", "ffmpeg": str(version), "pyav": av.__version__,
                "libavcodec": ".".join(str(x) for x in codec) if codec else "", "formats": FORMAT_NOTE,
                "extensions": EXTENSIONS, "cli_needed": False}
    except Exception as e:  # noqa: BLE001 - a broken install must still show the page
        return {"ok": False, "engine": "FFmpeg (built in via PyAV)", "error": str(e), "extensions": EXTENSIONS,
                "formats": FORMAT_NOTE, "cli_needed": False}


class _Cancelled(Exception):
    pass


def _source_path(token: str) -> Path:
    return CACHE_DIR / f"source-{token}.wav"


def recording_path() -> str | None:
    with _lock:
        token = _state["token"] if _state["status"] == "ready" else None
    return str(CACHE_DIR / f"recording-{token}.wav") if token else None


def _cleanup(keep: set) -> None:
    """Deletes the cache's older decoded copies and recordings (ones still open
    by a worker are left for next time)."""
    try:
        names = os.listdir(CACHE_DIR)
    except OSError:
        return
    for name in names:
        if name in keep or name == STATE_FILE.name:
            continue
        if name.startswith(("source-", "recording-", "upload-", "heard-")):
            target = CACHE_DIR / name
            try:
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
            except OSError:
                pass


def open_file(path: str, name: str | None = None) -> dict:
    """Starts decoding `path` (replacing the file loaded before). Answers at once;
    state() follows the progress."""
    path = (path or "").strip().strip('"')
    if not path or not os.path.isfile(path):
        raise ValueError("That file doesn't exist.")
    _restore()
    token = uuid.uuid4().hex[:12]
    cancel = threading.Event()
    global _source, _recording
    with _lock:
        if _job["cancel"]:
            _job["cancel"].set()
        _job.update(token=token, cancel=cancel)
        _source, _recording = None, None
        _rec.update(armed=False, version=_rec["version"] + 1)
        _state.update(status="decoding", error="", progress=0.0, path=path, name=name or os.path.basename(path),
                      token=None, info=None, edits=[], version=_state["version"] + 1)
    threading.Thread(target=_decode, args=(token, path, cancel), daemon=True).start()
    return state()


def _decode(token: str, path: str, cancel: threading.Event) -> None:
    import av
    import numpy as np

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    final = _source_path(token)
    part = final.with_suffix(".part")
    started = time.monotonic()
    try:
        try:
            container = av.open(path)
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"FFmpeg can't read this file ({e})") from None
        try:
            if not container.streams.audio:
                raise ValueError("This file has no audio.")
            stream = container.streams.audio[0]
            ctx = stream.codec_context
            rate = int(ctx.sample_rate or stream.rate or 0)
            if rate <= 0:
                raise ValueError("FFmpeg couldn't tell this file's sample rate.")
            source_channels = int(getattr(ctx, "channels", 0) or getattr(getattr(ctx, "layout", None), "nb_channels", 0)
                                  or 2)
            channels = 1 if source_channels == 1 else 2
            resampler = av.AudioResampler(format="flt", layout="mono" if channels == 1 else "stereo", rate=rate)
            duration = None
            if container.duration:
                duration = container.duration / av.time_base
            elif stream.duration and stream.time_base:
                duration = float(stream.duration * stream.time_base)
            frames = 0
            with open(part, "wb") as f:
                f.write(host_worker.float_wav_header(channels, rate, 0))

                def put(chunks):
                    nonlocal frames
                    for out in chunks:
                        data = out.to_ndarray()
                        f.write(np.ascontiguousarray(data, dtype="<f4").tobytes())
                        frames += out.samples
                    if frames * channels * 4 > MAX_DATA_BYTES:
                        raise ValueError("This file is too long for the Audio tab (more than 4 GB once decoded).")

                for frame in container.decode(stream):
                    if cancel.is_set():
                        raise _Cancelled()
                    put(resampler.resample(frame))
                    if duration and frame.time is not None:
                        with _lock:
                            if _job["token"] == token:
                                _state["progress"] = round(min(0.97, max(0.0, frame.time / duration)), 3)
                put(resampler.resample(None))
                if not frames:
                    raise ValueError("FFmpeg found no audio samples in this file.")
                f.seek(0)
                f.write(host_worker.float_wav_header(channels, rate, frames))
            info = {
                "format": container.format.long_name or container.format.name,
                "codec": (ctx.codec.long_name or ctx.codec.name) if ctx.codec else "",
                "source_rate": rate, "source_channels": source_channels,
                "bit_rate": int(container.bit_rate or 0) or None, "size": os.path.getsize(path),
            }
        finally:
            container.close()
        os.replace(part, final)
        layout = host_worker.read_wav_layout(str(final))
        track = _Track(final, layout)
        track.build()
        info.update(track.info(), decode_s=round(time.monotonic() - started, 3))
        global _source
        with _lock:
            if _job["token"] != token:
                raise _Cancelled()
            _source = track
            _state.update(status="ready", progress=1.0, token=token, info=info, version=_state["version"] + 1)
            _save_state()
        _cleanup({final.name})
    except _Cancelled:
        for p in (part, final):
            try:
                p.unlink()
            except OSError:
                pass
    except Exception as e:  # noqa: BLE001 - shown in the Audio tab
        try:
            part.unlink()
        except OSError:
            pass
        with _lock:
            if _job["token"] == token:
                _state.update(status="error", error=str(e) or type(e).__name__, progress=0.0)


def _save_state() -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        data = {k: _state[k] for k in ("path", "name", "token", "info", "edits")}
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, STATE_FILE)
    except OSError:
        pass


def _restore() -> None:
    """After a restart: the file that was loaded (its decoded copy, edits and recording) comes back."""
    global _restored, _source, _recording
    with _lock:
        if _restored:
            return
        _restored = True
        try:
            saved = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        token = saved.get("token")
        layout = host_worker.read_wav_layout(str(_source_path(token))) if token else None
        if not layout:
            return
        track = _Track(_source_path(token), layout)
        track.mark(0, track.frames)  # peaked on the first view
        _source = track
        _state.update(status="ready", error="", progress=1.0, path=saved.get("path") or "", token=token,
                      name=saved.get("name") or "", info=saved.get("info"),
                      edits=host_worker._clean_envelope(saved.get("edits")), version=_state["version"] + 1)
        rec = CACHE_DIR / f"recording-{token}.wav"
        rec_layout = host_worker.read_wav_layout(str(rec))
        if rec_layout:
            _recording = _Track(rec, rec_layout)
            _recording.mark(0, _recording.frames)


def unload() -> dict:
    global _source, _recording
    with _lock:
        if _job["cancel"]:
            _job["cancel"].set()
        _job.update(token=None, cancel=None)
        _source, _recording = None, None
        _rec.update(armed=False, version=_rec["version"] + 1)
        _state.update(status="empty", error="", progress=0.0, path="", name="", token=None, info=None, edits=[],
                      version=_state["version"] + 1)
        try:
            STATE_FILE.unlink()
        except OSError:
            pass
    _cleanup(set())
    return state()


def state() -> dict:
    """{status: empty|decoding|ready|error, error, progress, path, name, info,
    edits, can_undo, version, source_path, recording: {exists, armed, rate,
    channels, frames, duration_s, path, version} | {exists: False, armed}}."""
    _restore()
    with _lock:
        out = {k: _state[k] for k in ("status", "error", "progress", "path", "name", "info", "version")}
        out["edits"] = [dict(e) for e in _state["edits"]]
        out["can_undo"] = bool(_state["edits"])
        out["source_path"] = str(_source.path) if _source else None
        rec = {"exists": _recording is not None, "armed": _rec["armed"], "version": _rec["version"],
               "path": recording_path()}
        if _recording is not None:
            rec.update(_recording.info())
        out["recording"] = rec
    return out


def source_for_play() -> tuple[str, list, float]:
    """(decoded copy path, edits, duration) for the worker."""
    _restore()
    with _lock:
        if _state["status"] != "ready" or _source is None:
            raise ValueError("Load an audio file first.")
        return str(_source.path), [dict(e) for e in _state["edits"]], _source.duration


def heard_source_path() -> str:
    """The loaded file as it's heard with no plugin open: the decoded copy with the edits
    (fades, normalize) applied, as a float WAV the page plays (heard-<token>-v<version>.wav,
    remade when the edits change; older ones go with the cache cleanup)."""
    import numpy as np

    _restore()
    with _lock:
        if _state["status"] != "ready" or _source is None:
            raise ValueError("Load an audio file first.")
        track, ops = _source, [dict(e) for e in _state["edits"]]
        out = CACHE_DIR / f"heard-{_state['token']}-v{_state['version']}.wav"
    if out.exists():
        return str(out)
    for old in CACHE_DIR.glob(f"heard-{_state['token']}-v*.wav"):
        try:
            old.unlink()
        except OSError:
            pass
    tmp = out.with_suffix(f".{os.getpid()}.tmp")
    with open(tmp, "wb") as f:
        f.write(host_worker.float_wav_header(track.channels, track.rate, track.frames))
        pos = 0
        while pos < track.frames:
            stop = min(track.frames, pos + CHUNK_FRAMES)
            data = track.read(pos, stop)
            if not len(data):
                break
            if ops:
                t = (pos + np.arange(len(data), dtype=np.float64)) / track.rate
                data = data * host_worker.envelope_gain(ops, t)[:, None]
            f.write(np.ascontiguousarray(data, dtype=np.float32).tobytes())
            pos += len(data)
    os.replace(tmp, out)
    return str(out)


def recording_for_play() -> str:
    with _lock:
        if _recording is None:
            raise ValueError("There's no recording yet.")
        return str(_recording.path)


def peaks(which: str, start: int, end: int, width: int) -> bytes:
    _restore()
    with _lock:
        if which == "recording":
            track, ops = _recording, None
        else:
            track, ops = _source, [dict(e) for e in _state["edits"]]
        if track is None:
            return _HEADER.pack(b"VXPK", KIND_EMPTY, 0, 0)
        return track.query(start, end, width, ops)


# ---------- edits ----------

def _range(start_s, end_s, duration: float) -> tuple[float, float] | None:
    if start_s is None or end_s is None:
        return None
    a, b = sorted((max(0.0, float(start_s)), min(duration, float(end_s))))
    return (a, b) if b - a >= 0.001 else None


def _peak(track: _Track, ops: list, a: float, b: float) -> float:
    import numpy as np

    first, last = int(a * track.rate), min(track.frames, int(np.ceil(b * track.rate)))
    peak, pos = 0.0, first
    while pos < last:
        stop = min(last, pos + CHUNK_FRAMES)
        data = track.read(pos, stop)
        if not len(data):
            break
        if ops:
            t = (pos + np.arange(len(data), dtype=np.float64)) / track.rate
            data = data * host_worker.envelope_gain(ops, t)[:, None]
        peak = max(peak, float(np.abs(data).max()))
        pos += len(data)
    return peak


def edit(op: str, start_s=None, end_s=None, length_s=None) -> dict:
    """fade_in / fade_out over [start_s, end_s] (or `length_s` from the start /
    to the end), normalize (peaks to NORMALIZE_DBFS) over the range or the
    whole file. Non-destructive: one more gain op (see envelope_gain)."""
    with _lock:
        if _state["status"] != "ready" or _source is None:
            raise ValueError("Load an audio file first.")
        if len(_state["edits"]) >= MAX_EDITS:
            raise ValueError("That's a lot of edits: undo some first.")
        duration = _source.duration
        chosen = _range(start_s, end_s, duration)
        ops = [dict(e) for e in _state["edits"]]
        if op in ("fade_in", "fade_out"):
            if chosen is None:
                length = max(0.001, min(duration, float(length_s or 1.0)))
                chosen = (0.0, length) if op == "fade_in" else (duration - length, duration)
            new = {"type": op, "start_s": round(chosen[0], 6), "end_s": round(chosen[1], 6), "gain": 1.0}
        elif op == "normalize":
            a, b = chosen or (0.0, duration)
            peak = _peak(_source, ops, a, b)
            if peak < 1e-6:
                raise ValueError("That part is silent: there's nothing to normalize.")
            gain = 10 ** (NORMALIZE_DBFS / 20) / peak
            # Applies to the samples up to and including the last frame of the range.
            new = {"type": "gain", "start_s": round(a, 6), "end_s": round(b + 1 / _source.rate, 6),
                   "gain": round(gain, 6)}
        else:
            raise ValueError("op must be fade_in, fade_out or normalize")
        _state["edits"].append(new)
        _state["version"] += 1
        _save_state()
    return state()


def undo() -> dict:
    with _lock:
        if not _state["edits"]:
            raise ValueError("Nothing to undo.")
        _state["edits"].pop()
        _state["version"] += 1
        _save_state()
    return state()


# ---------- the recording ----------

def on_record(news: dict) -> None:
    """The worker's news about the recording (plugin_host's record_tap):
    armed or not, its file (re)opened, frames written."""
    global _recording
    with _lock:
        path = news.get("path")
        current = recording_path()
        if path and current and os.path.normcase(path) != os.path.normcase(current):
            return  # an older file's news
        if "on" in news:
            _rec["armed"] = bool(news["on"])
        if news.get("rate") and current:
            layout = host_worker.read_wav_layout(current)
            if layout:
                if news.get("created") or _recording is None or _recording.rate != layout["rate"]:
                    _recording = _Track(Path(current), layout)  # all silence so far
                    if not news.get("created"):
                        _recording.mark(0, _recording.frames)
        dirty = news.get("dirty")
        if dirty and _recording is not None:
            _recording.mark(int(dirty[0]), int(dirty[1]))
        _rec["version"] += 1


def clear_recording(busy: bool) -> dict:
    """Deletes the recording (not while a worker records it: `busy`)."""
    global _recording
    if busy:
        raise ValueError("Stop recording first.")
    with _lock:
        track, _recording = _recording, None
        _rec["version"] += 1
    if track is not None:
        # A reader that was just let go (the page's player, a stopped playback) may
        # still be closing the file: a few tries before giving up.
        for attempt in range(8):
            try:
                track.path.unlink()
                break
            except FileNotFoundError:
                break
            except OSError as e:
                if attempt < 7:
                    time.sleep(0.15)
                    continue
                with _lock:
                    _recording = track
                raise ValueError(f"Couldn't delete the recording ({e.strerror or e}).") from None
    return state()


def save_recording(dest: str | None, plugin_name: str = "") -> str:
    """Copies the recording to `dest` (a .wav path from the save dialog), or,
    without one, to Music\\VEXT\\<file> (<plugin>) <time>.wav. Returns the path."""
    from app import plugin_host

    with _lock:
        if _recording is None:
            raise ValueError("There's no recording to save.")
        src = _recording.path
        base = Path(_state["name"] or "Recording").stem
    if dest:
        target = Path(dest)
        if target.suffix.lower() != ".wav":
            target = target.with_name(target.name + ".wav")
    else:
        stamp = time.strftime("%Y-%m-%d %H.%M.%S")
        label = f"{base} ({plugin_name})" if plugin_name else f"{base} (processed)"
        folder = plugin_host.known_folder(MUSIC_FOLDER, "Music") / "VEXT"
        target = folder / f"{plugin_host.safe_file_name(label)} {stamp}.wav"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, target)
    return str(target)


def upload_target(name: str) -> Path:
    """Where a file dropped on the page without the desktop app (so without its
    path) is written before decoding: cache/playground/upload-<n>/<name>. The
    folder goes once the next file is decoded."""
    from app import plugin_host

    folder = CACHE_DIR / f"upload-{uuid.uuid4().hex[:8]}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / plugin_host.safe_file_name(os.path.basename((name or "audio").replace("\\", "/")))
