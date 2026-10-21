"""Tests for the Plugin buffer (render-ahead) in app/host_worker.py. No audio device, no real plugin.

A fake plugin spikes the way a spectral one does (60 ms on every 8th call, 1 ms otherwise). A fake output
stream runs at real-time pace: write() returns once the device has room for the block (it holds one block
ahead of what it plays). A device that ran dry plays the silence it had and reports the write as late, the
way PortAudio's underflow flag does. Player._open_stream and the audio module are replaced, and the
worker's events are captured instead of printed.

Run:  .venv-native\\Scripts\\python.exe tools\\test_render_ahead.py
"""
import os
import shutil
import sys
import tempfile
import threading
import time
import types

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import host_worker as hw  # noqa: E402

RATE, BLOCK = 48000, 512
FADE = int(RATE * hw.FADE_IN_S)  # the fade-in after a silence, in frames (compare() does not check it)
LEAD_IN = 4800  # 0.1 s of silence at the head of each test signal: the fade-in after the start lands on it
WORK = tempfile.mkdtemp(prefix="vext-render-ahead-")

EVENTS: list = []  # (monotonic time, event, fields) as the worker emits them
STREAMS: list = []  # every fake output stream opened, in order


class FakePlugin:
    """A heavy spectral plugin stand-in: 60 ms on every 8th call (an FFT frame), 1 ms otherwise.
    Its output is the input at half level."""

    name = "fake spectral"
    is_instrument = False

    def __init__(self, spikes: bool = True):
        self.spikes = spikes
        self.calls = 0

    def __call__(self, data, rate, reset=False):
        self.calls += 1
        time.sleep(0.060 if self.spikes and self.calls % 8 == 0 else 0.001)
        return data * np.float32(0.5)


class LatencyPlugin:
    """A plugin with latency, handed back the way pedalboard does it (measured with RX 8 De-click): the
    plugin's delay is DELAY frames, pedalboard withholds the first WITHHELD frames out (blocks come back
    shorter than they went in), and what does come back still lags DELAY - WITHHELD. Output at half level."""

    name = "fake latency"
    is_instrument = False
    DELAY, WITHHELD = 3000, 2000
    latency_samples = DELAY

    def __init__(self):
        self._line = np.zeros((2, self.DELAY), dtype=np.float32)
        self._skip = self.WITHHELD

    def __call__(self, data, rate, reset=False):
        n = data.shape[1]
        self._line = np.concatenate([self._line, data * np.float32(0.5)], axis=1)
        out, self._line = self._line[:, :n], self._line[:, n:]
        drop = min(self._skip, n)
        self._skip -= drop
        return out[:, drop:]


class FakeTap:
    """The analysis ring: keeps every Before / After block written."""

    def __init__(self):
        self.before, self.after = [], []

    def start(self, rate, mode, has_before=True):
        pass

    def idle(self):
        pass

    def close(self):
        pass

    def write(self, before, after, bypass=False):
        self.before.append(np.array(before))
        self.after.append(np.array(after))


class FakeStream:
    """A real-time output device. `played()` is what it played, in order, silence included."""

    def __init__(self, rate: int, block: int):
        self.rate, self.latency = rate, block / rate
        self._lock = threading.Lock()  # the output thread writes while the test reads played()
        self._parts: list = []  # what the device was given, in order: (n, 2) float32 arrays
        self._total = 0  # frames in _parts
        self._due = None  # the wall time at which the device runs out of what it was given
        self.late = 0  # writes that came after the device had run dry

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def close(self) -> None:
        pass

    def abort(self) -> None:
        """Drops what the device hasn't played yet (a pause)."""
        with self._lock:
            if self._due is not None and self._parts:
                remaining = max(0, int(round((self._due - time.perf_counter()) * self.rate)))
                keep = max(0, self._total - remaining)
                self._parts = [np.concatenate(self._parts)[:keep]]
                self._total = keep
            self._due = None

    def write(self, a) -> bool:
        with self._lock:
            now = time.perf_counter()
            late = False
            if self._due is None:
                self._due = now
            elif now > self._due:  # the device played silence until this block came
                gap = int(round((now - self._due) * self.rate))
                if gap:
                    self._parts.append(np.zeros((gap, 2), dtype=np.float32))
                    self._total += gap
                self._due += gap / self.rate
                late = gap > 0
            frames = np.array(a, dtype=np.float32, copy=True)
            self._parts.append(frames)
            self._total += len(frames)
            self._due += len(frames) / self.rate
            self.late += late
            wait = self._due - self.latency - now  # room for this block: the device holds one ahead
        if wait > 0:
            time.sleep(wait)
        return late

    def frames(self) -> int:
        with self._lock:
            return self._total

    def played(self) -> np.ndarray:
        with self._lock:
            if not self._parts:
                return np.zeros((0, 2), dtype=np.float32)
            self._parts = [np.concatenate(self._parts)]
            return self._parts[0]


