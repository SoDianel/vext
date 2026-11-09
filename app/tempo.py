"""Local, bounded tempo estimation and the Playground's shared BPM setting."""
import json
import math
import threading
import time
from pathlib import Path

from app.config import PROJECT_ROOT

DEFAULT = 100.0
SETTINGS = PROJECT_ROOT / "cache" / "playground" / "tempo.json"
_lock = threading.RLock()
_listeners = []
_state = {"bpm": DEFAULT, "mode": "auto", "source": "default", "confidence": 0.0}
_loaded = False
_transport = {"playing": False, "position": 0.0, "at": time.monotonic()}
_transport_listeners = []
_transport_owner = None


def state():
    global _loaded
    with _lock:
        if not _loaded:
            _loaded = True
            try:
                saved = json.loads(SETTINGS.read_text(encoding="utf-8"))
                if isinstance(saved, dict) and saved.get("mode") == "manual":
                    value = float(saved["bpm"])
                    if math.isfinite(value) and 30 <= value <= 300:
                        _state.update(bpm=value, mode="manual", source="manual")
            except (OSError, ValueError, TypeError, KeyError):
                pass
        return dict(_state)


def listen(callback):
    _listeners.append(callback)


def set_value(bpm=None, mode=None, *, source="manual", confidence=0.0):
    current = state()
    if mode not in (None, "auto", "manual"):
        raise ValueError("Tempo mode must be auto or manual")
    if bpm is not None:
        bpm = float(bpm)
        if not math.isfinite(bpm) or not 30 <= bpm <= 300:
            raise ValueError("BPM must be between 30 and 300")
    with _lock:
        if source != "manual" and _state["mode"] == "manual":
            return dict(_state)
        if mode:
            _state["mode"] = mode
        if mode == "auto" and bpm is None:
            bpm, source, confidence = DEFAULT, "default", 0.0
        if bpm is not None:
            _state.update(bpm=round(bpm, 1), source=source, confidence=round(confidence, 3))
        result = dict(_state)
        if source == "manual" or mode:
            SETTINGS.parent.mkdir(parents=True, exist_ok=True)
            SETTINGS.write_text(json.dumps(result), encoding="utf-8")
    if result != current:
        for callback in tuple(_listeners):
            callback(result)
    return result


def estimate(envelope, rate=200):
    """Return (BPM, confidence), or the default when no stable pulse exists.

    Autocorrelation of positive energy changes avoids guessing BPM from duration.
    Broad/flat correlations (noise, sustained tones) do not establish a beat.
    """
    import numpy as np

    x = np.asarray(envelope, dtype=np.float64)
    if x.size < rate * 6 or not np.isfinite(x).all() or x.max(initial=0) < 1e-8:
        return DEFAULT, 0.0
    x = np.convolve(x, np.ones(max(1, round(rate * .1))) / max(1, round(rate * .1)), mode="valid")
    if x.max() - x.min() < max(1e-7, float(x.mean()) * .12):
        return DEFAULT, 0.0
    onset = np.maximum(np.diff(x, prepend=x[0]), 0)
    floor = np.median(onset)
    onset = np.maximum(onset - floor * 2, 0)
    if onset.max(initial=0) < 1e-8 or np.count_nonzero(onset > onset.max() * .3) < 5:
        return DEFAULT, 0.0
    onset = np.convolve(onset, np.ones(3) / 3, mode="same")
    onset -= onset.mean()
    size = 1 << (2 * len(onset) - 1).bit_length()
    spectrum = np.fft.rfft(onset, size)
    corr = np.fft.irfft(spectrum * spectrum.conjugate(), size)[:len(onset)]
    if corr[0] <= 0:
        return DEFAULT, 0.0
    corr /= corr[0]
    lo, hi = int(rate * 60 / 200), int(rate * 60 / 50)
    region = corr[lo:hi + 1]
    peaks = [lag for lag in range(lo + 1, hi)
             if corr[lag] > corr[lag - 1] and corr[lag] >= corr[lag + 1]]
    if not peaks:
        return DEFAULT, 0.0
    # Prefer the shortest equally strong period, avoiding half-tempo multiples.
    best = max(corr[p] for p in peaks)
    lag = min(p for p in peaks if corr[p] >= best * .88)
    confidence = float(corr[lag])
    if confidence < .35 or confidence - float(np.median(region)) < .25:
        return DEFAULT, 0.0
    delta = .5 * (corr[lag - 1] - corr[lag + 1]) / (corr[lag - 1] - 2 * corr[lag] + corr[lag + 1])
    return float(round(60 * rate / (lag + delta), 1)), confidence


