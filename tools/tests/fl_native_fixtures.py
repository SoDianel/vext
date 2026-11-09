# SPDX-License-Identifier: GPL-3.0-or-later
"""Fixtures for native plugins that only load samples through their own editor."""
import ctypes as C
from ctypes import wintypes as W
import os
from pathlib import Path
import sys
import threading
import time
import wave

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.hosting.native_dialogs import owned_dialogs  # noqa: E402

user32 = C.WinDLL("user32", use_last_error=True)
ENUM_CHILD = C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
user32.ChildWindowFromPointEx.argtypes = [W.HWND, W.POINT, W.UINT]
user32.ChildWindowFromPointEx.restype = W.HWND
user32.MapWindowPoints.argtypes = [W.HWND, W.HWND, C.POINTER(W.POINT), W.UINT]
user32.SendMessageW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
user32.SendMessageW.restype = C.c_ssize_t
user32.PostMessageW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
user32.EnumChildWindows.argtypes = [W.HWND, ENUM_CHILD, W.LPARAM]
user32.GetClassNameW.argtypes = [W.HWND, W.LPWSTR, C.c_int]
user32.GetDlgCtrlID.argtypes = [W.HWND]
user32.SendMessageTimeoutW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM, W.UINT, W.UINT,
                                       C.POINTER(C.c_size_t)]


def write_rhythm_wav(path, seconds=8, rate=44100, hz=220):
    """Stereo PCM16 tone with two onsets a second, so slicers find regions."""
    t = np.arange(rate * seconds) / rate
    samples = .3 * np.sin(2 * np.pi * hz * t) * (.4 + .6 * np.exp(-((t * 2) % 1) * 8))
    pcm = np.asarray(np.column_stack((samples, samples)) * 32767, dtype="<i2")
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(2)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(pcm.tobytes())


def _deepest_child(hwnd, x, y):
    point = W.POINT(x, y)
    while True:
        child = user32.ChildWindowFromPointEx(hwnd, point, 0)
        if not child or child == hwnd:
            return hwnd, point
        user32.MapWindowPoints(hwnd, child, C.byref(point), 1)
        hwnd = child


def click(hwnd, x, y):
    """Clicks a control at editor-client coordinates without system mouse input."""
    target, point = _deepest_child(hwnd, x, y)
    packed = (point.x & 65535) | ((point.y & 65535) << 16)
    user32.SendMessageW(target, 0x200, 0, packed)
    user32.SendMessageW(target, 0x201, 1, packed)
    user32.SendMessageW(target, 0x202, 0, packed)


def _answer_dialog(pid, path, timeout, errors):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        dialogs = [d for d in owned_dialogs(pid) if d["class"] == "#32770"]
        if dialogs:
            fields, buttons = [], []

            @ENUM_CHILD
            def visit(child, _):
                name = C.create_unicode_buffer(256)
                user32.GetClassNameW(child, name, 256)
                identifier = user32.GetDlgCtrlID(child)
                if name.value == "Edit" and identifier == 1148:
                    fields.append(child)
                if name.value == "Button" and identifier == 1:
                    buttons.append(child)
                return True

            user32.EnumChildWindows(dialogs[0]["hwnd"], visit, 0)
            if len(fields) == len(buttons) == 1:
                text = C.create_unicode_buffer(str(path))
                result = C.c_size_t()
                user32.SendMessageTimeoutW(fields[0], 12, 0, C.addressof(text), 2, 1000, C.byref(result))
                user32.PostMessageW(buttons[0], 0xF5, 0, 0)
                return
        time.sleep(.05)
    errors.append(TimeoutError("native file dialog did not appear"))


def _dismiss_messages(pid, stop):
    """Answers the plugin's own error box after a rejected file."""
    while not stop.is_set():
        for dialog in owned_dialogs(pid):
            if dialog["class"] == "#32770" and not any("Nombre" in t or "Name" in t for t in dialog["text"]):
                user32.PostMessageW(dialog["hwnd"], 0x10, 0, 0)
        time.sleep(.05)


def reject_file(hwnd, x, y, directory):
    """Picks a malformed WAV: the editor and its worker must simply carry on."""
    junk = Path(directory) / "junk.wav"
    junk.write_bytes(b"RIFF\x10\0\0\0WAVEjunk" + bytes(64))
    stop = threading.Event()
    watcher = threading.Thread(target=_dismiss_messages, args=(os.getpid(), stop), daemon=True)
    watcher.start()
    try:
        pick_file(hwnd, x, y, junk)
    finally:
        stop.set()
        watcher.join(2)


def pick_file(hwnd, x, y, path, timeout=10):
    """Presses a plugin's load button and answers its native Open dialog with path.
    The click blocks on the editor's thread until that modal dialog closes."""
    errors = []
    helper = threading.Thread(target=_answer_dialog, args=(os.getpid(), path, timeout, errors), daemon=True)
    helper.start()
    click(hwnd, x, y)
    helper.join(timeout)
    assert not errors, errors
