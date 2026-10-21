"""Records the browser's sound into the Audio tab (AGENTS.md "Analyzer").

POST /api/playground/browser-record {on: true} starts a thread that follows the
analysis tap (app/analysis_tap.py) and appends what the browser plays to a float
WAV in cache/playground/captures/. Only frames of the browser count: mode live
(through the open plugin: the sound after it) or browser (no plugin). {on: false}
stops, finalises the file and the route opens it as the Audio tab's file.

The thread reads the ring one block after another (the ring holds ~5 s, polled
every 40 ms), so nothing is skipped. Stretches where the stream stopped (the tap's
silence skip) are simply not recorded. A change of sample rate ends the recording
(what was recorded is kept) with an error the page shows.

Each ring has its own cursor: a ring that plays something else (a plugin looping a
file) is left alone, and going back to a ring continues where it stopped. The
silence before the first sound and after the last one is cut when the file is
finalised.
"""

import os
import threading
import time

import numpy as np

from app import analysis_tap, host_worker, playground_audio

CAPTURES = playground_audio.CACHE_DIR / "captures"
POLL_S = 0.04
READ_FRAMES = 1 << 16  # frames taken per read
MODES = (analysis_tap.LIVE, analysis_tap.BROWSER)  # what a recording takes: the browser's sound
MAX_FRAMES = playground_audio.MAX_DATA_BYTES // 8  # one RIFF file (stereo float32)
NOTHING = "Nothing was recorded: play something in the browser"


class RecordError(Exception):
    """A request that can't be done now (the route answers 409)."""


class _Job:
    def __init__(self, path, file, cursors):
        self.path, self.file = path, file
        self.name = f"Browser recording {time.strftime('%Y-%m-%d %H.%M.%S')}.wav"
        self.cursors = cursors  # id(ring) -> (gen, next frame): what was already there when it started
        self.rate = 0
        self.frames = 0
        self.first = self.last = None  # frame numbers of the first and last sound above SILENCE
        self.peak = 0.0  # since the last status(take_peak=True)
        self.through = None  # the plugin the sound went through
        self.error = None
        self.active = True
        self.stop = threading.Event()
        self.thread = None


_lock = threading.RLock()
_job: _Job | None = None


def _clock(frames: int, rate: int) -> str:
    s = int(frames / rate) if rate else 0
    return f"{s // 60}:{s % 60:02d}"


def _run(job: _Job) -> None:
    try:
        while not job.stop.is_set():
            # The plugin's ring or the browser's, whichever plays one of MODES now. The
            # other rings keep their cursors: back to one, it continues from there.
            reader, plugin = analysis_tap.pick(MODES)
            if reader is None:
                job.stop.wait(POLL_S)
                continue
            key = id(reader)
            gen, end = job.cursors.get(key, (None, 0))
            gen, rate, mode, _flags, end, _before, after = reader.read(gen, end, READ_FRAMES)
            job.cursors[key] = (gen, end)
            n = len(after)
            if not n:
                job.stop.wait(POLL_S)
                continue
            if mode in MODES:
                if job.rate and rate != job.rate:
                    job.error = f"The sample rate changed: recording stopped at {_clock(job.frames, job.rate)}"
                    return
                if job.frames + n > MAX_FRAMES:
                    job.error = f"The recording is too long: stopped at {_clock(job.frames, job.rate)}"
                    return
                job.rate = rate
                loud = np.flatnonzero(np.abs(after).max(axis=1) > analysis_tap.SILENCE)
                if loud.size:  # where the sound is, for _finish()
                    if job.first is None:
                        job.first = job.frames + int(loud[0])
                    job.last = job.frames + int(loud[-1])
                job.file.write(np.ascontiguousarray(after, dtype="<f4").tobytes())
                with _lock:
                    job.frames += n
                    job.peak = max(job.peak, float(np.abs(after).max()))
                    job.through = plugin if mode == analysis_tap.LIVE else None
            if n < READ_FRAMES:
                job.stop.wait(POLL_S)
    except Exception as e:  # noqa: BLE001 - shown in the page
        job.error = f"The recording stopped: {e}"
    finally:
        try:
            _finish(job)
        except OSError:
            pass
        with _lock:
            job.active = False
            # Not after a later start(): that one set the flag on for its own job.
            if _job is None or _job is job:
                analysis_tap.set_recording(False)


