"""Win32 plumbing for the plugin formats VEXT hosts itself (VST2, CLAP; see
app/hosting/vst2.py and app/hosting/clap.py): a main-thread dispatcher and the
editor window the plugin's GUI is embedded in.

Only used inside the host worker process (app/host_worker.py), on the thread
that loaded the plugin: that thread is the plugin's "main thread" (GUI and
setup calls), and runs the Windows message loop while the editor is open.

The editor window is an ordinary top-level window of the worker process, like
the one pedalboard opens for VST3, so the desktop shell adopts it the same way
(plugin_frame.rs: owner set to the frame, caption stripped, moved/hidden from
outside). The window is ours, so hiding it never closes the plugin.
"""

import collections
import ctypes
import sys
import threading
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON)]


user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
user32.RegisterClassExW.restype = wintypes.ATOM
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                   wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
user32.CreateWindowExW.restype = wintypes.HWND
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.IsWindow.argtypes = [wintypes.HWND]
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.restype = LRESULT
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
user32.SetTimer.restype = ctypes.c_size_t
user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
user32.AdjustWindowRectEx.argtypes = [ctypes.POINTER(wintypes.RECT), wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = wintypes.LONG
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                wintypes.UINT]
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
user32.LoadCursorW.restype = wintypes.HANDLE
user32.SetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
try:
    user32.GetDpiForWindow.argtypes = [wintypes.HWND]
    user32.GetDpiForWindow.restype = wintypes.UINT
except AttributeError:  # before Windows 10 1607
    pass

WM_DESTROY, WM_SIZE, WM_CLOSE, WM_TIMER = 0x0002, 0x0005, 0x0010, 0x0113
WM_APP = 0x8000
WM_RUN = WM_APP + 1  # the dispatcher's "run the queued calls"
WS_OVERLAPPED, WS_CAPTION, WS_SYSMENU, WS_THICKFRAME = 0x0, 0x00C00000, 0x00080000, 0x00040000
WS_MINIMIZEBOX, WS_MAXIMIZEBOX, WS_CLIPCHILDREN = 0x00020000, 0x00010000, 0x02000000
GWL_STYLE, GWL_EXSTYLE = -16, -20
SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE = 0x0002, 0x0004, 0x0010
CW_USEDEFAULT = -0x80000000
HWND_MESSAGE = wintypes.HWND(-3)
SW_SHOW = 5
IDC_ARROW = 32512
COLOR_BTNFACE = 15
SIZE_MINIMIZED = 2

IDLE_TIMER = 1  # the editor's idle tick (VST2 effEditIdle; also checks for close)
IDLE_MS = 20
PLUGIN_TIMER_BASE = 0x1000  # CLAP timer-support ids are offset by this


def _log(msg: str) -> None:
    try:
        sys.stderr.write(f"[win_editor] {msg}\n")
        sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass


_classes: dict[str, WNDPROC] = {}


def _register(name: str, proc) -> None:
    if name in _classes:
        return
    wndproc = WNDPROC(proc)
    wc = WNDCLASSEXW(cbSize=ctypes.sizeof(WNDCLASSEXW), lpfnWndProc=wndproc,
                     hInstance=kernel32.GetModuleHandleW(None), lpszClassName=name,
                     hCursor=user32.LoadCursorW(None, ctypes.c_void_p(IDC_ARROW)),
                     hbrBackground=wintypes.HBRUSH(COLOR_BTNFACE + 1))
    if not user32.RegisterClassExW(ctypes.byref(wc)):
        raise OSError(f"RegisterClassExW failed ({ctypes.get_last_error()})")
    _classes[name] = wndproc  # kept alive for the life of the process