DEVICE_RATE = None  # set to make the device take another rate than asked (the live resampling case)


def fake_open(sd, audio, notes):
    rate = DEVICE_RATE or int(audio.get("samplerate") or RATE)
    block = int(audio.get("blocksize") or BLOCK)
    stream = FakeStream(rate, block)
    STREAMS.append(stream)
    info = {"driver": "fake", "device": "", "device_name": "fake device", "hostapi": "", "rate": rate,
            "block": block, "safe": False, "buffer_ms": round(block * 1000 / rate, 1),
            "latency_ms": round(stream.latency * 1000, 1)}
    return stream, rate, block, info


def fake_emit(event, **fields):
    EVENTS.append((time.monotonic(), event, fields))


hw.emit = fake_emit
hw.sounddevice_module = lambda asio=False: None  # no PortAudio: fake_open makes the stream
hw.Player._open_stream = staticmethod(fake_open)
os.environ.pop("VEXT_TEST_OUTPUT_GAIN", None)


# ---------- helpers ----------

def new_player(live_input=None) -> "hw.Player":
    EVENTS.clear()
    return hw.Player(FakePlugin(), live_input)


def fields(event: str) -> list:
    return [f for _, e, f in EVENTS if e == event]


def wait_for(predicate, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def last_position() -> dict:
    found = fields("position")
    return found[-1] if found else {}


def audio(buffer: str) -> dict:
    return {"samplerate": RATE, "blocksize": BLOCK, "buffer": buffer}


def make_source(seconds: float, seed: int) -> np.ndarray:
    """Seeded noise (so every frame is distinct: a position can be found exactly), LEAD_IN of silence."""
    rng = np.random.default_rng(seed)
    src = rng.uniform(-0.5, 0.5, (int(round(seconds * RATE)), 2)).astype(np.float32)
    src[:LEAD_IN] = 0
    return src


def write_wav(name: str, src: np.ndarray) -> str:
    path = os.path.join(WORK, name)
    with open(path, "wb") as f:
        f.write(hw.float_wav_header(2, RATE, len(src)))
        f.write(np.ascontiguousarray(src, dtype="<f4").tobytes())
    return path


def compare(dev: np.ndarray, src: np.ndarray) -> dict:
    """What the device played (dev) against the source `src` (at half level), run by run. A run is a
    stretch of sound between silences. Each run is placed in the source by an exact match of its first
    unfaded frame, then checked frame by frame. The first FADE frames after a silence are the fade-in
    (the first of them is exactly zero: the source frame it belongs to is skipped by the device, not lost).
    gaps: silent frames between the runs. jumps: runs that don't follow on from the one before (source
    skipped or repeated). first / end: the source frames the device played first and up to (exclusive)."""
    scaled = src * np.float32(0.5)
    lut = {float(v): i for i, v in enumerate(scaled[:, 0])}
    live = np.any(dev != 0, axis=1)
    edges = np.diff(np.concatenate(([0], live.astype(np.int8), [0])))
    starts, ends = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)
    out = {"runs": len(starts), "gaps": 0, "checked": 0, "bad": 0, "jumps": 0, "first": None, "end": None}
    prev_end = prev_next = None
    for s, e in zip(starts, ends):
        s, e = int(s), int(e)
        silent_before = prev_end is not None and s > prev_end
        if silent_before:
            out["gaps"] += s - prev_end
        prev_end = e
        r = s + FADE + 1  # the first frame that is not faded
        i = lut.get(float(dev[r, 0])) if r < e else None
        if i is None:
            out["bad"] += max(0, e - r)
            prev_next = None
            continue
        base = i - (r - s)  # the source frame at the run's first sample
        n = min(e - r, len(scaled) - i)
        same = np.all(np.isclose(dev[r:r + n], scaled[i:i + n], atol=1e-6), axis=1)
        out["checked"] += n
        out["bad"] += int(np.count_nonzero(~same))
        if prev_next is not None:
            # After a silence the chunk's first source frame is the faded zero: the sound resumes one on.
            expect = prev_next + (1 if silent_before else 0)
            if base != expect:
                out["jumps"] += 1
        if out["first"] is None:
            out["first"] = base
        out["end"] = base + (e - s)
        prev_next = base + (e - s)
    return out


