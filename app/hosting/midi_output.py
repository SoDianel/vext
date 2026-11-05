"""One-shot WinMM command output; never opens a MIDI input or sends musical notes."""

import ctypes
import time
from ctypes import wintypes


class Caps(ctypes.Structure):
    _fields_ = [("mid", wintypes.WORD), ("pid", wintypes.WORD), ("version", wintypes.UINT),
                ("name", ctypes.c_wchar * 32), ("technology", wintypes.WORD), ("voices", wintypes.WORD),
                ("notes", wintypes.WORD), ("channels", wintypes.WORD), ("support", wintypes.DWORD)]


class Header(ctypes.Structure):
    _fields_ = [("data", ctypes.c_void_p), ("length", wintypes.DWORD), ("recorded", wintypes.DWORD),
                ("user", ctypes.c_size_t), ("flags", wintypes.DWORD), ("next", ctypes.c_void_p),
                ("reserved", ctypes.c_size_t), ("offset", wintypes.DWORD), ("reserved2", ctypes.c_size_t * 8)]


def _api():
    api = ctypes.WinDLL("winmm")
    api.midiOutGetDevCapsW.argtypes = [ctypes.c_size_t, ctypes.POINTER(Caps), wintypes.UINT]
    api.midiOutOpen.argtypes = [ctypes.POINTER(wintypes.HANDLE), wintypes.UINT, ctypes.c_size_t, ctypes.c_size_t, wintypes.DWORD]
    for name in ("midiOutPrepareHeader", "midiOutLongMsg", "midiOutUnprepareHeader"):
        getattr(api, name).argtypes = [wintypes.HANDLE, ctypes.POINTER(Header), wintypes.UINT]
    api.midiOutReset.argtypes = [wintypes.HANDLE]
    api.midiOutClose.argtypes = [wintypes.HANDLE]
    return api


def _outputs(*, strict: bool = False) -> list[tuple[int, str]]:
    api = _api()
    names = []
    for index in range(api.midiOutGetNumDevs()):
        caps = Caps()
        if api.midiOutGetDevCapsW(index, ctypes.byref(caps), ctypes.sizeof(caps)) == 0:
            names.append((index, caps.name))
        elif strict:
            raise OSError(f"Could not inspect MIDI output {index}.")
    return names


def list_outputs(*, strict: bool = False) -> list[str]:
    return [name for _, name in _outputs(strict=strict)]


def send_sysex(name: str, message: bytes) -> None:
    indices = [index for index, candidate in _outputs() if candidate == name]
    if len(indices) != 1:
        raise ValueError(f"Cannot send to MIDI output {name!r}: the name is missing or ambiguous. Refresh devices and select a unique output.")
    api, handle = _api(), wintypes.HANDLE()
    if api.midiOutOpen(ctypes.byref(handle), indices[0], 0, 0, 0):
        raise OSError(f"Could not open MIDI output '{name}'. It may be busy or disconnected.")
    data = ctypes.create_string_buffer(message)
    header = Header(data=ctypes.cast(data, ctypes.c_void_p), length=len(message))
    prepared = False
    try:
        if api.midiOutPrepareHeader(handle, ctypes.byref(header), ctypes.sizeof(header)):
            raise OSError(f"Could not prepare a command for MIDI output '{name}'.")
        prepared = True
        if api.midiOutLongMsg(handle, ctypes.byref(header), ctypes.sizeof(header)):
            raise OSError(f"Could not send a command to MIDI output '{name}'.")
        deadline = time.monotonic() + 2
        while not header.flags & 1:
            if time.monotonic() > deadline:
                raise OSError(f"MIDI output '{name}' timed out sending the command.")
            time.sleep(0.005)
    finally:
        api.midiOutReset(handle)
        if prepared:
            api.midiOutUnprepareHeader(handle, ctypes.byref(header), ctypes.sizeof(header))
        api.midiOutClose(handle)