class MainThread:
    """Runs calls on the thread that created it (the plugin's main thread), from
    any thread: they are queued and a message wakes that thread's message loop.
    Calls made while the main thread is busy (loading) run once it pumps
    messages. Also owns timers: `add_timer(ms, fn)`."""

    _CLASS = "VEXTHostDispatcher"

    def __init__(self):
        self.thread_id = kernel32.GetCurrentThreadId()
        self._queue: collections.deque = collections.deque()
        self._lock = threading.Lock()
        self._timers: dict[int, object] = {}
        self._handlers = {}  # window procedures of the editor windows, by hwnd
        _register(self._CLASS, self._proc)
        self.hwnd = user32.CreateWindowExW(0, self._CLASS, "VEXT host", 0, 0, 0, 0, 0, HWND_MESSAGE, None,
                                           kernel32.GetModuleHandleW(None), None)
        if not self.hwnd:
            raise OSError(f"couldn't create the host's message window ({ctypes.get_last_error()})")

    def is_main(self) -> bool:
        return kernel32.GetCurrentThreadId() == self.thread_id

    def post(self, fn) -> None:
        """Runs `fn()` on the main thread soon (never inline)."""
        with self._lock:
            self._queue.append(fn)
        user32.PostMessageW(self.hwnd, WM_RUN, 0, 0)

    def call(self, fn, timeout: float = 10.0):
        """Runs `fn()` on the main thread and returns its result (inline when
        already there). Raises what it raised, or TimeoutError."""
        if self.is_main():
            return fn()
        done, box = threading.Event(), {}

        def run():
            try:
                box["value"] = fn()
            except BaseException as e:  # noqa: BLE001 - handed to the caller
                box["error"] = e
            finally:
                done.set()

        self.post(run)
        if not done.wait(timeout):
            raise TimeoutError("the plugin's main thread didn't answer")
        if "error" in box:
            raise box["error"]
        return box.get("value")

    def run_pending(self) -> None:
        while True:
            with self._lock:
                if not self._queue:
                    return
                fn = self._queue.popleft()
            try:
                fn()
            except Exception as e:  # noqa: BLE001 - one failing call must not stop the others
                _log(f"main-thread call failed: {e!r}")

    def add_timer(self, timer_id: int, period_ms: int, fn) -> bool:
        """Calls `fn()` every `period_ms` on the main thread (main thread only)."""
        self._timers[timer_id] = fn
        return bool(user32.SetTimer(self.hwnd, PLUGIN_TIMER_BASE + timer_id, max(1, int(period_ms)), None))

    def remove_timer(self, timer_id: int) -> bool:
        self._timers.pop(timer_id, None)
        return bool(user32.KillTimer(self.hwnd, PLUGIN_TIMER_BASE + timer_id))

    def _proc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_RUN:
                self.run_pending()
                return 0
            if msg == WM_TIMER and wparam >= PLUGIN_TIMER_BASE:
                fn = self._timers.get(wparam - PLUGIN_TIMER_BASE)
                if fn:
                    fn()
                return 0
        except Exception as e:  # noqa: BLE001 - never let an exception unwind into Windows
            _log(f"dispatcher message {msg:#x} failed: {e!r}")
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def pump(self, keep_going) -> None:
        """Runs the message loop while `keep_going()` is true. An idle timer on
        the message window wakes the loop at least every IDLE_MS, so a stop
        requested from another thread is noticed quickly."""
        tick = user32.SetTimer(self.hwnd, IDLE_TIMER, IDLE_MS, None)
        msg = wintypes.MSG()
        try:
            while keep_going():
                got = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if got == 0 or got == -1:  # WM_QUIT, or an error
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
                self.run_pending()
        finally:
            if tick:
                user32.KillTimer(self.hwnd, IDLE_TIMER)