def _finish(job: _Job) -> None:
    """Cuts the silence before the first sound (the worker's live loop writes silence
    until the browser's frames arrive) and after the last one, moves the sound to the
    front of the file and writes the header. Nothing loud: no frames (stop() reports it)."""
    head = host_worker.WAV_FLOAT_HEADER
    f = job.file
    first, count = (job.first, job.last - job.first + 1) if job.first is not None else (0, 0)
    if count:
        f.flush()
        step = 1 << 20  # in chunks: the write never runs ahead of what is still to be read
        for done in range(0, count * 8, step):
            f.seek(head + first * 8 + done)
            chunk = f.read(min(step, count * 8 - done))
            f.seek(head + done)
            f.write(chunk)
    f.seek(0)
    f.write(host_worker.float_wav_header(2, job.rate or 48000, count))
    f.truncate(head + count * 8)
    f.close()
    with _lock:
        job.frames = count


def _status(job: _Job | None, take_peak: bool = False) -> dict:
    if job is None:
        return {"on": False, "seconds": 0.0, "frames": 0, "rate": 0, "peak": 0.0, "through": None, "error": None,
                "pending": False}
    with _lock:
        peak = job.peak
        if take_peak:
            job.peak = 0.0
        return {"on": job.active, "seconds": round(job.frames / job.rate, 3) if job.rate else 0.0,
                "frames": job.frames, "rate": job.rate, "peak": round(peak, 5), "through": job.through,
                "error": job.error, "pending": not job.active and job.frames > 0}


def status(take_peak: bool = False) -> dict:
    """{on, seconds, frames, rate, peak (linear, since the last take_peak), through
    (the plugin's name), error, pending (stopped by itself, waiting to be opened by stop())}."""
    with _lock:
        return _status(_job, take_peak)


def running() -> bool:
    with _lock:
        return _job is not None and _job.active


def start() -> dict:
    """Starts recording. RecordError if it already does."""
    global _job
    with _lock:
        if _job is not None and _job.active:
            raise RecordError("Already recording the browser.")
        _job = None
        CAPTURES.mkdir(parents=True, exist_ok=True)
        for name in os.listdir(CAPTURES):  # only the latest one is kept
            try:
                os.unlink(CAPTURES / name)
            except OSError:
                pass
        path = CAPTURES / f"browser-{time.strftime('%Y%m%d-%H%M%S')}.wav"
        try:
            file = open(path, "w+b")  # read back when the silence is cut (_finish)
            file.write(host_worker.float_wav_header(2, 48000, 0))
        except OSError as e:
            raise RecordError(f"Couldn't start recording ({e.strerror or e}).") from None
        # Sound that is already in the rings isn't part of it: from now on.
        cursors = {}
        for reader, _name in analysis_tap.rings():
            h = reader.header()
            cursors[id(reader)] = (h["gen"], h["written"])
        job = _job = _Job(str(path), file, cursors)
        analysis_tap.set_recording(True)  # the pages are told to send their frames
        job.thread = threading.Thread(target=_run, args=(job,), daemon=True, name="browser-record")
        job.thread.start()
        return _status(job)


def stop() -> dict:
    """Stops (or collects one that stopped by itself) and finalises the file:
    {path, name, status}. RecordError when nothing runs or nothing was recorded."""
    global _job
    with _lock:
        job = _job
    if job is None:
        raise RecordError("Not recording.")
    job.stop.set()
    if job.thread is not None:
        job.thread.join(timeout=5)
    with _lock:
        if _job is job:
            _job = None
        done = _status(job)
    if not job.frames:
        try:
            os.unlink(job.path)
        except OSError:
            pass
        raise RecordError(job.error or NOTHING)
    return {"path": job.path, "name": job.name, "status": {**done, "on": False, "pending": False}}
