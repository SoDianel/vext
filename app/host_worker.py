"""Out-of-process plugin host: loads ONE plugin, shows its real editor window,
plays audio through it (a local file, or live input: the in-app browser's
audio, streamed in by the backend) and can screenshot the editor.

  python -m app.host_worker <plugin path> [--format VST3|VST2|CLAP] [--name N]
         [--hint <catalog name>] [--waves <bundle name>] [--cache <folder>] [--probe]
         [--hidden] [--capture <png>]
  python -m app.host_worker --standby

Formats: VST3 through pedalboard (a bundle folder is opened through its inner
binary: pedalboard 0.9.26 can't open bundle folders on Windows), VST2 and CLAP
through VEXT's own ctypes hosts (app/hosting/vst2.py, clap.py; their editor is
embedded in a VEXT window). A Waves bundle is opened through its WaveShell
(--waves picks the shell plugin, app/hosting/waves.py). --probe only loads the
plugin, processes a little silence and exits (plugin_host's background check).

--hidden: the editor's windows are kept off the screen (DWM-cloaked, see _Hold)
until the desktop shell has put them in its frame and posts REVEAL_MESSAGE to
the editor (or the "reveal" command comes, or REVEAL_FALLBACK_S pass). No
window ever flashes at the top-left before the frame adopts it.

--capture <png>: the picture job (plugin_host.pictures_*): opens the editor
cloaked (never shown), waits until it has rendered, saves it as <png> and
exits. No audio, no MIDI. Events: captured_auto {path, width, height, tries,
method} or error {message, code: blank | dialog | timeout | ...}.

--standby: a warm worker. Starts Python and imports the hosting modules, says
"ready", then waits for {"cmd":"load","path":...,"args":[...]} on stdin and
goes on as if started with those (plugin_host keeps one ready: opening a
plugin then skips process start-up and imports).

Runs as its own process (spawned by app/plugin_host.py) so a misbehaving plugin
can only take this process down, never the VEXT backend. Talks over pipes:
JSON lines in on stdin (commands), JSON lines out on stdout (events). Native
code writing to stdout can't corrupt them (see _take_stdout).

  commands: {"cmd":"play","source":"<audio file>","start_s":0, <AUDIO>,
             "envelope":[<EDIT>...], "loop":[start_s, end_s]|null, "dry":false,
             "until":[start_s, end_s]|null,  (until: a selection played once, it ends at end_s)
             "mix":{"path":"<recording WAV>","source":1.0,"recording":1.0}|null}
            (mix: the Audio tab's recording plays along with the file, into the plugin)
            {"cmd":"mix","source":1.0,"recording":0.0}  (its gains while it plays: the M buttons)
            (dry: no plugin, no gains: the Audio tab's recording played back as it is)
            {"cmd":"edit","envelope":[<EDIT>...]}   {"cmd":"loop","range":[start_s, end_s]|null}
            {"cmd":"record","on":true,"path":"<float WAV>","duration_s":187.2}   {"cmd":"record","on":false}
            {"cmd":"pause"}  {"cmd":"resume"}  {"cmd":"seek","seconds":12.5}
            {"cmd":"stop"}   {"cmd":"bypass","on":true}
            {"cmd":"live","on":true,"rate":48000, <AUDIO>}     {"cmd":"live","on":false}
            {"cmd":"instrument","on":true, <AUDIO>}  (render an instrument from MIDI; on:false stops)
            {"cmd":"reconfigure", <AUDIO>}   (whatever plays restarts with these settings)
            {"cmd":"midi","events":[[144,60,100],...]}   (the on-screen keyboard; no ack needed)
            {"cmd":"midi_inputs","inputs":"*"|["<id>",...],"channel":0..16,"block_pitchbend":bool}   {"cmd":"panic"}
            {"cmd":"tone"}   (Configuration's Test, mixed into what plays)
            {"cmd":"tap","path":"<ring file>"}   (the analysis ring every block is written to: app/analysis_tap.py)
            {"cmd":"capture","path":"<png>"}   {"cmd":"reveal"}   {"cmd":"quit"}
            {"cmd":"gain","in_db":0,"out_db":0,"auto_balance":false}   (any of them; see _Gains)
            <AUDIO> = "driver":"wasapi|wasapi_exclusive|directsound|mme|asio","device":"<id>",
                      "samplerate":48000,"blocksize":512,"safe":false,"buffer":"auto|25|50|100|200|400|800"
                      (see _open_stream; buffer: the Plugin buffer in ms, see _RenderQueue)
            <EDIT> = {"type":"fade_in|fade_out|gain","start_s":0,"end_s":1.5,"gain":0.8}   (envelope_gain)
  events:   status {message, dialog, seconds}   (while loading: progress, or a dialog to answer)
            loaded {is_instrument, accepts_midi, name, live_port, live_key, format, plugin_name, load_s,
                    no_output (an analyzer: fed the audio, the input is heard)}
            error {message, code}  (before loaded: why it can't open; code: plugin_host.ISSUE_CODES)
            probed {ok, format, plugin_name, load_s, is_instrument, has_editor}   (--probe only)
            rendered {ok, peak}   (--probe, instruments: it survived playing a note)
            editor {hwnd, width, height, hidden}   (once the editor window exists, again on
            resize; hidden: still cloaked, waiting for the frame)   revealed {how}
            ready {pid}   (--standby: imports done, waiting for "load")
            playing {dry, loop} / position {xruns, peak_out, rec_dirty, latency_ms, ahead_ms, target_ms,
                    starves, load_pct, render_peak_ms} / paused / resumed / stopped
            (file playback; rec_dirty: [first, end) frames of the recording written since the last one;
             ahead_ms / target_ms: what the Plugin buffer holds / aims at; starves: times it ran dry;
             load_pct: the plugin's render time as a share of real time; render_peak_ms: its slowest block)
            record {on, path, rate, frames, created, dirty}   (on: armed; rate/frames once its file is open)
            live_started {rate, out_rate, block, note} / live {stats, xruns, ahead_ms, target_ms, starves,
                    load_pct, render_peak_ms} / live_stopped
            instrument_started {note} / instrument {peak_out, xruns, held, latency_ms} / instrument_stopped
            midi {count, last, held, recent: [[t, device, [bytes]]]}   (MIDI activity, at most every 0.1 s)
            midi_inputs {open, failed: [{id, error}], available}   tone {audio}
            bypass / captured / error / ack / closed
            gain {in_db, out_db, auto_balance, balance_db, reduction_db}   (the periodic events
            position / live / instrument also carry balance_db and reduction_db)
            (every *_started / playing event carries "audio": the stream that opened
            {driver, device, device_name, hostapi, rate, block, safe, buffer_ms,
            latency_ms, buffer, target_ms}, and a "note" when the device, rate or mode had to fall back)

File playback, live input and an instrument's sound share the output and
exclude each other: starting one stops the other. MIDI (hardware
inputs opened here, and the "midi" command) reaches instruments, and VST2/CLAP
effects that take it, in whatever plays (_MidiRouter).

Live input: the worker listens on 127.0.0.1:<live_port>. A client sends
"<live_key>\\n", then raw interleaved float32 stereo at the live rate. The most
recent connection wins. A short buffer (see _LiveBuffer) absorbs the jitter, and
the Plugin buffer (_RenderQueue) runs the plugin ahead of the device with it.

File and live playback run two threads: a render thread (source or browser input
through the plugin, into the Plugin buffer, ahead of the device) and the output
thread (what the device gets: the analysis tap, test tone, peak, the heard position,
the periodic events). The plugin is called by one thread at a time (Player._plugin_lock).
Instruments are unchanged: one thread, no buffer (MIDI played live needs the lowest latency).

A command may carry an "id"; the worker answers with {"event":"ack","id":...,
"error":null|"<why>"} once the command has been applied (or refused), so the
backend can return the new state, or the reason. Commands without an id report
a failure as an "error" event instead.

The audio-device helpers below are also imported by the backend (to list
devices and validate the setting), so importing this module must have no side
effects: stdout is only taken over in main().
"""

import collections
import ctypes
import json
import os
import secrets
import socket
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

_out_lock = threading.Lock()
_stdout = sys.stdout


def emit(event: str, **fields) -> None:
    with _out_lock:
        _stdout.write(json.dumps({"event": event, **fields}) + "\n")
        _stdout.flush()


# ---------- editor window ----------

# Private library objects, so the argtypes set here never leak into other code
# of the backend (which imports this module for the device helpers).
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

_ENUM_PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
_user32.EnumWindows.argtypes = [_ENUM_PROC, wintypes.LPARAM]
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.IsWindow.argtypes = [wintypes.HWND]
_user32.IsWindowVisible.argtypes = [wintypes.HWND]
_user32.IsIconic.argtypes = [wintypes.HWND]
_user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
_user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.SetForegroundWindow.argtypes = [wintypes.HWND]
_user32.GetDC.argtypes = [wintypes.HWND]
_user32.GetDC.restype = wintypes.HDC
_user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
_user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
_gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
_gdi32.CreateCompatibleDC.restype = wintypes.HDC
_gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
_gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
_gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
_gdi32.SelectObject.restype = wintypes.HGDIOBJ
_gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
_gdi32.DeleteDC.argtypes = [wintypes.HDC]
_gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                             ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
_gdi32.CreateRectRgn.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
_gdi32.CreateRectRgn.restype = wintypes.HRGN
_user32.GetWindowRgn.argtypes = [wintypes.HWND, wintypes.HRGN]
_user32.SetWindowRgn.argtypes = [wintypes.HWND, wintypes.HRGN, wintypes.BOOL]
_user32.GetWindowRgnBox.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_user32.RedrawWindow.argtypes = [wintypes.HWND, ctypes.c_void_p, wintypes.HRGN, wintypes.UINT]

_WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
# 32-bit user32 has no *LongPtrW exports (they are macros for *LongW there): the
# 32-bit worker (32-bit plugins, see plugin_host.worker_command) uses those.
_PTR64 = ctypes.sizeof(ctypes.c_void_p) == 8
_set_window_long = _user32.SetWindowLongPtrW if _PTR64 else _user32.SetWindowLongW
_get_window_long = _user32.GetWindowLongPtrW if _PTR64 else _user32.GetWindowLongW
_set_window_long.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
_set_window_long.restype = ctypes.c_void_p
_get_window_long.argtypes = [wintypes.HWND, ctypes.c_int]
_get_window_long.restype = ctypes.c_void_p
_user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_user32.CallWindowProcW.restype = ctypes.c_ssize_t
_user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_user32.DefWindowProcW.restype = ctypes.c_ssize_t

_HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
_user32.SetWindowsHookExW.argtypes = [ctypes.c_int, _HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
_user32.SetWindowsHookExW.restype = wintypes.HHOOK
_user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
_user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
_user32.CallNextHookEx.restype = ctypes.c_ssize_t
_user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
_user32.RegisterWindowMessageW.restype = wintypes.UINT
_user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                 wintypes.UINT]
_user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]

PW_CLIENTONLY = 1
PW_RENDERFULLCONTENT = 2  # also captures DirectComposition / GPU-drawn content
GWLP_WNDPROC = -4
WM_SHOWWINDOW, WM_WINDOWPOSCHANGING, WM_WINDOWPOSCHANGED, WM_NCDESTROY = 0x0018, 0x0046, 0x0047, 0x0082
SWP_HIDEWINDOW, SWP_SHOWWINDOW = 0x0080, 0x0040
WH_CBT, HCBT_CREATEWND = 5, 3
WS_CHILD = 0x40000000
DWMWA_CLOAK, DWMWA_CLOAKED = 13, 14
# The desktop shell posts this to the editor window once it has put it in its
# frame (plugin_frame.rs, REVEAL_MESSAGE there: same name): the editor shows.
REVEAL_MESSAGE = _user32.RegisterWindowMessageW("VEXT.RevealEditor")
# --hidden without a frame that ever reveals it (the shell failed to adopt it):
# shown anyway this long after the editor was first reported.
REVEAL_FALLBACK_S = 8.0


class _WINDOWPOS(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("insert_after", wintypes.HWND), ("x", ctypes.c_int), ("y", ctypes.c_int),
                ("cx", ctypes.c_int), ("cy", ctypes.c_int), ("flags", wintypes.UINT)]


class _CREATESTRUCTW(ctypes.Structure):
    _fields_ = [("lpCreateParams", wintypes.LPVOID), ("hInstance", wintypes.HINSTANCE), ("hMenu", wintypes.HMENU),
                ("hwndParent", wintypes.HWND), ("cy", ctypes.c_int), ("cx", ctypes.c_int), ("y", ctypes.c_int),
                ("x", ctypes.c_int), ("style", wintypes.LONG), ("lpszName", wintypes.LPCWSTR),
                ("lpszClass", wintypes.LPCWSTR), ("dwExStyle", wintypes.DWORD)]


class _CBT_CREATEWNDW(ctypes.Structure):
    _fields_ = [("lpcs", ctypes.POINTER(_CREATESTRUCTW)), ("hwndInsertAfter", wintypes.HWND)]


_dwm = None


def _cloak(hwnd, on: bool) -> bool:
    """DWM-cloaks a window of this process (or uncloaks it). A cloaked window is
    still "visible" to its program (no hide: JUCE would take that as closed),
    DWM still composes it (PrintWindow captures it, GPU content included), but
    nothing of it reaches the screen and it gets no mouse input. Windows only
    lets a process cloak its own windows."""
    global _dwm
    if _dwm is None:
        _dwm = ctypes.WinDLL("dwmapi")
        _dwm.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        _dwm.DwmSetWindowAttribute.restype = ctypes.c_long
    value = wintypes.BOOL(bool(on))
    return _dwm.DwmSetWindowAttribute(hwnd, DWMWA_CLOAK, ctypes.byref(value), ctypes.sizeof(value)) == 0


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD),
    ]


def _client_size(hwnd) -> tuple[int, int]:
    rect = wintypes.RECT()
    if not _user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return 0, 0
    return rect.right - rect.left, rect.bottom - rect.top


def _biggest_window() -> int | None:
    """The biggest visible top-level window of this process: the plugin editor."""
    pid = os.getpid()
    best = {"hwnd": None, "area": 0}

    @_ENUM_PROC
    def visit(hwnd, _):
        if not _user32.IsWindowVisible(hwnd):
            return True
        owner = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value != pid:
            return True
        width, height = _client_size(hwnd)
        if width * height > best["area"]:
            best.update(hwnd=hwnd, area=width * height)
        return True

    _user32.EnumWindows(visit, 0)
    return best["hwnd"]


class _Editor:
    """The editor window, found once and then remembered. The desktop shell
    re-owns it, strips its caption and may hide it (minimize) from outside the
    process, so it isn't searched again while it still exists: a new search
    could pick another window (or none, while it is hidden).

    Hiding it must not close it. pedalboard keeps the editor open only while
    JUCE thinks its window is visible, and JUCE marks it invisible as soon as
    the window is hidden from outside: by the shell (minimize), or by Windows
    itself when the window's owner is minimized. So once found, the window is
    subclassed: hide notifications (WM_WINDOWPOSCHANGING/CHANGED carrying
    SWP_HIDEWINDOW) go straight to DefWindowProc, past JUCE. The window still
    really hides and shows again; JUCE just never notices. Closing still works:
    pedalboard and JUCE's own close clear JUCE's visible flag first.

    `ready` is set once the editor is on screen (or its loop is over): a
    pedalboard plugin's audio waits for it (see _MonoAdapter.editor_ready).

    The same window procedure serves _Hold: windows it subclasses at creation
    are cloaked when they are first shown, and REVEAL_MESSAGE uncloaks them."""

    def __init__(self):
        self._lock = threading.Lock()
        self._sub_lock = threading.Lock()
        self.hwnd: int | None = None
        self._original: dict[int, int] = {}  # hwnd -> its original window procedure
        self._proc = _WNDPROC(self._window_proc)  # referenced here so it's never collected
        # VST2/CLAP (app/hosting): the editor is VEXT's own window, known by the
        # plugin object; it is never searched for, and needs no subclassing.
        self.owner = None
        self.ready = threading.Event()

    def find(self) -> int | None:
        with self._lock:
            owner = self.owner
            if owner is not None and not getattr(owner, "floating_editor", False):
                self.hwnd = getattr(owner, "editor_hwnd", None)
                return self.hwnd
            if self.hwnd and _user32.IsWindow(self.hwnd):
                return self.hwnd
            self.hwnd = _biggest_window()
            if owner is not None:  # a floating CLAP editor: the plugin's own window, closed by the plugin
                return self.hwnd
            if self.hwnd:
                self.subclass(self.hwnd)
            return self.hwnd

    def subclass(self, hwnd: int) -> None:
        """Puts _window_proc in front of the window's own procedure (once)."""
        with self._sub_lock:
            if hwnd in self._original:
                return
            # Recorded before the swap: the new procedure may run at once.
            original = _get_window_long(hwnd, GWLP_WNDPROC)
            if original:
                self._original[hwnd] = original
                _set_window_long(hwnd, GWLP_WNDPROC, ctypes.cast(self._proc, ctypes.c_void_p))

    def unsubclass_others(self) -> None:
        """Gives every subclassed window but the editor its own procedure back
        (the ones _Hold caught besides it), unless someone subclassed it since."""
        ours = ctypes.cast(self._proc, ctypes.c_void_p).value
        with self._sub_lock:
            for hwnd, original in list(self._original.items()):
                if hwnd == self.hwnd or not _user32.IsWindow(hwnd):
                    continue
                if _get_window_long(hwnd, GWLP_WNDPROC) == ours:
                    _set_window_long(hwnd, GWLP_WNDPROC, original)
                    self._original.pop(hwnd, None)

    def _window_proc(self, hwnd, msg, wparam, lparam):
        original = self._original.get(hwnd)
        if original is None:  # not ours (can't happen): behave like a plain window
            return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)
        try:
            if msg == REVEAL_MESSAGE:
                _hold.reveal("frame" if wparam == 0 else "command" if wparam == 1 else "timeout")
                return 0
            if msg in (WM_WINDOWPOSCHANGING, WM_WINDOWPOSCHANGED) and lparam:
                flags = _WINDOWPOS.from_address(lparam).flags
                if flags & SWP_HIDEWINDOW and hwnd == self.hwnd:
                    return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)
                if msg == WM_WINDOWPOSCHANGING and flags & SWP_SHOWWINDOW:
                    _hold.on_show(hwnd)
            elif msg == WM_SHOWWINDOW and wparam:
                _hold.on_show(hwnd)
            elif msg == WM_NCDESTROY:  # the window's last message: put the original back
                _set_window_long(hwnd, GWLP_WNDPROC, original)
                with self._sub_lock:
                    self._original.pop(hwnd, None)
        except Exception:  # noqa: BLE001 - never let an exception reach the plugin's message loop
            pass
        return _user32.CallWindowProcW(original, hwnd, msg, wparam, lparam)

    def visible(self, hwnd) -> bool:
        """Shown and not minimized (the shell hides it while its frame is minimized)."""
        return bool(_user32.IsWindowVisible(hwnd)) and not _user32.IsIconic(hwnd)


_editor = _Editor()