def play_file(player, path: str, start_s: float = 0.0, buffer: str = "auto", **kw) -> None:
    player.play(path, start_s, audio(buffer), **kw)


def stopped_within(timeout: float) -> bool:
    return wait_for(lambda: bool(fields("stopped")), timeout)


# ---------- cases (each returns (passed, detail)) ----------

def case_targets():
    got = {
        "auto": hw.render_target("auto", RATE, BLOCK, False),
        "25": hw.render_target("25", RATE, BLOCK, False),
        "800": hw.render_target("800", RATE, BLOCK, False),
        "live 25": hw.render_target("25", RATE, BLOCK, True),
        "auto learned 0.2s": hw.render_target("auto", RATE, BLOCK, False, learned_s=0.2),
    }
    want = {"auto": (2880, True), "25": (1200, False), "800": (38400, False), "live 25": (4096, False),
            "auto learned 0.2s": (9600, True)}
    return got == want, f"{got}"


def case_file_auto():
    src = make_source(2.0, seed=1)
    player = new_player()
    play_file(player, write_wav("auto.wav", src), buffer="auto")
    stopped = stopped_within(12)
    stream = STREAMS[-1]
    res = compare(stream.played(), src)
    pos = last_position()
    complete = res["first"] == LEAD_IN and res["end"] == len(src)
    ok = (stopped and stream.late == 0 and res["gaps"] == 0 and res["bad"] == 0 and res["jumps"] == 0
          and complete)
    return ok, (f"finished={fields('stopped')[-1].get('finished') if stopped else None} "
                f"gaps={res['gaps']} xruns={stream.late} starves={pos.get('starves')} "
                f"target_ms={pos.get('target_ms')} runs={res['runs']} first={res['first']} end={res['end']} "
                f"checked={res['checked']} bad={res['bad']} jumps={res['jumps']}")


def case_file_25_spikes():
    src = make_source(2.0, seed=1)
    player = new_player()
    play_file(player, write_wav("25.wav", src), buffer="25")
    stopped_within(12)
    stream = STREAMS[-1]
    res = compare(stream.played(), src)
    pos = last_position()
    starves = pos.get("starves", 0)
    return starves > 0 or res["gaps"] > 0, (f"starves={starves} gaps={res['gaps']} xruns={stream.late} "
                                            f"load_pct={pos.get('load_pct')} "
                                            f"render_peak_ms={pos.get('render_peak_ms')}")


def case_record():
    src = make_source(2.0, seed=2)
    rec = os.path.join(WORK, "recording.wav")
    player = new_player()
    player.record_on(rec, 2.0)
    play_file(player, write_wav("record.wav", src), buffer="auto")
    stopped = stopped_within(12)
    closed = wait_for(lambda: any(not f.get("on") for f in fields("record")), 5)
    data = np.fromfile(rec, dtype="<f4", offset=hw.WAV_FLOAT_HEADER).reshape(-1, 2)
    exact = data.shape[0] >= len(src) and np.array_equal(data[:len(src)], src * np.float32(0.5))
    stream = STREAMS[-1]
    gaps = compare(stream.played(), src)["gaps"]
    ok = stopped and closed and exact and stream.late == 0 and gaps == 0
    return ok, f"recorded_frames={data.shape[0]} exact={exact} gaps={gaps} xruns={stream.late}"


