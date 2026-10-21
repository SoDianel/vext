"""The analysis tap: every sound VEXT plays, as frames the Analyzer section and
the browser recording can read (AGENTS.md "Analyzer").

One writer per audio producer, one reader in the backend. The plugin's worker
(another process) writes a file-backed ring (`TapWriter`, an mmap), the backend
reads it (`TapReader`); the browser's sound without a plugin goes through an
in-process ring (`InProcessTap`, same layout in memory). The ring holds two
signals, "before" (what goes into the plugin) and "after" (what comes out):

  header (64 bytes, little-endian):
    0  4s  magic b"VXTP"
    4  I   version 1
    8  I   rate            Hz of the frames
   12  I   mode            0 idle, 1 file, 2 live (the browser through the plugin),
                          3 instrument, 4 browser (no plugin), 5 dry (the Audio tab's
                          recording played back)
   16  I   flags           bit0 has_before, bit1 bypass
   20  I   capacity        frames (a power of two), default 1 << 18 (~5 s at 48 kHz)
   24  I   gen             bumped on every (re)start, rate or mode change
   28  I   reserved
   32  Q   written         total frames written in this gen (stored LAST, after the data)
   40  d   wall            time.time() of the last write
   48..63  reserved
  data: before[capacity][2] float32, then after[capacity][2] float32 (interleaved L R)

Writers take (2, n) float32 blocks; readers give (n, 2) float32 (interleaved, so
.tobytes() is what goes on the wire). Silence skip: a writer stops writing once
both signals stayed below SILENCE for SILENCE_S, and resumes on the first loud
block, so a consumer sees the stream stop (wall goes stale) when nothing sounds.

Threads: a writer's methods and a reader's read() are guarded by a lock each (the
browser's frames arrive on the event loop, the recording thread reads on its own).
The worker's writer is only ever touched by its playback thread and its command
thread. This module must stay light: the worker imports it (numpy only).
"""

import mmap
import os
import struct
import threading
import time

import numpy as np

MAGIC = b"VXTP"
VERSION = 1
HEADER = 64
# The header's fields are read and written through numpy views, not struct: struct moves a
# 64-bit value byte by byte, and a reader in another process could catch `written` half
# changed (it did, as 0). A numpy item is one aligned 4 or 8 byte move.
_MAGIC = struct.unpack("<I", MAGIC)[0]
_VERSION_I, _RATE_I, _MODE_I, _FLAGS_I, _CAPACITY_I, _GEN_I = 1, 2, 3, 4, 5, 6  # uint32 slots
_WRITTEN_I, _WALL_I = 4, 5  # 64-bit slots (offsets 32 and 40)
CAPACITY = 1 << 18
IDLE, FILE, LIVE, INSTRUMENT, BROWSER, DRY = range(6)
SOURCES = {FILE: "file", LIVE: "browser", INSTRUMENT: "instrument", BROWSER: "browser", DRY: "dry"}
SILENCE = 1e-6  # peak below this = silence
SILENCE_S = 0.5  # silence that is still written before the writer stops
FRESH_S = 0.3  # a ring written to less than this long ago is playing