class _Hold:
    """Keeps the editor off the screen until the desktop shell has placed it
    (--hidden), or for good (--capture: the picture job).

    Before: pedalboard/JUCE (and VEXT's own EditorWindow) showed the editor at
    once, at the top-left of the screen, and the frame adopted it ~0.2 s later:
    a visible jump. Now a CBT hook on the main thread (the thread that creates
    the editor) subclasses every top-level window as it is created, and the
    subclass cloaks it (DWM) when it is first shown, before anything reaches
    the screen. No hide, no move: JUCE sees a normal visible window. The frame
    places the cloaked editor, shows itself, then posts REVEAL_MESSAGE; the
    "reveal" command (no frame) and REVEAL_FALLBACK_S do the same. Once
    revealed, the hook goes and later windows (menus, popups) show normally.

    start() runs on the main thread before the windows exist: for the
    Playground right before the editor opens (loading dialogs still show), for
    the picture job before loading (a license dialog stays hidden too; the job
    gives up on it)."""

    def __init__(self):
        self.active = False
        self.forever = False
        self._hook = None
        self._held: set[int] = set()
        self._cloaked: set[int] = set()
        self._pending: str | None = None  # a reveal asked for before any window existed
        self._lock = threading.Lock()
        self._cbt = _HOOKPROC(self._on_cbt)
        self.revealed = threading.Event()

    def start(self, forever: bool = False) -> None:
        self.forever = forever
        kernel32 = ctypes.WinDLL("kernel32")
        self._hook = _user32.SetWindowsHookExW(WH_CBT, self._cbt, None, kernel32.GetCurrentThreadId())
        self.active = bool(self._hook)  # no hook: the editor shows the old way

    def _on_cbt(self, code, wparam, lparam):
        try:
            if code == HCBT_CREATEWND and self.active and wparam and lparam:
                cs = ctypes.cast(lparam, ctypes.POINTER(_CBT_CREATEWNDW)).contents.lpcs.contents
                message_only = ((cs.hwndParent or 0) & 0xFFFFFFFFFFFFFFFF) == 0xFFFFFFFFFFFFFFFD  # HWND_MESSAGE
                if not cs.style & WS_CHILD and not message_only:
                    with self._lock:
                        self._held.add(int(wparam))
                    _editor.subclass(int(wparam))
        except Exception:  # noqa: BLE001 - the hook must always pass the event on
            pass
        return _user32.CallNextHookEx(self._hook, code, wparam, lparam)

    def on_show(self, hwnd: int) -> None:
        """A held window is about to show (its own thread): cloak it first."""
        with self._lock:
            if not self.active or self._pending or hwnd not in self._held or hwnd in self._cloaked:
                return
            if _cloak(hwnd, True):
                self._cloaked.add(hwnd)

    def cloaked(self, hwnd) -> bool:
        return int(hwnd or 0) in self._cloaked

    def reveal(self, how: str) -> None:
        """Shows every held window and stops holding (main thread)."""
        with self._lock:
            if not self.active or self.forever:
                return
            self.active = False
            for hwnd in list(self._cloaked):
                _cloak(hwnd, False)
            self._cloaked.clear()
            hook, self._hook = self._hook, None
        if hook:
            _user32.UnhookWindowsHookEx(hook)
        _editor.unsubclass_others()
        self.revealed.set()
        emit("revealed", how=how)

    def request_reveal(self, how: str = "command") -> None:
        """From any thread: the editor's thread reveals it as soon as it can."""
        if not self.active or self.forever:
            return
        hwnd = _editor.hwnd
        if hwnd and int(hwnd) in self._original_targets():
            _user32.PostMessageW(hwnd, REVEAL_MESSAGE, {"frame": 0, "command": 1}.get(how, 2), 0)
        else:
            with self._lock:
                self._pending = how  # nothing to post to yet: nothing gets cloaked from now on

    def _original_targets(self) -> set:
        return set(_editor._original)

    def uncloak_all(self, bottom: bool = False) -> None:
        """The picture job's fallback (cloaked capture stayed blank): really show
        the windows, at the bottom of the z-order, without activating them."""
        with self._lock:
            targets = list(self._cloaked)
            self._cloaked.clear()
            self.active = False
        for hwnd in targets:
            _cloak(hwnd, False)
            if bottom:
                _user32.SetWindowPos(hwnd, wintypes.HWND(1), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)  # HWND_BOTTOM


_hold = _Hold()


# The desktop shell's frame (plugin_frame.rs, HOTKEY_MESSAGE there: same name) hears Space
# pressed in the editor through this message (wParam: its number) and tells the page.
HOTKEY_MESSAGE = _user32.RegisterWindowMessageW("VEXT.Hotkey")
WH_GETMESSAGE, PM_REMOVE, VK_SPACE = 3, 1, 0x20
WM_NULL, WM_KEYDOWN, WM_KEYUP, WM_CHAR = 0x0000, 0x0100, 0x0101, 0x0102
GA_ROOT, GW_OWNER = 2, 4
_user32.GetFocus.restype = wintypes.HWND
_user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
_user32.GetAncestor.restype = wintypes.HWND
_user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
_user32.GetWindow.restype = wintypes.HWND
_user32.GetKeyState.argtypes = [ctypes.c_int]
_user32.GetKeyState.restype = ctypes.c_short


class _SpaceKey:
    """Space is VEXT's Play / Stop, even while the plugin's editor has the
    keyboard: a message hook on the main thread (the editor's thread) turns
    it into WM_NULL before the plugin sees it (a plugin that wants Space, e.g. a text
    field, never gets it), and each press is reported: `hotkey {seq}` to the
    backend (host state `space_seq`) and HOTKEY_MESSAGE to the frame, which
    owns the editor, for the page at once. With Ctrl / Alt / Win it is the
    plugin's."""

    def __init__(self):
        self.seq = 0
        self._hook = None
        self._proc = _HOOKPROC(self._on_key)

    def start(self) -> None:
        kernel32 = ctypes.WinDLL("kernel32")
        self._hook = _user32.SetWindowsHookExW(WH_GETMESSAGE, self._proc, None, kernel32.GetCurrentThreadId())

    @staticmethod
    def _modified() -> bool:
        return any(_user32.GetKeyState(vk) & 0x8000 for vk in (0x11, 0x12, 0x5B, 0x5C))  # Ctrl, Alt, Win

    def _on_key(self, code, wparam, lparam):
        try:
            if code >= 0 and lparam:
                msg = ctypes.cast(lparam, ctypes.POINTER(wintypes.MSG)).contents
                space = (msg.message in (WM_KEYDOWN, WM_KEYUP) and msg.wParam == VK_SPACE) or (
                    msg.message == WM_CHAR and msg.wParam == 0x20)
                if space and not self._modified():
                    if wparam == PM_REMOVE and msg.message == WM_KEYDOWN and not msg.lParam & (1 << 30):  # not a repeat
                        self._pressed()
                    msg.message = WM_NULL  # swallowed: never reaches the plugin
        except Exception:  # noqa: BLE001 - the hook must always pass the message on
            pass
        return _user32.CallNextHookEx(self._hook, code, wparam, lparam)

    def _pressed(self) -> None:
        self.seq += 1
        emit("hotkey", key="space", seq=self.seq)
        root = _user32.GetAncestor(_user32.GetFocus(), GA_ROOT)
        frame = _user32.GetWindow(root, GW_OWNER) if root else None
        if frame:
            pid = wintypes.DWORD()
            _user32.GetWindowThreadProcessId(frame, ctypes.byref(pid))
            if pid.value != os.getpid():  # the shell's frame, not one of the plugin's own windows
                _user32.PostMessageW(frame, HOTKEY_MESSAGE, self.seq, 0)


_space = _SpaceKey()


def _watch_editor(stop: threading.Event) -> None:
    """Reports the editor window as soon as it exists, and again whenever its
    client size changes (physical pixels; the worker is DPI aware). Looks
    often until it's there: the plugin's audio waits for it (_Editor.ready).
    A held editor (--hidden) is revealed after REVEAL_FALLBACK_S if the frame
    never did it."""
    last = None
    fallback = None
    while True:
        hwnd = _editor.find()
        if hwnd:
            width, height = _client_size(hwnd)
            if width > 0 and height > 0 and (hwnd, width, height) != last:  # 0x0 = minimized
                last = (hwnd, width, height)
                emit("editor", hwnd=int(hwnd), width=width, height=height, hidden=_hold.active)
                _editor.ready.set()
            if _hold.active and not _hold.forever:
                if _hold._pending:
                    _hold._pending = None
                    _hold.request_reveal("command")
                elif fallback is None:
                    fallback = threading.Timer(REVEAL_FALLBACK_S, _hold.request_reveal, args=("timeout",))
                    fallback.daemon = True
                    fallback.start()
        if stop.wait(0.25 if _editor.ready.is_set() else 0.03):
            if fallback is not None:
                fallback.cancel()
            return


def _print_window(hwnd, width: int, height: int):
    """The window's client area rendered by PrintWindow (works while covered by
    other windows), or None if that failed or came back blank."""
    from PIL import Image

    screen_dc = _user32.GetDC(None)
    mem_dc = _gdi32.CreateCompatibleDC(screen_dc)
    bitmap = _gdi32.CreateCompatibleBitmap(screen_dc, width, height)
    try:
        previous = _gdi32.SelectObject(mem_dc, bitmap)
        ok = _user32.PrintWindow(hwnd, mem_dc, PW_CLIENTONLY | PW_RENDERFULLCONTENT)
        _gdi32.SelectObject(mem_dc, previous)  # GetDIBits needs the bitmap deselected
        if not ok:
            return None
        header = _BITMAPINFOHEADER(biSize=ctypes.sizeof(_BITMAPINFOHEADER), biWidth=width,
                                   biHeight=-height, biPlanes=1, biBitCount=32)  # top-down, BI_RGB
        pixels = ctypes.create_string_buffer(width * height * 4)
        if not _gdi32.GetDIBits(mem_dc, bitmap, 0, height, pixels, ctypes.byref(header), 0):
            return None
    finally:
        _gdi32.DeleteObject(bitmap)
        _gdi32.DeleteDC(mem_dc)
        _user32.ReleaseDC(None, screen_dc)
    image = Image.frombuffer("RGB", (width, height), pixels.raw, "raw", "BGRX", 0, 1)
    if all(low == high for low, high in image.getextrema()):
        return None  # one flat colour: the content wasn't rendered into the bitmap
    return image


def _grab_screen(hwnd, width: int, height: int):
    """Fallback: raise the (visible) window and copy its client area from the screen."""
    from PIL import ImageGrab

    _user32.SetForegroundWindow(hwnd)
    threading.Event().wait(0.3)  # let GPU-drawn UIs repaint after being raised
    origin = wintypes.POINT(0, 0)
    _user32.ClientToScreen(hwnd, ctypes.byref(origin))
    box = (origin.x, origin.y, origin.x + width, origin.y + height)
    return ImageGrab.grab(bbox=box, all_screens=True)


class _whole_editor:
    """While capturing: an editor the desktop shell scrolls in its frame (a
    window region smaller than its client area, plugin_frame.rs clip) only
    paints the part that shows, so PrintWindow would give the rest black. For
    the capture it is cloaked (off the screen: the frame's body shows for a
    moment), its region taken off and a full repaint awaited; afterwards the
    region goes back (unless the shell set a new one meanwhile) and the cloak
    comes off. An editor that shows whole is left alone."""

    REPAINT_S = 0.35  # GPU-drawn editors repaint on their own timer

    def __init__(self, hwnd):
        self.hwnd = hwnd
        self.saved = None
        self.cloaked = False

    def _clipped(self) -> bool:
        box, win = wintypes.RECT(), wintypes.RECT()
        if _user32.GetWindowRgnBox(self.hwnd, ctypes.byref(box)) == 0:  # ERROR: no region
            return False
        _user32.GetWindowRect(self.hwnd, ctypes.byref(win))
        origin = wintypes.POINT(0, 0)
        _user32.ClientToScreen(self.hwnd, ctypes.byref(origin))
        left, top = origin.x - win.left, origin.y - win.top
        width, height = _client_size(self.hwnd)
        return box.left > left or box.top > top or box.right < left + width or box.bottom < top + height

    def __enter__(self):
        if not self._clipped():
            return self
        region = _gdi32.CreateRectRgn(0, 0, 0, 0)
        if not region or _user32.GetWindowRgn(self.hwnd, region) == 0:
            if region:
                _gdi32.DeleteObject(region)
            return self
        self.saved = region
        self.cloaked = not _hold.cloaked(self.hwnd) and _cloak(self.hwnd, True)
        _user32.SetWindowRgn(self.hwnd, None, True)
        _user32.RedrawWindow(self.hwnd, None, None, 0x0001 | 0x0080 | 0x0100)  # INVALIDATE | ALLCHILDREN | UPDATENOW
        threading.Event().wait(self.REPAINT_S)
        return self

    def __exit__(self, *exc):
        if self.saved is not None:
            box = wintypes.RECT()
            if _user32.GetWindowRgnBox(self.hwnd, ctypes.byref(box)) == 0:  # still none: put it back
                if _user32.SetWindowRgn(self.hwnd, self.saved, True):
                    self.saved = None  # the system owns it now
            if self.saved is not None:
                _gdi32.DeleteObject(self.saved)
        if self.cloaked:
            _cloak(self.hwnd, False)
        return False


def capture_editor(path: str) -> str:
    """Saves the editor as a PNG at `path`. Returns how ('print' or 'screen').
    A hidden editor (its frame minimized) is never shown for this: the desktop
    shell owns its visibility."""
    hwnd = _editor.find()
    if not hwnd:
        raise RuntimeError("the plugin editor window is not open")
    if _user32.IsIconic(hwnd):  # a free-standing editor minimized by the user (no desktop shell)
        _user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        threading.Event().wait(0.3)
    width, height = _client_size(hwnd)
    if width <= 0 or height <= 0:
        raise RuntimeError("the plugin editor window has no size")
    with _whole_editor(hwnd):
        image, how = _print_window(hwnd, width, height), "print"
    if image is None:
        if _hold.cloaked(hwnd):  # not on screen yet: a screen grab would copy what's behind it
            raise RuntimeError("the plugin's window hasn't drawn anything yet; try again in a moment")
        if not _editor.visible(hwnd):
            raise RuntimeError("the plugin window is minimized: restore it to capture it")
        image, how = _grab_screen(hwnd, width, height), "screen"
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    image.save(path, "PNG")
    return how


# ---------- audio devices (shared with the backend and app/audio_tool.py) ----------

# Driver types (the audio_driver setting) -> the PortAudio host API they use.
# WDM-KS is left out: it opens the device exclusively and is unreliable with
# many drivers (WASAPI exclusive covers that need). ASIO needs PortAudio's ASIO
# build, which sounddevice ships (see sounddevice_module).
DRIVERS = {
    "wasapi": "Windows WASAPI",
    "wasapi_exclusive": "Windows WASAPI",
    "directsound": "Windows DirectSound",
    "mme": "MME",
    "asio": "ASIO",
}
DRIVER_LABELS = {"wasapi": "WASAPI", "wasapi_exclusive": "WASAPI (exclusive)", "directsound": "DirectSound",
                 "mme": "MME", "asio": "ASIO"}
# Host APIs offered, best first.
OUTPUT_HOSTAPIS = ("Windows WASAPI", "Windows DirectSound", "MME", "ASIO")
SAMPLE_RATES = (44100, 48000, 88200, 96000, 176400, 192000)
BLOCK_SIZES = (64, 128, 256, 512, 1024, 2048, 4096)
# Safe Mode: this many extra buffers queued ahead of the device (and twice the
# live input's jitter buffer). Each one adds one buffer of latency.
SAFE_EXTRA_BLOCKS = 2
TEST_TONE = {"hz": 440.0, "seconds": 0.6, "amplitude": 0.03}
# The Plugin buffer (Player._run_file / _run_live): how far ahead of the device the plugin runs.
# "auto" starts at AUTO_START_S and grows (up to AUTO_MAX_S) when a plugin's slow moments outlast it.
PLUGIN_BUFFERS = ("auto", "25", "50", "100", "200", "400", "800")  # audio_plugin_buffer choices (ms)
AUTO_START_S = 0.06      # Auto's first target
AUTO_MAX_S = 1.0         # Auto never grows past this
BROWSER_CHUNK = 2048     # frames per message from browser_capture.js (its ScriptProcessor size): keep in step
LIVE_IDLE_S = 0.5        # no browser input for this long: silence is fed through the plugin (tails ring out)


def render_target(setting, rate: int, block: int, live: bool, learned_s: float = 0.0) -> tuple[int, bool]:
    """(target frames, adaptive) for the Plugin buffer setting. Never below two blocks; the browser
    path never below two of its chunks (its messages arrive one chunk at a time)."""
    floor = max(2 * block, 2 * BROWSER_CHUNK) if live else 2 * block
    if str(setting or "auto") == "auto":
        return max(floor, int(max(AUTO_START_S, learned_s) * rate)), True
    try:
        ms = int(setting)
    except (TypeError, ValueError):
        ms = int(AUTO_START_S * 1000)
    return max(floor, int(ms * rate / 1000)), False
# The backend restarts PortAudio to see new devices (plugin_host.audio_devices,
# audio_setup): never from two threads at once.
PORTAUDIO_LOCK = threading.Lock()


def asio_driver_names() -> list[str]:
    """The ASIO drivers installed, from the registry (nothing is loaded)."""
    import winreg

    names = []
    for view in (winreg.KEY_WOW64_64KEY, 0):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\ASIO", 0, winreg.KEY_READ | view) as key:
                for i in range(256):
                    try:
                        name = winreg.EnumKey(key, i)
                    except OSError:
                        break
                    if name not in names:
                        names.append(name)
        except OSError:
            continue
    return names


def sounddevice_module(asio: bool = False):
    """sounddevice, imported on first use. Its ASIO build of PortAudio is only
    loaded when the first user in this process wants ASIO: initializing it
    loads every installed ASIO driver (seconds, and a driver a DAW is using
    may not like being opened by another process). The choice is fixed once
    imported; has_asio(sd) tells which one this process got."""
    if asio and "sounddevice" not in sys.modules:
        os.environ["SD_ENABLE_ASIO"] = "1"
    import sounddevice as sd

    return sd


def has_asio(sd) -> bool:
    return any(api["name"] == "ASIO" for api in sd.query_hostapis())


def stream_extra(sd, device: dict | None, driver: str | None):
    """PortAudio's host-specific settings: WASAPI shared mode lets Windows
    convert the rate (any rate opens); exclusive mode talks to the device
    directly (only its own rates, lowest latency, other apps go silent)."""
    if device and device["hostapi"] == "Windows WASAPI":
        if driver == "wasapi_exclusive":
            return sd.WasapiSettings(exclusive=True)
        return sd.WasapiSettings(auto_convert=True)
    return None


def _system_default_index(sd) -> int | None:
    """Windows' default output as WASAPI sees it (shared mode follows the
    Windows mixer, so any rate works). PortAudio's own default is the MME one,
    which has far more latency; it's only the fallback."""
    for api in sd.query_hostapis():
        if api["name"] == "Windows WASAPI" and api["default_output_device"] >= 0:
            return api["default_output_device"]
    index = sd.default.device[1]
    return index if index is not None and index >= 0 else None


def _devices(sd) -> list[dict]:
    apis = sd.query_hostapis()
    default_index = _system_default_index(sd)
    out = []
    for index, d in enumerate(sd.query_devices()):
        api = apis[d["hostapi"]]
        channels = d["max_output_channels"]
        if channels < 1 or api["name"] not in OUTPUT_HOSTAPIS:
            continue
        out.append({
            "index": index, "id": f"{d['name']}, {api['name']}", "name": d["name"],
            "hostapi": api["name"], "default": index == default_index,
            "api_default": index == api.get("default_output_device", -1),
            "channels": channels, "default_samplerate": d["default_samplerate"],
            "low_latency_ms": round(float(d["default_low_output_latency"]) * 1000, 1),
        })
    out.sort(key=lambda d: OUTPUT_HOSTAPIS.index(d["hostapi"]))  # stable: device order kept per API
    return out


def output_devices(sd) -> list[dict]:
    """Output devices of the offered host APIs, WASAPI first. `id` is the
    "name, host API" string, which is what the audio_output_device setting
    stores (a bare name is accepted too). `default` marks the one device that
    the empty setting ("system default") plays through; `api_default` the
    default of its own host API (an empty setting with that driver type)."""
    return _devices(sd)


def find_output_device(sd, wanted: str, driver: str | None = None) -> dict | None:
    """The device a setting value names: its exact id first, else the first
    device with that name (the driver type's host API first). An empty value
    is the driver type's default device (no driver: the system default).
    None if it isn't there."""
    wanted = (wanted or "").strip()
    devices = _devices(sd)
    api = DRIVERS.get(driver or "")
    if not wanted:
        if api:
            mine = [d for d in devices if d["hostapi"] == api]
            return next((d for d in mine if d["api_default"]), None) or (mine[0] if mine else None)
        return next((d for d in devices if d["default"]), None)
    return (next((d for d in devices if d["id"] == wanted), None)
            or next((d for d in devices if d["name"] == wanted and (not api or d["hostapi"] == api)), None)
            or next((d for d in devices if d["name"] == wanted), None))


