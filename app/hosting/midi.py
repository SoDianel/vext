"""MIDI input from hardware: Windows' WinMM MIDI API, driven through ctypes.

Why WinMM through ctypes (and not python-rtmidi / mido, or Web MIDI in the
page): RtMidi's Windows backend *is* WinMM, so a wrapper library would add a
compiled dependency (a wheel per Python version, PyInstaller hooks) for the
same driver calls; Web MIDI would route every note through the page, the
backend and a pipe, and stop working whenever the page isn't showing. Here the
plugin's own worker process (app/host_worker.py) opens the keyboard and feeds
the notes straight to the audio thread. Every USB class-compliant keyboard,
vendor driver and virtual port (loopMIDI, Windows MIDI Services' WinMM
compatibility ports) shows up in WinMM.

Delivery uses CALLBACK_THREAD: WinMM posts MM_MIM_DATA messages to a thread of
ours that waits in GetMessage, so no Python code ever runs inside the driver's
callback context (MSDN restricts what a CALLBACK_FUNCTION may do). Only
channel messages (note, aftertouch, CC, program change, pitch bend) are passed
on; clock, active sensing and SysEx are ignored.

Classic WinMM drivers let one application at a time open an input: a keyboard
in use by a DAW fails with "in use by another application".

Listing devices (list_inputs) only reads their names, it never opens them, so
the backend can call it freely.
"""

import ctypes
import threading
import time
from ctypes import wintypes

_winmm = ctypes.WinDLL("winmm")
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

HMIDIIN = wintypes.HANDLE
CALLBACK_THREAD = 0x00020000
MM_MIM_OPEN, MM_MIM_CLOSE, MM_MIM_DATA, MM_MIM_MOREDATA = 0x3C1, 0x3C2, 0x3C3, 0x3CC
WM_QUIT, WM_USER = 0x0012, 0x0400
PM_NOREMOVE = 0
MMSYSERR_ALLOCATED, MMSYSERR_BADDEVICEID, MMSYSERR_NODRIVER, MMSYSERR_NOMEM = 4, 2, 6, 7


class MIDIINCAPSW(ctypes.Structure):
    _fields_ = [("wMid", wintypes.WORD), ("wPid", wintypes.WORD), ("vDriverVersion", wintypes.UINT),
                ("szPname", ctypes.c_wchar * 32), ("dwSupport", wintypes.DWORD)]


_winmm.midiInGetNumDevs.restype = wintypes.UINT
_winmm.midiInGetDevCapsW.argtypes = [ctypes.c_size_t, ctypes.POINTER(MIDIINCAPSW), wintypes.UINT]
_winmm.midiInGetDevCapsW.restype = wintypes.UINT
_winmm.midiInOpen.argtypes = [ctypes.POINTER(HMIDIIN), wintypes.UINT, ctypes.c_size_t, ctypes.c_size_t,
                              wintypes.DWORD]
_winmm.midiInOpen.restype = wintypes.UINT
_winmm.midiInStart.argtypes = [HMIDIIN]
_winmm.midiInStart.restype = wintypes.UINT
_winmm.midiInStop.argtypes = [HMIDIIN]
_winmm.midiInReset.argtypes = [HMIDIIN]
_winmm.midiInClose.argtypes = [HMIDIIN]
_winmm.midiInClose.restype = wintypes.UINT
_winmm.midiInGetErrorTextW.argtypes = [wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
_user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
_user32.GetMessageW.restype = wintypes.BOOL
_user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT,
                                 wintypes.UINT]
_user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_user32.PostThreadMessageW.restype = wintypes.BOOL
_kernel32.GetCurrentThreadId.restype = wintypes.DWORD


def _caps_name(index: int) -> str | None:
    caps = MIDIINCAPSW()
    if _winmm.midiInGetDevCapsW(index, ctypes.byref(caps), ctypes.sizeof(caps)) != 0:
        return None
    return caps.szPname.strip() or f"MIDI input {index + 1}"


def list_inputs() -> list[dict]:
    """The MIDI inputs connected now: [{index, id, name}]. `id` is the name, or
    "name #2" for a second device of the same name (two identical keyboards):
    that's what the midi_inputs setting stores."""
    out, seen = [], {}
    for index in range(_winmm.midiInGetNumDevs()):
        name = _caps_name(index)
        if name is None:
            continue
        seen[name] = seen.get(name, 0) + 1
        out.append({"index": index, "id": name if seen[name] == 1 else f"{name} #{seen[name]}", "name": name})
    return out