def case_seek():
    src = make_source(2.0, seed=3)
    player = new_player()
    play_file(player, write_wav("seek.wav", src), buffer="auto")
    wait_for(lambda: last_position().get("position_s", 0) >= 0.5, 6)
    stream = STREAMS[-1]
    before = stream.frames()
    player.seek(1.0)
    reached = wait_for(lambda: last_position().get("position_s", 0) >= 1.5, 6)
    player.stop()
    post = stream.played()[before:]
    k = int(round(1.0 * RATE)) + FADE  # the first FADE frames after the silence are faded: look past them
    hits = np.flatnonzero(np.all(post == src[k] * np.float32(0.5), axis=1))
    if not len(hits):
        return False, "the sound at 1.0 s never came out after the seek"
    j = int(hits[0])
    seg = post[j:j + 20000]
    ref = src[k:k + len(seg)] * np.float32(0.5)
    ok = reached and len(seg) == 20000 and np.allclose(seg, ref, atol=1e-6)
    return ok, f"reached={reached} sound_at_1.0s_after_frames={j} compared={len(seg)}"


def case_pause_resume():
    src = make_source(2.0, seed=4)
    player = new_player()
    play_file(player, write_wav("pause.wav", src), buffer="auto")
    wait_for(lambda: last_position().get("position_s", 0) >= 0.6, 6)
    stream = STREAMS[-1]
    player.pause()
    time.sleep(0.1)  # the block in flight at the pause lands first; the position is then what was heard
    held = player.position
    time.sleep(0.3)
    still = player.position == held
    cut = stream.frames()  # the abort dropped what was buffered: resume appends from here
    player.resume()
    wait_for(lambda: last_position().get("position_s", 0) >= held + 0.3, 6)
    player.stop()
    post = stream.played()[cut:]
    k = int(round(held * RATE))
    best = None  # the resumed sound must start at the heard position (exact, give or take a frame)
    for off in range(-2, 3):
        ref = src[k + off:k + off + 4000] * np.float32(0.5)
        if len(post) >= 4000 and np.allclose(post[:4000], ref, atol=1e-6):
            best = off
            break
    ok = still and best is not None
    return ok, f"position_held={still} resumed_from_frame_offset={best} held_at={held:.4f}s"


def case_loop():
    src = make_source(2.0, seed=5)
    player = new_player()
    play_file(player, write_wav("loop.wav", src), buffer="auto", loop=[0.1, 0.6])
    stream = STREAMS[-1]
    half = int(0.5 * RATE)
    want = LEAD_IN + 2 * half + RATE // 5
    reached = wait_for(lambda: stream.frames() >= want, 10)
    player.stop()
    dev = stream.played()
    live = np.flatnonzero(np.any(dev != 0, axis=1))
    if not len(live):
        return False, "nothing played"
    seg = dev[live[0]:live[0] + 2 * half]
    loop_ref = src[LEAD_IN:LEAD_IN + half] * np.float32(0.5)
    ok = reached and len(seg) == 2 * half and np.allclose(seg, np.concatenate([loop_ref, loop_ref]), atol=1e-6)
    return ok, f"loop_played_twice={ok} frames={len(seg)}"


def source_index(dev: np.ndarray, src: np.ndarray) -> np.ndarray:
    """For each frame the device played: the source frame it is (at half level), -1 for silence / unknown."""
    half = src * np.float32(0.5)
    lut = {(float(a), float(b)): i for i, (a, b) in enumerate(half)}
    return np.array([lut.get((float(a), float(b)), -1) if a != 0 else -1 for a, b in dev])