def supported_rates(sd, device: dict | None, driver: str | None) -> dict:
    """{"rates": [the SAMPLE_RATES that open], "native": [those the device runs
    at itself]}. Shared modes (WASAPI shared, DirectSound, MME) open at any
    rate because Windows converts; `native` is then the Windows mixer's rate
    (WASAPI) or unknown ([]). Exclusive WASAPI and ASIO only open at native rates."""
    if device is None:
        return {"rates": list(SAMPLE_RATES), "native": []}
    def opens(rate, extra) -> bool:
        try:
            sd.check_output_settings(device=device["index"], samplerate=rate, channels=min(2, device["channels"]), dtype="float32",
                  extra_settings=extra)
            return True
        except Exception:  # noqa: BLE001 - PortAudioError / ValueError: refused
            return False

    extra = stream_extra(sd, device, driver)
    rates = [r for r in SAMPLE_RATES if opens(r, extra)]
    if device["hostapi"] == "Windows WASAPI" and driver != "wasapi_exclusive":
        native = [r for r in SAMPLE_RATES if opens(r, None)]
    elif device["hostapi"] in ("ASIO",) or driver == "wasapi_exclusive":
        native = list(rates)
    else:
        native = []
    return {"rates": rates, "native": native}


def stream_latency(block: int, rate: int, safe: bool, device: dict | None) -> float:
    """The latency asked of PortAudio (seconds): one buffer, never below what
    the device itself calls low latency (MME and DirectSound glitch below it);
    Safe Mode queues SAFE_EXTRA_BLOCKS more buffers ahead of the device."""
    base = block / rate
    if device and device["hostapi"] in ("MME", "Windows DirectSound"):
        base = max(base, (device.get("low_latency_ms") or 0) / 1000)
    return base + (SAFE_EXTRA_BLOCKS * block / rate if safe else 0.0)


def _pro_audio_thread():
    """Raises the calling (audio) thread to Windows' "Pro Audio" scheduling
    class (MMCSS), as DAWs do. Returns a handle for _normal_thread, or None."""
    try:
        avrt = ctypes.WinDLL("avrt")
        avrt.AvSetMmThreadCharacteristicsW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
        avrt.AvSetMmThreadCharacteristicsW.restype = wintypes.HANDLE
        task = wintypes.DWORD(0)
        return avrt.AvSetMmThreadCharacteristicsW("Pro Audio", ctypes.byref(task)) or None
    except (OSError, AttributeError):
        return None


def _normal_thread(handle) -> None:
    if handle:
        try:
            avrt = ctypes.WinDLL("avrt")
            avrt.AvRevertMmThreadCharacteristics.argtypes = [wintypes.HANDLE]
            avrt.AvRevertMmThreadCharacteristics(handle)
        except (OSError, AttributeError):
            pass


def _com_init() -> bool:
    """Initializes COM on the calling audio thread. ASIO drivers are COM
    objects: PortAudio only initializes COM on the thread that started it,
    so an ASIO stream opened from any other thread fails with "Failed to
    load ASIO driver". Returns whether _com_done must undo it."""
    try:
        return ctypes.windll.ole32.CoInitializeEx(None, 0x2) in (0, 1)  # COINIT_APARTMENTTHREADED: S_OK / S_FALSE
    except OSError:
        return False


def _com_done(initialized: bool) -> None:
    if initialized:
        try:
            ctypes.windll.ole32.CoUninitialize()
        except OSError:
            pass


def _test_gain() -> float:
    """VEXT_TEST_OUTPUT_GAIN scales what reaches the device (automated tests
    keep real plugins quiet); the levels reported are measured before it."""
    try:
        return max(0.0, min(1.0, float(os.environ.get("VEXT_TEST_OUTPUT_GAIN", "1"))))
    except ValueError:
        return 1.0


# ---------- audio sources ----------

def check_source(source: str) -> None:
    """Raises if `source` can't be played: only local audio files can."""
    if not os.path.isfile(source):
        raise ValueError("that file doesn't exist")


class _Decoder:
    """A media file decoded to stereo float32 at a fixed rate, read in blocks. Seekable."""

    def __init__(self, path: str, rate: int):
        import av

        self.rate = rate
        self.container = av.open(path)
        self.stream = self.container.streams.audio[0]
        if self.container.duration:
            self.duration = self.container.duration / av.time_base
        elif self.stream.duration and self.stream.time_base:
            self.duration = float(self.stream.duration * self.stream.time_base)
        else:
            self.duration = None
        self._restart(skip_until=None)

    def _restart(self, skip_until):
        import av
        import numpy as np

        self.resampler = av.AudioResampler(format="fltp", layout="stereo", rate=self.rate)
        self.pending = np.zeros((2, 0), dtype=np.float32)
        self.frames = self.container.decode(self.stream)
        self.skip_until = skip_until
        self.eof = False

    def seek(self, seconds: float) -> None:
        import av

        seconds = max(0.0, seconds)
        # Lands on the packet at or before the target; the rest is trimmed while decoding.
        self.container.seek(int(seconds * av.time_base))
        self._restart(skip_until=seconds)

    def _append(self, frame) -> None:
        import numpy as np

        chunks = [out.to_ndarray() for out in self.resampler.resample(frame)]
        if not chunks:
            return
        data = np.concatenate(chunks, axis=1)
        if self.skip_until is not None and frame is not None and frame.time is not None:
            drop = int(round((self.skip_until - frame.time) * self.rate))
            data = data[:, max(drop, 0):]
        self.skip_until = None
        self.pending = np.concatenate([self.pending, data], axis=1)

    def read(self, n: int):
        """The next (2, <=n) block, or None at the end."""
        while self.pending.shape[1] < n and not self.eof:
            try:
                frame = next(self.frames)
            except StopIteration:
                self.eof = True
                self._append(None)  # flush what the resampler still holds
                break
            if self.skip_until is not None and frame.time is not None:
                if frame.time + frame.samples / frame.sample_rate <= self.skip_until:
                    continue
            self._append(frame)
        if not self.pending.shape[1]:
            return None
        block, self.pending = self.pending[:, :n], self.pending[:, n:]
        return block

    def close(self) -> None:
        self.container.close()


# ---------- the Audio tab: edits, the recording after the plugin ----------

# A 32-bit float WAV (WAVE_FORMAT_IEEE_FLOAT): RIFF, fmt (16 bytes), fact, then the
# data. The Audio tab's decoded file and its recording are both this, so FFmpeg
# plays them (a "play" source like any other) and the backend reads samples at
# a known offset (app/playground_audio.py).
WAV_FLOAT_HEADER = 56


def float_wav_header(channels: int, rate: int, frames: int) -> bytes:
    import struct

    data = frames * channels * 4
    return (b"RIFF" + struct.pack("<I", WAV_FLOAT_HEADER - 8 + data) + b"WAVE"
            + b"fmt " + struct.pack("<IHHIIHH", 16, 3, channels, rate, rate * channels * 4, channels * 4, 32)
            + b"fact" + struct.pack("<II", 4, frames & 0xFFFFFFFF)
            + b"data" + struct.pack("<I", data))