def envelope(samples, rate):
    import numpy as np

    data = np.asarray(samples, dtype=np.float32)
    if data.ndim == 2:
        data = np.mean(data * data, axis=0)
    else:
        data = data * data
    hop = max(1, int(rate) // 200)
    data = data[:len(data) // hop * hop]
    return np.sqrt(data.reshape(-1, hop).mean(axis=1)) if len(data) else np.empty(0)


def analyze_file(path, valid=lambda: True):
    """Analyze bounded windows distributed over the decoded file's duration."""
    import numpy as np
    from app.host_worker import read_wav_layout

    layout = read_wav_layout(str(path))
    if not layout or not layout["frames"]:
        return None
    rate, channels, frames = layout["rate"], layout["channels"], layout["frames"]
    data = np.memmap(path, dtype="<f4", mode="r", offset=layout["offset"], shape=(frames, channels))
    try:
        length = min(frames, rate * 20)
        results = []
        for start in sorted({0, max(0, (frames - length) // 2), max(0, frames - length)}):
            results.append(estimate(envelope(data[start:start + length].T, rate), rate / max(1, rate // 200)))
        bpm, confidence = max(results, key=lambda result: result[1])
    finally:
        del data
    if valid():
        return set_value(bpm, source="file" if confidence else "default", confidence=confidence)


class Live:
    """Reduce blocks on ingress; correlate a bounded snapshot off the audio loop."""
    def __init__(self):
        self._lock = threading.Lock()
        self._values = []
        self._tail = None
        self._key = None
        self._last = 0.0
        self._busy = False

    def feed(self, samples, rate, key):
        import numpy as np

        if state()["mode"] != "auto":
            return
        key = (key, int(rate))
        now = time.monotonic()
        with self._lock:
            if key != self._key or now - self._last > 3:
                self._values, self._tail, self._key = [], None, key
            self._last = now
            data = np.asarray(samples, dtype=np.float32)
            if self._tail is not None:
                data = np.concatenate((self._tail, data), axis=1)
            hop = max(1, int(rate) // 200)
            count = data.shape[1] // hop * hop
            self._tail = data[:, count:].copy()
            self._values.extend(envelope(data[:, :count], rate).tolist())
            self._values = self._values[-6000:]
            if len(self._values) < 1200 or self._busy:
                return
            self._busy = True
            snapshot = self._values.copy()

        def detect():
            try:
                bpm, confidence = estimate(snapshot, rate / hop)
                if self._key == key and time.monotonic() - self._last < 3:
                    set_value(bpm, source="live" if confidence else "default", confidence=confidence)
                time.sleep(2)
            finally:
                self._busy = False
        threading.Thread(target=detect, daemon=True).start()


def transport_state():
    with _lock:
        value = dict(_transport)
    elapsed = time.monotonic() - value.pop("at")
    if value["playing"]:
        value["position"] += elapsed
    return value


def listen_transport(callback):
    _transport_listeners.append(callback)


def set_transport(playing, position=None, *, owner=None):
    global _transport_owner
    current = transport_state()
    value = max(0.0, float(position)) if position is not None else current["position"]
    if not math.isfinite(value):
        return
    with _lock:
        _transport_owner = owner
        _transport.update(playing=bool(playing), position=value, at=time.monotonic())
    for callback in tuple(_transport_listeners):
        callback({"playing": bool(playing), "position": value})


def release_transport(owner, position=None, *, claim_idle=False):
    """A closed source cannot stop the clock of the source that replaced it."""
    global _transport_owner
    with _lock:
        if _transport_owner != owner and not (claim_idle and _transport_owner is None):
            return
        value = max(0.0, float(position)) if position is not None else transport_state()["position"]
        if not math.isfinite(value):
            return
        _transport_owner = None
        _transport.update(playing=False, position=value, at=time.monotonic())
    for callback in tuple(_transport_listeners):
        callback({"playing": False, "position": value})