def case_latency_aligned():
    src = make_source(2.0, seed=11)
    rec = os.path.join(WORK, "latency-rec.wav")
    EVENTS.clear()
    player = hw.Player(LatencyPlugin(), None)
    tap = FakeTap()
    player._tap = tap
    player.record_on(rec, 2.0)
    play_file(player, write_wav("latency.wav", src), buffer="auto")
    stopped = stopped_within(12)
    wait_for(lambda: any(not f.get("on") for f in fields("record")), 5)
    res = compare(STREAMS[-1].played(), src)
    data = np.fromfile(rec, dtype="<f4", offset=hw.WAV_FLOAT_HEADER).reshape(-1, 2)
    exact = data.shape[0] >= len(src) and np.array_equal(data[:len(src)], src * np.float32(0.5))
    before, after = np.concatenate(tap.before, axis=1), np.concatenate(tap.after, axis=1)
    in_phase = before.shape == after.shape and np.allclose(after, before * np.float32(0.5), atol=1e-6)
    # The whole file is heard (its last DELAY frames come out of the plugin after the end), in place.
    ok = (stopped and exact and in_phase and res["gaps"] == 0 and res["bad"] == 0 and res["jumps"] == 0
          and res["first"] == LEAD_IN and res["end"] == len(src))
    return ok, (f"recording_exact={exact} before_after_in_phase={in_phase} first={res['first']} "
                f"end={res['end']} gaps={res['gaps']} bad={res['bad']} jumps={res['jumps']}")


def case_selection_behind_playhead():
    """A selection made behind the playhead while playing with a big buffer: what was rendered ahead is
    dropped and the selection is heard next, not after the old buffer has played out."""
    src = make_source(3.0, seed=12)
    player = new_player()
    player.plugin.spikes = False
    play_file(player, write_wav("behind.wav", src), buffer="400")
    stream = STREAMS[-1]
    wait_for(lambda: player.position >= 1.0, 10)
    called = stream.frames()
    player.set_loop([0.2, 0.45])
    wait_for(lambda: stream.frames() >= called + int(1.2 * RATE), 10)
    player.stop()
    idx = source_index(stream.played(), src)[called:]
    # One device block may already have been handed over when the loop changed; after that, only the loop.
    later = idx[3 * BLOCK:]
    later = later[later >= 0]
    inside = bool(len(later)) and later.min() >= int(0.2 * RATE) and later.max() < int(0.45 * RATE)
    buffering = any(f.get("buffering") for f in fields("position"))
    return inside and buffering, (f"inside_loop={inside} heard={len(later)} "
                                  f"min={later.min() if len(later) else None} max={later.max() if len(later) else None} "
                                  f"buffering_reported={buffering}")


def case_loop_off_while_looping():
    """Loop turned off while the loop plays: the jump back that was already rendered is dropped."""
    src = make_source(3.0, seed=13)
    player = new_player()
    player.plugin.spikes = False
    play_file(player, write_wav("loopoff.wav", src), buffer="400", loop=[0.2, 0.7])
    stream = STREAMS[-1]
    wait_for(lambda: 0.55 <= player.position < 0.65, 10)
    called = stream.frames()
    player.set_loop(None)
    wait_for(lambda: stream.frames() >= called + int(1.0 * RATE), 10)
    player.stop()
    idx = source_index(stream.played(), src)[called:]
    idx = idx[idx >= 0]
    back = int(np.count_nonzero(np.diff(idx) < 0))  # jumps backwards (a wrap to the loop's start)
    went_on = bool(len(idx)) and idx.max() > int(0.9 * RATE)
    return back == 0 and went_on, f"backward_jumps={back} reached={idx.max() / RATE if len(idx) else None}s"