def read_wav_layout(path: str) -> dict | None:
    """{channels, rate, frames, offset} of a 32-bit float WAV (only those: what
    VEXT writes itself), else None."""
    import struct

    try:
        with open(path, "rb") as f:
            head = f.read(4096)
    except OSError:
        return None
    if head[:4] != b"RIFF" or head[8:12] != b"WAVE":
        return None
    pos, fmt = 12, None
    while pos + 8 <= len(head):
        cid, size = head[pos:pos + 4], struct.unpack("<I", head[pos + 4:pos + 8])[0]
        if cid == b"fmt ":
            fmt = struct.unpack("<HHIIHH", head[pos + 8:pos + 24])
        elif cid == b"data":
            if not fmt or fmt[0] != 3 or fmt[5] != 32:
                return None
            channels = fmt[1]
            try:
                avail = os.path.getsize(path) - (pos + 8)
            except OSError:
                return None
            return {"channels": channels, "rate": fmt[2], "offset": pos + 8,
                    "frames": max(0, min(size, avail)) // (channels * 4)}
        pos += 8 + size + (size & 1)
    return None


# ---------- without FFmpeg: the 32-bit worker ----------

# PyAV has no 32-bit Windows build, so the worker that hosts 32-bit plugins
# (plugin_host.worker_command) has no FFmpeg. Everything it plays is a float WAV
# VEXT wrote itself (the Audio tab's decoded file, its recording), so it reads
# those directly and resamples with _SincResampler. The 64-bit worker keeps FFmpeg.

def _have_av() -> bool:
    try:
        import av  # noqa: F401
    except ImportError:
        return False
    return True


class _SincResampler:
    """Streams (2, n) float32 blocks from one rate to another: a Kaiser-windowed
    sinc (32 taps, 1024 phases), low-passed below the lower Nyquist."""

    HALF, PHASES, BETA = 16, 1024, 8.6

    def __init__(self, in_rate: int, out_rate: int):
        import numpy as np

        self.step = in_rate / out_rate  # input samples per output sample
        cutoff = min(1.0, out_rate / in_rate) * 0.95
        k = np.arange(-self.HALF + 1, self.HALF + 1, dtype=np.float64)
        x = k[None, :] - (np.arange(self.PHASES + 1, dtype=np.float64) / self.PHASES)[:, None]
        window = np.i0(self.BETA * np.sqrt(np.clip(1 - (x / self.HALF) ** 2, 0, None))) / np.i0(self.BETA)
        self.table = (cutoff * np.sinc(cutoff * x) * window).astype(np.float32)  # (PHASES + 1, 2 * HALF)
        self.offsets = k.astype(np.int64)
        self.buf = np.zeros((2, self.HALF - 1), dtype=np.float32)  # history: the first taps see silence
        self.t = float(self.HALF - 1)  # where the next output sample falls in buf

    def __call__(self, block, final: bool = False):
        import numpy as np

        self.buf = np.concatenate([self.buf, np.asarray(block, dtype=np.float32).reshape(2, -1)], axis=1)
        if final:
            self.buf = np.concatenate([self.buf, np.zeros((2, self.HALF), dtype=np.float32)], axis=1)
        last = self.buf.shape[1] - self.HALF - 1  # the last position whose taps are all in buf
        if last < self.t:
            return np.zeros((2, 0), dtype=np.float32)
        n = int((last - self.t) // self.step) + 1
        pos = self.t + self.step * np.arange(n)
        base = np.floor(pos).astype(np.int64)
        phase = np.rint((pos - base) * self.PHASES).astype(np.int64)
        idx = base[:, None] + self.offsets[None, :]
        weights = self.table[phase]
        out = np.stack([(self.buf[c][idx] * weights).sum(axis=1) for c in (0, 1)]).astype(np.float32)
        self.t += self.step * n
        drop = max(0, int(self.t) - self.HALF + 1)
        self.buf, self.t = self.buf[:, drop:], self.t - drop
        return out


class _WavDecoder:
    """_Decoder for VEXT's float WAVs, without FFmpeg (the 32-bit worker). Reads
    to the end of the file (a recording still being written grows), mono as
    both sides, resampled to `rate` when the file's rate differs."""

    def __init__(self, path: str, rate: int):
        layout = read_wav_layout(path)
        if layout is None:
            raise ValueError("this host (32-bit, no FFmpeg) only plays VEXT's own WAV files")
        self.rate, self.layout = rate, layout
        self.file = open(path, "rb")  # noqa: SIM115 - held until close()
        self.duration = layout["frames"] / layout["rate"] if layout["rate"] else None
        self.seek(0)

    def seek(self, seconds: float) -> None:
        import numpy as np

        lay = self.layout
        self.frame = min(int(max(0.0, seconds) * lay["rate"]), self._frames())
        self.resampler = _SincResampler(lay["rate"], self.rate) if lay["rate"] != self.rate else None
        self.pending = np.zeros((2, 0), dtype=np.float32)
        self.eof = False

    def _frames(self) -> int:
        lay = self.layout
        try:
            return max(0, os.fstat(self.file.fileno()).st_size - lay["offset"]) // (lay["channels"] * 4)
        except OSError:
            return 0

    def _raw(self, n: int):
        import numpy as np

        ch = self.layout["channels"]
        n = max(0, min(n, self._frames() - self.frame))
        self.file.seek(self.layout["offset"] + self.frame * ch * 4)
        data = np.frombuffer(self.file.read(n * ch * 4), dtype="<f4")
        n = data.size // ch
        self.frame += n
        data = data[:n * ch].reshape(n, ch).T
        if ch == 1:
            return np.vstack([data, data])
        return np.ascontiguousarray(data[:2])

    def read(self, n: int):
        """The next (2, <=n) block, or None at the end."""
        import numpy as np

        if self.resampler is None:
            block = self._raw(n)
            return block if block.shape[1] else None
        while self.pending.shape[1] < n and not self.eof:
            raw = self._raw(max(n, 1024))
            self.eof = raw.shape[1] == 0
            self.pending = np.concatenate([self.pending, self.resampler(raw, final=self.eof)], axis=1)
        if not self.pending.shape[1]:
            return None
        block, self.pending = self.pending[:, :n], self.pending[:, n:]
        return block

    def close(self) -> None:
        self.file.close()


def _open_decoder(path: str, rate: int):
    return _Decoder(path, rate) if _have_av() else _WavDecoder(path, rate)


def envelope_gain(ops, t):
    """The gain the Audio tab's edits give the samples at times `t` (seconds,
    float64 array): every op multiplies in over [start_s, end_s): fade_in
    rises 0 -> 1 linearly, fade_out falls 1 -> 0, gain is a constant
    (Normalize). Outside an op's range it changes nothing."""
    import numpy as np

    g = np.ones(t.shape, dtype=np.float32)
    if not ops or not t.size:
        return g
    lo, hi = float(t[0]), float(t[-1])
    for op in ops:
        s, e = float(op.get("start_s", 0)), float(op.get("end_s", 0))
        if e <= s or e <= lo or s > hi:
            continue
        inside = (t >= s) & (t < e)
        kind = op.get("type")
        if kind == "gain":
            g[inside] *= np.float32(op.get("gain", 1.0))
            continue
        x = ((t[inside] - s) / (e - s)).astype(np.float32)
        g[inside] *= x if kind == "fade_in" else (1.0 - x) if kind == "fade_out" else 1.0
    return g


class _Recorder:
    """The Audio tab's recording: what comes out of the plugin while the file
    plays, written at the file's own time positions into a float WAV that
    covers the whole file at the stream's rate (punch-in: a later pass over
    the same time overwrites it; never-played parts stay silent).

    Tap point: after IN, the plugin, OUT and Auto-Balance (exactly the signal
    sent to the device), before the Test tone and the test-only output gain.
    Playback thread only, except arm()/disarm() under Player._lock."""

    def __init__(self):
        self.want: dict | None = None  # {path, duration_s} while armed
        self.f = None
        self.path = self.rate = None
        self.frames = 0
        self.dirty: list | None = None  # [first, end) frames written since the last report

    def ensure(self, rate: int) -> dict | None:
        """Opens the file for this rate (once). Returns the 'record' event
        fields when it (re)opened, else None."""
        want = self.want
        if not want or (self.f and self.path == want["path"] and self.rate == rate):
            return None
        self.close()
        frames = max(1, int(-(-float(want["duration_s"]) * rate // 1)))
        path = want["path"]
        layout = read_wav_layout(path)
        created = not (layout and layout["channels"] == 2 and layout["rate"] == rate and layout["frames"] == frames
                       and layout["offset"] == WAV_FLOAT_HEADER)
        if created:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:  # silence everywhere until played (the file system zero-fills)
                f.write(float_wav_header(2, rate, frames))
                f.truncate(WAV_FLOAT_HEADER + frames * 8)
        self.f = open(path, "r+b")
        self.path, self.rate, self.frames = path, rate, frames
        return {"on": True, "path": path, "rate": rate, "frames": frames, "created": created}

    def write(self, frame: int, sound) -> None:
        import numpy as np

        if not self.f:
            return
        n = sound.shape[1]
        start, end = max(0, frame), min(self.frames, frame + n)
        if end <= start:
            return
        part = sound[:, start - frame:end - frame]
        self.f.seek(WAV_FLOAT_HEADER + start * 8)
        self.f.write(np.ascontiguousarray(part.T, dtype="<f4").tobytes())
        self.dirty = [min(self.dirty[0], start), max(self.dirty[1], end)] if self.dirty else [start, end]

    def take_dirty(self) -> list | None:
        """The frames written since the last call (flushed: the backend reads them now)."""
        if not self.dirty or not self.f:
            return None
        self.f.flush()
        dirty, self.dirty = self.dirty, None
        return dirty

    def close(self) -> list | None:
        dirty = self.take_dirty()
        if self.f:
            try:
                self.f.close()
            except OSError:
                pass
        self.f = None
        self.path = self.rate = None
        return dirty


class _LiveBuffer:
    """Live input waiting to be played: a FIFO of (n, 2) float32 chunks from the browser. The render
    thread takes block-sized pieces as the plugin needs them (take); the Plugin buffer does the priming
    and the jitter absorbing. Past `limit` frames the oldest audio is dropped back to `limit // 2` (the
    sender ran ahead for a long time: latency stays bounded). Input is discarded while live playback
    isn't running."""

    def __init__(self):
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self.active = False
        self.configure(limit=2 * BROWSER_CHUNK)

    def configure(self, limit: int) -> None:
        with self._lock:
            self._chunks: collections.deque = collections.deque()
            self._size = 0
            self.limit = limit
            self.frames_in = self.dropped = 0
            self._peak = 0.0

    def push(self, frames) -> None:
        import numpy as np

        with self._cond:
            if not self.active or not len(frames):
                return
            self._chunks.append(frames)
            self._size += len(frames)
            self.frames_in += len(frames)
            self._peak = max(self._peak, float(np.abs(frames).max()))
            if self._size > self.limit:
                self.dropped += self._discard(self._size - self.limit // 2)
            self._cond.notify_all()

    def _discard(self, n: int) -> int:
        dropped = 0
        while n > 0 and self._chunks:
            head = self._chunks[0]
            if len(head) <= n:
                self._chunks.popleft()
                n -= len(head)
                dropped += len(head)
            else:
                self._chunks[0] = head[n:]
                dropped += n
                n = 0
        self._size -= dropped
        return dropped

    def take(self, n: int):
        """The next (2, n) float32 block, or None while fewer than n frames are buffered."""
        import numpy as np

        with self._lock:
            if self._size < n:
                return None
            parts, left = [], n
            while left:
                head = self._chunks[0]
                if len(head) <= left:
                    parts.append(self._chunks.popleft())
                    left -= len(head)
                else:
                    parts.append(head[:left])
                    self._chunks[0] = head[left:]
                    left = 0
            self._size -= n
        return np.ascontiguousarray(np.concatenate(parts).T, dtype=np.float32)

    def wait(self, timeout: float) -> None:
        """The render thread waits here for input: until the next push, or `timeout` s."""
        with self._cond:
            self._cond.wait(timeout)

    def stats(self, rate: int, underruns: int = 0) -> dict:
        """Counters, the buffered time and the input peak since the last call (underruns: the queue's)."""
        with self._lock:
            peak, self._peak = self._peak, 0.0
            return {"frames_in": self.frames_in, "dropped": self.dropped, "underruns": underruns,
                    "buffer_ms": round(self._size * 1000 / rate, 1), "peak_in": round(peak, 5)}


FADE_IN_S = 0.003  # after a silence (a starve, a seek, the start): the sound ramps in over this long


def fade_in(sound, rate: int):
    """A copy of `sound` (2, n) whose first FADE_IN_S ramp up from zero: no click where the sound comes
    back after silence. The chunk's own arrays stay as they are (the analysis tap has them)."""
    import numpy as np

    out = sound.copy()
    m = min(out.shape[1], int(rate * FADE_IN_S))
    out[:, :m] *= np.linspace(0.0, 1.0, m, dtype=np.float32)[None, :]
    return out


MAX_PLUGIN_DELAY_S = 4.0  # a plugin's reported latency beyond this is taken as nonsense (ignored)


class _Segment:
    """A run of a block out of the plugin that came from one stretch of input (see _Align.match):
    output frames [o0, o1), the file time `at` of their input (None: no file time, e.g. silence fed to let
    the plugin's tail out), the input's `epoch`, and that input as it went in: `data` (before IN: the
    analysis tap's Before) and `sent` (after IN: what Bypass plays)."""

    __slots__ = ("o0", "o1", "at", "epoch", "data", "sent")

    def __init__(self, o0, o1, at, epoch, data, sent):
        self.o0, self.o1, self.at, self.epoch, self.data, self.sent = o0, o1, at, epoch, data, sent


class _Align:
    """Plugin delay compensation. A plugin with latency hands back what went in `delay` frames earlier,
    and pedalboard withholds part of that delay (a block can come back shorter than it went in): RX 8
    De-click reports 31 931 frames, pedalboard handed back 26 299 frames fewer than it got, and what did
    come back still lagged 5 632. Whatever the split, the newest frame out is the input frame `delay`
    before the newest frame in. This keeps the recent input (with its file time and epoch) and says, for
    each block out, which input it is: the Analyzer's Before is that input (in phase with After), the
    recording and the playhead use its file time, and output that is still the plugin's start-up latency
    (or older than a seek) can be recognized and dropped. Render thread only."""

    def __init__(self, rate: int):
        import numpy as np

        self.rate = rate
        self.fed = 0  # input frames fed so far
        self.real_end = 0  # `fed` after the last input that had a file time
        self._hist: collections.deque = collections.deque()  # (start, at, epoch, data, sent)
        self._keep = int(MAX_PLUGIN_DELAY_S * rate) + 16384
        self._zeros = np.zeros((2, 0), dtype=np.float32)

    def feed(self, at, epoch, data, sent=None) -> None:
        n = data.shape[1]
        self._hist.append((self.fed, at, epoch, data, data if sent is None else sent))
        self.fed += n
        if at is not None:
            self.real_end = self.fed
        while self._hist and self._hist[0][0] + self._hist[0][3].shape[1] < self.fed - self._keep:
            self._hist.popleft()

    def pending(self, delay: int) -> int:
        """Frames of input with a file time that the plugin hasn't handed back yet."""
        return max(0, self.real_end - (self.fed - delay))

    def match(self, n_out: int, delay: int) -> list:
        """The input behind the `n_out` frames the plugin just handed back, as _Segments in order
        (contiguous input merged). Input from before anything was fed / no longer kept: at None, zeros."""
        import numpy as np

        segs: list = []
        idx = self.fed - delay - n_out  # the input frame behind the first frame out
        o = 0
        for start, at, epoch, data, sent in self._hist:
            n = data.shape[1]
            if o >= n_out:
                break
            if start + n <= idx:
                continue
            if idx < start:  # before this chunk: nothing kept for it
                k = min(n_out - o, start - idx)
                z = np.zeros((2, k), dtype=np.float32)
                segs.append(_Segment(o, o + k, None, None, z, z))
                o, idx = o + k, idx + k
                if o >= n_out:
                    break
            off = idx - start
            k = min(n_out - o, n - off)
            t = None if at is None else at + off / self.rate
            last = segs[-1] if segs else None
            if (last is not None and t is not None and last.at is not None and last.epoch == epoch
                    and abs(last.at + (last.o1 - last.o0) / self.rate - t) < 0.5 / self.rate):
                last.o1 += k  # the same stretch of input goes on
                last.data = np.concatenate([last.data, data[:, off:off + k]], axis=1)
                last.sent = np.concatenate([last.sent, sent[:, off:off + k]], axis=1)
            else:
                segs.append(_Segment(o, o + k, t, epoch, data[:, off:off + k], sent[:, off:off + k]))
            o, idx = o + k, idx + k
        if o < n_out:  # past the newest input: only when the plugin reports less delay than it has
            z = np.zeros((2, n_out - o), dtype=np.float32)
            segs.append(_Segment(o, n_out, None, None, z, z))
        return segs

    def before(self, n_out: int, delay: int):
        """The input behind the `n_out` frames just handed back, as one (2, n_out) block (zeros where none)."""
        import numpy as np

        segs = self.match(n_out, delay)
        if len(segs) == 1:
            return segs[0].data
        return np.concatenate([s.data for s in segs], axis=1) if segs else self._zeros


class _Chunk:
    """One rendered block: `sound` (2, n) float32 = what goes to the device (after IN, plugin, OUT,
    Auto-Balance); `before` = what went into the plugin (the analysis tap's Before), `at` = the file time
    of its first frame (None: not from the file), `device` = `sound` at the device's rate (the browser
    path resamples when the device refused the browser's rate), `end` = the source finished (no audio)."""

    __slots__ = ("sound", "before", "device", "at", "n", "bypass", "end")

    def __init__(self, sound=None, before=None, at=None, bypass=False, end=False, device=None):
        self.sound, self.before, self.at, self.bypass, self.end = sound, before, at, bypass, end
        self.device = sound if device is None else device
        self.n = 0 if sound is None else int(sound.shape[1])


class _RenderQueue:
    """The Plugin buffer: processed audio waiting for the device. The render thread runs the plugin
    ahead of the device by up to `target` frames, so a block that takes longer than real time (iZotope
    RX's FFT frames, a busy moment) is absorbed instead of heard as a gap. The output thread takes
    chunks as the device needs them. Output starts (and restarts after running dry) only once `target`
    frames are queued, or the source's end is. Running dry while playing is a *starve*: Auto then grows
    `target` (x1.5 + a block, up to AUTO_MAX_S). `flush` drops what wasn't heard (seek, pause) and bumps
    `epoch`: a chunk rendered for an older epoch is refused by `put`."""

    def __init__(self, rate: int, block: int, target: int, adaptive: bool):
        self.rate, self.block = rate, block
        self.target, self.adaptive = target, adaptive
        self.max = max(target, int(AUTO_MAX_S * rate))
        self._cond = threading.Condition()
        self._items: collections.deque = collections.deque()
        self.frames = 0
        self.epoch = 0
        self.seek_to: float | None = None   # set by flush(seek_to=): the render thread seeks there first
        self.primed = False
        self.ended = False
        self.closed = False
        self.failed = False  # the render thread gave up (its error is reported): the output ends unfinished
        self.starves = 0
        # Render statistics since the last take_stats(): seconds spent rendering, seconds of audio rendered,
        # the slowest chunk.
        self._busy = self._audio = self._worst = 0.0

    def wait_room(self, timeout: float) -> bool:
        """Render thread: True once there's room for another chunk (or the queue closed)."""
        with self._cond:
            if self.frames < self.target or self.closed:
                return True
            self._cond.wait(timeout)
            return self.frames < self.target or self.closed

    def take_seek(self) -> tuple[int, float | None]:
        """Render thread, before each chunk: (the epoch to render for, a seek to apply first or None)."""
        with self._cond:
            seek, self.seek_to = self.seek_to, None
            return self.epoch, seek

    def put(self, chunk: _Chunk, epoch: int) -> bool:
        with self._cond:
            if epoch != self.epoch or self.closed:
                return False  # flushed meanwhile: never heard
            self._items.append(chunk)
            self.frames += chunk.n
            self.ended = self.ended or chunk.end
            self._cond.notify_all()
            return True

    def get(self) -> _Chunk | None:
        """Output thread (never blocks): the next chunk, or None = play silence (priming or starved)."""
        with self._cond:
            if not self.primed:
                if self.frames >= self.target or (self.ended and self._items):
                    self.primed = True
                else:
                    return None
            if not self._items:
                self.primed = False
                self.starves += 1
                if self.adaptive:
                    self.target = min(self.max, int(self.target * 1.5) + self.block)
                    self._cond.notify_all()
                return None
            chunk = self._items.popleft()
            self.frames -= chunk.n
            self._cond.notify_all()
            return chunk

    def flush(self, seek_to: float | None = None) -> None:
        with self._cond:
            self._items.clear()
            self.frames, self.primed, self.ended = 0, False, False
            self.epoch += 1
            self.seek_to = seek_to
            self._cond.notify_all()

    def follow_loop(self, loop) -> bool:
        """The loop changed while it plays (a selection made or moved, Loop pressed): what's queued was
        rendered for the old one. From the first chunk that no longer fits (past the new end, or a jump back
        the new loop doesn't make) it is dropped (a chunk across the new end is cut there), and the render
        thread goes on from where the kept audio ends, or from the loop's start when that is its end. What
        is heard then follows the selection at once instead of playing out the old buffer. Returns whether
        anything was dropped."""
        eps = 3.0 / self.rate
        start, end = loop if loop else (None, None)
        with self._cond:
            items = list(self._items)
            cut, resume, keep, prev_end = None, None, 0, None
            for i, ch in enumerate(items):
                if ch.end:
                    if loop:  # the file's end is queued, but a loop goes back instead
                        cut, resume = i, start
                    break
                if ch.at is None:
                    break
                ch_end = ch.at + ch.n / self.rate
                if prev_end is not None and abs(ch.at - prev_end) > eps:
                    wrap = loop and abs(prev_end - end) < 0.002 and abs(ch.at - start) < 0.002
                    if not wrap:  # a jump back the old loop made
                        cut, resume = i, (start if loop and prev_end >= end - eps else prev_end)
                        break
                if loop and ch.at >= end - eps:
                    cut, resume = i, start  # past the new end (the selection is behind it): back to its start
                    break
                if loop and ch.at < end < ch_end - eps:
                    cut, resume, keep = i, start, int(round((end - ch.at) * self.rate))
                    break
                prev_end = ch_end
            if cut is None:
                return False
            kept = items[:cut]
            if keep > 0:
                c = items[cut]
                kept.append(_Chunk(c.sound[:, :keep], before=None if c.before is None else c.before[:, :keep],
                                   at=c.at, bypass=c.bypass))
            self._items = collections.deque(kept)
            self.frames = sum(c.n for c in kept)
            self.ended = False
            if not kept:
                self.primed = False
            self.epoch += 1
            self.seek_to = resume
            self._cond.notify_all()
            return True

    def wait_flush(self, epoch: int, timeout: float) -> None:
        """Render thread, after queuing the end: waits for a flush (a seek / loop back) or the close."""
        with self._cond:
            if self.epoch == epoch and not self.closed:
                self._cond.wait(timeout)

    def grow(self, frames: int) -> None:
        """Auto: a chunk took long enough that the buffer may not cover the next one: grow before it starves."""
        with self._cond:
            if self.adaptive and frames > self.target:
                self.target = min(self.max, frames)
                self._cond.notify_all()

    def set_target(self, frames: int) -> None:
        with self._cond:
            self.target = frames
            self._cond.notify_all()

    def note_render(self, seconds: float, frames: int) -> None:
        with self._cond:
            self._busy += seconds
            self._audio += frames / self.rate
            self._worst = max(self._worst, seconds)

    def take_stats(self) -> dict:
        """For the periodic events: the buffer, its fill, starves, and the plugin's load since the last call
        (load_pct = render time / audio time: above 100 the plugin can't keep up at all, no buffer helps)."""
        with self._cond:
            load = 100.0 * self._busy / self._audio if self._audio else 0.0
            worst = self._worst
            self._busy = self._audio = self._worst = 0.0
            return {"ahead_ms": round(self.frames * 1000 / self.rate, 1),
                    "target_ms": round(self.target * 1000 / self.rate, 1),
                    "starves": self.starves, "load_pct": round(load, 1), "render_peak_ms": round(worst * 1000, 1)}

    def close(self) -> None:
        with self._cond:
            self.closed = True
            self._cond.notify_all()


class _LiveInput:
    """The local TCP socket the backend streams the browser's audio into.
    Listens on 127.0.0.1 only and needs `key` first, so nothing else on the
    machine can feed the plugin. A new connection replaces the previous one."""

    def __init__(self):
        self.key = secrets.token_hex(16)
        self.buffer = _LiveBuffer()
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(4)
        self.port = self._server.getsockname()[1]
        self._lock = threading.Lock()
        self._conn: socket.socket | None = None
        threading.Thread(target=self._accept, daemon=True).start()

    @property
    def connected(self) -> bool:
        return self._conn is not None

    def _accept(self) -> None:
        while True:
            try:
                conn, _addr = self._server.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        import numpy as np

        try:
            conn.settimeout(3)
            head = b""
            while b"\n" not in head and len(head) < 256:
                chunk = conn.recv(256)
                if not chunk:
                    raise OSError("closed")
                head += chunk
            line, _, carry = head.partition(b"\n")
            if not secrets.compare_digest(line.strip(), self.key.encode()):
                raise OSError("wrong key")
            conn.settimeout(None)
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            conn.close()
            return
        with self._lock:
            previous, self._conn = self._conn, conn
        if previous:
            previous.close()
        try:
            data = carry
            while self._conn is conn:
                usable = len(data) - len(data) % 8  # whole stereo float32 frames only
                if usable:
                    self.buffer.push(np.frombuffer(data[:usable], dtype=np.float32).reshape(-1, 2))
                data = data[usable:]
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
        except OSError:
            pass
        finally:
            with self._lock:
                if self._conn is conn:
                    self._conn = None
            conn.close()


class _Resampler:
    """Streams (2, n) float32 blocks from one rate to another (only used when
    the output device refused the browser's rate)."""

    def __init__(self, in_rate: int, out_rate: int):
        import fractions

        import av

        self._av = av
        self.in_rate = in_rate
        self._time_base = fractions.Fraction(1, in_rate)
        self._pts = 0
        self._resampler = av.AudioResampler(format="fltp", layout="stereo", rate=out_rate)

    def __call__(self, block):
        import numpy as np

        frame = self._av.AudioFrame.from_ndarray(np.ascontiguousarray(block, dtype=np.float32),
                                                 format="fltp", layout="stereo")
        frame.sample_rate = self.in_rate
        frame.pts, frame.time_base = self._pts, self._time_base
        self._pts += block.shape[1]
        out = [f.to_ndarray() for f in self._resampler.resample(frame)]
        return np.concatenate(out, axis=1) if out else np.zeros((2, 0), dtype=np.float32)


def _is_release(data: bytes) -> bool:
    """Note-off (or note-on at velocity 0), sustain off, all notes/sound off:
    messages that are always safe to deliver late."""
    kind = data[0] & 0xF0
    return (kind == 0x80 or (kind == 0x90 and len(data) > 2 and data[2] == 0)
            or (kind == 0xB0 and len(data) > 2 and (data[1] in (120, 123) or (data[1] == 64 and data[2] < 64))))


class _MidiRouter:
    """MIDI for the open plugin: hardware inputs (app/hosting/midi.py, opened
    in this process) and the Playground's on-screen keyboard (the "midi"
    command), queued for the audio thread. Each block takes what arrived
    during the previous block, placed at the same relative position: a
    constant one-block delay instead of jitter. While no audio loop delivers
    MIDI, note-ons are dropped (never replayed later in a burst); releases are
    kept, so a key let go during a restart doesn't hang. Held notes are
    tracked so panic() can release them."""

    MAX_QUEUE = 4096

    def __init__(self):
        self._lock = threading.Lock()
        self._queue: collections.deque = collections.deque(maxlen=self.MAX_QUEUE)
        self._listening = False
        self._panic = False
        self.channel = 0  # hardware inputs: 0 = every channel, else only 1..16
        # Bypass pitch bend: hardware pitch-bend messages (0xEn, 14-bit, centre 8192) are dropped
        # before they count (count / last / recent) or reach the plugin.
        # Turning it on centres the bend on every channel, so a bend held at that moment can't stick.
        self.block_pitchbend = False
        self._center_bend = False
        self.held: set = set()  # (channel, key) the plugin got a note-on for (audio thread only)
        self.count = 0  # messages received, for the activity indicator
        self.last = ""  # the last one, as hex
        self.recent: list = []  # [[time.time(), device, [bytes]]] since the last "midi" event (capped)
        self.dirty = False
        self._inputs = None  # app.hosting.midi.MidiInputs, created on first use
        self.inputs = {"open": [], "failed": [], "available": []}

    def push(self, data: bytes, t: float | None = None, device: str = "") -> None:
        """A channel message from a hardware input (`device`) or the on-screen
        keyboard (no device: the channel filter doesn't apply)."""
        if not data or not 0x80 <= data[0] <= 0xEF:
            return
        data = bytes(data)
        if self.block_pitchbend and (data[0] & 0xF0) == 0xE0:
            return  # bypassed pitch bend: not counted, not shown, not played
        if device:  # for the toolbar's MIDI indicator: every hardware message, even filtered ones
            with self._lock:
                self.recent.append([time.time(), device, list(data)])
                self.dirty = True
        if device and self.channel and (data[0] & 0x0F) != self.channel - 1:
            return
        with self._lock:
            self.count += 1  # a filtered-out message still shows the keyboard is sending
            self.last = data.hex(" ").upper()
            self.dirty = True
            if self._listening or _is_release(data):
                self._queue.append((time.perf_counter() if t is None else t, data))

    def listen(self, on: bool) -> None:
        """An audio loop starts (or stops) delivering MIDI to the plugin."""
        with self._lock:
            self._listening = on
            if on:  # what waited meanwhile: only releases are still meaningful
                kept = [(t, d) for t, d in self._queue if _is_release(d)]
                self._queue.clear()
                self._queue.extend(kept)

    def panic(self) -> None:
        """All notes off: the next block releases every held note and sends
        sustain off, all notes off and all sound off on every channel."""
        with self._lock:
            self._queue.clear()
            self._panic = True

    def drain(self, now: float, since: float, block: int) -> list:
        """[(offset, bytes)] for the block about to render: what arrived
        between `since` (the previous drain) and `now`, spread over the block
        the way it was spread over that time. Audio thread."""
        with self._lock:
            items = list(self._queue)
            self._queue.clear()
            panic, self._panic = self._panic, False
            center, self._center_bend = self._center_bend, False
        events = [(0, m) for m in self._release_all()] if panic else []
        if center and not panic:  # panic centres the bend too
            events += [(0, bytes((0xE0 | ch, 0x00, 0x40))) for ch in range(16)]
        if panic:
            self.dirty = True  # the held count changed: report it
        span = now - since
        for t, data in items:
            offset = int((t - since) / span * block) if span > 0 else 0
            events.append((min(max(offset, 0), block - 1), data))
            self._track(data)
        events.sort(key=lambda e: e[0])  # stable: the panic messages stay first
        return events

    def _track(self, data: bytes) -> None:
        kind, ch = data[0] & 0xF0, data[0] & 0x0F
        if kind == 0x90 and len(data) > 2 and data[2] > 0:
            self.held.add((ch, data[1]))
        elif kind in (0x80, 0x90) and len(data) > 1:
            self.held.discard((ch, data[1]))
        elif kind == 0xB0 and len(data) > 2 and data[1] in (120, 123):
            self.held = {(c, k) for c, k in self.held if c != ch}

    def _release_all(self) -> list[bytes]:
        msgs = [bytes((0x80 | ch, key, 0)) for ch, key in sorted(self.held)]
        self.held = set()
        for ch in range(16):  # sustain off, all notes off, all sound off, pitch bend to centre
            msgs += [bytes((0xB0 | ch, 64, 0)), bytes((0xB0 | ch, 123, 0)), bytes((0xB0 | ch, 120, 0)),
                     bytes((0xE0 | ch, 0x00, 0x40))]
        return msgs

    def set_block_pitchbend(self, on: bool) -> None:
        with self._lock:
            if on and not self.block_pitchbend:
                self._center_bend = True
            self.block_pitchbend = bool(on)

    def set_inputs(self, wanted, channel: int) -> dict:
        """Opens the hardware inputs `wanted` ("*" = all, a list of ids, empty
        = none) and sets the channel filter. Returns {open, failed, available}."""
        from app.hosting import midi

        self.channel = channel if 0 <= channel <= 16 else 0
        if not wanted and self._inputs is None:
            self.inputs = {"open": [], "failed": [], "available": [d["id"] for d in midi.list_inputs()]}
            return self.inputs
        if self._inputs is None:
            self._inputs = midi.MidiInputs(self.push)
        self.inputs = self._inputs.open(wanted)
        return self.inputs

    def shutdown(self) -> None:
        if self._inputs is not None:
            try:
                self._inputs.shutdown()
            except Exception:  # noqa: BLE001
                pass

    def stats(self) -> dict:
        with self._lock:
            self.dirty = False
            recent, self.recent = self.recent[-32:], []
            return {"count": self.count, "last": self.last, "held": len(self.held), "recent": recent}


def _report_midi(router: _MidiRouter, stop: threading.Event) -> None:
    """The activity indicator: a "midi" event at most every 0.1 s, only when
    something arrived."""
    while not stop.wait(0.1):
        if router.dirty:
            emit("midi", **router.stats())


def tone_block(start: int, n: int, rate: int, total: int):
    """Frames [start, start+n) of the test tone (TEST_TONE: a quiet sine with
    10 ms fades), as a (2, n) float32 block."""
    import numpy as np

    i = np.arange(start, start + n, dtype=np.float64)
    fade = max(1, int(rate * 0.01))
    envelope = np.clip(np.minimum(i / fade, (total - i) / fade), 0.0, 1.0)
    wave = TEST_TONE["amplitude"] * envelope * np.sin(2 * np.pi * TEST_TONE["hz"] * i / rate)
    return np.repeat(wave[None, :].astype(np.float32), 2, axis=0)


class _Tone:
    """Configuration > Audio's Test while this worker is playing: the test tone
    mixed into what it plays (the device is already this worker's)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._pos = self._total = 0
        self._rate = 0

    def start(self) -> None:
        with self._lock:
            self._pos, self._total, self._rate = 0, -1, 0  # sized at the first block (its rate)

    def mix(self, sound, rate: int):
        with self._lock:
            if self._total == -1 or self._rate != rate and self._total:
                self._pos, self._total, self._rate = 0, int(TEST_TONE["seconds"] * rate), rate
            if self._pos >= self._total:
                return sound
            n = min(sound.shape[1], self._total - self._pos)
            start = self._pos
            self._pos += n
        sound = sound.copy()
        sound[:, :n] += tone_block(start, n, rate, self._total)
        return sound


GAIN_MIN_DB, GAIN_MAX_DB = -60.0, 12.0  # the frame's IN / OUT knobs; at the minimum: silence
BALANCE_RANGE_DB = (-40.0, 24.0)  # how far Auto-Balance may turn the output down / up
BALANCE_TAU_S = 0.6  # its loudness estimates (and its gain) follow over about this long
BALANCE_REF_DBFS = -18.0  # an instrument has no input to match: its output aims here (RMS)
LIMIT_CEILING = 10 ** (-1.0 / 20)  # Auto-Balance's safety limiter: peaks stay under -1 dBFS
LIMIT_RELEASE_S = 0.25


def _db_to_gain(db) -> float:
    if db is None or db <= GAIN_MIN_DB:
        return 0.0
    return 10 ** (float(db) / 20)


class _Gains:
    """The frame's IN and OUT gains around the plugin, and Auto-Balance.

    IN scales what goes into the plugin, OUT what comes out (bypassed: the dry
    signal, IN and OUT still apply). Auto-Balance (not while bypassed) keeps
    the output about as loud as what went in, so a plugin that adds +30 dB can
    still be heard safely: two slow loudness estimates (input after IN,
    output after OUT) set a gain that glides there, and a limiter with an
    instant attack keeps peaks under -1 dBFS. Every gain change is ramped over
    a block: no zipper noise. Audio thread; set() from any thread."""

    def __init__(self):
        self._lock = threading.Lock()
        self.in_db, self.out_db, self.auto = 0.0, 0.0, False
        self._in = self._out = 1.0  # the gains the last block ended at
        self._ms_in = self._ms_out = 0.0  # mean squares (Auto-Balance's loudness estimates)
        self.balance_db = 0.0
        self._balance = 1.0
        self._limit = 1.0
        self.reduction_db = 0.0  # what the limiter takes off now (report)

    def set(self, in_db=None, out_db=None, auto=None) -> dict:
        with self._lock:
            if in_db is not None:
                self.in_db = max(GAIN_MIN_DB, min(GAIN_MAX_DB, float(in_db)))
            if out_db is not None:
                self.out_db = max(GAIN_MIN_DB, min(GAIN_MAX_DB, float(out_db)))
            if auto is not None and bool(auto) != self.auto:
                self.auto = bool(auto)
                self._ms_in = self._ms_out = 0.0
            return self.info()

    def info(self) -> dict:
        return {"in_db": self.in_db, "out_db": self.out_db, "auto_balance": self.auto,
                "balance_db": round(self.balance_db, 1) if self.auto else 0.0,
                "reduction_db": round(self.reduction_db, 1) if self.auto else 0.0}

    @staticmethod
    def _ramp(block, start: float, end: float):
        import numpy as np

        if start == end:
            return block if end == 1.0 else block * np.float32(end)
        return block * np.linspace(start, end, block.shape[1], dtype=np.float32)[None, :]

    def pre(self, data):
        """IN: what the plugin gets."""
        with self._lock:
            target = _db_to_gain(self.in_db)
        out = self._ramp(data, self._in, target)
        self._in = target
        return out

    def post(self, sound, reference, rate: int, balance: bool = True):
        """OUT, then Auto-Balance and its limiter. `reference`: what went into
        the plugin (None: an instrument, which aims at BALANCE_REF_DBFS)."""
        import numpy as np

        with self._lock:
            target, auto = _db_to_gain(self.out_db), self.auto and balance
        sound = self._ramp(sound, self._out, target)
        self._out = target
        if not auto:
            self.reduction_db = 0.0
            self._balance, self._limit = 1.0, 1.0
            return sound
        n = sound.shape[1]
        # The first audible block sets the estimates outright: no loud start while they settle.
        a = 1.0 if self._ms_out <= 1e-12 else 1.0 - np.exp(-n / (rate * BALANCE_TAU_S))
        ms_out = float(np.mean(sound * sound))
        self._ms_out += a * (ms_out - self._ms_out)
        if reference is not None:
            ms_in = float(np.mean(reference * reference))
            self._ms_in += (1.0 if self._ms_in <= 1e-12 else a) * (ms_in - self._ms_in)
            ref_ms = self._ms_in
        else:
            ref_ms = 10 ** (BALANCE_REF_DBFS / 10) if self._ms_out > 1e-10 else 0.0
        if ref_ms > 1e-8 and self._ms_out > 1e-12:  # both sides audible: aim the output at the input
            want = 10 * np.log10(ref_ms / self._ms_out)
            want = max(BALANCE_RANGE_DB[0], min(BALANCE_RANGE_DB[1], want))
            self.balance_db += a * (want - self.balance_db)
        gain = 10 ** (self.balance_db / 20)
        if a == 1.0:
            self._balance = gain  # primed: no ramp from unity
        sound = self._ramp(sound, self._balance, gain)
        self._balance = gain
        peak = float(np.abs(sound).max()) if n else 0.0
        need = LIMIT_CEILING / peak if peak > LIMIT_CEILING else 1.0
        release = self._limit + (1.0 - self._limit) * (1.0 - np.exp(-n / (rate * LIMIT_RELEASE_S)))
        limit = min(need, release)
        sound = self._ramp(sound, min(self._limit, need), limit)
        self._limit = limit
        self.reduction_db = -20 * float(np.log10(limit)) if limit < 1.0 else 0.0
        return np.clip(sound, -LIMIT_CEILING, LIMIT_CEILING)


def _clean_envelope(ops) -> list:
    out = []
    for op in ops or []:
        if isinstance(op, dict) and op.get("type") in ("fade_in", "fade_out", "gain"):
            out.append({"type": op["type"], "start_s": float(op.get("start_s") or 0),
                        "end_s": float(op.get("end_s") or 0), "gain": float(op.get("gain") or 1.0)})
    return out


def _clean_mix(mix) -> dict | None:
    if not isinstance(mix, dict) or not mix.get("path"):
        return None
    gain = lambda key: min(1.0, max(0.0, float(mix.get(key, 1.0))))  # noqa: E731
    return {"path": str(mix["path"]), "source": gain("source"), "recording": gain("recording")}


def _clean_loop(loop) -> tuple | None:
    try:
        start, end = float(loop[0]), float(loop[1])
    except (TypeError, ValueError, IndexError):
        return None
    return (max(0.0, start), end) if end - max(0.0, start) >= 0.01 else None


class Player:
    """Plays one source at a time through the plugin: a file, or live input.
    Commands (from the stdin thread) only change flags under the lock; the
    playback thread applies them. Every start gets a new generation number, so
    a superseded playback thread can never emit events again."""

    REPORT_EVERY = 0.25  # seconds between position / live / instrument events

    def __init__(self, plugin, live_input: _LiveInput | None, midi: _MidiRouter | None = None):
        self.plugin = plugin
        self.live_input = live_input
        self.midi = midi or _MidiRouter()
        self.tone = _Tone()
        self.gains = _Gains()  # the frame's IN / OUT and Auto-Balance
        self.bypass = False
        self.position = 0.0
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._gen = 0
        self._mode: str | None = None  # 'file' | 'live' | 'instrument' | None
        self._paused = False
        self._seek_to: float | None = None
        self._thread: threading.Thread | None = None
        self._source: str | None = None  # the file playing
        self._live_rate = 48000  # the rate the live input arrives at
        self.stream_info: dict | None = None  # the open stream (see _open_stream), while one plays
        # The Audio tab (file playback): its edits as a gain envelope (envelope_gain), the
        # loop (start_s, end_s) or None, dry = no plugin (its recording played back), and
        # the recording of what comes out of the plugin (_Recorder).
        self.envelope: list = []
        self.loop: tuple | None = None
        # A selection played once (start_s, end_s): playing ends at end_s (exactly, so a
        # recording ends there too); a seek outside it plays on to the file's end.
        self.until: tuple | None = None
        # The Audio tab's recording played along with the file, both into the plugin (a chain:
        # what one plugin made is what the next one hears): {path, source, recording} (gains,
        # the panes' M buttons), or None. Recording again drops it (that file gets overwritten).
        self.mix: dict | None = None
        self.dry = False
        self.recorder = _Recorder()
        # The analysis ring (app/analysis_tap.py): every block that plays is copied into it for
        # the Analyzer and the browser recording. None until plugin_host sends the "tap" command.
        self._tap = None
        # How MIDI reaches it: pedalboard (VST3) takes MIDI *or* audio, so only
        # its instruments get MIDI; VEXT's VST2/CLAP hosts take both.
        self._pedalboard = isinstance(plugin, _MonoAdapter)
        self.is_instrument = bool(getattr(plugin, "is_instrument", False))
        self.accepts_midi = self.is_instrument or (not self._pedalboard and bool(getattr(plugin, "accepts_midi", False)))
        # No audio output (VST3 analyzers, app/hosting/vst3.py): fed the audio, the input is heard.
        self.no_output = bool(getattr(plugin, "no_output", False))
        self._vst3_channels = 2
        # The Plugin buffer's learned target (seconds) per kind of playback: the next play starts with what
        # this one needed (Auto only; never shrinks while the worker lives).
        self._learned_s = 0.0
        self._learned_live_s = 0.0
        # One plugin call at a time: a render thread that outlived a timed-out join can't overlap the next one.
        self._plugin_lock = threading.Lock()
        # The Plugin buffer of the file playing (follow_loop when the loop changes), and whether the output
        # is waiting for it (priming after a start / seek, or starved): the playhead doesn't move then.
        self._queue: _RenderQueue | None = None
        self.buffering = False

    def _plugin_delay(self) -> int:
        """The latency the plugin reports now, in frames (it may change with its settings): see _Align."""
        try:
            if self._pedalboard:
                value = self.plugin.reported_latency_samples
            else:
                value = getattr(self.plugin, "latency_samples", 0)
            return max(0, min(int(value or 0), int(MAX_PLUGIN_DELAY_S * 192000)))
        except Exception:  # noqa: BLE001 - not reported: taken as none
            return 0

    def _process(self, data, rate: int, events=()):
        """One block through the plugin (with the MIDI `events` [(offset, bytes)]
        if it takes MIDI). Always (2, n) float32."""
        with self._plugin_lock:
            if self._pedalboard and self.is_instrument:
                return self._render_vst3(data.shape[1], rate, events)
            if events and self.accepts_midi and not self._pedalboard:
                return self.plugin(data, rate, reset=False, midi=events)
            return self.plugin(data, rate, reset=False)

    def _through(self, data, rate: int, events, bypass: bool):
        """One block of a source through IN, the plugin, OUT and Auto-Balance.
        Bypassed: the dry signal (IN and OUT still apply), but the plugin
        still processes it, so its state stays warm for switching back."""
        sent = self.gains.pre(data)
        if bypass and self.no_output:
            # An analyzer (no output: it's heard dry anyway): bypassed = no longer fed.
            return self.gains.post(sent, sent, rate, balance=False)
        wet = self._process(sent, rate, events)
        return self.gains.post(sent if bypass else wet, sent, rate, balance=not bypass)

    def _gain_report(self) -> dict:
        g = self.gains.info()
        return {"balance_db": g["balance_db"], "reduction_db": g["reduction_db"]}

    def _render_vst3(self, n: int, rate: int, events):
        """A VST3 instrument (pedalboard): `n` frames rendered from MIDI. The
        half-frame in the duration keeps float rounding from losing a frame."""
        import numpy as np

        msgs = [(m, off / rate) for off, m in events]
        try:
            out = self.plugin.process(msgs, (n + 0.5) / rate, rate, self._vst3_channels, n, False)
        except Exception as e:  # noqa: BLE001
            if self._vst3_channels != 2 or "channel" not in str(e):
                raise
            self._vst3_channels = 1  # a mono instrument
            out = self.plugin.process(msgs, (n + 0.5) / rate, rate, 1, n, False)
        out = np.asarray(out, dtype=np.float32)
        if out.ndim == 1:
            out = out[None, :]
        if out.shape[0] == 1:
            out = np.repeat(out, 2, axis=0)
        if out.shape[1] != n:
            fixed = np.zeros((2, n), dtype=np.float32)
            fixed[:, :min(n, out.shape[1])] = out[:2, :n]
            out = fixed
        return out[:2]

    def _midi_for(self, now: float, since: float, block: int) -> list:
        """The MIDI for the next block, when this plugin takes MIDI."""
        return self.midi.drain(now, since, block) if self.accepts_midi else []

    def _fresh_start(self) -> None:
        """Clears tails before a new source. Only VEXT's VST2/CLAP hosts: a
        pedalboard reset() off the main thread leaves some VST3s (TAL-Chorus-LX)
        refusing every later block ("must be reloaded on the main thread"),
        and the main thread is busy running the editor."""
        if not self._pedalboard and hasattr(self.plugin, "reset"):
            self.plugin.reset()

    def _say(self, gen: int, event: str, **fields) -> bool:
        """Emits only while `gen` is still the current playback."""
        with self._lock:
            if gen != self._gen:
                return False
            emit(event, **fields)
            return True

    def play(self, source: str, start_s: float, audio: dict, paused: bool = False, envelope=None, loop=None,
             dry: bool = False, until=None, mix=None) -> None:
        stopped = self.stop()
        if stopped == "live":
            emit("live_stopped")
        if dry:
            self.record_off()  # the recording plays back: nothing to record
        started = threading.Event()
        with self._lock:
            self._gen += 1
            gen = self._gen
            self._mode, self._paused, self._seek_to = "file", bool(paused), None
            self._source = source
            self.position = max(0.0, float(start_s or 0))
            self.envelope = _clean_envelope(envelope)
            self.loop = _clean_loop(loop)
            self.until = _clean_loop(until)
            self.dry = bool(dry)
            self.mix = _clean_mix(mix) if not dry and self.recorder.want is None else None
        self._thread = threading.Thread(
            target=self._run_file, args=(gen, source, self.position, audio, started), daemon=True)
        self._thread.start()
        started.wait(timeout=5)  # until playing (or the failure) has been reported

    def instrument(self, audio: dict) -> None:
        """Renders the instrument continuously (silence until notes arrive)
        until stopped: what makes it playable from MIDI."""
        if not self.is_instrument:
            raise RuntimeError("this plugin isn't an instrument")
        self.stop()
        started, result = threading.Event(), {}
        with self._lock:
            self._gen += 1
            gen = self._gen
            self._mode, self._paused, self._seek_to = "instrument", False, None
        self._thread = threading.Thread(target=self._run_instrument, args=(gen, audio, started, result), daemon=True)
        self._thread.start()
        started.wait(timeout=10)
        if result.get("error"):
            raise RuntimeError(result["error"])

    def reconfigure(self, audio: dict) -> str | None:
        """New audio settings: whatever plays restarts with them right away (a
        file from where it is, still paused if it was). Returns the mode."""
        with self._lock:
            mode, paused, source, position = self._mode, self._paused, self._source, self.position
        if mode == "file" and source:
            self.play(source, position, audio, paused=paused, envelope=self.envelope, loop=self.loop, dry=self.dry)
        elif mode == "live":
            self.live(audio, self._live_rate)
        elif mode == "instrument":
            self.instrument(audio)
        return mode

    def set_envelope(self, ops) -> None:
        """The Audio tab's edits changed while the file plays: heard from the next block."""
        with self._lock:
            self.envelope = _clean_envelope(ops)

    def set_mix(self, source=None, recording=None) -> None:
        """The M buttons while the recording plays along (gains 0..1; None: unchanged)."""
        with self._lock:
            if self.mix is None:
                return
            mix = dict(self.mix)
            for key, value in (("source", source), ("recording", recording)):
                if value is not None:
                    mix[key] = min(1.0, max(0.0, float(value)))
            self.mix = mix

    def set_loop(self, loop) -> None:
        """A new loop (or none) while the file plays: what the Plugin buffer holds for the old one is dropped
        (_RenderQueue.follow_loop), so the sound follows the selection at once, in step with the playhead."""
        with self._lock:
            self.loop = _clean_loop(loop)
            loop, queue = self.loop, self._queue if self._mode == "file" and not self._paused else None
        if queue is not None and queue.follow_loop(loop) and not queue.frames:
            with self._lock:
                self.buffering = True  # nothing left to play until it's rendered again: the playhead waits
                emit("position", position_s=round(self.position, 3), buffering=True)

    def record_on(self, path: str, duration_s: float) -> None:
        """Arms the recording: while the file plays through the plugin, what
        comes out is written to `path` (see _Recorder)."""
        if not path or not duration_s or float(duration_s) <= 0:
            raise ValueError("record needs the file's path and length")
        with self._lock:
            self.recorder.want = {"path": str(path), "duration_s": float(duration_s)}
            mixed, self.mix = self.mix is not None, None  # it's about to be overwritten: the file alone plays on
        if mixed:
            emit("mix", on=False)
        emit("record", on=True, path=str(path))

    def record_off(self) -> None:
        """Disarms the recording and closes its file (the backend may delete it then)."""
        with self._lock:
            armed = self.recorder.want is not None
            self.recorder.want = None
            dirty = self.recorder.close()
        if armed or dirty:
            emit("record", on=False, dirty=dirty)

    def set_tap(self, path: str) -> None:
        """Creates the analysis ring at `path` (what plays from the next start writes into it)."""
        from app import analysis_tap

        self.close_tap()
        self._tap = analysis_tap.TapWriter(str(path))

    def close_tap(self) -> None:
        """Lets go of the ring file (Windows can't delete a mapped file): before the worker exits."""
        tap, self._tap = self._tap, None
        if tap is not None:
            tap.close()

    def _tap_start(self, rate: int, mode: int, has_before: bool = True):
        """Starts the ring for this playback (None without one); returns the writer."""
        tap = self._tap
        if tap is not None:
            tap.start(rate, mode, has_before)
        return tap

    def test_tone(self) -> dict:
        """Mixes the test tone into what plays now; returns its stream info."""
        with self._lock:
            info = dict(self.stream_info) if self.stream_info and self._mode and not self._paused else None
        if info is None:
            raise RuntimeError("nothing is playing")
        self.tone.start()
        return info

    def live(self, audio: dict, rate: int) -> None:
        """Plays the live input (at `rate`) through the plugin until stopped."""
        stopped = self.stop()
        if stopped == "file":
            emit("stopped", finished=False, position_s=round(self.position, 3))
        self.record_off()
        started, result = threading.Event(), {}
        with self._lock:
            self._gen += 1
            gen = self._gen
            self._mode, self._paused, self._seek_to = "live", False, None
            self._live_rate = int(rate or 48000)
        self._thread = threading.Thread(
            target=self._run_live, args=(gen, audio, int(rate or 48000), started, result), daemon=True)
        self._thread.start()
        started.wait(timeout=5)
        if result.get("error"):
            raise RuntimeError(result["error"])

    def stop(self) -> str | None:
        """Ends the current playback. Returns what was playing ('file', 'live' or None)."""
        with self._lock:
            mode = self._mode
            self._gen += 1
            self._mode, self._paused, self._seek_to = None, False, None
            self._wake.notify_all()
        if self._thread and self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=3)
        return mode

    def stop_live(self) -> bool:
        with self._lock:
            live = self._mode == "live"
        return bool(live and self.stop())

    def pause(self) -> None:
        with self._lock:
            if self._mode == "live":
                raise RuntimeError("the browser is playing: pause it there")
            if self._mode == "file" and not self._paused:
                self._paused = True
                emit("paused", position_s=round(self.position, 3))

    def resume(self) -> None:
        with self._lock:
            if self._mode == "live":
                raise RuntimeError("the browser is playing: control it there")
            if self._mode == "file" and self._paused:
                self._paused = False
                self._wake.notify_all()
                emit("resumed", position_s=round(self.position, 3))

    def seek(self, seconds: float) -> None:
        with self._lock:
            if self._mode != "file":
                raise RuntimeError("nothing is playing")
            self._seek_to = self.position = max(0.0, float(seconds))
            if self.until and not self.until[0] - 0.001 <= self.position < self.until[1]:
                self.until = None  # moved out of the selection: plays on
            self._wake.notify_all()
            # Rendered again from there before it's heard: the playhead waits for it (buffering).
            self.buffering = not self._paused
            emit("position", position_s=round(self.position, 3), buffering=self.buffering)

    def set_bypass(self, on: bool) -> None:
        with self._lock:
            self.bypass = bool(on)
            emit("bypass", on=self.bypass)

    @staticmethod
    def _open_stream(sd, audio: dict, notes: list):
        """The output stream for the audio settings `audio` {driver, device,
        samplerate, blocksize, safe} (see stream_latency for the latency and
        Safe Mode). Rather than failing, falls back, each step explained in
        `notes`: the device's own rate, shared mode (exclusive refused), then
        the system default device. Returns (stream, rate, block, info)."""
        driver = audio.get("driver") if audio.get("driver") in DRIVERS else "wasapi"
        api = DRIVERS[driver]
        wanted = (audio.get("device") or "").strip()
        rate = int(audio.get("samplerate") or 48000)
        block = int(audio.get("blocksize") or 1024)
        safe = bool(audio.get("safe"))
        system = find_output_device(sd, "")
        if api == "ASIO" and not has_asio(sd):
            notes.append("ASIO applies from the next plugin you open (this one started its sound with another "
                         "driver type); playing through the system default until then.")
            driver, device = "wasapi", system
        else:
            device = find_output_device(sd, wanted, driver)
            if device is not None and device["hostapi"] != api:
                # Saved under another driver type: its namesake under this one, else this one's default.
                device = find_output_device(sd, device["name"], driver)
                if device is None or device["hostapi"] != api:
                    device = find_output_device(sd, "", driver)
            if device is None:
                device = system
                driver = "wasapi" if device and device["hostapi"] == "Windows WASAPI" else driver
                if wanted:
                    notes.append(f"The output device '{wanted.rsplit(', ', 1)[0]}' isn't connected; "
                                 "playing through the system default.")

        def name(d):
            return d["name"] if d else "the default device"

        attempts = [(device, rate, driver, None)]
        if device and int(device["default_samplerate"]) != rate:
            own = int(device["default_samplerate"])
            attempts.append((device, own, driver, f"{name(device)} refused {rate:,} Hz; playing at {own:,} Hz."))
        if driver == "wasapi_exclusive":
            attempts.append((device, rate, "wasapi", f"{name(device)} refused exclusive mode (another app may "
                                                     "hold it); playing in shared mode."))
        if system and (device is None or system["index"] != device["index"]):
            attempts.append((system, rate, "wasapi", f"{name(device)} couldn't play; playing through the system "
                                                     f"default ({system['name']})."))
        error = None
        for dev, r, drv, note in attempts:
            try:
                out = sd.OutputStream(samplerate=r, channels=2, dtype="float32", blocksize=block,
                                      device=dev["index"] if dev else None, extra_settings=stream_extra(sd, dev, drv),
                                      latency=stream_latency(block, r, safe, dev))
            except (sd.PortAudioError, ValueError) as e:
                error = error or e
                continue
            if note:
                notes.append(note)
            info = {"driver": drv, "device": dev["id"] if dev else "", "device_name": name(dev),
                    "hostapi": dev["hostapi"] if dev else "", "rate": r, "block": block, "safe": safe,
                    "buffer_ms": round(block * 1000 / r, 1), "latency_ms": round(float(out.latency) * 1000, 1)}
            return out, r, block, info
        raise error or RuntimeError("no output device")

    def _run_file(self, gen, source, start_s, audio, started: threading.Event) -> None:
        """The output thread of file playback: opens the stream and takes the rendered blocks from the
        Plugin buffer as the device needs them (the render thread, _render_file, runs the plugin ahead)."""
        import numpy as np

        com = _com_init()  # this thread opens the stream (ASIO needs COM here)
        sd = sounddevice_module(audio.get("driver") == "asio")
        decoder = None
        along = None  # the recording playing along (self.mix), read in step with the file
        queue = render = None  # the Plugin buffer, and the render thread filling it (it owns decoder and along)
        adaptive = False
        finished = False
        boost = None
        try:
            notes: list[str] = []
            out, rate, block, info = self._open_stream(sd, audio, notes)
            try:
                target, adaptive = render_target(audio.get("buffer"), rate, block, False, self._learned_s)
                queue = _RenderQueue(rate, block, target, adaptive)
                info = {**info, "buffer": str(audio.get("buffer") or "auto"),
                        "target_ms": round(target * 1000 / rate, 1)}
                decoder = _open_decoder(source, rate)
                if start_s > 0:
                    decoder.seek(start_s)
                if self.mix:
                    try:
                        along = _open_decoder(self.mix["path"], rate)
                        if start_s > 0:
                            along.seek(start_s)
                    except Exception:  # noqa: BLE001 - unreadable: the file alone plays
                        along = None
                        with self._lock:
                            self.mix = None
                self._fresh_start()  # no tail of a previous track
                running = False  # the stream starts with the first block (not at all if paused at once)
                with self._lock:
                    if gen != self._gen:
                        return
                    self.stream_info = info
                    emit("playing", source=source, file=os.path.basename(source), duration_s=decoder.duration,
                         position_s=round(self.position, 3), paused=self._paused, note=" ".join(notes), audio=info,
                         dry=self.dry, loop=list(self.loop) if self.loop else None, mix=self.mix is not None)
                started.set()
                boost = _pro_audio_thread()
                if self.accepts_midi:
                    self.midi.listen(True)
                tap = self._tap_start(rate, 5 if self.dry else 1)
                render = threading.Thread(target=self._render_file,
                                          args=(gen, queue, decoder, along, rate, block, start_s), daemon=True)
                render.start()  # from here the render thread owns decoder and along
                with self._lock:
                    if gen == self._gen:
                        self._queue, self.buffering = queue, True
                silence = np.zeros((block, 2), dtype=np.float32)
                gain, xruns, peak, after_silence = _test_gain(), 0, 0.0, False
                loop = until = None
                last_report = time.monotonic()
                while True:
                    with self._lock:
                        if gen != self._gen:
                            break
                        seek_to, self._seek_to = self._seek_to, None
                        paused, loop, until = self._paused, self.loop, self.until
                    if seek_to is not None:
                        queue.flush(seek_to=seek_to)
                    if paused:
                        if running:
                            out.abort()  # silence now, drop what's buffered
                            running = False
                            # What was buffered was never heard: the render thread starts again from what was.
                            with self._lock:
                                heard_at = self.position
                            queue.flush(seek_to=heard_at)
                        with self._wake:
                            if gen == self._gen and self._paused and self._seek_to is None:
                                self._wake.wait(timeout=0.5)
                        continue
                    if not running:
                        out.start()
                        running = True
                    chunk = queue.get()
                    heard = None
                    if chunk is None:
                        out.write(silence)  # priming, or the plugin fell behind: nothing to play yet
                        after_silence = True
                    elif chunk.end:
                        finished = not queue.failed
                        break
                    else:
                        sound = chunk.sound
                        if tap is not None:  # what is heard now (the render thread ran ahead of this)
                            try:
                                tap.write(chunk.before, sound, chunk.bypass)
                            except Exception:  # noqa: BLE001 - the analysis ring is not worth the playback
                                tap = None
                        if after_silence:  # no click where the sound comes back after a silence
                            sound, after_silence = fade_in(sound, rate), False
                        sound = self.tone.mix(sound, rate)
                        peak = max(peak, float(np.abs(sound).max()))
                        if gain != 1.0:
                            sound = sound * gain
                        if out.write(np.ascontiguousarray(sound.T, dtype=np.float32)):
                            xruns += 1  # the device ran dry before this block arrived
                        # What's audible lags what's written by the stream's output latency.
                        heard = max(chunk.at, chunk.at + chunk.n / rate - out.latency)
                    with self._lock:
                        if gen != self._gen:
                            break
                        if heard is not None:
                            self.position = heard
                        now = time.monotonic()
                        # Waiting for the buffer (heard is None) or playing again: said at once, so the page's
                        # playhead stops while nothing is heard and goes on with the sound (not on a guess).
                        changed = self.buffering != (heard is None)
                        self.buffering = heard is None
                        if changed or now - last_report >= self.REPORT_EVERY:
                            last_report = now
                            emit("position", position_s=round(self.position, 3), xruns=xruns, peak_out=round(peak, 5),
                                 rec_dirty=self.recorder.take_dirty(), **self._gain_report(), **queue.take_stats(),
                                 latency_ms=round((out.latency + queue.target / rate) * 1000, 1),
                                 buffering=self.buffering)
                            peak = 0.0
                if finished:
                    if running:
                        out.stop()  # let the last buffered blocks play out
                    end = until[1] if until and not loop else decoder.duration
                    if end:
                        with self._lock:
                            if gen == self._gen:
                                self.position = min(end, decoder.duration or end)
            finally:
                with self._lock:
                    if self._queue is queue:
                        self._queue, self.buffering = None, False
                if queue is not None:
                    queue.close()  # the render thread stops at its next step
                if render is not None:
                    render.join(timeout=3)
                if adaptive:  # the next play with this plugin starts with what this one needed
                    self._learned_s = max(self._learned_s, queue.target / rate)
                if self.accepts_midi:
                    self.midi.listen(False)
                out.close()
        except Exception as e:  # noqa: BLE001 - report anything, keep the editor alive
            self._say(gen, "error", message=f"Playback failed: {e}")
            finished = False
        finally:
            _normal_thread(boost)
            _com_done(com)
            started.set()
            if render is None:  # the render thread never started: the decoders are still ours to close
                if decoder:
                    decoder.close()
                if along is not None:
                    along.close()
            with self._lock:
                ended = gen == self._gen
                if ended:
                    self._mode, self._paused = None, False
                    self.stream_info = None
                    tap_now = self._tap  # read once: close_tap() may run at quit
                    if tap_now is not None:
                        tap_now.idle()
                    emit("stopped", finished=finished, position_s=round(self.position, 3))
            if ended:
                self.record_off()  # finished (or failed): the recording stops with it

    def _render_file(self, gen, queue: _RenderQueue, decoder, along, rate: int, block: int, start_s: float) -> None:
        """The render thread of file playback: the source through the plugin into `queue`, ahead of the
        device (the output thread in _run_file takes the blocks). Owns `decoder` and `along`: closes them."""
        import numpy as np

        com = _com_init()
        boost = _pro_audio_thread()
        base, written, ended, since = start_s, 0, False, time.perf_counter()
        align = _Align(rate)  # plugin delay compensation: what comes out is put at the file time it came from
        feed = 0  # the stretch of input the plugin is fed now: every seek / jump starts another (see emit)
        expect = None  # the file time the next audio out should have (None: whatever comes, after a seek)
        epoch = queue.epoch

        def go_to(t: float) -> None:
            nonlocal base, written, ended, feed, expect
            decoder.seek(t)
            if along is not None:
                along.seek(t)
            base, written, ended = t, 0, False
            feed += 1  # what the plugin still holds of the input before this is dropped as it comes out
            expect = t

        def emit_out(wet, n_out: int, bypass: bool, loop) -> float | None:
            """Puts what the plugin just handed back into the queue, each stretch at the file time of the
            input it came from (_Align), and records it there. Bypassed, the input itself (after IN) is played,
            delayed the same way, so switching is seamless. Output that is still the plugin's start-up latency,
            the tail of the silence fed after the end, or from before a seek, is dropped. Output that was
            rendered for a loop that has changed since (past the new end, or a jump back it doesn't make)
            stops it: returns where to go on from, else None."""
            nonlocal expect
            eps = 3.0 / rate
            for seg in align.match(n_out, delay):
                if seg.at is None or seg.epoch != feed:
                    continue
                at, k = seg.at, seg.o1 - seg.o0
                if expect is not None and abs(at - expect) > eps:
                    wrap = loop and abs(expect - loop[1]) < 0.002 and abs(at - loop[0]) < 0.002
                    if not wrap:
                        return loop[0] if loop and expect >= loop[1] - eps else expect
                if loop and at >= loop[1] - eps:
                    return loop[0]
                cut = int(round((loop[1] - at) * rate)) if loop and at + k / rate > loop[1] + eps else k
                sent = seg.sent[:, :cut]
                sound = self.gains.post(sent if bypass else wet[:, seg.o0:seg.o0 + cut], sent, rate,
                                        balance=not bypass)
                with self._lock:
                    if gen == self._gen and self.recorder.want:
                        opened = self.recorder.ensure(rate)
                        if opened:
                            emit("record", **opened)
                        self.recorder.write(int(round(at * rate)), sound)
                queue.put(_Chunk(sound, before=seg.data[:, :cut], at=at, bypass=bypass), epoch)
                expect = at + cut / rate
                if cut < k:
                    return loop[0]
            return None

        def finish(bypass: bool) -> None:
            """The source ended: silence goes through the plugin until what it still holds of the file has
            come out (a plugin with latency hands its last part back later), then the end goes in."""
            nonlocal delay
            for _ in range(int(delay / block) + 8):
                if align.pending(delay) <= 0 or queue.closed or gen != self._gen:
                    break
                silent = np.zeros((2, block), dtype=np.float32)
                sent = self.gains.pre(silent)
                wet = self._process(sent, rate)
                delay = self._plugin_delay()
                align.feed(None, feed, silent, sent)
                if emit_out(wet, wet.shape[1], bypass, None) is not None:
                    break
            queue.put(_Chunk(end=True), epoch)

        delay = self._plugin_delay()
        try:
            while gen == self._gen and not queue.closed:
                epoch, seek = queue.take_seek()
                if seek is not None:  # a seek (or a pause): render from there
                    go_to(seek)
                    expect = None
                if ended:  # the end is queued: nothing more to render until a seek or the stop
                    queue.wait_flush(epoch, 0.1)
                    continue
                if not queue.wait_room(0.1):  # the buffer is full: the device has the rest
                    continue
                with self._lock:
                    if gen != self._gen:
                        break
                    bypass, envelope, loop = self.bypass, self.envelope, self.loop
                    dry, until, mix = self.dry, self.until, self.mix
                if along is not None and mix is None:
                    along.close()  # dropped (recording again overwrites it)
                    along = None
                data = decoder.read(block)
                at = base + written / rate  # the file time of this block's first frame
                if loop:
                    # Looping the selection: cut the block at its end, then go back to its start
                    # (at the file's end too, unless nothing was played since the last jump).
                    start, end = loop
                    if data is not None and at < end:
                        keep = int(round((end - at) * rate))
                        if keep < data.shape[1]:
                            data = data[:, :max(keep, 0)]
                    if data is None or at >= end or not data.shape[1]:
                        if data is None and at <= start + 0.01:
                            if dry:
                                queue.put(_Chunk(end=True), epoch)
                            else:
                                finish(bypass)
                            ended = True
                            continue
                        # The plugin goes on hearing the file (from the loop's start): what it hands back
                        # wraps in step (emit_out), so no new stretch begins here.
                        decoder.seek(start)
                        if along is not None:
                            along.seek(start)
                        base, written = start, 0
                        continue
                elif until and data is not None:
                    # A selection played once: cut at its end, then finish.
                    keep = int(round((until[1] - at) * rate))
                    if keep <= 0:
                        data = None
                    elif keep < data.shape[1]:
                        data = data[:, :keep]
                if data is None:
                    if dry:
                        queue.put(_Chunk(end=True), epoch)
                    else:
                        finish(bypass)
                    ended = True
                    continue
                if envelope:  # the Audio tab's fades / Normalize, before the plugin
                    times = at + np.arange(data.shape[1], dtype=np.float64) / rate
                    data = data * envelope_gain(envelope, times)[None, :]
                if along is not None:
                    # The recording (already edited when it was made) joins the file, per the M buttons.
                    n = data.shape[1]
                    extra = along.read(n)
                    if extra is not None and extra.shape[1] < n:
                        extra = np.pad(extra, ((0, 0), (0, n - extra.shape[1])))
                    data = data * np.float32(mix["source"])
                    if extra is not None:
                        data = data + extra * np.float32(mix["recording"])
                n = data.shape[1]
                tick = time.perf_counter()
                resume = None
                if dry:
                    sound = np.asarray(data, dtype=np.float32)  # the recording, as it was recorded
                    queue.put(_Chunk(sound, before=sound, at=at, bypass=bypass), epoch)
                else:
                    # Bypass plays the dry signal but still runs the plugin, so its
                    # state (envelopes, tails) stays warm for switching back.
                    sent = self.gains.pre(data)
                    skip = bypass and self.no_output  # an analyzer bypassed: no longer fed (heard dry anyway)
                    wet = None if skip else self._process(sent, rate, self._midi_for(tick, since, n))
                    delay = 0 if skip else self._plugin_delay()
                    align.feed(at, feed, data, sent)
                    resume = emit_out(wet, n if skip else wet.shape[1], bypass, loop)
                since = tick
                dt = time.perf_counter() - tick
                queue.note_render(dt, n)
                if dt > 0.5 * queue.target / rate:  # a block that slow may not be covered by the buffer next time
                    queue.grow(int(2 * dt * rate) + block)
                written += n
                if resume is not None:  # it rendered on for a loop that has changed since: go where it goes now
                    go_to(resume)
        except Exception as e:  # noqa: BLE001 - report anything: the output thread must end, not play silence
            queue.failed = True
            self._say(gen, "error", message=f"Playback failed: {e}")
            queue.put(_Chunk(end=True), queue.epoch)
        finally:
            decoder.close()
            if along is not None:
                along.close()
            _normal_thread(boost)
            _com_done(com)

    def _run_live(self, gen, audio, rate, started: threading.Event, result: dict) -> None:
        """The output thread of live input: opens the stream and takes the rendered blocks from the Plugin
        buffer as the device needs them (the render thread, _render_live, runs the plugin ahead)."""
        import numpy as np

        com = _com_init()  # this thread opens the stream (ASIO needs COM here)
        sd = sounddevice_module(audio.get("driver") == "asio")
        buffer = self.live_input.buffer
        queue = render = None  # the Plugin buffer, and the render thread filling it
        adaptive = False
        boost = None
        try:
            notes: list[str] = []
            # Opened at the browser's rate when the device takes it.
            out, out_rate, block, info = self._open_stream(sd, {**audio, "samplerate": rate}, notes)
            try:
                resample = (_Resampler if _have_av() else _SincResampler)(rate, out_rate) if out_rate != rate else None
                target, adaptive = render_target(audio.get("buffer"), rate, block, True, self._learned_live_s)
                queue = _RenderQueue(rate, block, target, adaptive)
                info = {**info, "buffer": str(audio.get("buffer") or "auto"),
                        "target_ms": round(target * 1000 / rate, 1)}
                buffer.configure(limit=2 * target + 4 * BROWSER_CHUNK)
                buffer.active = True
                self._fresh_start()
                with self._lock:
                    if gen != self._gen:
                        return
                    self.stream_info = info
                    emit("live_started", rate=rate, out_rate=out_rate, block=block, note=" ".join(notes), audio=info)
                started.set()
                boost = _pro_audio_thread()
                if self.accepts_midi:
                    self.midi.listen(True)
                out.start()
                tap = self._tap_start(rate, 2)  # at the browser's rate: before the resampling
                render = threading.Thread(target=self._render_live,
                                          args=(gen, queue, buffer, rate, block, resample), daemon=True)
                render.start()
                silence = np.zeros((block, 2), dtype=np.float32)  # the device's silence
                gain, xruns, frames_out, peak_out, after_silence = _test_gain(), 0, 0, 0.0, False
                last_report = time.monotonic()
                while True:
                    with self._lock:
                        if gen != self._gen:
                            break
                    chunk = queue.get()
                    if chunk is None:
                        out.write(silence)  # not primed yet, or the browser's audio stopped coming
                        after_silence = True
                    elif chunk.end:
                        break
                    else:
                        if tap is not None:
                            try:
                                tap.write(chunk.before, chunk.sound, chunk.bypass)
                            except Exception:  # noqa: BLE001 - the analysis ring is not worth the playback
                                tap = None
                        device = chunk.device
                        if after_silence and device.shape[1]:  # no click where the sound comes back
                            device, after_silence = fade_in(device, out_rate), False
                        if device.shape[1]:  # the resampler may hold a block back for a moment
                            device = self.tone.mix(device, out_rate)
                            peak_out = max(peak_out, float(np.abs(device).max()))
                            if gain != 1.0:
                                device = device * gain
                            if out.write(np.ascontiguousarray(device.T, dtype=np.float32)):
                                xruns += 1
                        frames_out += chunk.n
                    now = time.monotonic()
                    if now - last_report >= self.REPORT_EVERY:
                        last_report = now
                        stats = buffer.stats(rate, queue.starves)
                        ahead = queue.take_stats()
                        self._say(gen, "live", **stats, **ahead, **self._gain_report(), frames_out=frames_out,
                                  peak_out=round(peak_out, 5),
                                  latency_ms=round(ahead["ahead_ms"] + stats["buffer_ms"] + out.latency * 1000, 1),
                                  connected=self.live_input.connected, xruns=xruns)
                        peak_out = 0.0
            finally:
                buffer.active = False
                if queue is not None:
                    queue.close()  # the render thread stops at its next step
                if render is not None:
                    render.join(timeout=3)
                if adaptive:
                    self._learned_live_s = max(self._learned_live_s, queue.target / rate)
                if self.accepts_midi:
                    self.midi.listen(False)
                out.close()
        except Exception as e:  # noqa: BLE001 - report anything, keep the editor alive
            result["error"] = f"Couldn't play the browser's audio: {e}"
            if started.is_set():  # failed later: nobody is waiting for the result
                self._say(gen, "error", message=result["error"])
        finally:
            _normal_thread(boost)
            _com_done(com)
            started.set()
            with self._lock:
                if gen == self._gen:
                    self._mode = None
                    self.stream_info = None
                    tap_now = self._tap  # read once: close_tap() may run at quit
                    if tap_now is not None:
                        tap_now.idle()
                    emit("live_stopped")

    def _render_live(self, gen, queue: _RenderQueue, buffer: _LiveBuffer, rate: int, block: int, resample) -> None:
        """The render thread of live input: the browser's frames through the plugin into `queue`, ahead of
        the device. When the input stops (the page went away) silence is rendered instead, so the tails
        ring out and the queue stays full: input that resumes is heard at the buffer's latency, not after
        a gap."""
        import numpy as np

        com = _com_init()
        boost = _pro_audio_thread()
        epoch = queue.epoch  # live input is never flushed: one epoch for the whole run
        align = _Align(rate)  # the Analyzer's Before is delayed like the plugin delays After (see _Align)
        silence = np.zeros((2, block), dtype=np.float32)
        last_input, since = time.monotonic(), time.perf_counter()
        gap_target = None  # the queue's target when the input last stopped coming
        try:
            while gen == self._gen and not queue.closed:
                if not queue.wait_room(0.05):
                    continue
                data = buffer.take(block)
                now = time.monotonic()
                if data is not None:
                    if gap_target is not None and now - last_input >= LIVE_IDLE_S:
                        # The page had stopped sending (a navigation): running dry then wasn't the plugin
                        # or the page's jitter, so whatever Auto grew for it is undone.
                        queue.set_target(gap_target)
                    gap_target = None
                    last_input = now
                else:
                    if gap_target is None:
                        gap_target = queue.target  # input stopped coming: the target before this gap
                    if now - last_input < LIVE_IDLE_S:
                        buffer.wait(block / rate)  # the next block's worth of input, or the next push
                        continue
                    data = silence
                with self._lock:
                    if gen != self._gen:
                        break
                    bypass = self.bypass
                tick = time.perf_counter()
                sound = self._through(data, rate, self._midi_for(tick, since, block), bypass)
                since = tick
                dt = time.perf_counter() - tick
                queue.note_render(dt, block)
                if dt > 0.5 * queue.target / rate:  # a block that slow may not be covered by the buffer next time
                    queue.grow(int(2 * dt * rate) + block)
                align.feed(None, 0, data)
                # Bypassed, the dry input is heard as it is: Before is that same block.
                before = data if bypass else align.before(sound.shape[1], self._plugin_delay())
                device = resample(sound) if resample else sound
                queue.put(_Chunk(sound, before=before, bypass=bypass, device=device), epoch)
        except Exception as e:  # noqa: BLE001 - report anything: the output thread must end, not play silence
            self._say(gen, "error", message=f"Couldn't play the browser's audio: {e}")
            queue.put(_Chunk(end=True), queue.epoch)
        finally:
            _normal_thread(boost)
            _com_done(com)

    def _run_instrument(self, gen, audio, started: threading.Event, result: dict) -> None:
        """The instrument's sound: renders block after block from the MIDI that
        arrived meanwhile (silence while nothing plays)."""
        import numpy as np

        com = _com_init()  # this thread opens the stream (ASIO needs COM here)
        sd = sounddevice_module(audio.get("driver") == "asio")
        boost = None
        try:
            notes: list[str] = []
            out, rate, block, info = self._open_stream(sd, audio, notes)
            try:
                with self._lock:
                    if gen != self._gen:
                        return
                    self.stream_info = info
                    emit("instrument_started", note=" ".join(notes), audio=info)
                started.set()
                boost = _pro_audio_thread()
                self.midi.listen(True)
                silence = np.zeros((2, block), dtype=np.float32)
                gain, xruns, frames_out, peak_out = _test_gain(), 0, 0, 0.0
                since, last_report = time.perf_counter(), time.monotonic()
                out.start()
                tap = self._tap_start(rate, 3, False)  # an instrument has no input
                while True:
                    with self._lock:
                        if gen != self._gen:
                            break
                        bypass = self.bypass
                    tick = time.perf_counter()
                    events = self.midi.drain(tick, since, block)
                    since = tick
                    wet = self._process(silence, rate, events)
                    # An instrument has no dry signal: bypassed = muted.
                    sound = self.gains.post(silence if bypass else wet, None, rate, balance=not bypass)
                    if tap is not None:
                        try:
                            tap.write(None, sound, bypass)
                        except Exception:  # noqa: BLE001 - the analysis ring is not worth the playback
                            tap = None
                    peak_out = max(peak_out, float(np.abs(sound).max()))
                    sound = self.tone.mix(sound, rate)
                    if gain != 1.0:
                        sound = sound * gain
                    if out.write(np.ascontiguousarray(sound.T, dtype=np.float32)):
                        xruns += 1
                    frames_out += block
                    now = time.monotonic()
                    if now - last_report >= self.REPORT_EVERY:
                        last_report = now
                        self._say(gen, "instrument", **self._gain_report(), peak_out=round(peak_out, 5), xruns=xruns,
                                  frames_out=frames_out,
                                  held=len(self.midi.held), latency_ms=round(float(out.latency) * 1000, 1))
                        peak_out = 0.0
            finally:
                self.midi.listen(False)
                out.close()
        except Exception as e:  # noqa: BLE001 - report anything, keep the editor alive
            result["error"] = f"Couldn't start the instrument's sound: {e}"
            if started.is_set():
                self._say(gen, "error", message=result["error"])
        finally:
            _normal_thread(boost)
            _com_done(com)
            started.set()
            with self._lock:
                if gen == self._gen:
                    self._mode = None
                    self.stream_info = None
                    tap_now = self._tap  # read once: close_tap() may run at quit
                    if tap_now is not None:
                        tap_now.idle()
                    emit("instrument_stopped")


# ---------- main ----------

def _commands(plugin, player: Player, close_editor: threading.Event) -> None:
    for line in sys.stdin:
        msg, failure = {}, None
        try:
            msg = json.loads(line)
            cmd = msg.get("cmd")
            audio = {k: msg.get(k) for k in ("device", "samplerate", "blocksize", "driver", "safe", "buffer")}
            if cmd == "midi":  # the on-screen keyboard: [[status, data1, data2], ...]
                for event in msg.get("events") or []:
                    player.midi.push(bytes(int(b) & 0xFF for b in event[:3]))
            elif cmd == "play":
                player.play(msg["source"], msg.get("start_s") or 0, audio, envelope=msg.get("envelope"),
                            loop=msg.get("loop"), dry=bool(msg.get("dry")), until=msg.get("until"), mix=msg.get("mix"))
            elif cmd == "mix":  # the M buttons while the recording plays along
                player.set_mix(msg.get("source"), msg.get("recording"))
            elif cmd == "edit":  # the Audio tab's edits (fades, Normalize) while it plays
                player.set_envelope(msg.get("envelope"))
            elif cmd == "loop":
                player.set_loop(msg.get("range"))
            elif cmd == "record":
                if msg.get("on", True):
                    player.record_on(msg.get("path"), msg.get("duration_s"))
                else:
                    player.record_off()
            elif cmd == "live":
                if msg.get("on", True):
                    player.live(audio, msg.get("rate") or msg.get("samplerate") or 48000)
                elif player.stop_live():
                    emit("live_stopped")
            elif cmd == "instrument":
                if msg.get("on", True):
                    player.instrument(audio)
                elif player.stop() == "instrument":
                    emit("instrument_stopped")
            elif cmd == "reconfigure":
                player.reconfigure(audio)
            elif cmd == "panic":
                player.midi.panic()
            elif cmd == "midi_inputs":
                player.midi.set_block_pitchbend(bool(msg.get("block_pitchbend")))
                emit("midi_inputs", **player.midi.set_inputs(msg.get("inputs"), int(msg.get("channel") or 0)))
            elif cmd == "tone":
                emit("tone", audio=player.test_tone())
            elif cmd == "tap":  # the analysis ring's file (app/analysis_tap.py)
                player.set_tap(msg["path"])
            elif cmd == "pause":
                player.pause()
            elif cmd == "resume":
                player.resume()
            elif cmd == "seek":
                player.seek(msg["seconds"])
            elif cmd == "bypass":
                player.set_bypass(msg.get("on", False))
            elif cmd == "stop":
                mode = player.stop()
                player.record_off()
                emit("stopped", finished=False, position_s=round(player.position, 3))
                if mode in ("live", "instrument"):
                    emit(f"{mode}_stopped")
            elif cmd == "capture":
                how = capture_editor(msg["path"])
                emit("captured", path=msg["path"], method=how)
            elif cmd == "gain":  # the frame's IN / OUT knobs and Auto-Balance (any of them)
                emit("gain", **player.gains.set(msg.get("in_db"), msg.get("out_db"), msg.get("auto_balance")))
            elif cmd == "reveal":  # a held editor (--hidden) that no frame will reveal
                _hold.request_reveal("command")
            elif cmd == "quit":
                break
        except Exception as e:  # noqa: BLE001
            failure = str(e) or type(e).__name__
            if not isinstance(msg, dict) or msg.get("id") is None:
                emit("error", message=failure)
        finally:
            if isinstance(msg, dict) and msg.get("id") is not None:
                emit("ack", id=msg["id"], error=failure)
    player.stop()
    player.record_off()
    player.close_tap()  # its file can be deleted once the worker is gone
    player.midi.shutdown()  # the MIDI keyboards are free for other apps again
    close_editor.set()


# ---------- loading ----------

class LoadError(Exception):
    """A plugin that can't be opened, with a reason code the backend keeps
    (plugin_host.ISSUE_CODES) and a sentence for the user."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _options(args) -> dict:
    """--format VST3|VST2|CLAP  --name <plugin in a multi-plugin file / shell>
    --hint <catalog name, to pick in a multi-plugin file>  --waves <bundle name>
    --cache <folder for the shell-names cache>  --probe (load, process, exit)
    --hidden (wait for the frame, see _Hold)  --capture <png> (the picture job)."""
    opts = {"format": None, "name": None, "hint": None, "waves": None, "cache": None, "probe": False,
            "hidden": False, "capture": None, "rate": 48000, "block": 1024}
    items = list(args)
    i = 0
    while i < len(items):
        key = items[i]
        if key in ("--probe", "--hidden"):
            opts[key[2:]] = True
            i += 1
        elif key.startswith("--") and i + 1 < len(items):
            opts[key[2:]] = items[i + 1]
            i += 2
        else:
            i += 1
    return opts


def _format_of(path: str) -> str:
    low = path.lower().rstrip("\\/")
    if low.endswith(".clap"):
        return "CLAP"
    if low.endswith(".dll"):
        return "VST2"
    return "VST3"


def _take_stdout():
    """The event pipe, kept for ourselves. Plugins' native code writes to fd 1
    (printf, std::cout) and would corrupt the JSON lines: fd 1 (and the Win32
    standard output) now point at stderr, and events go to a private copy."""
    sys.stdout.flush()
    try:
        fd = os.dup(1)
        os.dup2(2, 1)
        return os.fdopen(fd, "w", encoding="utf-8", errors="replace", buffering=1)
    except OSError:  # no usable stderr: keep the old behaviour
        return sys.stdout


def _add_dll_dirs(path: str) -> None:
    """Lets the plugin find DLLs it ships next to itself (or in its vendor's
    folder). pedalboard/JUCE load VST3s with a plain LoadLibrary, which
    doesn't look in the plugin's own folder; PATH is searched last, so this
    never shadows a system DLL."""
    from app.hosting import deps

    binary = deps.vst3_binary(path) if _format_of(path) == "VST3" else path
    dirs = deps.search_dirs(binary or path)
    if dirs:
        os.environ["PATH"] = os.pathsep.join(dirs + [os.environ.get("PATH", "")])


def _shell_names(shell: str, cache_dir: str | None) -> list[str]:
    """The plugins a VST3 shell (a WaveShell) exposes; cached per file
    (path, size, mtime), since listing them means loading every one (~20 s)."""
    from pedalboard import VST3Plugin

    st = os.stat(shell)
    key = f"{shell.lower()}|{st.st_size}|{int(st.st_mtime)}"
    cache_file = os.path.join(cache_dir, "shell-names.json") if cache_dir else None
    cache = {}
    if cache_file and os.path.exists(cache_file):
        try:
            with open(cache_file, encoding="utf-8") as f:
                cache = json.load(f)
        except (OSError, ValueError):
            cache = {}
    if key in cache:
        return cache[key]
    global _loading_step
    _loading_step = "Listing the plugins in the Waves shell (first time only)"
    emit("status", message=f"{_loading_step}…")
    names = list(VST3Plugin.get_plugin_names_for_file(shell))
    _loading_step = "Loading the plugin"
    if cache_file:
        cache = {k: v for k, v in cache.items() if not k.startswith(shell.lower() + "|")}
        cache[key] = names
        os.makedirs(cache_dir, exist_ok=True)
        tmp = cache_file + f".{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f)
        os.replace(tmp, cache_file)
    return names


def _names_in_error(message: str) -> list[str]:
    """pedalboard lists a multi-plugin file's names in its error message."""
    if "contains" not in message or "plugin_name" not in message:
        return []
    import re

    return re.findall(r'"([^"\n]+)"', message.split("following values:", 1)[-1])


def _best_name(names: list[str], hint: str | None) -> str:
    low = (hint or "").strip().lower()
    for name in names:
        if name.lower() == low:
            return name
    for name in names:
        if low and (name.lower().startswith(low) or low.startswith(name.lower())):
            return name
    return names[0]


def _explain_vst3(error: Exception, binary: str) -> LoadError:
    """A pedalboard load failure in words: wrong bus layout, 32-bit, a missing
    DLL (by name), or a plugin that refused to start."""
    from app.hosting import deps
    from app.hosting.vst2 import describe_load_error

    text = str(error)
    if "does not support" in text and "0 output channels" in text:
        return LoadError("no_output", "it has no audio output (an analyzer or meter), "
                                      "so there is nothing to play through it")
    if "does not support" in text:
        return LoadError("bus_layout", "its channel layout isn't supported (it doesn't take stereo audio)")
    if why := deps.wrong_arch(binary):
        return LoadError("bitness", why)
    try:
        ctypes.WinDLL(binary, winmode=0x8)  # LOAD_WITH_ALTERED_SEARCH_PATH: why can't Windows load it?
    except OSError as e:
        code = {193: "bitness", 126: "missing_dll", 127: "missing_dll"}.get(getattr(e, "winerror", None), "failed")
        return LoadError(code, describe_load_error(binary, e))
    return LoadError("refused", "the plugin loaded but didn't start (it may need to be activated in its "
                                "vendor's app, or its installation is damaged)")


def _realtime(binary: str, name: str | None) -> None:
    """pedalboard runs VST3s in offline mode; analyzers (SPAN, TDR Prism, Waves
    PAZ) stop drawing then. See app/hosting/realtime.py."""
    from app.hosting import realtime

    try:
        realtime.force_realtime(binary, name)
    except Exception:  # noqa: BLE001 - best effort: the plugin still loads as before
        pass


def _load_vst3(path: str, opts: dict):
    from pedalboard import load_plugin

    from app.hosting import deps, realtime, waves

    binary = deps.vst3_binary(path)
    if binary is None:
        if "x86-win" in deps.bundle_arches(path):
            raise LoadError("bitness", "it is a 32-bit plugin (its bundle has no 64-bit binary)")
        raise LoadError("damaged", "the bundle has no Windows 64-bit binary (damaged installation?)")
    # pedalboard 0.9.26 can't open VST3 *bundle folders* on Windows ("unsupported
    # plugin format or scan failure"), but opens the binary inside them fine.
    name = opts["name"]
    if opts["waves"]:
        names = _shell_names(binary, opts["cache"])
        name = waves.pick(opts["waves"], names)
        if not name:
            raise LoadError("not_licensed", f"the Waves shell doesn't offer '{opts['waves']}' (it lists "
                                            f"{len(names)} plugins): most likely it isn't licensed — "
                                            "check Waves Central")
    timeout = 30.0 if opts["waves"] else 10.0
    try:
        _realtime(binary, name)
        try:
            return load_plugin(binary, plugin_name=name, initialization_timeout=timeout), name
        except Exception as e:  # noqa: BLE001
            mono = waves.mono_variant(name, names) if opts["waves"] and "does not support" in str(e) else None
            if mono:
                # pedalboard can't run a component whose input and output channel counts differ
                # (Waves "Mono/Stereo": 1 in, 2 out); the Mono one plays through _MonoAdapter.
                _realtime(binary, mono)
                try:
                    return load_plugin(binary, plugin_name=mono, initialization_timeout=timeout), mono
                except Exception as e2:  # noqa: BLE001
                    raise _explain_vst3(e2, binary) from None
            names = _names_in_error(str(e))
            if names and not name:  # a multi-plugin file: the one the catalog row is about
                name = _best_name(names, opts["hint"])
                _realtime(binary, name)
                try:
                    return load_plugin(binary, plugin_name=name), name
                except Exception as e2:  # noqa: BLE001
                    raise _explain_vst3(e2, binary) from None
            raise _explain_vst3(e, binary) from None
    finally:
        realtime.release_probes()  # pedalboard is done loading, loaded or not (app/hosting/realtime.py)


class _MonoAdapter:
    """A VST3 that only takes mono (Blue Cat's "(Mono)" versions, Waves "Mono"
    components) refuses VEXT's stereo blocks; it then gets L+R mixed down and
    its output is copied to both sides. Everything else goes to the plugin.

    Audio waits for the editor (`editor_ready`, set by main()): JUCE creates
    the editor on the main thread, and a plugin activated by its first block
    on the audio thread at the same time can deadlock. iZotope Insight 2 did
    (its window never appeared, while the Playground said "Open"): the
    Playground starts the browser's audio as soon as the plugin has loaded. A
    plugin activated before or after its editor is created is fine, so only
    the first audio call waits (at most EDITOR_WAIT_S)."""

    EDITOR_WAIT_S = 30.0
    _AUDIO_CALLS = ("process", "reset")

    def __init__(self, plugin):
        self._plugin = plugin
        self._mono = False
        self.editor_ready: threading.Event | None = None

    def _after_editor(self) -> None:
        ready = self.editor_ready
        if ready is not None and not ready.is_set():
            ready.wait(self.EDITOR_WAIT_S)

    def __getattr__(self, name):
        value = getattr(self._plugin, name)
        if name in self._AUDIO_CALLS and callable(value):
            def audio_call(*args, **kwargs):
                self._after_editor()
                return value(*args, **kwargs)
            return audio_call
        return value

    def __call__(self, audio, sample_rate, reset=False):
        import numpy as np

        self._after_editor()
        if not self._mono:
            try:
                return self._plugin(audio, sample_rate, reset=reset)
            except Exception as e:  # noqa: BLE001
                if "does not support 2-channel" not in str(e):
                    raise
                self._mono = True
        out = self._plugin(audio.mean(axis=0, keepdims=True).astype(np.float32), sample_rate, reset=reset)
        return np.repeat(out, 2, axis=0) if out.shape[0] == 1 else out[:2]


def _load(path: str, opts: dict):
    """(plugin, its main-thread dispatcher or None, format, chosen name)."""
    fmt = (opts["format"] or _format_of(path)).upper()
    if not os.path.exists(path):
        raise LoadError("missing", "the plugin file no longer exists")
    _add_dll_dirs(path)
    rate, block = int(opts["rate"] or 48000), int(opts["block"] or 1024)
    if fmt in ("VST2", "CLAP"):
        from app.hosting import deps, win_editor

        if why := deps.wrong_arch(path):
            raise LoadError("bitness", why)
        main_thread = win_editor.MainThread()
        try:
            if fmt == "CLAP":
                from app.hosting.clap import ClapPlugin

                plugin = ClapPlugin(path, main_thread, rate, block, plugin_name=opts["name"] or None,
                                    hint=opts["hint"] or None)
            else:
                from app.hosting import waves
                from app.hosting.vst2 import Vst2Plugin

                choose = opts["name"] or None
                if opts["waves"]:
                    bundle = opts["waves"]
                    choose = lambda names: waves.pick(bundle, names)  # noqa: E731
                plugin = Vst2Plugin(path, main_thread, rate, block, shell_name=choose)
        except LoadError:
            raise
        except Exception as e:  # noqa: BLE001
            text = str(e) or type(e).__name__
            code = ("bitness" if "32-bit" in text else "missing_dll" if "DLL" in text
                    else "not_licensed" if "licensed" in text else "failed")
            raise LoadError(code, text) from None
        if not plugin.has_editor:
            try:
                plugin.close()
            except Exception:  # noqa: BLE001
                pass
            raise LoadError("no_editor", "it has no editor window of its own (VEXT shows plugins by their editor)")
        return plugin, main_thread, fmt, getattr(plugin, "name", "")
    try:
        plugin, name = _load_vst3(path, opts)
    except LoadError as e:
        if e.code != "no_output":
            raise
        return _load_analyzer(path, opts, rate, block, e)
    return _MonoAdapter(plugin), None, "VST3", name


def _load_analyzer(path: str, opts: dict, rate: int, block: int, why: LoadError):
    """A VST3 without an audio output (analyzer, meter), which pedalboard
    can't load: VEXT's own minimal VST3 host feeds it the audio and the input
    is what's heard (app/hosting/vst3.py)."""
    from app.hosting import deps, win_editor
    from app.hosting.vst3 import Vst3Plugin

    binary = deps.vst3_binary(path) or path
    main_thread = win_editor.MainThread()
    try:
        plugin = Vst3Plugin(binary, main_thread, rate, block, hint=opts["hint"])
    except Exception as e:  # noqa: BLE001
        raise LoadError("failed", f"{why} (VEXT's analyzer host couldn't load it either: {e})") from None
    if not plugin.has_editor:
        plugin.close()
        raise LoadError("no_editor", "it has no editor window of its own (VEXT shows plugins by their editor)")
    return plugin, main_thread, "VST3", plugin.name


def _visible_windows(exclude=None) -> list[str]:
    """Titles of this process's visible top-level windows (a dialog while
    loading), except `exclude` (the editor). Cloaked windows count: shown,
    just not on the screen yet."""
    pid, titles = os.getpid(), []

    @_ENUM_PROC
    def visit(hwnd, _):
        owner = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and _user32.IsWindowVisible(hwnd) and int(hwnd or 0) != int(exclude or 0):
            buf = ctypes.create_unicode_buffer(200)
            _user32.GetWindowTextW(hwnd, buf, 200)
            titles.append(buf.value or "untitled")
        return True

    _user32.EnumWindows(visit, 0)
    return titles


_loading_step = "Loading the plugin"  # what the load is doing now, for the status line


def _watch_loading(done: threading.Event) -> None:
    """While the plugin loads: how long it's been, and any window it opened
    (registration / authorization dialogs block loading until answered)."""
    start, last = time.monotonic(), None
    while not done.wait(1.0):
        seconds = int(time.monotonic() - start)
        windows = _visible_windows()
        if windows:
            message = f"The plugin opened a window (“{windows[0]}”): answer it to finish loading."
        elif seconds >= 5:
            message = f"{_loading_step}… {seconds // 5 * 5} s"
        else:
            continue
        if message != last:
            last = message
            emit("status", message=message, dialog=bool(windows), seconds=seconds)


def _exit_now(code: int) -> None:
    """Ends the process without running DLL unload code: several plugins hang or
    crash in it (seen: Antares AVOX, Arturia REFRACT, NI Supercharger). Every
    event has been written by then."""
    try:
        _stdout.flush()
    except Exception:  # noqa: BLE001
        pass
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess(kernel32.GetCurrentProcess(), code)
    os._exit(code)  # not reached


def _release(plugin, main_thread) -> None:
    """Closes a VST2/CLAP plugin properly (main thread); a watchdog ends the
    process if that hangs."""
    close = getattr(plugin, "close", None)
    if close is None:
        return
    watchdog = threading.Timer(5.0, _exit_now, args=(0,))
    watchdog.daemon = True
    watchdog.start()
    try:
        close()
    except Exception:  # noqa: BLE001
        pass
    watchdog.cancel()


def _disable_ime() -> None:
    """Turns the Windows IME (IMM) off for every thread of this process. Must
    run before any window exists.

    JUCE 7/8 plugins (55 of the 479 binaries here: every Vox Samples plugin,
    IK Multimedia, BABY Audio, W.A.Production, Convology XT…) answer
    WM_IME_SETCONTEXT with ImmIsUIMessageW(their own window, …), and
    ImmIsUIMessageW sends the message straight back to that window: a
    recursion that only ends when win32k refuses to nest more callbacks. With
    python.exe's 2.9 MB main-thread stack that limit comes first; with the
    PyInstaller sidecar's 1.9 MB the stack overflows first, inside a window
    callback, and the worker dies with STATUS_FATAL_USER_CALLBACK_EXCEPTION
    (0xc000041d in USER32/IMM32/ntdll). It happened whenever such a plugin's
    window got or lost keyboard focus (a click in it, then a click anywhere
    else). Without IMM no window gets WM_IME_SETCONTEXT, and where one still
    does (text services: IK's Classic Comp v6), _guard_imm_is_ui_message
    makes ImmIsUIMessageW stop forwarding to non-IME windows. The cost: no IME
    composition (CJK input) in plugin text fields; typing, dead keys and
    shortcuts are unaffected."""
    try:
        imm = ctypes.WinDLL("imm32")
        imm.ImmDisableIME.argtypes = [wintypes.DWORD]
        imm.ImmDisableIME(0xFFFFFFFF)  # (DWORD)-1: all threads of this process
    except (OSError, AttributeError):
        pass
    _guard_imm_is_ui_message()


# The IME UI messages ImmIsUIMessageW forwards (from its code on Windows 11 26100):
# WM_IME_STARTCOMPOSITION, _ENDCOMPOSITION, _COMPOSITION, _SETCONTEXT, _NOTIFY,
# _COMPOSITIONFULL, _SELECT, _SYSTEM.
_IME_UI_MESSAGES = frozenset((0x10D, 0x10E, 0x10F, 0x281, 0x282, 0x284, 0x285, 0x287))
_IMM_UI_PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
_imm_hook: dict = {}


def _imm_is_ui_message(hwnd, msg, wparam, lparam):
    """ImmIsUIMessageW as Windows does it, except that it only forwards to an
    IME window: JUCE passes its own window, and forwarding there is the
    recursion _disable_ime describes."""
    if msg not in _IME_UI_MESSAGES:
        return 0
    try:
        if hwnd:
            name = ctypes.create_unicode_buffer(64)
            _user32.GetClassNameW(hwnd, name, 64)
            if "IME" in name.value.upper():
                _user32.SendMessageW(hwnd, msg, wparam, lparam)
    except Exception:  # noqa: BLE001 - never let anything unwind into a window procedure
        pass
    return 1


def _guard_imm_is_ui_message() -> None:
    """Points IMM32's ImmIsUIMessageW at _imm_is_ui_message, for every module
    of this process (a 12-byte jump over its entry). Needed beside
    ImmDisableIME: some plugins (IK Multimedia's Classic Comp v6) still get IME
    messages (text services) and recursed the same way. Only done when the
    entry looks as expected (sub rsp, imm8); otherwise ImmDisableIME alone."""
    import struct

    try:
        imm = ctypes.WinDLL("imm32")
        entry = ctypes.cast(imm.ImmIsUIMessageW, ctypes.c_void_p).value
        if not entry or ctypes.string_at(entry, 3) != b"\x48\x83\xec":
            return
        _user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        _user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        _user32.SendMessageW.restype = ctypes.c_ssize_t
        proc = _IMM_UI_PROC(_imm_is_ui_message)
        target = ctypes.cast(proc, ctypes.c_void_p).value
        code = b"\x48\xb8" + struct.pack("<Q", target) + b"\xff\xe0"  # mov rax, target; jmp rax
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.VirtualProtect.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD,
                                            ctypes.POINTER(wintypes.DWORD)]
        kernel32.FlushInstructionCache.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t]
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        old = wintypes.DWORD()
        if not kernel32.VirtualProtect(entry, len(code), 0x40, ctypes.byref(old)):  # PAGE_EXECUTE_READWRITE
            return
        ctypes.memmove(entry, code, len(code))
        kernel32.VirtualProtect(entry, len(code), old.value, ctypes.byref(wintypes.DWORD()))
        kernel32.FlushInstructionCache(kernel32.GetCurrentProcess(), entry, len(code))
        _imm_hook["proc"] = proc  # referenced for the life of the process
    except (OSError, AttributeError, ValueError):
        pass


def _prepare() -> None:
    """Process set-up, before anything can create a window."""
    global _stdout
    _disable_ime()
    _stdout = _take_stdout()
    sys.stdout = sys.stderr  # Python-level prints too
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # real pixels for capture and editor sizes
    except Exception:  # noqa: BLE001
        pass


def _standby() -> tuple[str, list[str]] | None:
    """--standby: imports what every plugin needs, says "ready", then waits for
    {"cmd":"load","path":...,"args":[...]}. None if the backend let it go."""
    import numpy  # noqa: F401 - imported now so the plugin's open doesn't wait for it
    import pedalboard  # noqa: F401

    from app.hosting import deps, midi, waves, win_editor  # noqa: F401
    from app.hosting import clap, vst2  # noqa: F401

    emit("ready", pid=os.getpid())
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("cmd") == "load" and msg.get("path"):
            return str(msg["path"]), [str(a) for a in msg.get("args") or []]
        if msg.get("cmd") == "quit":
            break
    return None


CAPTURE_WAIT_S = 30.0  # the picture job: how long an editor may take to draw itself
CAPTURE_POLL_S = 0.7
CAPTURE_SETTLE_S = 2.0  # never before this (splash screens, fade-ins)
CAPTURE_ONSCREEN_AFTER_S = 10.0  # blank while cloaked this long: try it really shown, at the bottom


def _picture_signature(image):
    """(64x40 grey thumbnail bytes, detail) of a capture: what "it drew
    something" and "it stopped changing" are judged on."""
    small = image.convert("L").resize((64, 40))
    data = small.tobytes()
    low, high = min(data), max(data)
    mean = sum(data) / len(data)
    spread = (sum((v - mean) ** 2 for v in data) / len(data)) ** 0.5
    return data, (high - low, spread)


def _auto_capture(path: str, done: threading.Event) -> None:
    """--capture: waits for the (cloaked) editor to draw itself, then saves it
    as `path` (PNG, written whole) and ends the editor loop. Gives up on a
    dialog next to the editor (a license / registration window), on an editor
    that stays blank, or after CAPTURE_WAIT_S."""
    start = time.monotonic()
    previous, last_good, tries, method = None, None, 0, "print"
    blank_since = None
    try:
        while not done.wait(CAPTURE_POLL_S):
            elapsed = time.monotonic() - start
            hwnd = _editor.find()
            others = _visible_windows(exclude=hwnd)
            if others:
                emit("error", code="dialog", message=f"it opened a window that needs an answer (“{others[0]}”)")
                return
            if not hwnd:
                if elapsed > CAPTURE_WAIT_S:
                    emit("error", code="no_editor", message="its editor window never appeared")
                    return
                continue
            width, height = _client_size(hwnd)
            if width <= 0 or height <= 0:
                continue
            tries += 1
            image = _print_window(hwnd, width, height)
            signature = _picture_signature(image) if image is not None else None
            drew = signature is not None and signature[1][0] > 24 and signature[1][1] > 4
            if not drew:
                blank_since = blank_since or time.monotonic()
                if (method == "print" and last_good is None
                        and time.monotonic() - blank_since > CAPTURE_ONSCREEN_AFTER_S):
                    _hold.uncloak_all(bottom=True)  # GPU editors that only draw on screen
                    method = "screen"
                if elapsed > CAPTURE_WAIT_S:
                    break
                continue
            blank_since = None
            last_good = image
            settled = elapsed >= CAPTURE_SETTLE_S
            if previous is not None and settled:
                diff = sum(abs(a - b) for a, b in zip(previous[0], signature[0])) / len(signature[0])
                if diff < 1.5 and signature[1][1] <= previous[1][1] * 1.05 + 0.5:
                    break  # it stopped changing (and isn't still filling in)
            previous = signature
            if elapsed > CAPTURE_WAIT_S:
                break  # still moving (meters, animations): the latest frame will do
        else:
            return  # stopped from outside
        if last_good is None:
            emit("error", code="blank", message="its window stayed blank (black or one colour)")
            return
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        last_good.save(tmp, "PNG")
        os.replace(tmp, path)
        emit("captured_auto", path=path, width=last_good.width, height=last_good.height, tries=tries,
             method=method, seconds=round(time.monotonic() - start, 1))
    except Exception as e:  # noqa: BLE001
        emit("error", code="failed", message=f"capturing failed: {e}")
    finally:
        done.set()


def _capture_commands(done: threading.Event) -> None:
    """--capture: the only command is quit (or the pipe closing)."""
    for line in sys.stdin:
        if '"quit"' in line:
            break
    done.set()


def main(plugin_path: str, *args: str) -> int:
    _prepare()
    if plugin_path == "--standby":
        job = _standby()
        if job is None:
            _exit_now(0)
        plugin_path, args = job
    return _run(plugin_path, _options(args))


def _run(plugin_path: str, opts: dict) -> int:
    # The output thread waits at most ~1 ms for the interpreter while a render thread runs Python code.
    sys.setswitchinterval(0.001)
    if opts["capture"]:
        _hold.start(forever=True)  # before loading: a license dialog stays off the screen too
    loading = threading.Event()
    threading.Thread(target=_watch_loading, args=(loading,), daemon=True).start()
    started = time.monotonic()
    try:
        plugin, main_thread, fmt, chosen = _load(plugin_path, opts)
    except LoadError as e:
        loading.set()
        emit("error", message=f"Can't open this plugin: {e}", code=e.code)
        emit("closed")
        _exit_now(1)
    except Exception as e:  # noqa: BLE001
        loading.set()
        emit("error", message=f"Can't open this plugin: {e}", code="failed")
        emit("closed")
        _exit_now(1)
    loading.set()
    load_s = round(time.monotonic() - started, 1)

    if opts["probe"]:
        # Headless check (plugin_host check): process a little silence, then quit.
        # An instrument is reported as loaded first, then rendered from MIDI the
        # way the Playground plays it: some crash there (BreakTweaker), and the
        # backend then keeps that instrument's sound off ("rendered" never comes).
        import numpy as np

        instrument = bool(getattr(plugin, "is_instrument", False))
        try:
            if not instrument:
                plugin(np.zeros((2, 4800), dtype=np.float32), 48000)
            emit("probed", ok=True, format=fmt, plugin_name=chosen, load_s=load_s, is_instrument=instrument,
                 has_editor=bool(getattr(plugin, "has_editor", True)))
        except Exception as e:  # noqa: BLE001
            emit("error", message=f"Can't open this plugin: processing failed ({e})", code="process_failed")
            instrument = False
        if instrument:
            try:
                player = Player(plugin, None)
                silence, peak = np.zeros((2, 512), dtype=np.float32), 0.0
                for i in range(30):  # ~0.3 s: a note on, then off
                    events = [(0, b"\x90\x3c\x64")] if i == 0 else [(0, b"\x80\x3c\x00")] if i == 15 else []
                    peak = max(peak, float(np.abs(player._process(silence, 48000, events)).max()))
                emit("rendered", ok=True, peak=round(peak, 5))
            except Exception as e:  # noqa: BLE001
                emit("rendered", ok=False, message=str(e))
        emit("closed")
        _exit_now(0)

    if main_thread is not None:
        _editor.owner = plugin
    if opts["capture"]:
        # The picture job: the editor, cloaked, drawn and saved; no audio, no MIDI.
        emit("loaded", is_instrument=bool(getattr(plugin, "is_instrument", False)), name=getattr(plugin, "name", ""),
             format=fmt, plugin_name=chosen, load_s=load_s, accepts_midi=False)
        done = threading.Event()
        threading.Thread(target=_capture_commands, args=(done,), daemon=True).start()
        threading.Thread(target=_watch_editor, args=(done,), daemon=True).start()
        threading.Thread(target=_auto_capture, args=(opts["capture"], done), daemon=True).start()
        try:
            plugin.show_editor(done)
        except Exception as e:  # noqa: BLE001
            emit("error", code="failed", message=f"its editor failed: {e}")
        done.set()
        emit("closed")
        _release(plugin, main_thread)
        _exit_now(0)
    if isinstance(plugin, _MonoAdapter):
        plugin.editor_ready = _editor.ready  # its audio starts once the editor is up
    if opts["hidden"]:
        _hold.start()  # the frame reveals it once placed (no window at the top-left first)
    _space.start()  # Space plays / stops in VEXT, even with the editor focused
    live_input = _LiveInput()
    player = Player(plugin, live_input)
    emit("loaded", is_instrument=player.is_instrument, name=getattr(plugin, "name", ""),
         live_port=live_input.port, live_key=live_input.key, format=fmt, plugin_name=chosen, load_s=load_s,
         accepts_midi=player.accepts_midi, no_output=player.no_output)
    close_editor = threading.Event()
    threading.Thread(target=_report_midi, args=(player.midi, close_editor), daemon=True).start()
    threading.Thread(target=_commands, args=(plugin, player, close_editor), daemon=True).start()
    threading.Thread(target=_watch_editor, args=(close_editor,), daemon=True).start()
    try:
        plugin.show_editor(close_editor)  # blocks on the main thread until closed
    except Exception as e:  # noqa: BLE001
        emit("error", message=f"Editor failed: {e}")
    _editor.ready.set()  # no editor any more: nothing for the audio to wait for
    close_editor.set()
    player.stop()
    player.midi.shutdown()
    emit("closed")
    _release(plugin, main_thread)
    _exit_now(0)
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
