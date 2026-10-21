"""VEXT's own memory, for the toolbar's meter (GET /api/system/memory).

The figure is the private bytes (PROCESS_MEMORY_COUNTERS_EX.PrivateUsage, Task
Manager's "Commit size" column in Details) summed over VEXT's whole process
tree: the desktop shell (VEXT.exe), its WebView2 processes, this backend and
the plugin workers. Private bytes rather than the working set: working sets
count shared DLL pages once per process (WebView2 shares a lot), so their sum
overstates; private bytes are each process's own memory and add up.

The tree's root is the topmost vext*.exe ancestor of this process (the shell
that started the sidecar); run from a terminal (dev) there is none, and the
tree is just this backend and its children. Pure ctypes (Toolhelp snapshot +
K32GetProcessMemoryInfo), no extra package. Cached for a second.
"""

import ctypes
import os
import threading
import time
from ctypes import wintypes

_CACHE_S = 1.0
_lock = threading.Lock()
_cache: dict = {"at": 0.0, "value": None}


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]


class _PMC_EX(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
                ("PrivateUsage", ctypes.c_size_t)]


_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
_k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
_k32.Process32FirstW.argtypes = _k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PMC_EX), wintypes.DWORD]
_k32.K32GetProcessMemoryInfo.restype = wintypes.BOOL
_k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
_k32.GetProcessTimes.restype = wintypes.BOOL
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _processes() -> dict[int, tuple[int, str]]:
    """pid -> (parent pid, exe name), from one Toolhelp snapshot."""
    snap = _k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snap or snap == wintypes.HANDLE(-1).value:
        return {}
    out = {}
    try:
        entry = _PROCESSENTRY32W(dwSize=ctypes.sizeof(_PROCESSENTRY32W))
        ok = _k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            out[entry.th32ProcessID] = (entry.th32ParentProcessID, entry.szExeFile)
            ok = _k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        _k32.CloseHandle(snap)
    return out


def _created(pid: int) -> int | None:
    h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        t = [wintypes.FILETIME() for _ in range(4)]
        if not _k32.GetProcessTimes(h, *[ctypes.byref(x) for x in t]):
            return None
        return (t[0].dwHighDateTime << 32) | t[0].dwLowDateTime
    finally:
        _k32.CloseHandle(h)


def _private_bytes(pid: int) -> int:
    h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return 0
    try:
        pmc = _PMC_EX(cb=ctypes.sizeof(_PMC_EX))
        return int(pmc.PrivateUsage) if _k32.K32GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb) else 0
    finally:
        _k32.CloseHandle(h)


def _root(procs: dict, me: int) -> int:
    """The topmost vext*.exe ancestor of `me` (a live parent, started before
    its child: a recycled PID is not an ancestor), else `me`."""
    root, pid, seen = me, me, {me}
    born = _created(me)
    while True:
        parent = procs.get(pid, (0, ""))[0]
        if not parent or parent in seen or parent not in procs:
            break
        parent_born = _created(parent)
        if parent_born is None or (born is not None and parent_born > born):
            break
        seen.add(parent)
        name = procs[parent][1].lower()
        if name.startswith("vext") and name.endswith(".exe"):
            root = parent
        pid, born = parent, parent_born
    return root


def _measure() -> dict:
    procs = _processes()
    me = os.getpid()
    root = _root(procs, me)
    children: dict[int, list[int]] = {}
    for pid, (parent, _name) in procs.items():
        children.setdefault(parent, []).append(pid)

    def subtree(top: int) -> list[int]:
        out, queue = [top], [top]
        while queue:
            for c in children.get(queue.pop(), []):
                if c not in out and c != top:
                    out.append(c)
                    queue.append(c)
        return out

    tree = subtree(root)
    backend_tree = set(subtree(me))
    # The backend: this process and the sidecar's bootloader between it and the shell.
    backend = {me}
    pid = me
    while pid != root and pid in procs:
        backend.add(pid)
        pid = procs[pid][0]
    groups = {"app": 0, "backend": 0, "plugins": 0}
    counts = {"app": 0, "backend": 0, "plugins": 0}
    for pid in tree:
        size = _private_bytes(pid)
        group = "backend" if pid in backend else "plugins" if pid in backend_tree else "app"
        groups[group] += size
        counts[group] += 1
    return {"total": sum(groups.values()), "measure": "private_bytes", "root_pid": root,
            "root": procs.get(root, (0, ""))[1], "processes": len(tree),
            "parts": [{"key": k, "bytes": groups[k], "processes": counts[k]} for k in ("app", "backend", "plugins")],
            "at": time.time()}


def usage() -> dict:
    """{total, measure, root, processes, parts: [{key: app|backend|plugins, bytes, processes}]}."""
    with _lock:
        if _cache["value"] is None or time.monotonic() - _cache["at"] > _CACHE_S:
            _cache["value"] = _measure()
            _cache["at"] = time.monotonic()
        return _cache["value"]