def _error_text(code: int) -> str:
    if code == MMSYSERR_ALLOCATED:
        return "in use by another application (close it there, or use a multi-client MIDI driver)"
    if code in (MMSYSERR_BADDEVICEID, MMSYSERR_NODRIVER):
        return "not connected"
    buf = ctypes.create_unicode_buffer(256)
    if _winmm.midiInGetErrorTextW(code, buf, 256) == 0 and buf.value:
        return buf.value
    return f"WinMM error {code}"


def message_length(status: int) -> int:
    """Bytes in a channel message with this status byte (0 = not a channel message)."""
    kind = status & 0xF0
    if kind in (0x80, 0x90, 0xA0, 0xB0, 0xE0):
        return 3
    if kind in (0xC0, 0xD0):
        return 2
    return 0


class MidiInputs:
    """The MIDI inputs a worker listens to. `on_message(data: bytes, t: float,
    device_id: str)` is called from this object's own thread for every
    channel message (t = time.perf_counter() when it arrived)."""

    def __init__(self, on_message):
        self._on_message = on_message
        self._lock = threading.Lock()
        self._handles: dict[int, str] = {}   # HMIDIIN value -> device id
        self._open_ids: list[str] = []
        self._ready = threading.Event()
        self._thread_id = 0
        self._thread = threading.Thread(target=self._loop, name="midi-in", daemon=True)
        self._thread.start()
        self._ready.wait(5)

    def _loop(self) -> None:
        msg = wintypes.MSG()
        _user32.PeekMessageW(ctypes.byref(msg), None, WM_USER, WM_USER, PM_NOREMOVE)  # creates the queue
        self._thread_id = _kernel32.GetCurrentThreadId()
        self._ready.set()
        while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message not in (MM_MIM_DATA, MM_MIM_MOREDATA):
                continue
            now = time.perf_counter()
            packed = int(msg.lParam) & 0xFFFFFFFF
            status = packed & 0xFF
            size = message_length(status)
            if not size:
                continue
            data = bytes(((packed >> (8 * i)) & 0xFF) for i in range(size))
            with self._lock:
                device = self._handles.get(int(msg.wParam or 0), "")
            try:
                self._on_message(data, now, device)
            except Exception:  # noqa: BLE001 - one bad message must not stop the input
                pass

    @property
    def open_ids(self) -> list[str]:
        with self._lock:
            return list(self._open_ids)

    def open(self, wanted) -> dict:
        """Opens the inputs `wanted` names ("*" = every connected input, a list
        of ids, or empty = none), closing the others. Returns {"open": [ids],
        "failed": [{"id", "error"}], "available": [ids]}."""
        self.close()
        available = list_inputs()
        if wanted == "*":
            targets = available
            missing = []
        else:
            ids = [w for w in (wanted or []) if isinstance(w, str)]
            targets = [d for d in available if d["id"] in ids]
            missing = [w for w in ids if w not in {d["id"] for d in available}]
        opened, failed = [], [{"id": m, "error": "not connected"} for m in missing]
        for d in targets:
            handle = HMIDIIN()
            code = _winmm.midiInOpen(ctypes.byref(handle), d["index"], self._thread_id, 0, CALLBACK_THREAD)
            if code != 0:
                failed.append({"id": d["id"], "error": _error_text(code)})
                continue
            with self._lock:
                self._handles[int(handle.value or 0)] = d["id"]
            code = _winmm.midiInStart(handle)
            if code != 0:
                failed.append({"id": d["id"], "error": _error_text(code)})
                self._close_handle(int(handle.value or 0))
                continue
            opened.append(d["id"])
        with self._lock:
            self._open_ids = opened
        return {"open": opened, "failed": failed, "available": [d["id"] for d in available]}

    def _close_handle(self, value: int) -> None:
        handle = HMIDIIN(value)
        try:
            _winmm.midiInStop(handle)
            _winmm.midiInReset(handle)
            _winmm.midiInClose(handle)
        finally:
            with self._lock:
                self._handles.pop(value, None)

    def close(self) -> None:
        """Closes every open input (the thread stays, ready for open())."""
        with self._lock:
            handles = list(self._handles)
            self._open_ids = []
        for value in handles:
            try:
                self._close_handle(value)
            except Exception:  # noqa: BLE001
                pass

    def shutdown(self) -> None:
        self.close()
        if self._thread_id:
            _user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
