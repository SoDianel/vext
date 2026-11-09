"""Local evaluation adapter for the installed teVirtualMIDI driver; not cleared for distribution."""
import ctypes
from ctypes import wintypes
import os
import struct
import threading
import time
from collections import deque

MAX_MESSAGE = 65535


def validate_name(name):
    if not name or not name.strip() or len(name.encode('utf-16-le')) // 2 > 31 or any(c in name for c in '\\/:*?"<>|') or any(ord(c) < 32 for c in name):
        raise ValueError('Port name must be nonempty, at most 31 UTF-16 units, and contain no path characters.')
    return name


def native():
    if os.name != 'nt' or struct.calcsize('P') != 8:
        raise RuntimeError('This evaluation requires 64-bit Windows Python.')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetSystemDirectoryW.argtypes = [wintypes.LPWSTR, wintypes.UINT]
    kernel.GetSystemDirectoryW.restype = wintypes.UINT
    path = ctypes.create_unicode_buffer(32768)
    length = kernel.GetSystemDirectoryW(path, len(path))
    if not length or length >= len(path):
        raise ctypes.WinError(ctypes.get_last_error())
    dll_path = os.path.join(path.value, 'teVirtualMIDI64.dll')
    try:
        dll = ctypes.WinDLL(dll_path, use_last_error=True)
    except OSError as exc:
        raise OSError(f'Loading installed system DLL {dll_path} failed: {exc}') from exc
    byteptr = ctypes.POINTER(ctypes.c_ubyte)
    dll.virtualMIDICreatePortEx2.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD]
    dll.virtualMIDICreatePortEx2.restype = ctypes.c_void_p
    dll.virtualMIDIGetData.argtypes = [ctypes.c_void_p, byteptr, ctypes.POINTER(wintypes.DWORD)]
    dll.virtualMIDIGetData.restype = wintypes.BOOL
    dll.virtualMIDISendData.argtypes = [ctypes.c_void_p, byteptr, wintypes.DWORD]
    dll.virtualMIDISendData.restype = wintypes.BOOL
    dll.virtualMIDIShutdown.argtypes = [ctypes.c_void_p]
    dll.virtualMIDIShutdown.restype = wintypes.BOOL
    dll.virtualMIDIClosePort.argtypes = [ctypes.c_void_p]
    dll.virtualMIDIClosePort.restype = None
    return dll


def error(operation):
    code = ctypes.get_last_error()
    return f'{operation} failed (Windows error {code}): {ctypes.FormatError(code).strip()}'


class Cable:
    def __init__(self, dll, name, messages_per_second=2000, bytes_per_second=262144, on_fault=None):
        validate_name(name)
        if messages_per_second < 1 or bytes_per_second < 1:
            raise ValueError('Rate limits must be positive.')
        self.dll, self.name = dll, name
        self.on_fault = on_fault
        self.message_limit, self.byte_limit = messages_per_second, bytes_per_second
        self.events = deque()
        self.byte_count = 0
        self.received = self.sent = self.failures = self.overflows = 0
        self.muted = False
        self.problem = ''
        self.stopping = threading.Event()
        self.lock = threading.Lock()
        self.close_lock = threading.Lock()
        self.handle = dll.virtualMIDICreatePortEx2(name, None, 0, MAX_MESSAGE, 1 | 12)
        if not self.handle:
            raise OSError(error('Create port (collision, inactive driver, version mismatch or resource exhaustion)'))
        self.thread = threading.Thread(target=self._read, name='midi-cable', daemon=True)
        try:
            self.thread.start()
        except BaseException:
            dll.virtualMIDIClosePort(self.handle)
            self.handle = None
            raise

    def _read(self):
        buffer = (ctypes.c_ubyte * MAX_MESSAGE)()
        try:
            while not self.stopping.is_set():
                size = wintypes.DWORD(MAX_MESSAGE)
                if not self.dll.virtualMIDIGetData(self.handle, buffer, ctypes.byref(size)):
                    if not self.stopping.is_set():
                        self.failures += 1
                        if size.value > MAX_MESSAGE:
                            self.overflows += 1
                        self.problem = error('Receive (driver may be disabled, unavailable or require a larger buffer)')
                        self.muted = True
                    break
                if self.stopping.is_set():
                    break
                now = time.monotonic()
                while self.events and now - self.events[0][0] >= 1:
                    self.byte_count -= self.events.popleft()[1]
                self.received += 1
                length = size.value
                if not 0 < length <= MAX_MESSAGE or len(self.events) >= self.message_limit or self.byte_count + length > self.byte_limit:
                    self.overflows += 1
                    self.muted = True
                    self.problem = 'Rate/size limit exceeded; cable muted. Stop and restart after removing feedback.'
                    break
                self.events.append((now, length))
                self.byte_count += length
                with self.lock:
                    if self.stopping.is_set():
                        break
                    if not self.dll.virtualMIDISendData(self.handle, buffer, length):
                        self.failures += 1
                        self.muted = True
                        self.problem = error('Send (driver may be disabled or unavailable)')
                        break
                    self.sent += 1
        except Exception as exc:
            self.failures += 1
            self.muted = True
            self.problem = f'Relay failed: {exc}'
        finally:
            if self.muted and self.on_fault:
                try:
                    self.on_fault(self.problem)
                except Exception:
                    pass

    def close(self):
        with self.close_lock:
            self._close()

    def _close(self):
        if self.handle is None:
            return
        self.stopping.set()
        # Shutdown unblocks a pending read; serialise against sends.
        if not self.lock.acquire(timeout=5):
            raise RuntimeError('Send did not stop; port retained until process exit.')
        try:
            if not self.dll.virtualMIDIShutdown(self.handle):
                self.problem = self.problem or error('Shutdown')
        finally:
            self.lock.release()
        self.thread.join(5)
        if self.thread.is_alive():
            # Never close/unload a port while a native read is still using it.
            raise RuntimeError('Receive did not stop; port retained until process exit.')
        self.dll.virtualMIDIClosePort(self.handle)
        self.handle = None