def case_until():
    src = make_source(2.0, seed=6)
    player = new_player()
    play_file(player, write_wav("until.wav", src), start_s=0.5, buffer="auto", until=[0.5, 1.0])
    stopped = stopped_within(8)
    stop_fields = fields("stopped")[-1] if stopped else {}
    res = compare(STREAMS[-1].played(), src)
    # The selection is 0.5 s to 1.0 s: the device plays source frames 24000..47999 (the first is faded to zero).
    ok = (stopped and stop_fields.get("finished") is True and abs(stop_fields.get("position_s", -1) - 1.0) < 1e-3
          and res["gaps"] == 0 and res["bad"] == 0 and res["jumps"] == 0
          and res["first"] in (24000, 24001) and res["end"] == int(1.0 * RATE))
    return ok, (f"finished={stop_fields.get('finished')} position_s={stop_fields.get('position_s')} "
                f"first={res['first']} end={res['end']} gaps={res['gaps']} checked={res['checked']} "
                f"bad={res['bad']} jumps={res['jumps']}")


def case_live_bursts():
    buffer = hw._LiveBuffer()
    live_input = types.SimpleNamespace(buffer=buffer, connected=True)
    player = new_player(live_input)
    src = make_source(2.0, seed=7)
    audio_dict = {"samplerate": RATE, "blocksize": BLOCK, "buffer": "auto"}
    player.live(audio_dict, RATE)
    t0 = time.monotonic()
    pos, burst = 0, 0
    # The browser's frames: 4 chunks of 2048 every 170 ms (bursty, a little under real time).
    while pos < len(src):
        for _ in range(4):
            piece = src[pos:pos + hw.BROWSER_CHUNK]
            if not len(piece):
                break
            buffer.push(piece)
            pos += len(piece)
        burst += 1
        time.sleep(max(0.0, t0 + burst * 0.170 - time.monotonic()))
    t_last = time.monotonic()
    time.sleep(0.6)  # let the device play out what's left
    player.stop()
    # Starves while the input was still arriving, after the first second (the Auto buffer's learning).
    lives = [(t, f) for t, e, f in EVENTS if e == "live"]
    window = [f["starves"] for t, f in lives if t0 + 1.0 <= t <= t_last]
    steady = (window[-1] - window[0]) if window else None
    total = lives[-1][1]["starves"] if lives else None
    res = compare(STREAMS[-1].played(), src)
    ok = steady == 0 and res["bad"] == 0 and res["jumps"] == 0 and res["checked"] > 0
    return ok, (f"starves_after_1s={steady} starves_total={total} gaps={res['gaps']} runs={res['runs']} "
                f"checked={res['checked']} bad={res['bad']} jumps={res['jumps']} "
                f"dropped={lives[-1][1]['dropped'] if lives else None}")


def first_sound(dev: np.ndarray) -> int:
    return int(np.flatnonzero(np.any(dev != 0, axis=1))[0])


def case_envelope():
    src = make_source(2.0, seed=8)
    player = new_player()
    gain = [{"type": "gain", "start_s": 0.0, "end_s": 2.0, "gain": 0.5}]
    play_file(player, write_wav("env.wav", src), envelope=gain)
    stopped = stopped_within(12)
    res = compare(STREAMS[-1].played(), src * np.float32(0.5))  # the Audio tab's gain, then the plugin's half
    ok = stopped and res["bad"] == 0 and res["gaps"] == 0 and res["jumps"] == 0
    return ok, f"runs={res['runs']} bad={res['bad']}"


def case_mix_recording_along():
    src, rec = make_source(2.0, seed=9), make_source(2.0, seed=10)
    player = new_player()
    mix = {"path": write_wav("mix_rec.wav", rec), "source": 1.0, "recording": 1.0}
    play_file(player, write_wav("mix_src.wav", src), mix=mix)
    stopped = stopped_within(12)
    res = compare(STREAMS[-1].played(), src + rec)  # both files, then the plugin's half
    ok = stopped and res["bad"] == 0 and res["jumps"] == 0
    return ok, f"runs={res['runs']} bad={res['bad']} jumps={res['jumps']}"


def case_dry_and_bypass():
    src = make_source(2.0, seed=11)
    path = write_wav("dry.wav", src)
    out = []
    for name, kw in (("dry", {"dry": True}), ("bypass", {"bypass": True})):
        player = new_player()
        if kw.get("bypass"):
            player.bypass = True
        play_file(player, path, dry=bool(kw.get("dry")))
        stopped = stopped_within(12)
        dev = STREAMS[-1].played()
        f = first_sound(dev)
        n = min(20000, len(dev) - f)
        same = stopped and np.allclose(dev[f:f + n], src[LEAD_IN:LEAD_IN + n], atol=1e-6)
        out.append((name, bool(same)))
    return all(ok for _, ok in out), str(out)