class _Ring:
    """A header buffer (an mmap or a bytearray) plus the two data arrays; the
    writer and reader halves live here so every kind of ring behaves the same."""

    def __init__(self, buf, before, after, capacity: int):
        self._bind(buf)
        self._before, self._after = before, after
        self.capacity = capacity
        self.lock = threading.Lock()
        # writer state
        self._rate = self._mode = self._flags = self._gen = 0
        self._written = 0
        self._quiet = 0

    def _bind(self, buf) -> None:
        """The header lives in `buf` (an mmap or a bytearray), seen as 32 and 64-bit slots."""
        self._buf = buf
        self._u32 = np.frombuffer(buf, dtype=np.uint32, count=HEADER // 4)
        self._u64 = np.frombuffer(buf, dtype=np.uint64, count=HEADER // 8)
        self._f64 = np.frombuffer(buf, dtype=np.float64, count=HEADER // 8)

    def _init_header(self, capacity: int) -> None:
        self._u32[0] = _MAGIC
        self._u32[_VERSION_I] = VERSION
        self._u32[_CAPACITY_I] = capacity

    def _unbind(self) -> None:
        """Lets go of the buffer's views (before its mmap is closed); a stray call lands in a spare one."""
        self._bind(bytearray(HEADER))

    # ---- writer ----

    def start(self, rate: int, mode: int, has_before: bool = True) -> None:
        """(Re)starts the ring for a new stream: bumps gen, empties it."""
        with self.lock:
            self._rate, self._mode = int(rate), int(mode)
            self._flags = 1 if has_before else 0
            self._gen += 1
            self._written = 0
            self._quiet = 0
            u32 = self._u32
            # written = 0 first: a reader that catches a half-done restart sees nothing new,
            # never the old frames under the new gen.
            self._u64[_WRITTEN_I] = 0
            u32[_RATE_I], u32[_MODE_I], u32[_FLAGS_I] = self._rate, self._mode, self._flags
            u32[_CAPACITY_I], u32[_GEN_I] = self.capacity, self._gen
            self._f64[_WALL_I] = time.time()

    def ensure(self, rate: int, mode: int, has_before: bool = True) -> None:
        """start() unless it already runs with exactly this."""
        if self._mode != mode or self._rate != int(rate) or bool(self._flags & 1) != bool(has_before):
            self.start(rate, mode, has_before)

    def idle(self) -> None:
        """Nothing plays: mode idle, wall stale."""
        with self.lock:
            if not self._mode:
                return
            self._mode = IDLE
            self._u32[_MODE_I] = IDLE
            self._f64[_WALL_I] = 0.0

    def write(self, before, after, bypass: bool = False) -> None:
        """One block: `before` / `after` are (2, n) float32 (`before` None: no
        input, an instrument). Cheap: one slice assignment per signal (two when
        the block wraps), then the header with `written` last."""
        with self.lock:
            if not self._mode:
                return
            n = after.shape[1]
            if not n:
                return
            quiet = float(np.abs(after).max()) < SILENCE and (
                before is None or float(np.abs(before).max()) < SILENCE)
            if quiet:
                self._quiet += n
                if self._quiet > SILENCE_S * self._rate:
                    return  # nothing sounds: the stream stops
            else:
                self._quiet = 0
            cap = self.capacity
            if n > cap:  # a block longer than the ring: its newest part
                after = after[:, n - cap:]
                before = None if before is None else before[:, n - cap:]
                n = cap
            pos = self._written % cap
            k = min(n, cap - pos)
            self._after[pos:pos + k] = after[:, :k].T
            if before is not None and self._flags & 1:
                self._before[pos:pos + k] = before[:, :k].T
            if k < n:
                self._after[:n - k] = after[:, k:].T
                if before is not None and self._flags & 1:
                    self._before[:n - k] = before[:, k:].T
            flags = (self._flags & 1) | (2 if bypass else 0)
            if flags != self._flags:
                self._flags = flags
                self._u32[_FLAGS_I] = flags
            self._written += n
            self._f64[_WALL_I] = time.time()
            self._u64[_WRITTEN_I] = self._written  # last: the data is in place

    # ---- reader ----

    def header(self) -> dict:
        """{rate, mode, flags, capacity, gen, written, wall} right now."""
        with self.lock:
            return self._header()

    def _header(self) -> dict:
        u32 = self._u32
        written = int(self._u64[_WRITTEN_I])  # first: the frames it counts were written before it
        return {"rate": int(u32[_RATE_I]), "mode": int(u32[_MODE_I]), "flags": int(u32[_FLAGS_I]),
                "capacity": int(u32[_CAPACITY_I]), "gen": int(u32[_GEN_I]), "written": written,
                "wall": float(self._f64[_WALL_I]), "ok": int(u32[0]) == _MAGIC}

    def read(self, since_gen, since_frame: int, max_frames: int, newest: bool = False):
        """(gen, rate, mode, flags, end, before, after): the frames from `since_frame`
        (of gen `since_gen`; another gen starts at its frame 0), at most `max_frames`,
        `end` = the frame after the last one returned (the next since_frame).
        A writer that lapped the reader costs it the oldest frames. `newest`: when
        there are more than `max_frames`, the newest ones (a client that fell behind),
        else the oldest (a sequential consumer). before / after: (n, 2) float32;
        before is zeros without an input."""
        with self.lock:
            h = self._header()
            gen, written, cap = h["gen"], h["written"], self.capacity
            empty = np.zeros((0, 2), dtype=np.float32)
            if not h["ok"] or not h["mode"] and not written:
                return gen, h["rate"], h["mode"], h["flags"], 0, empty, empty
            lo = int(since_frame) if gen == since_gen else 0
            start = max(lo, written - cap)
            if newest:
                start = max(start, written - max_frames)
            count = max(0, min(max_frames, written - start))
            if not count:
                return gen, h["rate"], h["mode"], h["flags"], min(lo, written), empty, empty
            after = self._copy(self._after, start, count)
            before = self._copy(self._before, start, count) if h["flags"] & 1 else np.zeros_like(after)
            # The writer may have run on while copying: what it has overwritten since is gone.
            h2 = self._header()
            if h2["gen"] != gen:
                return h2["gen"], h2["rate"], h2["mode"], h2["flags"], 0, empty, empty
            lost = max(0, h2["written"] - cap - start)
            if lost:
                if lost >= count:
                    return gen, h["rate"], h["mode"], h["flags"], start + count, empty, empty
                after, before = after[lost:], before[lost:]
                start += lost
                count -= lost
            return gen, h["rate"], h["mode"], h["flags"], start + count, before, after

    def _copy(self, data, start: int, count: int):
        cap = self.capacity
        pos = start % cap
        k = min(count, cap - pos)
        if k == count:
            return np.array(data[pos:pos + count], dtype=np.float32)
        return np.concatenate((data[pos:], data[:count - k])).astype(np.float32, copy=False)


class InProcessTap(_Ring):
    """The same ring in memory (the browser's sound without a plugin: the backend
    itself is the producer)."""

    def __init__(self, capacity: int = CAPACITY):
        super().__init__(bytearray(HEADER), np.zeros((capacity, 2), dtype=np.float32),
                         np.zeros((capacity, 2), dtype=np.float32), capacity)
        self._init_header(capacity)


class TapWriter(_Ring):
    """The worker's side: creates (or reuses) the ring file at `path` and maps it."""

    def __init__(self, path: str, capacity: int = CAPACITY):
        size = HEADER + capacity * 16
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w+b") as f:  # zero-filled by the file system
            f.truncate(size)
            mm = mmap.mmap(f.fileno(), size)  # the mapping keeps its own handle
        self.path = path
        self._mm = mm
        super().__init__(mm, np.frombuffer(mm, dtype=np.float32, count=capacity * 2, offset=HEADER).reshape(
            capacity, 2), np.frombuffer(mm, dtype=np.float32, count=capacity * 2,
                                        offset=HEADER + capacity * 8).reshape(capacity, 2), capacity)
        self._init_header(capacity)

    def close(self) -> None:
        """Lets go of the file (Windows can't delete a mapped file). Idempotent."""
        with self.lock:
            self._mode = IDLE
            self._before = self._after = None
            mm, self._mm = self._mm, None
            self._unbind()  # a stray write after close lands in a spare header
            if mm is not None:
                try:
                    mm.close()
                except (BufferError, ValueError):
                    pass


class TapReader(_Ring):
    """The backend's side of a worker's ring file. None of its arrays escape:
    read() copies, so close() can always let go of the mapping."""

    def __init__(self, path: str):
        with open(path, "rb") as f:
            mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            magic, _v, _rate, _mode, _flags, cap = struct.unpack_from("<4sIIIII", mm, 0)
            if magic != MAGIC or cap < 1024 or len(mm) < HEADER + cap * 16:
                raise ValueError("the ring isn't ready")
            before = np.frombuffer(mm, dtype=np.float32, count=cap * 2, offset=HEADER).reshape(cap, 2)
            after = np.frombuffer(mm, dtype=np.float32, count=cap * 2, offset=HEADER + cap * 8).reshape(cap, 2)
        except Exception:
            mm.close()
            raise
        self.path = path
        self._mm = mm
        super().__init__(mm, before, after, cap)

    def start(self, *a, **k):  # pragma: no cover - read only
        raise RuntimeError("a TapReader only reads")

    write = ensure = idle = start

    def close(self) -> None:
        with self.lock:
            self._before = self._after = None
            mm, self._mm = self._mm, None
            self._unbind()  # reads after close see an idle, empty ring
            if mm is not None:
                try:
                    mm.close()
                except (BufferError, ValueError):
                    pass


# ---------- what is wanted ----------

_want_lock = threading.Lock()
_clients = 0  # analyzer streams connected
_recording = False  # a browser recording runs
_listeners: list = []


def add_listener(fn) -> None:
    """`fn()` is called (from any thread) when tap_wanted() may have changed."""
    _listeners.append(fn)


def _wanted_changed() -> None:
    for fn in list(_listeners):
        try:
            fn()
        except Exception:  # noqa: BLE001 - a listener must never break the tap
            pass


def client_added() -> None:
    global _clients
    with _want_lock:
        _clients += 1
    _wanted_changed()


def client_removed() -> None:
    global _clients
    with _want_lock:
        _clients = max(0, _clients - 1)
    _wanted_changed()


def set_recording(on: bool) -> None:
    global _recording
    with _want_lock:
        _recording = bool(on)
    _wanted_changed()


def wanted() -> bool:
    """Somebody reads the tap: an analyzer stream is connected or a browser recording runs."""
    return _clients > 0 or _recording


# ---------- choosing what to read ----------

browser = InProcessTap()  # the browser's sound when no plugin takes it (app/browser_audio.py)
_readers: dict = {}
_readers_lock = threading.Lock()
_last = {"rate": 0}


def _session_reader():
    """(reader, plugin name) of the open plugin's worker ring, else (None, None)."""
    from app import plugin_host  # late: plugin_host imports this module

    info = plugin_host.active_tap()
    if not info:
        return None, None
    path, name = info
    with _readers_lock:
        reader = _readers.get(path)
        if reader is None:
            try:
                reader = TapReader(path)
            except (OSError, ValueError):
                return None, None  # not created yet (or not initialised): next time
            _readers[path] = reader
    return reader, name


def release(path: str) -> None:
    """Lets go of a worker's ring file (before it's deleted)."""
    with _readers_lock:
        reader = _readers.pop(path, None)
    if reader is not None:
        reader.close()


def remove(path: str, tries: int = 40) -> None:
    """Deletes a ring file once nothing maps it any more (the worker closes its
    mapping when it quits; a killed one is cleaned up by Windows)."""
    release(path)
    for _ in range(tries):
        try:
            os.unlink(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(0.1)


def sweep(folder: str) -> None:
    """Deletes ring files left by workers that are gone (a crash, a kill)."""
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for name in names:
        if name.startswith("tap-") and name.endswith(".bin"):
            try:
                os.unlink(os.path.join(folder, name))
            except OSError:
                pass  # still mapped by a live worker


def _fresh(reader, modes=None) -> bool:
    """Written to less than FRESH_S ago and playing (one of `modes`, when given)."""
    h = reader.header()
    if not (h["ok"] and h["mode"] and time.time() - h["wall"] < FRESH_S):
        return False
    return modes is None or h["mode"] in modes


def pick(modes=None):
    """(reader, plugin name | None): the first ring of rings() that is fresh and,
    with `modes`, plays one of them: the open plugin's, then the browser's. Else
    (None, None): nothing plays. `plugin` is only set for the plugin's ring."""
    for reader, name in rings():
        if _fresh(reader, modes):
            return reader, name
    return None, None


def select():
    """The reader to use now (see pick()), or None."""
    return pick()[0]


def rings() -> list:
    """Every ring that exists now, [(reader, plugin name | None)]: the open plugin's,
    then the browser's (the recording takes a snapshot of them when it starts)."""
    reader, name = _session_reader()
    return ([(reader, name)] if reader is not None else []) + [(browser, None)]


def status(picked=None) -> dict:
    """{mode, rate, has_before, bypass, plugin, source}: what plays now (idle:
    mode 0, source None). `picked`: a pick() result already at hand."""
    reader, name = picked if picked is not None else pick()
    if reader is None:
        return {"mode": 0, "rate": _last["rate"], "has_before": False, "bypass": False, "plugin": None,
                "source": None}
    h = reader.header()
    _last["rate"] = h["rate"]
    return {"mode": h["mode"], "rate": h["rate"], "has_before": bool(h["flags"] & 1), "bypass": bool(h["flags"] & 2),
            "plugin": name, "source": SOURCES.get(h["mode"])}


# The Analyzer's stream message: b"VXAN", uint32 rate, mode, flags, gen, uint64 end_frame (the
# frame after the last one sent), uint32 n, then float32 before[n][2], then float32 after[n][2].
_STREAM = struct.Struct("<4sIIIIQI")


def pack_frame(gen, rate, mode, flags, end, before, after) -> bytes:
    """One read() result as the stream's binary message."""
    n = len(after)
    return (_STREAM.pack(b"VXAN", rate, mode, flags, gen, end, n)
            + np.ascontiguousarray(before, dtype="<f4").tobytes() + np.ascontiguousarray(after, dtype="<f4").tobytes())