class EditorWindow:
    """The top-level window a plugin GUI is embedded in, sized by its client
    area. `on_resized(w, h)` (resizable plugins only) is called when someone
    else (the user, the desktop shell's full screen) changed the size; it
    returns the size the plugin accepted, and the window snaps to it.
    `on_idle()` runs every IDLE_MS while the window exists."""

    _CLASS = "VEXTPluginEditor"

    def __init__(self, title: str, width: int, height: int, resizable: bool, on_resized=None, on_idle=None,
                 on_close=None):
        _register(self._CLASS, EditorWindow._proc_entry)
        self.on_resized, self.on_idle, self.on_close = on_resized, on_idle, on_close
        self.closed = False
        self._setting_size = False
        self.style = WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU | WS_MINIMIZEBOX | WS_CLIPCHILDREN
        if resizable:
            self.style |= WS_THICKFRAME | WS_MAXIMIZEBOX
        w, h = self._outer(width, height)
        self.hwnd = user32.CreateWindowExW(0, self._CLASS, title or "Plugin", self.style, CW_USEDEFAULT,
                                           CW_USEDEFAULT, w, h, None, None, kernel32.GetModuleHandleW(None), None)
        if not self.hwnd:
            raise OSError(f"couldn't create the editor window ({ctypes.get_last_error()})")
        _windows[int(self.hwnd)] = self
        user32.SetTimer(self.hwnd, IDLE_TIMER, IDLE_MS, None)

    def _outer(self, width: int, height: int) -> tuple[int, int]:
        rect = wintypes.RECT(0, 0, int(width), int(height))
        user32.AdjustWindowRectEx(ctypes.byref(rect), self.style, False, 0)
        return rect.right - rect.left, rect.bottom - rect.top

    def dpi_scale(self) -> float:
        try:
            return (user32.GetDpiForWindow(self.hwnd) or 96) / 96.0
        except AttributeError:
            return 1.0

    def client_size(self) -> tuple[int, int]:
        rect = wintypes.RECT()
        user32.GetClientRect(self.hwnd, ctypes.byref(rect))
        return rect.right - rect.left, rect.bottom - rect.top

    def set_client_size(self, width: int, height: int) -> None:
        """Resizes the window so its client area is width x height. The style is
        read back: the desktop shell changes it (no min/max boxes) once it adopted it."""
        if self.closed or width <= 0 or height <= 0:
            return
        style = user32.GetWindowLongW(self.hwnd, GWL_STYLE) & 0xFFFFFFFF
        ex_style = user32.GetWindowLongW(self.hwnd, GWL_EXSTYLE) & 0xFFFFFFFF
        rect = wintypes.RECT(0, 0, int(width), int(height))
        user32.AdjustWindowRectEx(ctypes.byref(rect), style, False, ex_style)
        self._setting_size = True
        try:
            user32.SetWindowPos(self.hwnd, None, 0, 0, rect.right - rect.left, rect.bottom - rect.top,
                                SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE)
        finally:
            self._setting_size = False

    def show(self) -> None:
        user32.ShowWindow(self.hwnd, SW_SHOW)

    def destroy(self) -> None:
        if not self.closed and user32.IsWindow(self.hwnd):
            user32.DestroyWindow(self.hwnd)
        self.closed = True

    @staticmethod
    def _proc_entry(hwnd, msg, wparam, lparam):
        window = _windows.get(int(hwnd or 0))
        if window is None:
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
        try:
            return window._proc(hwnd, msg, wparam, lparam)
        except Exception as e:  # noqa: BLE001 - never let an exception unwind into Windows
            _log(f"editor message {msg:#x} failed: {e!r}")
            return 0

    def _proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_TIMER and wparam == IDLE_TIMER:
            if self.on_idle:
                self.on_idle()
            return 0
        if msg == WM_SIZE and wparam != SIZE_MINIMIZED and not self._setting_size and self.on_resized:
            width, height = lparam & 0xFFFF, (lparam >> 16) & 0xFFFF
            if width and height:
                accepted = self.on_resized(width, height)
                if accepted and tuple(accepted) != (width, height):
                    self.set_client_size(*accepted)
            return 0
        if msg == WM_CLOSE:
            # The user closed it (its own caption, before the shell adopted it):
            # the plugin's GUI goes first, then the window (see on_close).
            if self.on_close:
                self.on_close()
            else:
                self.destroy()
            return 0
        if msg == WM_DESTROY:
            self.closed = True
            user32.KillTimer(hwnd, IDLE_TIMER)
            _windows.pop(int(hwnd), None)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


_windows: dict[int, EditorWindow] = {}
