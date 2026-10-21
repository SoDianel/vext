"""The toolbar's MIDI indicator (GET /api/midi/activity).

What arrives from the selected MIDI inputs (the midi_inputs setting), with or
without a plugin open:
- no plugin session: this backend opens the selected inputs itself (WinMM via
  app/hosting/midi.py) only to watch them, and never when the setting is empty;
- a plugin session exists: the inputs belong to its worker (classic WinMM
  inputs are exclusive), so the monitor closes them before the worker opens
  them (plugin_host calls release() first), and the worker's "midi" events
  carry what it received (plugin_host forwards them to record()). Once no
  session is left, the monitor opens them again (within a second).

Everything is shown, even what doesn't reach a plugin: other channels under a
channel filter are marked. Pitch bend isn't shown at all while "Bypass pitch
bend" is on (the light doesn't blink for it). The last messages are kept in a
small ring with a sequence number; the page asks for those after the last one
it saw.
"""

import collections
import json
import threading
import time

RING = 64
RETRY_S = 5.0  # a selected input that failed to open (in use elsewhere) is tried again this often

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
CC_NAMES = {0: "Bank select", 1: "Mod wheel", 2: "Breath", 4: "Foot", 5: "Portamento time", 7: "Volume",
            8: "Balance", 10: "Pan", 11: "Expression", 32: "Bank select LSB", 64: "Sustain", 65: "Portamento",
            66: "Sostenuto", 67: "Soft pedal", 71: "Resonance", 74: "Cutoff", 120: "All sound off",
            121: "Reset controllers", 123: "All notes off"}


def note_name(n: int) -> str:
    """60 -> "C4" (middle C as C4, the MIDI/scientific convention)."""
    return f"{NOTE_NAMES[n % 12]}{n // 12 - 1}"


def decode(data: bytes) -> str:
    """A channel message in words, with its numbers."""
    if not data:
        return ""
    status = data[0]
    kind, ch = status & 0xF0, (status & 0x0F) + 1
    d1 = data[1] if len(data) > 1 else 0
    d2 = data[2] if len(data) > 2 else 0
    if kind == 0x90 and d2 == 0:
        kind = 0x80  # note-on at velocity 0 is a note-off
    if kind in (0x80, 0x90):
        # Notes: the name and the number only (no velocity, no channel).
        word = "Note Off" if kind == 0x80 else "Note On"
        return f"{word} · {note_name(d1)} ({d1})"
    if kind == 0xA0:
        text = f"Poly aftertouch · {note_name(d1)} ({d1}) = {d2}"
    elif kind == 0xB0:
        name = CC_NAMES.get(d1)
        text = f"CC {d1}" + (f" ({name})" if name else "") + f" = {d2}"
    elif kind == 0xC0:
        text = f"Program {d1}"
    elif kind == 0xD0:
        text = f"Aftertouch = {d1}"
    elif kind == 0xE0:
        value = (d2 << 7) | d1
        text = f"Pitch bend {value - 8192:+d} ({value})"
    else:
        return data.hex(" ").upper()
    return f"{text} · ch {ch}"