def case_plugin_fails():
    class Failing(FakePlugin):
        def __call__(self, data, rate, reset=False):
            if self.calls >= 20:
                raise RuntimeError("plugin exploded")
            return super().__call__(data, rate, reset)

    src = make_source(2.0, seed=12)
    EVENTS.clear()
    player = hw.Player(Failing(spikes=False), None)
    play_file(player, write_wav("fail.wav", src))
    stopped = stopped_within(12)
    errors = [f.get("message") for f in fields("error")]
    finished = [f.get("finished") for f in fields("stopped")]
    ok = stopped and any("plugin exploded" in (m or "") for m in errors) and finished == [False]
    return ok, f"errors={errors} stopped_finished={finished}"


def case_live_resampled():
    global DEVICE_RATE
    DEVICE_RATE = 44100  # the browser sends 48 kHz; the device takes 44.1 kHz: the resampler runs
    try:
        buffer = hw._LiveBuffer()
        player = new_player(types.SimpleNamespace(buffer=buffer, connected=True))
        src = make_source(1.5, seed=13)
        player.live({"samplerate": RATE, "blocksize": BLOCK, "buffer": "auto"}, RATE)
        pos = 0
        t0 = time.monotonic()
        while pos < len(src):
            piece = src[pos:pos + hw.BROWSER_CHUNK]
            buffer.push(piece)
            pos += len(piece)
            time.sleep(max(0.0, t0 + pos / RATE - time.monotonic()))
        time.sleep(0.4)
        player.stop()
        errors = [f.get("message") for f in fields("error")]
        played = STREAMS[-1].played()
        ok = not errors and len(played) > 1.0 * 44100 and STREAMS[-1].rate == 44100
        return ok, f"errors={errors} device_frames={len(played)} rate={STREAMS[-1].rate}"
    finally:
        DEVICE_RATE = None


CASES = [
    ("Plugin buffer targets", case_targets),
    ("file, Auto, spiky plugin: gap-free, no xruns, exact", case_file_auto),
    ("file, 25 ms, spiky plugin: the old behaviour starves", case_file_25_spikes),
    ("record: the recording is input x 0.5 exactly", case_record),
    ("seek: lands on the sample at the seek time", case_seek),
    ("pause / resume: holds the position, resumes from the heard frame", case_pause_resume),
    ("loop: the selection plays twice", case_loop),
    ("until: ends at the selection's end", case_until),
    ("live: bursty browser input, Auto, no starves once learned", case_live_bursts),
    ("envelope (the Audio tab's gain) reaches the plugin", case_envelope),
    ("mix: the recording plays along into the plugin", case_mix_recording_along),
    ("dry and bypass: the file comes out unprocessed", case_dry_and_bypass),
    ("a plugin that raises: reported, the playback ends unfinished", case_plugin_fails),
    ("live at a device rate other than the browser's: resampled, no errors", case_live_resampled),
    ("a plugin with latency: heard, recorded and analysed in place, nothing lost", case_latency_aligned),
    ("a selection behind the playhead: heard next, not after the old buffer", case_selection_behind_playhead),
    ("Loop off while looping: plays on, no jump back", case_loop_off_while_looping),
]


def main() -> int:
    results = []
    try:
        for name, case in CASES:
            try:
                passed, detail = case()
            except Exception as e:  # noqa: BLE001 - a crash is a failed case, the rest still run
                passed, detail = False, f"exception: {type(e).__name__}: {e}"
            results.append(passed)
            print(f"{'PASS' if passed else 'FAIL'}  {name}\n      {detail}", flush=True)
    finally:
        shutil.rmtree(WORK, ignore_errors=True)
    print(f"\n{sum(results)} of {len(results)} cases PASS")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
