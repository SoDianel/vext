"""Read visible dialogs owned by the native-editor worker; never click them."""
import ctypes as C
from ctypes import wintypes as W
import time

user = C.WinDLL('user32', use_last_error=True)
Callback = C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
user.EnumWindows.argtypes = [Callback, W.LPARAM]
user.EnumChildWindows.argtypes = [W.HWND, Callback, W.LPARAM]
user.GetWindowThreadProcessId.argtypes = [W.HWND, C.POINTER(W.DWORD)]
user.GetClassNameW.argtypes = [W.HWND, W.LPWSTR, C.c_int]
user.GetWindowTextW.argtypes = [W.HWND, W.LPWSTR, C.c_int]
user.IsWindowVisible.argtypes = [W.HWND]
user.SendMessageTimeoutW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM,
                                    W.UINT, W.UINT, C.POINTER(C.c_size_t)]
user.SendMessageTimeoutW.restype = C.c_ssize_t


def error_text(dialog):
    """Native exception dialogs can use the worker executable's title."""
    text = " ".join([dialog["title"], *dialog["text"]])[:700]
    if (dialog["title"].casefold() in ("error", "application error")
            or any(phrase in text.casefold() for phrase in (
                "runtime error", "shader file", "access violation at address"))):
        return text
    return None


def owned_dialogs(pid):
    rows = []

    @Callback
    def visit(hwnd, arg):
        owner = W.DWORD()
        user.GetWindowThreadProcessId(hwnd, C.byref(owner))
        if owner.value != pid or not user.IsWindowVisible(hwnd):
            return True
        title = C.create_unicode_buffer(4096)
        cls = C.create_unicode_buffer(256)
        user.GetWindowTextW(hwnd, title, len(title))
        user.GetClassNameW(hwnd, cls, len(cls))
        # Standard and Delphi dialogs, plus explicit error titles. Do not treat
        # an ordinary native editor as a dialog or touch another process's UI.
        if not (cls.value in ('#32770', 'TMessageForm', 'TTaskDialogForm', 'TPatchSelForm')
                or title.value.casefold() in ('error', 'application error')):
            return True
        parts = []
        deadline = time.monotonic() + 0.2

        @Callback
        def child(handle, unused):
            if len(parts) >= 64 or time.monotonic() > deadline:
                return False
            buf = C.create_unicode_buffer(4096)
            result = C.c_size_t()
            # WM_GETTEXT with a deadline: a hung plugin cannot hang the monitor.
            if user.SendMessageTimeoutW(handle, 13, len(buf), C.addressof(buf),
                                        0x22, 20, C.byref(result)) and buf.value:
                parts.append(buf.value)
            return True

        user.EnumChildWindows(hwnd, child, 0)
        rows.append({'hwnd': int(hwnd), 'pid': pid, 'title': title.value,
                     'class': cls.value, 'text': parts})
        return True

    user.EnumWindows(visit, 0)
    return rows