class MidiMonitor:
    def __init__(self, settings, plugin_busy, plugin_inputs):
        """settings: () -> {inputs: "*"|[ids], channel, block_pitchbend};
        plugin_busy: () -> True while a plugin session exists;
        plugin_inputs: () -> the open plugin's {open, failed, available} or None."""
        self._settings = settings
        self._plugin_busy = plugin_busy
        self._plugin_inputs = plugin_inputs
        self._lock = threading.Lock()        # the ring
        self._io_lock = threading.Lock()     # opening / closing the inputs
        self._ring: collections.deque = collections.deque(maxlen=RING)
        self._seq = 0
        self._inputs = None                  # app.hosting.midi.MidiInputs, created on first use
        self._opened_for: str | None = None  # the selection the open inputs were opened for
        self._result = {"open": [], "failed": [], "available": []}
        self._tried_at = 0.0
        self._cfg = {"inputs": [], "channel": 0, "block_pitchbend": False}
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- the ring ----

    def record(self, data: bytes, device: str = "", t: float | None = None, source: str = "monitor") -> None:
        if not data or not 0x80 <= data[0] <= 0xEF:
            return
        data = bytes(data)
        cfg = self._cfg
        if cfg.get("block_pitchbend") and (data[0] & 0xF0) == 0xE0:
            return  # bypassed pitch bend: not recorded, so the light doesn't blink for it
        flags = []
        if cfg.get("channel") and (data[0] & 0x0F) != cfg["channel"] - 1:
            flags.append("filtered")
        with self._lock:
            self._seq += 1
            self._ring.append({"seq": self._seq, "t": t or time.time(), "device": device, "source": source,
                               "bytes": list(data), "text": decode(data), "flags": flags})

    def record_worker(self, recent: list) -> None:
        """The worker's recent messages: [[t, device, [bytes]], ...]."""
        for item in recent or []:
            try:
                t, device, data = item[0], item[1], bytes(item[2])
            except (TypeError, ValueError, IndexError):
                continue
            self.record(data, device, t, source="plugin")

    def _on_message(self, data: bytes, _t: float, device: str) -> None:
        self.record(data, device)

    # ---- the inputs ----

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="midi-monitor", daemon=True)
            self._thread.start()

    def poke(self) -> None:
        """The settings changed, or a plugin session ended: look again now."""
        self._wake.set()

    def _loop(self) -> None:
        while True:
            try:
                self.reconcile()
            except Exception:  # noqa: BLE001 - the watcher must keep running
                pass
            self._wake.wait(1.0)
            self._wake.clear()

    def reconcile(self) -> None:
        cfg = self._settings()
        self._cfg = cfg
        wanted = cfg.get("inputs") or []
        key = json.dumps(wanted)
        with self._io_lock:
            if not wanted or self._plugin_busy():
                self._close_locked()
                return
            retry = bool(self._result["failed"]) and time.monotonic() - self._tried_at > RETRY_S
            if self._opened_for == key and not retry:
                return
            from app.hosting import midi

            if self._inputs is None:
                self._inputs = midi.MidiInputs(self._on_message)
            self._result = self._inputs.open(wanted)
            self._opened_for = key
            self._tried_at = time.monotonic()
        # A session may have started while the inputs were opening: give them back at once.
        if self._plugin_busy():
            self.release()

    def _close_locked(self) -> None:
        if self._inputs is not None and self._opened_for is not None:
            try:
                self._inputs.close()
            except Exception:  # noqa: BLE001
                pass
        self._opened_for = None
        self._result = {"open": [], "failed": [], "available": []}

    def release(self) -> None:
        """Closes the monitor's inputs now (a plugin's worker is about to open them)."""
        with self._io_lock:
            self._close_locked()

    def shutdown(self) -> None:
        with self._io_lock:
            self._close_locked()
            if self._inputs is not None:
                self._inputs.shutdown()
                self._inputs = None

    # ---- the page ----

    def activity(self, since: int = 0) -> dict:
        cfg = self._cfg
        selected = cfg.get("inputs") or []
        plugin = self._plugin_inputs() if self._plugin_busy() else None
        if plugin is not None:
            owner, inputs = "plugin", plugin
        else:
            owner, inputs = ("monitor" if self._opened_for is not None else None), self._result
        with self._lock:
            seq = self._seq
            events = [e for e in self._ring if e["seq"] > since] if since < seq else []
            if since <= 0:
                events = events[-12:]
        return {"seq": seq, "selected": selected, "owner": owner, "open": inputs.get("open") or [],
                "failed": inputs.get("failed") or [], "channel": cfg.get("channel") or 0,
                "block_pitchbend": bool(cfg.get("block_pitchbend")), "events": events}
