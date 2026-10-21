"""Backend side of plugin preview: starts the host worker process for the open
plugin (app/host_worker.py), tracks its state, and stores editor screenshots.

Only one plugin is open at a time: opening one closes the others.

Formats: VST3 (pedalboard), VST2 and CLAP (VEXT's own ctypes hosts in
app/hosting/), and Waves bundles (through their WaveShell VST3/VST2, see
app/hosting/waves.py). 64-bit only. FL-native plugins only run inside FL Studio.

Which plugins can open: host_issue() gives the reason a catalog row can't
(static rules + what the last attempt found); every failed load is recorded
(cache/hosting/host-status.json, keyed by path and file time) and every
successful one clears it. check_start() probes many plugins headlessly in the
background (the worker's --probe mode) to fill that record in advance.

Commands go to the worker with an id; the worker acks each one once applied
(or refused, with the reason), so every call here returns the state *after*
the command took effect. Between acks the worker's events keep the state
current (position every 0.25 s while a file plays, live stats while the
browser's audio plays).

Sources: a local file (play) or the in-app browser (set_source 'browser'). The
browser's audio reaches the worker through app/browser_audio.py, which asks
browser_target() where to send it and is told through add_listener() whenever
that may have changed. The Playground's Audio tab plays its decoded file with
its edits (envelope), a loop, and records what comes out of the plugin
(record(); app/playground_audio.py owns the file and gets the worker's
'record' news through configure(record_tap=...)).

Audio settings and MIDI (AGENTS.md "Audio settings and MIDI"): the web app
gives the settings through configure(); right after loading, a session opens
the MIDI inputs and starts an instrument's sound (unless it crashed the last
time it played: sound_crash()). apply_audio_settings / apply_midi_settings
push a change to what plays now; send_midi / panic / set_sound / test_tone.
"""

import atexit
import ctypes
import datetime
import hashlib
import io
import itertools
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import deque
from ctypes import wintypes
from pathlib import Path

from app import analysis_tap, host_worker, logbuf
from app.config import IMAGES_DIR, PROJECT_ROOT
from app.hosting import deps, waves

HOSTABLE_FORMATS = {"VST3", "VST2", "CLAP", "WAVES"}
CAPTURE_TARGETS = ("picture", "file", "clipboard")
HOSTING_DIR = PROJECT_ROOT / "cache" / "hosting"  # shell-names.json (worker), host-status.json
STATUS_FILE = HOSTING_DIR / "host-status.json"
# A plugin still loading after this long (and not showing a dialog) is given up on.
# Slowest seen here: Vienna Synchron Player ~95 s, Kontakt 7/8 > 60 s on a first load.
LOAD_TIMEOUT_S = 180
CHECK_TIMEOUT_S = 150
CHECK_JOBS = 2

# host_issue() codes. The first group is found without loading the plugin.
ISSUE_CODES = {
    "format": "can't be hosted outside its app",
    "bitness": "32-bit",
    "missing": "file gone",
    "flagged": "flagged in the catalog",
    "quarantined": "quarantined",
    "no_shell": "Waves shell missing",
    "not_licensed": "not licensed",
    "no_output": "no audio output",
    "bus_layout": "unsupported channel layout",
    "no_editor": "no editor window",
    "damaged": "damaged installation",
    # what a load attempt found (retrying may help after fixing the cause)
    "missing_dll": "a DLL is missing",
    "refused": "refused to start",
    "timeout": "took too long to load",
    "crashed": "crashed while loading",
    "process_failed": "failed to process audio",
    "memory": "needs more memory than the limit",
    "failed": "failed to load",
}
# Issues "Try again" may get past (host_issue's `retry`): the attempt codes, and
# a Waves plugin licensed since its shell was last listed (host_open lists it again).
RETRY_CODES = {"missing_dll", "refused", "timeout", "crashed", "process_failed", "failed", "not_licensed", "memory",
               "no_output"}
NO_OUTPUT_NOTE = "This plugin has no audio output: you hear the input unchanged."
BITNESS_REASON = ("it is a 32-bit plugin and this copy of VEXT has no 32-bit host "
                  "(reinstall VEXT; in a dev checkout, run tools\\setup_x86_host.ps1)")

_CLOSED = {
    "state": "closed", "is_instrument": False, "playing": False, "paused": False,
    "bypass": False, "position_s": 0.0, "duration_s": None, "source": None,
    # 'browser' | 'file' | None: what the plugin is listening to.
    "source_kind": None,
    # File playback: dry = played without the plugin (the Audio tab's recording);
    # loop = [start_s, end_s] while a selection loops; recording = the Audio tab's
    # recording is armed (what comes out of the plugin is written while the file plays).
    "dry": False, "loop": None, "recording": False,
    # The file plays, but the Plugin buffer is refilling (a start, a seek, a new loop): nothing is heard yet,
    # so the position stands still (host_worker Player.buffering).
    "buffering": False,
    # Space presses in the editor (the worker takes them from the plugin: Play / Stop is the
    # page's), counted per session; the page acts on each new one.
    "space_seq": 0,
    # The Audio tab's recording plays along with the file, both into the plugin.
    "mix": False,
    # While the browser's audio plays: {rate, out_rate, block, frames_in, frames_out,
    # underruns, dropped, buffer_ms, latency_ms, peak_in, peak_out, connected}.
    "live": None,
    # The editor window (physical pixels, client area), once it exists.
    "editor_hwnd": None, "editor_width": None, "editor_height": None,
    "message": "", "error": None,
    # How it's hosted, once loaded: 'VST3' | 'VST2' | 'CLAP' (a Waves bundle is
    # 'VST3' through its WaveShell), the plugin picked inside a multi-plugin
    # file or shell (e.g. "C1 comp Stereo"), and how long loading took.
    "format": None, "plugin_name": None, "load_s": None,
    # While loading: the plugin opened a window (license / registration dialog)
    # that the user has to answer; `message` quotes its title.
    "dialog": False,
    # Why it couldn't be opened ({code, reason}, see ISSUE_CODES), else None.
    "host_issue": None,
    # MIDI reaches it (instruments; VST2/CLAP effects with a note input).
    # pedalboard can't send MIDI to VST3 effects.
    "accepts_midi": False,
    # The output stream that plays now (any source): {driver, device, device_name,
    # hostapi, rate, block, safe, buffer, target_ms, buffer_ms, latency_ms, xruns, peak_out,
    # ahead_ms, starves, load_pct, render_peak_ms}. The last five come from the Plugin buffer
    # (host_worker._RenderQueue): ms queued ahead of the device, its target, starves so far,
    # the plugin's load (% of real time), and its slowest render. Else None.
    "audio": None,
    # MIDI: {inputs: {open, failed: [{id, error}], available}, count, last, held}.
    "midi": None,
    # An instrument whose sound is off (it crashed the last time VEXT played it):
    # the reason; start_instrument() turns it on. None otherwise.
    "sound_off": None,
    # The editor is still off the screen, waiting for the desktop shell's frame
    # to place and reveal it (opened with framed=True; see host_worker._Hold).
    "editor_hidden": False,
    # The worker's memory: {limit_gb, peak_mb} (peak: most committed so far).
    "memory": None,
    # It has no audio output (an analyzer): it's fed the audio and the input is heard
    # (no_output_note says so). Bypass then stops feeding it.
    "no_output": False, "no_output_note": None,
    # The frame's IN / OUT knobs and Auto-Balance: {in_db, out_db, auto_balance, balance_db
    # (what Auto-Balance applies now), reduction_db (its limiter)}; kept per plugin (gains.json).
    "gain": None,
}
# source_kind is also 'instrument' while an instrument renders (playable from MIDI).

# What a session's state becomes once its worker is gone (closed or crashed;
# a crash also sets "error"). Nothing plays, and the editor window is gone.
_ENDED = {
    "state": "closed", "playing": False, "paused": False, "source_kind": None, "live": None, "message": "",
    "editor_hwnd": None, "editor_width": None, "editor_height": None, "audio": None, "recording": False,
}

# Events after which the browser routing may have changed.
_ROUTING_EVENTS = {"loaded", "live_started", "live_stopped", "stopped", "playing", "closed"}
# Events that carry the output stream, MIDI or instrument state (_Session._audio_event).
_AUDIO_EVENTS = {"loaded", "playing", "live_started", "instrument_started", "position", "live",
                 "instrument", "stopped", "live_stopped", "instrument_stopped", "midi", "midi_inputs"}
SOUND_CRASHES_FILE = HOSTING_DIR / "sound-crashes.json"
TAP_DIR = PROJECT_ROOT / "cache" / "analysis"  # the workers' analysis rings, tap-<pid>.bin (app/analysis_tap.py)
SOUND_PROVEN_S = 5  # an instrument that played this long no longer counts as crashing

# Where the sessions read the audio and MIDI settings (set by the web app,
# which owns the database): () -> {driver, device, samplerate, blocksize, safe}
# and () -> {inputs: "*" | [ids], channel: 0..16}.
_providers: dict = {"audio": None, "midi": None, "memory": None, "midi_release": None, "midi_tap": None,
                    "record_tap": None, "auto_balance": None}


def configure(audio=None, midi=None, memory=None, midi_release=None, midi_tap=None, record_tap=None,
              auto_balance=None) -> None:
    """`memory`: () -> the Plugin memory limit in GB (one of MEMORY_LIMITS_GB).
    `midi_release`: () -> None, called before a worker opens the MIDI inputs (the
    toolbar's monitor lets go of them); `midi_tap`: (recent) -> None, gets the
    messages a worker received ([[t, device, [bytes]], ...]; app/midi_monitor.py).
    `record_tap`: (news) -> None, the Audio tab's recording: {on, path, rate, frames,
    created, dirty} from the worker's 'record' events, {dirty} while it records,
    {on: False, ended: True} when the worker is gone (app/playground_audio.py).
    `auto_balance`: () -> bool, Configuration's Auto Level Compensation (Auto-Balance on at every open)."""
    if auto_balance is not None:
        _providers["auto_balance"] = auto_balance
    if record_tap is not None:
        _providers["record_tap"] = record_tap
    if midi_release is not None:
        _providers["midi_release"] = midi_release
    if midi_tap is not None:
        _providers["midi_tap"] = midi_tap
    if audio is not None:
        _providers["audio"] = audio
    if midi is not None:
        _providers["midi"] = midi
    if memory is not None:
        _providers["memory"] = memory


def _audio_settings() -> dict:
    if _providers["audio"]:
        return _providers["audio"]()
    return {"driver": "wasapi", "device": "", "samplerate": 48000, "blocksize": 512, "safe": False, "buffer": "auto"}


def _auto_balance_on() -> bool:
    try:
        return bool(_providers["auto_balance"] and _providers["auto_balance"]())
    except Exception:  # noqa: BLE001 - a settings read must never stop a plugin from opening
        return False


def _midi_settings() -> dict:
    if _providers["midi"]:
        return _providers["midi"]()
    return {"inputs": "", "channel": 0, "block_pitchbend": False}


class HostError(Exception):
    pass


def image_path(host_path: str) -> str:
    """Keyed by the plugin's path (not its row id) so it survives rescans."""
    key = hashlib.sha1(host_path.lower().encode("utf-8")).hexdigest()[:16]
    return str(IMAGES_DIR / f"{key}.png")


# ---------- change listeners (browser audio routing) ----------

_listeners: list = []


def add_listener(fn) -> None:
    """`fn()` is called (from any thread) whenever routing may have changed."""
    _listeners.append(fn)


def _changed() -> None:
    for fn in list(_listeners):
        try:
            fn()
        except Exception:  # noqa: BLE001 - a listener must never break the host
            pass


_REPO_ROOT = Path(__file__).resolve().parent.parent


def x86_worker() -> list[str] | None:
    """How to start the 32-bit worker, which hosts 32-bit plugins: the same
    host_worker run by a 32-bit Python (no pedalboard, no FFmpeg: VST2 only; see
    AGENTS.md "32-bit plugins"). Installed: host32\\vext-host32.exe next to the
    backend (build_host32.ps1); dev: the repo's .python-x86 (tools/setup_x86_host.ps1).
    VEXT_HOST32 overrides either. None when this copy of VEXT has none."""
    override = os.environ.get("VEXT_HOST32")
    if override:
        if override.lower().endswith("python.exe"):
            return [override, "-m", "app.host_worker"]
        return [override, "--host-worker"]
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).parent / "host32" / "vext-host32.exe"
        return [str(exe), "--host-worker"] if exe.is_file() else None
    py = _REPO_ROOT / ".python-x86" / "python.exe"
    return [str(py), "-m", "app.host_worker"] if py.is_file() else None


def worker_arch(path: str) -> str:
    """'x86' when `path` is a 32-bit plugin binary (it needs the 32-bit worker), else 'x64'."""
    if path == "--standby" or os.path.isdir(path):
        return "x64"  # bundles: a 32-bit-only VST3 bundle isn't hosted (host_issue)
    return "x86" if deps.architecture(path) == "x86" else "x64"


def worker_command(path: str, args: list[str]) -> list[str]:
    """How to start the host worker: the frozen sidecar runs itself with
    --host-worker (sidecar_main.py); a dev backend runs the module. A 32-bit
    plugin gets the 32-bit worker (x86_worker)."""
    if worker_arch(path) == "x86":
        base = x86_worker()
        if base is None:
            raise HostError(BITNESS_REASON)
        return [*base, path, *args]
    if getattr(sys, "frozen", False):
        return [sys.executable, "--host-worker", path, *args]
    return [sys.executable, "-m", "app.host_worker", path, *args]


def _spawn_worker(path: str, args: list[str], stdin=subprocess.PIPE) -> tuple[subprocess.Popen, "_Job | None"]:
    """Starts a worker (no console window) inside a job object that holds the
    memory limit. Returns (process, job)."""
    command = worker_command(path, args)
    # A dev 32-bit worker runs `-m app.host_worker` from another interpreter: from the repo.
    cwd = str(_REPO_ROOT) if "-m" in command[1:3] else None
    proc = subprocess.Popen(
        command, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd,
        text=True, encoding="utf-8", errors="replace", bufsize=1, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    # stderr is drained here, not by callers, so no call site can forget it and
    # deadlock the worker on a full pipe. It goes to the Log and to a per-worker
    # tail that a failure report quotes (_failure_report).
    tag = "standby" if path == "--standby" else os.path.basename(path)
    threading.Thread(target=_drain_stderr, args=(proc, tag), daemon=True).start()
    return proc, _Job.attach(proc)


STDERR_TAIL_LINES = 25
_stderr_tails: dict[int, deque] = {}  # worker pid -> its last stderr lines


def _drain_stderr(proc: subprocess.Popen, tag: str) -> None:
    tail = _stderr_tails.setdefault(proc.pid, deque(maxlen=STDERR_TAIL_LINES))
    while len(_stderr_tails) > 64:  # spares that never became a session
        _stderr_tails.pop(next(iter(_stderr_tails)))
    try:
        for line in proc.stderr:
            line = line.rstrip()
            if line:
                tail.append(line)
                logbuf.add(f"worker:{tag}", line, "warning")
    except Exception:  # noqa: BLE001 - best-effort logging, never worth crashing over
        pass


# ---------- failure reports (the Log's detail for a plugin that failed) ----------

# Windows NTSTATUS codes a crashed worker exits with, in plain words.
_EXIT_CODES = {
    0xC0000005: "access violation: native code read or wrote memory it doesn't own",
    0xC0000409: "fail-fast / stack buffer overrun: native code detected corrupted state and stopped itself",
    0xC0000374: "heap corruption: native code damaged its own memory",
    0xC00000FD: "stack overflow: runaway recursion in native code",
    0xC0000135: "a DLL the plugin needs was not found",
    0xC0000139: "a function the plugin needs is missing from one of its DLLs (wrong DLL version)",
    0xC0000142: "a DLL failed to initialize",
    0xC000001D: "illegal instruction: the plugin needs a CPU feature this machine lacks",
    0xC0000094: "integer division by zero in native code",
    0xC0000417: "invalid parameter passed to the C runtime",
    0xC0000420: "assertion failure in native code",
    0xE06D7363: "unhandled C++ exception in the plugin",
    0x80000003: "breakpoint hit (a debug build or an assertion)",
    0xC000013A: "terminated (Ctrl+C or the console closed)",
    1: "stopped by its own error handling (see the worker output below)",
}


def describe_exit(code: int | None) -> str:
    """'0xc0000005 (access violation: ...)' or '' for a clean exit."""
    if not code:
        return ""
    unsigned = code & 0xFFFFFFFF
    text = f"{unsigned:#010x}" if unsigned > 255 else str(unsigned)
    meaning = _EXIT_CODES.get(unsigned)
    return f"{text} ({meaning})" if meaning else text


def _arg(args: list[str], name: str) -> str | None:
    try:
        return args[args.index(name) + 1]
    except (ValueError, IndexError):
        return None


def windows_crash_report(pid: int, attempts: int = 8) -> dict | None:
    """The Application Error event (id 1000) Windows records for a crashed
    process: which module faulted, where. Written by WER a moment after the
    crash, so it retries for a few seconds. None if there isn't one (not a
    native crash, or error reporting is off)."""
    import xml.etree.ElementTree as ET

    query = "*[System[Provider[@Name='Application Error'] and (EventID=1000)]]"
    ns = {"e": "http://schemas.microsoft.com/win/2004/08/events/event"}
    for _ in range(attempts):
        try:
            out = subprocess.run(["wevtutil", "qe", "Application", f"/q:{query}", "/c:15", "/rd:true", "/f:xml"],
                                 capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
                                 creationflags=subprocess.CREATE_NO_WINDOW).stdout
            for event in ET.fromstring(f"<r>{out}</r>").findall("e:Event", ns):
                data = [d.text or "" for d in event.findall("e:EventData/e:Data", ns)]
                # Fields: app, app version, app time, module, module version, module time,
                # exception code, offset, pid (hex), start time, app path, module path, report id.
                if len(data) >= 13 and data[8] and int(data[8], 16) == pid:
                    return {"module": data[3], "module_version": data[4], "exception": data[6],
                            "offset": data[7], "module_path": data[11], "report_id": data[12]}
        except (OSError, subprocess.SubprocessError, ET.ParseError, ValueError):
            pass
        time.sleep(1)
    return None


def _crash_hint(report: dict, plugin_path: str) -> str:
    module_path = report.get("module_path", "").lower()
    if module_path.startswith(plugin_path.lower()):
        return ("The crash is inside the plugin's own code: VEXT can't fix that. A plugin update, or "
                "reinstalling it, is the usual remedy.")
    if "_mei" in module_path or "\\python" in module_path:
        return ("The crash is in a runtime library bundled with VEXT itself, not in the plugin: likely a VEXT "
                "packaging problem (see vext-backend.spec).")
    if "\\windows\\" in module_path:
        return "The crash is in a Windows system library, usually a symptom of the plugin passing it bad data."
    return "The crash is in a library the plugin loaded (often one of its own dependencies)."


# ---------- memory limit (one job object per worker) ----------
# Configuration's "Plugin memory limit" (plugin_memory_limit_gb, default 1 GB, up
# to 16): the most memory one worker (its plugin) may commit. A Windows job
# object enforces it: past it, the plugin's allocations fail (it usually
# crashes or refuses to load), and the job reports the limit was hit, so the
# open fails with "needed more than N GB" instead of a generic crash. The jobs
# also end their workers if the backend dies (KILL_ON_JOB_CLOSE): no orphans.

MEMORY_LIMITS_GB = (1, 2, 4, 8, 12, 16)
DEFAULT_MEMORY_LIMIT_GB = 1

_JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x100
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JobObjectAssociateCompletionPortInformation, _JobObjectExtendedLimitInformation = 7, 9
_JOB_OBJECT_MSG_PROCESS_MEMORY_LIMIT = 9


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in ("r_ops", "w_ops", "o_ops", "r_bytes", "w_bytes", "o_bytes")]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION), ("IoInfo", _IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


class _JOBOBJECT_ASSOCIATE_COMPLETION_PORT(ctypes.Structure):
    _fields_ = [("CompletionKey", ctypes.c_void_p), ("CompletionPort", wintypes.HANDLE)]


_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
_k32.CreateJobObjectW.restype = wintypes.HANDLE
_k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
_k32.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                           ctypes.POINTER(wintypes.DWORD)]
_k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
_k32.CreateIoCompletionPort.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.c_size_t, wintypes.DWORD]
_k32.CreateIoCompletionPort.restype = wintypes.HANDLE
_k32.GetQueuedCompletionStatus.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD),
                                           ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_void_p),
                                           wintypes.DWORD]
_k32.CloseHandle.argtypes = [wintypes.HANDLE]


def memory_limit_gb() -> int:
    gb = _providers.get("memory")() if _providers.get("memory") else DEFAULT_MEMORY_LIMIT_GB
    return gb if gb in MEMORY_LIMITS_GB else DEFAULT_MEMORY_LIMIT_GB


class _Job:
    """The job object around one worker process."""

    _port = None
    _port_lock = threading.Lock()
    _by_key: dict = {}  # completion key -> _Job
    _keys = itertools.count(1)

    def __init__(self, handle):
        self.handle = handle
        self.key = next(_Job._keys)
        self.limit_gb: int | None = None
        self.hit = False  # the worker's memory limit was hit
        _Job._by_key[self.key] = self

    @classmethod
    def attach(cls, proc: subprocess.Popen) -> "_Job | None":
        """A job for `proc` with the current memory limit; None if Windows
        refused (the worker then runs without one)."""
        handle = _k32.CreateJobObjectW(None, None)
        if not handle:
            return None
        job = cls(handle)
        try:
            job.set_limit(memory_limit_gb())
            port = cls._completion_port()
            if port:
                assoc = _JOBOBJECT_ASSOCIATE_COMPLETION_PORT(CompletionKey=job.key, CompletionPort=port)
                _k32.SetInformationJobObject(handle, _JobObjectAssociateCompletionPortInformation,
                                             ctypes.byref(assoc), ctypes.sizeof(assoc))
            if not _k32.AssignProcessToJobObject(handle, wintypes.HANDLE(int(proc._handle))):
                raise OSError(ctypes.get_last_error())
        except (OSError, AttributeError, ValueError):
            job.close()
            return None
        return job

    def set_limit(self, gb: int) -> None:
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_PROCESS_MEMORY | _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        info.ProcessMemoryLimit = int(gb) * 1024 ** 3
        if _k32.SetInformationJobObject(self.handle, _JobObjectExtendedLimitInformation, ctypes.byref(info),
                                        ctypes.sizeof(info)):
            self.limit_gb = int(gb)

    def peak_mb(self) -> int | None:
        """The most memory a process of the job has committed so far."""
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        if not self.handle or not _k32.QueryInformationJobObject(self.handle, _JobObjectExtendedLimitInformation,
                                                                ctypes.byref(info), ctypes.sizeof(info), None):
            return None
        return round(info.PeakProcessMemoryUsed / 1024 ** 2)

    def close(self) -> None:
        """Only once the worker is gone (KILL_ON_JOB_CLOSE ends it otherwise)."""
        _Job._by_key.pop(self.key, None)
        handle, self.handle = self.handle, None
        if handle:
            _k32.CloseHandle(handle)

    @classmethod
    def _completion_port(cls):
        with cls._port_lock:
            if cls._port is None:
                port = _k32.CreateIoCompletionPort(wintypes.HANDLE(-1), None, 0, 1)
                cls._port = port or 0
                if port:
                    threading.Thread(target=cls._listen, args=(port,), daemon=True).start()
            return cls._port

    @classmethod
    def _listen(cls, port) -> None:
        """Job notifications: only "a process hit its memory limit" matters."""
        message, key, overlapped = wintypes.DWORD(), ctypes.c_size_t(), ctypes.c_void_p()
        while True:
            if not _k32.GetQueuedCompletionStatus(port, ctypes.byref(message), ctypes.byref(key),
                                                  ctypes.byref(overlapped), 0xFFFFFFFF):
                continue
            job = cls._by_key.get(key.value)
            if job is not None and message.value == _JOB_OBJECT_MSG_PROCESS_MEMORY_LIMIT:
                job.hit = True


def memory_reason(job: "_Job | None") -> str | None:
    """The sentence for a worker that died after hitting its memory limit."""
    if job is None or not job.hit:
        return None
    return (f"it needed more than {job.limit_gb or memory_limit_gb()} GB of memory; raise Plugin memory limit "
            "in Configuration › Performance")


# ---------- the warm standby worker ----------
# Starting a worker means starting a process and importing numpy, pedalboard and
# VEXT's hosts (~0.3 s packaged). One worker is kept started and waiting
# (host_worker --standby); opening a plugin hands it the plugin instead. Each
# plugin still gets a process of its own: the standby is used once, and a new
# one starts a little later. It is let go after STANDBY_IDLE_S unused.

STANDBY_IDLE_S = 600
STANDBY_RESPAWN_DELAY_S = 3.0
_standby_lock = threading.Lock()
_standby: dict = {"proc": None, "job": None, "at": 0.0}


def _standby_enabled() -> bool:
    return os.environ.get("VEXT_NO_STANDBY") != "1"


def ensure_standby(delay: float = 0.0) -> None:
    """Makes sure a standby worker is starting or waiting (in the background)."""
    if not _standby_enabled():
        return

    def start():
        if delay:
            time.sleep(delay)
        with _standby_lock:
            proc = _standby["proc"]
            if proc is not None and proc.poll() is None:
                _standby["at"] = time.monotonic()
                return
            try:
                proc, job = _spawn_worker("--standby", [])
            except OSError:
                return
            _standby.update(proc=proc, job=job, at=time.monotonic())
        threading.Thread(target=_expire_standby, args=(proc,), daemon=True).start()

    threading.Thread(target=start, daemon=True).start()


def _expire_standby(proc: subprocess.Popen) -> None:
    while proc.poll() is None:
        time.sleep(15)
        with _standby_lock:
            if _standby["proc"] is not proc:
                return  # taken (it's a plugin's worker now)
            if time.monotonic() - _standby["at"] < STANDBY_IDLE_S:
                continue
            _standby.update(proc=None, job=None)
        _stop_spare(proc)
        return
    with _standby_lock:
        if _standby["proc"] is proc:
            job = _standby["job"]
            _standby.update(proc=None, job=None)
            if job:
                job.close()


def _stop_spare(proc: subprocess.Popen, job: "_Job | None" = None) -> None:
    try:
        proc.stdin.write('{"cmd":"quit"}\n')
        proc.stdin.flush()
        proc.wait(timeout=5)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        kill_tree(proc)
    if job:
        job.close()


def _take_standby() -> tuple[subprocess.Popen, "_Job | None"] | None:
    """The waiting standby worker, now the caller's (with the current memory limit)."""
    with _standby_lock:
        proc, job = _standby["proc"], _standby["job"]
        _standby.update(proc=None, job=None)
    if proc is None or proc.poll() is not None:
        if job:
            job.close()
        return None
    if job is not None and job.limit_gb != memory_limit_gb():
        job.set_limit(memory_limit_gb())
    return proc, job


def stop_standby() -> None:
    with _standby_lock:
        proc, job = _standby["proc"], _standby["job"]
        _standby.update(proc=None, job=None)
    if proc is not None:
        _stop_spare(proc, job)


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]


def _descendants(pid: int) -> list[int]:
    """PIDs started by `pid`, children before grandchildren."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.Process32FirstW.argtypes = k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snap or snap == wintypes.HANDLE(-1).value:
        return []
    children: dict[int, list[int]] = {}
    try:
        entry = _PROCESSENTRY32W(dwSize=ctypes.sizeof(_PROCESSENTRY32W))
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            children.setdefault(entry.th32ParentProcessID, []).append(entry.th32ProcessID)
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    out, queue = [], [pid]
    while queue:
        for child in children.get(queue.pop(0), []):
            if child not in out and child != pid:
                out.append(child)
                queue.append(child)
    return out


def _terminate(pid: int) -> None:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = k32.OpenProcess(0x0001, False, pid)  # PROCESS_TERMINATE
    if handle:
        k32.TerminateProcess(handle, 1)
        k32.CloseHandle(handle)


def kill_tree(proc: subprocess.Popen) -> None:
    """Ends a worker and anything it started (helpers a plugin spawned). By PID:
    only ours. The worker's descendants go first. Packaged, a worker is a
    single process that reuses the backend's _MEI* folder (PyInstaller 6:
    started from the frozen backend, it inherits _PYI_APPLICATION_HOME_DIR and
    unpacks nothing; measured 2026-10-15); in development the process started is
    the venv's launcher, whose child is the real interpreter."""
    if proc.poll() is not None:
        return
    for pid in reversed(_descendants(proc.pid)):  # grandchildren first
        _terminate(pid)
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=10,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        except (OSError, subprocess.SubprocessError):
            pass
        if proc.poll() is None:
            proc.kill()


class _Session:
    def __init__(self, host_path: str, launch: tuple[str, list[str]]):
        self.state = {**_CLOSED, "state": "starting", "message": "Loading the plugin…"}
        self.started_at = time.monotonic()
        self.timings: dict = {}  # seconds since the open: loaded, editor (AGENTS.md "Opening speed")
        self.lock = threading.Lock()
        self.closing = False
        self.host_path = host_path
        self.live_port: int | None = None  # the worker's live input socket (internal)
        self.live_key: str | None = None
        self._acks: dict[int, dict] = {}  # command id -> {"done": Event, "error": str|None}
        self._ids = itertools.count(1)
        self._position_at: float | None = None  # when position_s was last reported
        self._now_playing = ""
        self._loaded = False
        self._issue: dict | None = None  # the worker's reason for not loading
        self._started = time.monotonic()
        self._write_lock = threading.Lock()
        self._sound_since: float | None = None  # when the instrument's sound started (while it runs)
        self._sound_proven = False  # it has played long enough to clear a recorded crash
        self.tap_path: str | None = None  # the worker's analysis ring file, once it was asked to make it
        self.label: str | None = None  # the catalog's name of the plugin (set by open_plugin)
        self.launch_args = launch[1]  # for the failure report
        self._logged_errors: set[str] = set()
        # The standby is a 64-bit worker: a 32-bit plugin always starts its own.
        spare = _take_standby() if worker_arch(launch[0]) == "x64" else None
        if spare is not None:
            # A worker that already started and imported everything: it loads this plugin now.
            self.proc, self.job = spare
            self.timings["standby"] = True
            try:
                self.proc.stdin.write(json.dumps({"cmd": "load", "path": launch[0], "args": launch[1]}) + "\n")
                self.proc.stdin.flush()
            except (OSError, ValueError):
                kill_tree(self.proc)
                if self.job:
                    self.job.close()
                spare = None
        if spare is None:
            self.proc, self.job = _spawn_worker(*launch)
        self.state["memory"] = {"limit_gb": self.job.limit_gb if self.job else None, "peak_mb": None}
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._watch_load, daemon=True).start()

    def alive(self) -> bool:
        return self.proc.poll() is None

    def _watch_load(self) -> None:
        """Gives up on a plugin that never finishes loading, unless it's waiting
        for the user to answer one of its dialogs."""
        while self.alive() and not self._loaded and not self.closing:
            time.sleep(1)
            with self.lock:
                waiting = self.state["dialog"]
            if not waiting and time.monotonic() - self._started > LOAD_TIMEOUT_S and not self._loaded:
                self._issue = {"code": "timeout", "reason": f"it didn't finish loading in {LOAD_TIMEOUT_S} s "
                               "(it may be waiting for its license service or its content)"}
                with self.lock:
                    self.state["error"] = f"Can't open this plugin: {self._issue['reason']}."
                kill_tree(self.proc)
                return

    def _set_position(self, msg: dict) -> None:
        if msg.get("position_s") is not None:
            self.state["position_s"] = msg["position_s"]
            self._position_at = time.monotonic()
        if "buffering" in msg:
            self.state["buffering"] = bool(msg["buffering"])

    def _read(self) -> None:
        clean_exit = False
        for line in self.proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            event = msg.get("event")
            with self.lock:
                s = self.state
                if event == "loaded":
                    self._loaded = True
                    self.timings.setdefault("loaded", round(time.monotonic() - self.started_at, 3))
                    self.live_port, self.live_key = msg.get("live_port"), msg.get("live_key")
                    s.update(state="open", is_instrument=msg.get("is_instrument", False), message="", error=None,
                             dialog=False, format=msg.get("format"), plugin_name=msg.get("plugin_name"),
                             load_s=msg.get("load_s"), host_issue=None, no_output=bool(msg.get("no_output")),
                             no_output_note=NO_OUTPUT_NOTE if msg.get("no_output") else None)
                elif event == "editor":
                    if "editor" not in self.timings:
                        ensure_standby(delay=1.0)  # ready for the next plugin, once this one is up
                    self.timings.setdefault("editor", round(time.monotonic() - self.started_at, 3))
                    s.update(editor_hwnd=msg.get("hwnd"), editor_width=msg.get("width"),
                             editor_height=msg.get("height"))
                    if "hidden" in msg and not self.timings.get("revealed"):
                        s["editor_hidden"] = bool(msg.get("hidden"))
                elif event == "gain":
                    s["gain"] = {k: msg.get(k) for k in ("in_db", "out_db", "auto_balance", "balance_db",
                                                         "reduction_db")}
                elif event == "revealed":
                    self.timings.setdefault("revealed", round(time.monotonic() - self.started_at, 3))
                    s["editor_hidden"] = False
                elif event == "status":
                    s["message"] = msg.get("message", "")
                    s["dialog"] = bool(msg.get("dialog"))
                elif event == "playing":
                    # The note says why playback differs from the settings (device gone, rate refused).
                    self._now_playing = " ".join(filter(None, [f"Playing {msg.get('file', '')}.", msg.get("note")]))
                    paused = bool(msg.get("paused"))
                    s.update(playing=True, paused=paused, duration_s=msg.get("duration_s"), source=msg.get("source"),
                             source_kind="file", live=None, message="Paused" if paused else self._now_playing,
                             error=None, dry=bool(msg.get("dry")), loop=msg.get("loop"), mix=bool(msg.get("mix")),
                             buffering=True)  # the Plugin buffer fills first
                    self._set_position(msg)
                elif event == "position":
                    self._set_position(msg)
                    if msg.get("rec_dirty"):
                        _record_news({"dirty": msg["rec_dirty"]})
                elif event == "record":
                    s["recording"] = bool(msg.get("on"))
                    _record_news({k: v for k, v in msg.items() if k != "event"})
                elif event == "paused":
                    s.update(paused=True, message="Paused")
                    self._set_position(msg)
                elif event == "resumed":
                    s.update(paused=False, message=self._now_playing if s["playing"] else s["message"])
                    self._set_position(msg)
                elif event == "live_started":
                    s.update(playing=False, paused=False, source_kind="browser", error=None,
                             live={"rate": msg.get("rate"), "out_rate": msg.get("out_rate"), "block": msg.get("block"),
                                   "frames_in": 0, "frames_out": 0, "underruns": 0, "dropped": 0, "buffer_ms": 0.0,
                                   "latency_ms": None, "peak_in": 0.0, "peak_out": 0.0, "connected": False},
                             message=" ".join(filter(None, ["Playing the browser's audio through the plugin.",
                                                            msg.get("note")])))
                elif event == "live":
                    if s["live"] is not None:
                        s["live"] = {**s["live"], **{k: v for k, v in msg.items() if k != "event"}}
                elif event == "live_stopped":
                    s["live"] = None
                    if s["source_kind"] == "browser":
                        s.update(source_kind=None, message="")
                elif event == "bypass":
                    s["bypass"] = bool(msg.get("on"))
                elif event == "mix":
                    s["mix"] = bool(msg.get("on"))
                elif event == "hotkey":
                    s["space_seq"] = int(msg.get("seq") or 0)
                elif event == "stopped":
                    s.update(playing=False, paused=False, mix=False, buffering=False,
                             message="Finished" if msg.get("finished") else "")
                    self._set_position(msg)
                elif event == "error":
                    s["error"] = msg.get("message") or "unknown error"
                    self._log_error(s["error"], msg.get("code"))
                    if not self._loaded and msg.get("code"):
                        reason = (msg.get("message") or "").removeprefix("Can't open this plugin: ")
                        self._issue = {"code": msg["code"], "reason": reason}
                elif event == "ack":
                    waiter = self._acks.get(msg.get("id"))
                    if waiter:
                        waiter["error"] = msg.get("error")
                        waiter["done"].set()
                elif event == "closed":
                    clean_exit = True
                    s.update(_ENDED)
                if "balance_db" in msg and s["gain"] is not None and event != "gain":
                    s["gain"] = {**s["gain"], "balance_db": msg["balance_db"],
                                 "reduction_db": msg.get("reduction_db", 0.0)}
                if event in _AUDIO_EVENTS:
                    self._audio_event(event, msg)
            if event == "loaded":
                # Recorded now, not at close: the Playground lists it again at once.
                record_result(self.host_path, ok=True, load_s=msg.get("load_s"))
                threading.Thread(target=self._after_load, daemon=True).start()
            if event in _ROUTING_EVENTS:
                _changed()
        try:
            self.proc.wait(timeout=5)  # the job reports a hit memory limit before the process is gone
        except subprocess.TimeoutExpired:
            pass
        self._drop_tap()
        memory = memory_reason(self.job)
        if self.state.get("recording"):
            _record_news({"on": False, "ended": True})
        with self.lock:
            self.state.update(_ENDED, editor_hidden=False)
            if memory and not self.closing:
                # Whatever it reported, the cause is the limit: say so.
                self._issue = {"code": "memory", "reason": memory}
                self.state["error"] = f"Can't open this plugin: {memory}." if not self._loaded else (
                    f"The plugin stopped: {memory}.")
            elif not clean_exit and not self.closing and not self._issue:
                self.state["error"] = "The plugin host stopped unexpectedly (the plugin may have crashed)."
            self._sound_crash(clean_exit)
            if not self._loaded and not self.closing:
                issue = self._issue or {"code": "crashed", "reason": "the plugin crashed while loading"}
                self.state.update(host_issue={**issue, "retry": issue["code"] in RETRY_CODES}, dialog=False)
                record_result(self.host_path, ok=False, **issue)
            for waiter in self._acks.values():
                waiter["done"].set()
            if self.job:
                self.state["memory"] = {"limit_gb": self.job.limit_gb, "peak_mb": self.job.peak_mb()}
            error = self.state.get("error") if not self.closing else None
            crashed = error is not None and not clean_exit and bool(self.proc.returncode)
            if crashed and error.startswith("The plugin host stopped unexpectedly"):
                # The status line says what kind of crash; the Log has the rest.
                kind = _EXIT_CODES.get((self.proc.returncode or 0) & 0xFFFFFFFF)
                if kind:
                    self.state["error"] = f"The plugin host crashed: {kind.split(':')[0]}. Details are in the Log."
        if error:
            report = self._failure_report(error, clean_exit)
            logbuf.add(self._log_source(), report, "error")
            if crashed:
                threading.Thread(target=self._log_windows_report, args=(self.proc.pid,), daemon=True).start()
        _stderr_tails.pop(self.proc.pid, None)
        if self.job:
            self.job.close()
        _changed()

    def _log_source(self) -> str:
        return f"host:{self.label or os.path.basename(self.host_path)}"

    def _log_error(self, message: str, code: str | None = None) -> None:
        """An error the worker reported while running (it may keep running)."""
        if message in self._logged_errors:
            return
        self._logged_errors.add(message)
        logbuf.add(self._log_source(), f"{message}{f' [{code}]' if code else ''}", "error")

    def _failure_report(self, error: str, clean_exit: bool) -> str:
        """Everything known about why this plugin stopped, for the Log."""
        s = self.state
        name = self.label or s.get("plugin_name") or os.path.basename(self.host_path)
        fmt = s.get("format") or _arg(self.launch_args, "--format") or os.path.splitext(self.host_path)[1][1:].upper()
        ran = time.monotonic() - self.started_at
        stage = (f"after it opened (it ran {ran:.1f} s; loaded in {self.timings['loaded']} s)"
                 if self._loaded and "loaded" in self.timings else f"while loading ({ran:.1f} s after the open)")
        code = self.proc.returncode
        lines = [f"{name} failed: {error}",
                 f"  Plugin:      {name} ({fmt})",
                 f"  File:        {self.host_path}",
                 f"  When:        {stage}"]
        if self._issue:
            lines.append(f"  Reason code: {self._issue.get('code')}: {self._issue.get('reason')}")
        if code is not None:
            lines.append(f"  Exit code:   {describe_exit(code) or '0 (normal exit)'}"
                         + ("" if clean_exit else ", without the worker's closing message"))
        mem = s.get("memory") or {}
        if mem.get("peak_mb") is not None:
            limit = f" of a {mem['limit_gb']} GB limit" if mem.get("limit_gb") else ""
            lines.append(f"  Memory:      peak {mem['peak_mb']:.0f} MB{limit}")
        origin = "the pre-started standby worker" if self.timings.get("standby") else "a worker started for it"
        lines.append(f"  Worker:      pid {self.proc.pid}, {origin}; options: {' '.join(self.launch_args) or '(none)'}")
        others = [m for m in self._logged_errors if m != error]
        if others:
            lines.append("  Earlier errors from the worker:")
            lines += [f"    {m}" for m in others]
        tail = list(_stderr_tails.get(self.proc.pid, ()))
        if tail:
            lines.append(f"  Last worker output ({len(tail)} lines):")
            lines += [f"    {t}" for t in tail]
        elif not clean_exit:
            lines.append("  Worker output: none (a native crash usually ends the process before anything is written)")
        return "\n".join(lines)

    def _log_windows_report(self, pid: int) -> None:
        report = windows_crash_report(pid)
        if report is None:
            logbuf.add(self._log_source(), f"No Windows crash report found for pid {pid} (Windows Error Reporting "
                                           "may be off, or the worker didn't crash natively).", "info")
            return
        logbuf.add(self._log_source(), "\n".join([
            f"Windows crash report for pid {pid}:",
            f"  Faulting module: {report['module']} {report['module_version']}",
            f"  Module path:     {report['module_path']}",
            f"  Exception:       {describe_exit(int(report['exception'], 16)) if report['exception'] else '?'}"
            f" at offset {report['offset']}",
            f"  Report id:       {report['report_id']}",
            f"  {_crash_hint(report, self.host_path)}",
        ]), "error")

    def command(self, cmd: str, timeout: float = 5, **fields) -> None:
        """Sends one command and waits until the worker has applied it. Raises
        HostError with the worker's reason if it refused. A slow worker isn't
        an error: the state returned afterwards just doesn't reflect it yet."""
        if not self.alive():
            raise HostError("the plugin window is not open")
        command_id = next(self._ids)
        waiter = {"done": threading.Event(), "error": None}
        with self.lock:
            self._acks[command_id] = waiter
        try:
            self.send(cmd, id=command_id, **fields)
            waiter["done"].wait(timeout)
        finally:
            with self.lock:
                self._acks.pop(command_id, None)
        if waiter["error"]:
            raise HostError(waiter["error"])

    def snapshot(self) -> dict:
        job = self.job
        peak = job.peak_mb() if job and job.handle else None
        with self.lock:
            if peak is not None:
                self.state["memory"] = {"limit_gb": job.limit_gb, "peak_mb": peak}
            snap = dict(self.state)
            snap["timings"] = dict(self.timings)
            # Between position events, the audio kept going: extrapolate.
            # Not while the Plugin buffer refills (after a seek, a new loop): nothing is heard, the playhead waits.
            if snap["playing"] and not snap["paused"] and not snap.get("buffering") and self._position_at is not None:
                position = snap["position_s"] + (time.monotonic() - self._position_at)
                if snap["duration_s"]:
                    position = min(position, snap["duration_s"])
                snap["position_s"] = round(position, 3)
            return snap

    # ---- audio settings, MIDI, instruments ----

    def send(self, cmd: str, **fields) -> None:
        """Writes one command line, without waiting (MIDI notes: no round trip).
        Lines from different threads never interleave."""
        line = json.dumps({"cmd": cmd, **fields}) + "\n"
        with self._write_lock:
            try:
                self.proc.stdin.write(line)
                self.proc.stdin.flush()
            except (OSError, ValueError):
                raise HostError("the plugin window is not open") from None

    def _audio_event(self, event: str, msg: dict) -> None:
        """State from the worker's audio and MIDI events (under self.lock)."""
        s = self.state
        if event == "loaded":
            s.update(accepts_midi=bool(msg.get("accepts_midi")),
                     midi={"inputs": None, "count": 0, "last": "", "held": 0})
        elif event in ("playing", "live_started", "instrument_started"):
            s["audio"] = {**(msg.get("audio") or {}), "xruns": 0, "peak_out": 0.0}
            if event == "instrument_started":
                self._sound_since = time.monotonic()
                s.update(source_kind="instrument", sound_off=None, error=None, message=msg.get("note") or "")
        elif event in ("position", "live", "instrument"):
            if s["audio"] is not None:
                keys = ("xruns", "peak_out", "latency_ms", "ahead_ms", "target_ms", "starves", "load_pct",
                        "render_peak_ms")
                s["audio"] = {**s["audio"], **{k: msg[k] for k in keys if k in msg}}
            if event == "instrument" and s["midi"] is not None and "held" in msg:
                s["midi"] = {**s["midi"], "held": msg["held"]}
            if (event == "instrument" and self._sound_since and not self._sound_proven
                    and time.monotonic() - self._sound_since > SOUND_PROVEN_S):
                self._sound_proven = True  # it plays: a crash recorded earlier no longer holds
                threading.Thread(target=clear_sound_crash, args=(self.host_path,), daemon=True).start()
        elif event in ("stopped", "live_stopped", "instrument_stopped"):
            s["audio"] = None
            if event == "instrument_stopped":
                self._sound_since = None
                if s["source_kind"] == "instrument":
                    s.update(source_kind=None, message="")
        elif event == "midi":
            s["midi"] = {**(s["midi"] or {}), **{k: msg[k] for k in ("count", "last", "held") if k in msg}}
            if msg.get("recent") and _providers["midi_tap"]:
                try:
                    _providers["midi_tap"](msg["recent"])
                except Exception:  # noqa: BLE001 - the indicator must never break a session
                    pass
        elif event == "midi_inputs":
            s["midi"] = {**(s["midi"] or {}), "inputs": {k: msg.get(k) or [] for k in ("open", "failed", "available")}}

    def _send_tap(self) -> None:
        """Gives the worker the file of its analysis ring (app/analysis_tap.py): the
        Analyzer and the browser recording read what it plays from there."""
        path = str(TAP_DIR / f"tap-{self.proc.pid}.bin")
        self.tap_path = path  # before asking: it is deleted with the session whatever happens
        try:
            self.command("tap", timeout=5, path=path)
        except HostError:
            pass

    def _drop_tap(self) -> None:
        """The worker is gone: delete its ring file (its mapping is closed by now; the
        backend's reader lets go first, and the delete retries a moment)."""
        path, self.tap_path = self.tap_path, None
        if path:
            analysis_tap.release(path)
            threading.Thread(target=analysis_tap.remove, args=(path,), daemon=True).start()

    def _after_load(self) -> None:
        """Right after loading: its analysis ring, its saved IN / OUT gains, the MIDI inputs and,
        for an instrument, its sound (unless it crashed the last time it played)."""
        self._send_tap()  # first: an instrument's sound starts below
        try:
            gain = saved_gain(self.host_path)
            # Auto Level Compensation: on at every open, whatever was saved (the frame can still turn it off).
            if _auto_balance_on():
                gain["auto_balance"] = True
            self.command("gain", timeout=5, **gain)
        except HostError:
            pass
        midi = _midi_settings()
        _release_midi()
        try:
            self.command("midi_inputs", timeout=10, inputs=midi["inputs"], channel=midi["channel"],
                         block_pitchbend=midi.get("block_pitchbend", False))
        except HostError:
            pass
        with self.lock:
            instrument = self.state["is_instrument"]
        if not instrument or self.closing:
            return
        crash = sound_crash(self.host_path)
        if crash:
            with self.lock:
                self.state["sound_off"] = (f"It crashed the last time VEXT played it ({crash['at'][:10]}), so its "
                                           "sound is off. Its window still works.")
            return
        try:
            self.start_sound()
        except HostError as e:
            with self.lock:
                self.state["error"] = str(e)

    def start_sound(self) -> None:
        """Starts the instrument's sound with the current audio settings."""
        with self.lock:
            self.state.update(sound_off=None, error=None)
            self._sound_proven = False
        self.command("instrument", timeout=15, on=True, **_audio_settings())

    def _sound_crash(self, clean_exit: bool) -> None:
        """The worker is gone (under self.lock). Gone without closing while the
        instrument was rendering: its sound crashes it, so the next open keeps
        the sound off (see _after_load)."""
        if self._sound_since is None or clean_exit or self.closing:
            return
        self._sound_since = None
        record_sound_crash(self.host_path)
        self.state["error"] = ("The instrument crashed while VEXT played it. Open it again to see its window: "
                               "its sound stays off until you turn it on.")


_sessions: dict[int, _Session] = {}
_sessions_lock = threading.RLock()


def _session(plugin_id: int) -> _Session | None:
    s = _sessions.get(plugin_id)
    return s if s and s.alive() else None


def _open_session(plugin_id: int) -> _Session:
    """The session to send a playback command to: open and finished loading."""
    s = _session(plugin_id)
    if not s:
        raise HostError("open the plugin first")
    if s.snapshot()["state"] != "open":
        raise HostError("the plugin is still loading; try again in a moment")
    return s


def launch_target(host_path: str, fmt: str, name: str | None = None) -> tuple[str, list[str]]:
    """(file the worker opens, its options) for a catalog row. Raises HostError
    if the format can't be hosted or a Waves bundle has no shell."""
    cache = ["--cache", str(HOSTING_DIR)]
    if fmt == "WAVES":
        shell = waves.find_shell(host_path)
        if shell is None:
            version = waves.bundle_version(host_path) or "?"
            raise HostError(f"no 64-bit WaveShell V{version} is installed (reinstall it from Waves Central)")
        path, shell_fmt = shell
        return path, ["--format", shell_fmt, "--waves", waves.bundle_name(host_path), *cache]
    if fmt in ("VST3", "VST2", "CLAP"):
        return host_path, ["--format", fmt, *(["--hint", name] if name else []), *cache]
    raise HostError(_format_reason(fmt))


def open_plugin(plugin_id: int, host_path: str, fmt: str, name: str | None = None, framed: bool = False) -> dict:
    """Opens the plugin's editor. Only one plugin is open at a time: any other
    one is closed first. `framed`: the desktop shell will put the editor in its
    frame, so it stays off the screen until the frame reveals it (no flash at
    the top-left first); see reveal()."""
    path, args = launch_target(host_path, fmt, name)
    if framed:
        args = [*args, "--hidden"]
    if not os.path.exists(host_path):
        raise HostError("the plugin file no longer exists")
    with _sessions_lock:
        for other in [pid for pid in _sessions if pid != plugin_id]:
            close(other)
        if not _session(plugin_id):
            _sessions[plugin_id] = s = _Session(host_path, (path, args))
            s.label = name  # the catalog's name: what the Analyzer calls it (active_tap)
    return state(plugin_id)


def reveal(plugin_id: int) -> dict:
    """Shows an editor opened framed that no frame will reveal (the shell
    couldn't adopt it): it stays a window of its own."""
    s = _session(plugin_id)
    if s and s.snapshot()["editor_hidden"]:
        s.command("reveal", timeout=3)
    return state(plugin_id)


# ---------- which plugins can open ----------

def _format_reason(fmt: str) -> str:
    if fmt == "FL-NATIVE":
        return "FL Studio's own plugins only run inside FL Studio"
    return f"{fmt} plugins can't be hosted"


_status_lock = threading.Lock()
_status: dict | None = None  # host_path (lower) -> {ok, code, reason, at, mtime, load_s}


def _file_time(path: str) -> float | None:
    """The plugin's modification time (a bundle's binary for VST3 bundles): a
    reinstall or update makes an old result stale."""
    target = deps.vst3_binary(path) if os.path.isdir(path) else path
    try:
        return round(os.path.getmtime(target or path), 1)
    except OSError:
        return None


def _load_status() -> dict:
    global _status
    if _status is None:
        try:
            with open(STATUS_FILE, encoding="utf-8") as f:
                _status = json.load(f)
        except (OSError, ValueError):
            _status = {}
        # Plugins without an audio output open since 2026-10-15 (app/hosting/vst3.py):
        # an old "no_output" failure no longer holds.
        for key in [k for k, v in _status.items() if not v.get("ok") and v.get("code") == "no_output"]:
            del _status[key]
    return _status


def record_result(host_path: str, ok: bool, code: str | None = None, reason: str | None = None,
                  load_s: float | None = None) -> None:
    """Remembers whether a plugin opened (or why not), for host_issue()."""
    entry = {"ok": ok, "code": None if ok else code, "reason": None if ok else reason,
             "at": datetime.datetime.now().isoformat(timespec="seconds"), "mtime": _file_time(host_path),
             "load_s": load_s}
    with _status_lock:
        status = _load_status()
        status[host_path.lower()] = entry
        try:
            HOSTING_DIR.mkdir(parents=True, exist_ok=True)
            tmp = STATUS_FILE.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(status, indent=1), encoding="utf-8")
            os.replace(tmp, STATUS_FILE)
        except OSError:
            pass


def forget_memory_failures() -> int:
    """The memory limit was raised: plugins that failed for lack of memory may
    open now, so their recorded failure goes (they're listed again)."""
    with _status_lock:
        status = _load_status()
        gone = [k for k, v in status.items() if not v.get("ok") and v.get("code") == "memory"]
        for k in gone:
            del status[k]
        if gone:
            try:
                tmp = STATUS_FILE.with_suffix(f".{os.getpid()}.tmp")
                tmp.write_text(json.dumps(status, indent=1), encoding="utf-8")
                os.replace(tmp, STATUS_FILE)
            except OSError:
                pass
    return len(gone)


def last_result(host_path: str) -> dict | None:
    """The last recorded attempt for this plugin, if the file hasn't changed since."""
    with _status_lock:
        entry = _load_status().get(host_path.lower())
    if entry and entry.get("mtime") == _file_time(host_path):
        return entry
    return None


def _shell_names_cache() -> dict:
    try:
        with open(HOSTING_DIR / "shell-names.json", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def host_issue(row, quarantined_paths: set = frozenset(), shell_names: dict | None = None) -> dict | None:
    """Why a catalog row can't be opened in the Playground ({code, reason,
    retry[, at]}; codes in ISSUE_CODES; retry: opening it anyway is allowed,
    since the cause may be gone; at: when a load attempt found it), or None if
    it can (as far as VEXT knows: a plugin never tried yet counts as
    openable). `shell_names`: pass _shell_names_cache() once when checking
    many rows."""
    issue = _host_issue(row, quarantined_paths, shell_names)
    return {**issue, "retry": issue["code"] in RETRY_CODES} if issue else None


def _host_issue(row, quarantined_paths: set, shell_names: dict | None) -> dict | None:
    fmt, path = row["format"], row["host_path"]
    if fmt not in HOSTABLE_FORMATS:
        return {"code": "format", "reason": _format_reason(fmt)}
    if row["status"] != "ok":
        return {"code": "flagged", "reason": f"it is flagged in the catalog ({row['status_reason'] or row['status']})"}
    if path in quarantined_paths:
        return {"code": "quarantined", "reason": "it is in the quarantine"}
    if not os.path.exists(path):
        return {"code": "missing", "reason": "the plugin file no longer exists"}
    if row["bitness"] == 32:
        # The 32-bit worker hosts VST2 only: 32-bit VST3 would need pedalboard, which has no 32-bit build.
        if fmt != "VST2":
            return {"code": "bitness", "reason": f"it is a 32-bit {fmt} plugin (VEXT hosts 32-bit VST2 only)"}
        if x86_worker() is None:
            return {"code": "bitness", "reason": BITNESS_REASON}
    if fmt == "VST3" and os.path.isdir(path) and deps.vst3_binary(path) is None:
        if "x86-win" in deps.bundle_arches(path):
            return {"code": "bitness", "reason": "it is a 32-bit plugin (its bundle has no 64-bit binary)"}
        return {"code": "damaged", "reason": "the bundle has no Windows 64-bit binary"}
    if fmt == "WAVES":
        shell = waves.find_shell(path)
        if shell is None:
            return {"code": "no_shell", "reason": f"no 64-bit WaveShell V{waves.bundle_version(path) or '?'} "
                                                  "is installed"}
        names = _cached_shell_names(shell[0], shell_names if shell_names is not None else _shell_names_cache())
        if names is not None and not waves.pick(waves.bundle_name(path), names):
            return {"code": "not_licensed", "reason": "the Waves shell doesn't offer it: most likely it isn't "
                                                      "licensed (check Waves Central)"}
    last = last_result(path)
    # A "32-bit" failure recorded before this copy had its 32-bit host no longer holds.
    stale = last and last["code"] == "bitness" and fmt == "VST2" and x86_worker() is not None
    if last and not last["ok"] and not stale:
        return {"code": last["code"] or "failed", "reason": last["reason"] or "it failed to load", "at": last["at"]}
    return None


def forget_shell_names(host_path: str) -> None:
    """Drops the cached plugin list of a Waves bundle's shell, so the next open
    lists it again (a shell only lists what's licensed, and licensing a plugin
    in Waves Central doesn't change the shell file the cache is keyed on)."""
    shell = waves.find_shell(host_path)
    if shell is None:
        return
    prefix = shell[0].lower() + "|"
    path = HOSTING_DIR / "shell-names.json"
    with _status_lock:
        cache = _shell_names_cache()
        kept = {k: v for k, v in cache.items() if not k.startswith(prefix)}
        if len(kept) == len(cache):
            return
        try:
            tmp = path.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(kept), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass


def _cached_shell_names(shell: str, cache: dict) -> list[str] | None:
    try:
        st = os.stat(shell)
    except OSError:
        return None
    return cache.get(f"{shell.lower()}|{st.st_size}|{int(st.st_mtime)}")


def verified(host_path: str) -> bool:
    """It opened (or passed a check) since its file last changed."""
    last = last_result(host_path)
    return bool(last and last["ok"])


# ---------- instruments whose sound crashes them ----------
# Some instruments open fine but crash as soon as they render (seen: iZotope
# BreakTweaker, which seems to need a DAW's transport). Remembered per file
# (path + mtime, like host-status.json) so the next open shows the editor with
# the sound off instead of crashing again; playing 5 s clears it.

def _sound_crashes() -> dict:
    try:
        with open(SOUND_CRASHES_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _write_sound_crashes(data: dict) -> None:
    try:
        HOSTING_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SOUND_CRASHES_FILE.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, SOUND_CRASHES_FILE)
    except OSError:
        pass


def sound_crash(host_path: str) -> dict | None:
    """{at, mtime} if this plugin crashed while rendering since its file last changed."""
    with _status_lock:
        entry = _sound_crashes().get(host_path.lower())
    return entry if entry and entry.get("mtime") == _file_time(host_path) else None


def record_sound_crash(host_path: str) -> None:
    with _status_lock:
        data = _sound_crashes()
        data[host_path.lower()] = {"at": datetime.datetime.now().isoformat(timespec="seconds"),
                                   "mtime": _file_time(host_path)}
        _write_sound_crashes(data)


def clear_sound_crash(host_path: str) -> None:
    with _status_lock:
        data = _sound_crashes()
        if data.pop(host_path.lower(), None) is not None:
            _write_sound_crashes(data)


# ---------- checking many plugins (headless) ----------

_check = {"running": False, "total": 0, "done": 0, "ok": 0, "failed": 0, "current": [], "started_at": None,
          "finished_at": None, "cancelled": False}
_check_lock = threading.Lock()
_check_procs: set = set()


def check_status() -> dict:
    with _check_lock:
        return {**_check, "current": list(_check["current"])}


def check_cancel() -> dict:
    with _check_lock:
        _check["cancelled"] = True
        procs = list(_check_procs)
    for proc in procs:
        kill_tree(proc)
    return check_status()


def _probe(row) -> dict:
    """Loads one plugin headlessly in a worker (--probe) and returns
    {ok, code?, reason?, load_s?}. A dialog the plugin opens can't be answered
    here: it ends as a timeout naming the window."""
    try:
        path, args = launch_target(row["host_path"], row["format"], row["name"])
    except HostError as e:
        return {"ok": False, "code": "format", "reason": str(e)}
    proc, job = _spawn_worker(path, [*args, "--probe"], stdin=subprocess.DEVNULL)
    with _check_lock:
        _check_procs.add(proc)
    result, dialog = {}, None
    instrument, rendered = False, None
    timer = threading.Timer(CHECK_TIMEOUT_S, kill_tree, args=(proc,))
    timer.daemon = True
    timer.start()
    try:
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            event = msg.get("event")
            if event == "rendered":
                rendered = bool(msg.get("ok"))
            if event == "probed":
                instrument = bool(msg.get("is_instrument"))
                result = {"ok": True, "load_s": msg.get("load_s")}
            elif event == "error" and not result:
                result = {"ok": False, "code": msg.get("code") or "failed",
                          "reason": (msg.get("message") or "").removeprefix("Can't open this plugin: ")}
            elif event == "status" and msg.get("dialog"):
                dialog = msg.get("message")
        proc.wait(timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass
    finally:
        timer.cancel()
        kill_tree(proc)
        with _check_lock:
            _check_procs.discard(proc)
        memory = memory_reason(job)
        if job:
            job.close()
    if result.get("ok") and instrument and not _check["cancelled"]:
        # It loaded; whether it survives playing decides if its sound starts by itself.
        if rendered is None and proc.returncode not in (None, 0, 1):
            record_sound_crash(row["host_path"])
        elif rendered:
            clear_sound_crash(row["host_path"])
    if memory and not result.get("ok"):
        outcome = {"ok": False, "code": "memory", "reason": memory}
    elif result:
        outcome = result
    elif _check["cancelled"]:
        return {}
    elif dialog:
        outcome = {"ok": False, "code": "timeout", "reason": f"it waits for an answer in its own window: {dialog}"}
    else:
        outcome = {"ok": False, "code": "crashed" if proc.returncode not in (None, 1) else "timeout",
                   "reason": "the plugin crashed while loading" if proc.returncode not in (None, 1)
                   else f"it didn't finish loading in {CHECK_TIMEOUT_S} s"}
    if not outcome.get("ok"):
        lines = [f"Check failed for {row['name'] or os.path.basename(row['host_path'])}: "
                 f"{outcome['reason']} [{outcome['code']}]",
                 f"  File:      {row['host_path']} ({row['format']})"]
        if describe_exit(proc.returncode):
            lines.append(f"  Exit code: {describe_exit(proc.returncode)}")
        tail = list(_stderr_tails.pop(proc.pid, ()))
        if tail:
            lines.append("  Last worker output:")
            lines += [f"    {t}" for t in tail[-10:]]
        logbuf.add(f"check:{row['name'] or os.path.basename(row['host_path'])}", "\n".join(lines), "warning")
    else:
        _stderr_tails.pop(proc.pid, None)
    return outcome


def check_start(rows, label=lambda r: r["name"]) -> dict:
    """Checks `rows` (catalog rows) in the background, CHECK_JOBS at a time,
    recording each result like a real open does. One check at a time."""
    rows = list(rows)
    with _check_lock:
        if _check["running"]:
            return {**_check, "current": list(_check["current"])}
        _check.update(running=True, total=len(rows), done=0, ok=0, failed=0, current=[], cancelled=False,
                      started_at=datetime.datetime.now().isoformat(timespec="seconds"), finished_at=None)

    def run():
        import concurrent.futures

        def one(row):
            with _check_lock:
                if _check["cancelled"]:
                    return
                _check["current"].append(label(row))
            try:
                result = _probe(row)
            finally:
                with _check_lock:
                    if label(row) in _check["current"]:
                        _check["current"].remove(label(row))
            if not result:
                return
            record_result(row["host_path"], **result)
            with _check_lock:
                _check["done"] += 1
                _check["ok" if result["ok"] else "failed"] += 1

        try:
            with concurrent.futures.ThreadPoolExecutor(CHECK_JOBS) as pool:
                list(pool.map(one, rows))
        finally:
            with _check_lock:
                _check.update(running=False, current=[],
                              finished_at=datetime.datetime.now().isoformat(timespec="seconds"))

    threading.Thread(target=run, daemon=True).start()
    return check_status()


# ---------- pictures: capturing every plugin's editor in the background ----------
# Each plugin opens in its own worker with --capture: its editor is cloaked
# from the start (never on the screen; host_worker._Hold), the worker waits
# until it has drawn itself and saves it as the plugin's picture (image_path:
# the same file Capture › "Set as plugin picture" writes), then exits. One at
# a time. Results are kept per file (picture-capture.json, path + mtime like
# host-status.json) so "only missing" runs don't retry a plugin that can't be
# captured until its file changes, unless its id is asked for explicitly.

PICTURES_FILE = HOSTING_DIR / "picture-capture.json"
PICTURE_LOAD_TIMEOUT_S = 150  # loading, like the check (Waves shells, big libraries)
PICTURE_DRAW_TIMEOUT_S = 60  # after loading: host_worker.CAPTURE_WAIT_S and a margin
PICTURE_RESULTS_KEPT = 1000

_pictures = {"running": False, "total": 0, "done": 0, "ok": 0, "failed": 0, "current": None, "started_at": None,
             "finished_at": None, "cancelled": False, "results": []}
_pictures_lock = threading.Lock()
_pictures_current: dict = {"proc": None}


def _picture_results() -> dict:
    try:
        with open(PICTURES_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def picture_capture_result(host_path: str, results: dict | None = None) -> dict | None:
    """{ok, reason, at} of the last automatic capture of this plugin, if its
    file hasn't changed since; None if it was never tried. Pass
    _picture_results() as `results` when asking for many plugins."""
    entry = (results if results is not None else _picture_results()).get(host_path.lower())
    if not entry or entry.get("mtime") != _file_time(host_path):
        return None
    return {"ok": bool(entry.get("ok")), "reason": entry.get("reason"), "at": entry.get("at")}


def picture_results() -> dict:
    """All recorded automatic-capture results (for picture_capture_result)."""
    with _status_lock:
        return _picture_results()


def _record_picture(host_path: str, ok: bool, reason: str | None = None, code: str | None = None,
                    method: str | None = None) -> None:
    with _status_lock:
        data = _picture_results()
        data[host_path.lower()] = {"ok": ok, "code": None if ok else code, "reason": None if ok else reason,
                                   "method": method, "at": datetime.datetime.now().isoformat(timespec="seconds"),
                                   "mtime": _file_time(host_path)}
        try:
            HOSTING_DIR.mkdir(parents=True, exist_ok=True)
            tmp = PICTURES_FILE.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
            os.replace(tmp, PICTURES_FILE)
        except OSError:
            pass


def pictures_status() -> dict:
    with _pictures_lock:
        return {**_pictures, "results": list(_pictures["results"])}


def pictures_cancel() -> dict:
    with _pictures_lock:
        if _pictures["running"]:
            _pictures["cancelled"] = True
        proc = _pictures_current["proc"]
    if proc is not None:
        kill_tree(proc)
    return pictures_status()


def _dialog_reason(message: str) -> str:
    title = re.search(r"“(.+?)”", message or "")
    window = f" (“{title.group(1)}”)" if title else ""
    return (f"needs attention: it opened a window{window} that needs an answer; open it once in the "
            "Playground and answer it")


def _capture_one(row) -> dict | None:
    """Captures one plugin's editor in a worker of its own. {ok, code, reason,
    method}; None if the job was cancelled meanwhile."""
    try:
        path, args = launch_target(row["host_path"], row["format"], row["name"])
    except HostError as e:
        return {"ok": False, "code": "format", "reason": str(e)}
    target = image_path(row["host_path"])
    proc, job = _spawn_worker(path, [*args, "--capture", target])
    with _pictures_lock:
        _pictures_current["proc"] = proc
    result: dict | None = None
    phase = {"loaded": False, "since": time.monotonic(), "timed_out": False}

    def watchdog():
        while proc.poll() is None:
            limit = PICTURE_DRAW_TIMEOUT_S if phase["loaded"] else PICTURE_LOAD_TIMEOUT_S
            if time.monotonic() - phase["since"] > limit:
                phase["timed_out"] = True
                kill_tree(proc)
                return
            time.sleep(0.5)

    threading.Thread(target=watchdog, daemon=True).start()
    try:
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            event = msg.get("event")
            if event == "status" and msg.get("dialog"):
                result = {"ok": False, "code": "dialog", "reason": _dialog_reason(msg.get("message") or "")}
                kill_tree(proc)
                break
            if event == "loaded":
                phase.update(loaded=True, since=time.monotonic())
                record_result(row["host_path"], ok=True, load_s=msg.get("load_s"))
            elif event == "error" and result is None:
                code = msg.get("code") or "failed"
                reason = (msg.get("message") or "it failed").removeprefix("Can't open this plugin: ")
                if code == "dialog":
                    reason = _dialog_reason(msg.get("message") or "")
                result = {"ok": False, "code": code, "reason": reason}
                if not phase["loaded"] and code not in ("dialog", "blank", "no_editor"):
                    record_result(row["host_path"], ok=False, code=code, reason=reason)
            elif event == "captured_auto":
                result = {"ok": True, "code": None, "reason": None, "method": msg.get("method")}
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
    except (OSError, ValueError):
        pass
    finally:
        kill_tree(proc)
        memory = memory_reason(job)
        if job:
            job.close()
        with _pictures_lock:
            _pictures_current["proc"] = None
    if _pictures["cancelled"] and not (result and result["ok"]):
        return None
    if memory:
        return {"ok": False, "code": "memory", "reason": memory}
    if result is not None:
        return result
    if phase["timed_out"]:
        return {"ok": False, "code": "timeout",
                "reason": "its editor didn't draw itself in time" if phase["loaded"]
                else f"it didn't finish loading in {PICTURE_LOAD_TIMEOUT_S} s"}
    return {"ok": False, "code": "crashed", "reason": "the plugin crashed" + ("" if phase["loaded"] else " while loading")}


def pictures_start(rows, label=lambda r: r["name"], on_saved=None) -> dict:
    """Captures the editors of `rows` (catalog rows) one after the other, in
    the background; returns the job's state (the running one if a job runs
    already). `on_saved(row)` runs after each picture is saved (the web app
    records the picture's source)."""
    rows = list(rows)
    with _pictures_lock:
        if _pictures["running"]:
            return {**_pictures, "results": list(_pictures["results"])}
        _pictures.update(running=True, total=len(rows), done=0, ok=0, failed=0, current=None, cancelled=False,
                         started_at=datetime.datetime.now().isoformat(timespec="seconds"), finished_at=None,
                         results=[])

    def run():
        try:
            for row in rows:
                with _pictures_lock:
                    if _pictures["cancelled"]:
                        break
                    _pictures["current"] = label(row)
                try:
                    result = _capture_one(row)
                except Exception as e:  # noqa: BLE001 - one plugin must not end the job
                    result = {"ok": False, "code": "failed", "reason": str(e) or type(e).__name__}
                if result is None:
                    break  # cancelled
                if result["ok"]:
                    _record_picture(row["host_path"], ok=True, method=result.get("method"))
                    if on_saved:
                        try:
                            on_saved(row)
                        except Exception:  # noqa: BLE001
                            pass
                else:
                    _record_picture(row["host_path"], ok=False, reason=result["reason"], code=result["code"])
                with _pictures_lock:
                    _pictures["done"] += 1
                    _pictures["ok" if result["ok"] else "failed"] += 1
                    _pictures["results"].append({"id": row["id"], "name": label(row), "ok": result["ok"],
                                                 "reason": result["reason"]})
                    del _pictures["results"][:-PICTURE_RESULTS_KEPT]
        finally:
            with _pictures_lock:
                _pictures.update(running=False, current=None,
                                 finished_at=datetime.datetime.now().isoformat(timespec="seconds"))

    threading.Thread(target=run, daemon=True).start()
    return pictures_status()


def state(plugin_id: int) -> dict:
    s = _sessions.get(plugin_id)
    return s.snapshot() if s else dict(_CLOSED)


def _no_instrument(s: _Session) -> None:
    if s.snapshot()["is_instrument"]:
        raise HostError("this is an instrument: it has no audio input to play through")


def play(plugin_id: int, source: str, start_s: float = 0, audio: dict | None = None, envelope=None,
         loop=None, dry: bool = False, until=None, mix=None) -> dict:
    """Plays the audio file `source` through the plugin from `start_s` (the
    source becomes 'file'; browser audio stops being routed). `audio` is
    {device, samplerate, blocksize}, read from the settings by the caller.
    The Audio tab adds its edits (`envelope`, host_worker.envelope_gain), a
    `loop` [start_s, end_s], or `dry` (its recording: no plugin, no gains);
    `until` [start_s, end_s]: a selection played once (it ends at end_s);
    `mix` {path, source, recording}: its recording plays along, into the plugin."""
    s = _open_session(plugin_id)
    _no_instrument(s)  # (dry too: playing a file would stop the instrument's sound)
    source = (source or "").strip().strip('"')
    try:
        host_worker.check_source(source)
    except ValueError as e:
        raise HostError(f"Can't play that: {e}.") from None
    with s.lock:
        s.state["error"] = None
    # The worker answers once playing has started (or failed).
    s.command("play", timeout=15, source=source, start_s=max(0.0, float(start_s or 0)), envelope=envelope or [],
              loop=loop, dry=bool(dry), until=until, mix=mix, **(audio or {}))
    return state(plugin_id)


def set_mix(plugin_id: int, source: float | None, recording: float | None) -> dict:
    """The gains of the file and of its recording playing along (the M buttons)."""
    s = _open_session(plugin_id)
    s.command("mix", source=source, recording=recording)
    return state(plugin_id)


def set_loop(plugin_id: int, loop) -> dict:
    """Loops [start_s, end_s] of the file playing (None: no loop)."""
    s = _open_session(plugin_id)
    s.command("loop", range=loop)
    with s.lock:
        s.state["loop"] = list(loop) if loop else None
    return state(plugin_id)


def set_envelope(source: str, envelope: list) -> None:
    """The Audio tab's edits changed: an open plugin playing `source` hears them at once."""
    for s in _open_sessions():
        snap = s.snapshot()
        if snap["playing"] and not snap["dry"] and (snap["source"] or "").lower() == source.lower():
            try:
                s.send("edit", envelope=envelope)
            except HostError:
                pass


def record(plugin_id: int, on: bool, path: str | None = None, duration_s: float | None = None) -> dict:
    """Arms (or disarms) the Audio tab's recording: while the file plays through
    the plugin, what comes out is written to `path` (host_worker._Recorder)."""
    s = _open_session(plugin_id)
    if on:
        _no_instrument(s)
        s.command("record", on=True, path=path, duration_s=duration_s)
    else:
        s.command("record", on=False)
    return state(plugin_id)


def recording_sessions() -> bool:
    """A worker has the recording armed (its file may be open)."""
    return any(s.snapshot()["recording"] for s in _open_sessions())


def stop_dry() -> None:
    """Stops the Audio tab's recording playing (back, or along with the file) before its
    file is deleted or replaced."""
    for s in _open_sessions():
        snap = s.snapshot()
        if (snap["dry"] or snap["mix"]) and snap["playing"]:
            try:
                s.command("stop")
            except HostError:
                pass


def _record_news(news: dict) -> None:
    if _providers["record_tap"]:
        try:
            _providers["record_tap"](news)
        except Exception:  # noqa: BLE001 - the waveform must never break a session
            pass


def set_source(plugin_id: int, kind: str | None, audio: dict | None = None) -> dict:
    """'browser': the in-app browser's audio plays through the plugin (file
    playback stops). 'file': browser audio stops; a file plays on play().
    None: everything stops."""
    s = _open_session(plugin_id)
    audio = audio or {}
    if kind == "browser":
        _no_instrument(s)
        with s.lock:
            s.state["error"] = None
        s.command("live", timeout=10, on=True, rate=int(audio.get("samplerate") or 48000), **audio)
    elif kind == "file":
        s.command("live", on=False)
        with s.lock:
            s.state.update(source_kind="file", live=None)
    else:
        s.command("stop")
        with s.lock:
            s.state.update(source_kind=None, live=None, error=None)
    _changed()
    return state(plugin_id)


def pause(plugin_id: int) -> dict:
    _open_session(plugin_id).command("pause")
    return state(plugin_id)


def resume(plugin_id: int) -> dict:
    _open_session(plugin_id).command("resume")
    return state(plugin_id)


def seek(plugin_id: int, seconds: float) -> dict:
    s = _open_session(plugin_id)
    if not s.snapshot()["playing"]:
        raise HostError("nothing is playing; press Play first")
    s.command("seek", seconds=max(0.0, float(seconds)))
    return state(plugin_id)


def stop(plugin_id: int) -> dict:
    """Stops whatever plays (a file or the browser's audio)."""
    s = _session(plugin_id)
    if s and s.snapshot()["state"] == "open":
        s.command("stop")
        with s.lock:
            # A stopped player has nothing left to explain.
            s.state.update(source_kind=None, live=None, error=None)
        _changed()
    return state(plugin_id)


def bypass(plugin_id: int, on: bool) -> dict:
    """Dry signal (the plugin keeps processing, unheard). Kept for later plays too."""
    _open_session(plugin_id).command("bypass", on=bool(on))
    return state(plugin_id)


# ---------- IN / OUT gains and Auto-Balance (the frame's knobs) ----------

GAINS_FILE = HOSTING_DIR / "gains.json"
_GAIN_DEFAULT = {"in_db": 0.0, "out_db": 0.0, "auto_balance": False}


def saved_gain(host_path: str) -> dict:
    """{in_db, out_db, auto_balance} last set for this plugin (defaults: 0 dB, off)."""
    try:
        with open(GAINS_FILE, encoding="utf-8") as f:
            entry = json.load(f).get(host_path.lower()) or {}
    except (OSError, ValueError):
        entry = {}
    return {**_GAIN_DEFAULT, **{k: entry[k] for k in _GAIN_DEFAULT if k in entry}}


def set_gain(plugin_id: int, in_db: float | None = None, out_db: float | None = None,
             auto_balance: bool | None = None) -> dict:
    """Changes the open plugin's IN / OUT gain (dB, -60 = silence .. +12) or
    Auto-Balance, and keeps them for the next time it opens."""
    s = _open_session(plugin_id)
    fields = {k: v for k, v in (("in_db", in_db), ("out_db", out_db), ("auto_balance", auto_balance)) if v is not None}
    for k in ("in_db", "out_db"):
        if k in fields:
            fields[k] = max(host_worker.GAIN_MIN_DB, min(host_worker.GAIN_MAX_DB, float(fields[k])))
    s.command("gain", timeout=5, **fields)
    with _status_lock:
        try:
            with open(GAINS_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        data[s.host_path.lower()] = {**saved_gain(s.host_path), **fields}
        try:
            HOSTING_DIR.mkdir(parents=True, exist_ok=True)
            tmp = GAINS_FILE.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
            os.replace(tmp, GAINS_FILE)
        except OSError:
            pass
    return state(plugin_id)


# ---------- MIDI and instruments ----------

def send_midi(plugin_id: int, events: list) -> None:
    """MIDI from the Playground's keyboard: [[status, data1, data2], ...]
    channel messages, sent without waiting for the worker (a note must not
    wait for a round trip)."""
    s = _open_session(plugin_id)
    if not s.snapshot()["accepts_midi"]:
        raise HostError("this plugin doesn't take MIDI")
    s.send("midi", events=events)


def panic(plugin_id: int) -> dict:
    """All notes off (and sustain off, all sound off) on every channel."""
    _open_session(plugin_id).command("panic")
    return state(plugin_id)


def set_sound(plugin_id: int, on: bool) -> dict:
    """An instrument's sound on (with the current audio settings) or off."""
    s = _open_session(plugin_id)
    if not s.snapshot()["is_instrument"]:
        raise HostError("only an instrument's sound can be turned on or off")
    if on:
        s.start_sound()
    else:
        s.command("instrument", on=False)
    return state(plugin_id)


def _release_midi() -> None:
    """The toolbar's MIDI monitor closes the inputs it watches (a worker opens them next)."""
    if _providers["midi_release"]:
        try:
            _providers["midi_release"]()
        except Exception:  # noqa: BLE001
            pass


def sessions_busy() -> bool:
    """A plugin session exists (loading or open): its worker owns the MIDI inputs."""
    return any(s.alive() for s in list(_sessions.values()))


def _open_sessions() -> list:
    return [s for s in list(_sessions.values()) if s.alive() and s.snapshot()["state"] == "open"]


def apply_audio_settings() -> None:
    """The audio settings changed: whatever plays in the open plugin restarts
    with them now (a file from where it is). Called by the web app."""
    audio = _audio_settings()
    for s in _open_sessions():
        if s.snapshot()["audio"] is not None:
            try:
                s.command("reconfigure", timeout=15, **audio)
            except HostError as e:
                with s.lock:
                    s.state["error"] = str(e)


def apply_midi_settings() -> None:
    """The MIDI settings changed: the open plugin's inputs follow now."""
    midi = _midi_settings()
    sessions = _open_sessions()
    if sessions:
        _release_midi()
    for s in sessions:
        try:
            s.command("midi_inputs", timeout=10, inputs=midi["inputs"], channel=midi["channel"],
                      block_pitchbend=midi.get("block_pitchbend", False))
        except HostError:
            pass


def test_tone() -> dict | None:
    """Configuration's Test while the open plugin plays: the tone is mixed into
    its sound (it holds the device). Returns that stream's info, or None when
    nothing plays (then the device is free for a test of its own)."""
    for s in _open_sessions():
        if s.snapshot()["audio"] is None:
            continue
        try:
            s.command("tone", timeout=5)
        except HostError:
            continue
        return s.snapshot()["audio"]
    return None


def open_midi_inputs() -> dict | None:
    """What the open plugin's worker has opened ({open, failed, available}), if any."""
    for s in _open_sessions():
        midi = s.snapshot()["midi"]
        if midi and midi.get("inputs") is not None:
            return midi["inputs"]
    return None


def browser_target() -> tuple[int, str, int] | None:
    """Where the browser's audio goes: (port, key, rate) of the open plugin's
    live input while its source is the browser and live playback runs."""
    for s in list(_sessions.values()):
        if not s.alive():
            continue
        with s.lock:
            st = s.state
            if (st["state"] == "open" and st["source_kind"] == "browser" and st["live"] is not None
                    and s.live_port and s.live_key):
                return s.live_port, s.live_key, int(st["live"].get("rate") or 48000)
    return None


def active_tap() -> tuple[str, str] | None:
    """(ring file, plugin name) of the open plugin's analysis ring, else None
    (app/analysis_tap.py picks the ring to read from it)."""
    for s in list(_sessions.values()):
        if not s.alive() or not s.tap_path:
            continue
        with s.lock:
            st = s.state
            if st["state"] == "open":
                return s.tap_path, st.get("plugin_name") or s.label or Path(s.host_path).stem
    return None


# ---------- capture ----------

def _pictures_dir() -> Path:
    """The user's Pictures folder (follows a redirected/OneDrive one), else %USERPROFILE%\\Pictures."""
    return known_folder("{33E28130-4E1E-4676-835A-98395C3BC3BB}", "Pictures")


def known_folder(folder_guid: str, fallback: str) -> Path:
    """A Windows known folder (a FOLDERID_* GUID; follows a redirected/OneDrive
    one), else %USERPROFILE%\\<fallback>."""
    class GUID(ctypes.Structure):
        _fields_ = [("d1", ctypes.c_ulong), ("d2", ctypes.c_ushort), ("d3", ctypes.c_ushort),
                    ("d4", ctypes.c_ubyte * 8)]

    folder_id = GUID.from_buffer_copy(uuid.UUID(folder_guid).bytes_le)
    path = ctypes.c_wchar_p()
    try:
        shell32 = ctypes.WinDLL("shell32")
        shell32.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE,
                                                 ctypes.POINTER(ctypes.c_wchar_p)]
        if shell32.SHGetKnownFolderPath(ctypes.byref(folder_id), 0, None, ctypes.byref(path)) == 0 and path.value:
            found = Path(path.value)
            ctypes.windll.ole32.CoTaskMemFree(path)
            return found
    except OSError:
        pass
    return Path(os.environ.get("USERPROFILE") or Path.home()) / fallback


_UNSAFE_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def safe_file_name(name: str) -> str:
    """`name` usable as a Windows file name (no reserved characters or names)."""
    cleaned = _UNSAFE_NAME.sub(" ", name or "").strip().rstrip(". ")
    cleaned = re.sub(r"\s+", " ", cleaned)[:100].rstrip(". ") or "Plugin"
    if cleaned.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                                         *(f"LPT{i}" for i in range(1, 10))}:
        cleaned = f"_{cleaned}"
    return cleaned


def _capture_to(s: _Session, path: str) -> None:
    before = os.path.getmtime(path) if os.path.exists(path) else None
    s.command("capture", timeout=15, path=path)
    if not os.path.exists(path) or os.path.getmtime(path) == before:
        raise HostError("capturing the editor timed out")


def capture(plugin_id: int, host_path: str, name: str, target: str = "picture") -> str | None:
    """Screenshots the editor. 'picture': the plugin's picture (keyed by path);
    'file': a new PNG in Pictures\\VEXT; 'clipboard': copied as an image.
    Returns the PNG's path (None for the clipboard)."""
    if target not in CAPTURE_TARGETS:
        raise HostError(f"target must be one of: {', '.join(CAPTURE_TARGETS)}")
    s = _open_session(plugin_id)
    if target == "picture":
        path = image_path(host_path)
    elif target == "file":
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H-%M-%S")
        folder = _pictures_dir() / "VEXT"
        path = str(folder / f"{safe_file_name(name)} {stamp}.png")
        for n in itertools.count(2):  # two captures within the same second
            if not os.path.exists(path):
                break
            path = str(folder / f"{safe_file_name(name)} {stamp} ({n}).png")
    else:
        path = os.path.join(tempfile.gettempdir(), f"vext-capture-{uuid.uuid4().hex}.png")
    _capture_to(s, path)
    if target != "clipboard":
        return path
    try:
        copy_image_to_clipboard(path)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    return None


# ---------- clipboard ----------

def copy_image_to_clipboard(png_path: str) -> None:
    """Puts the PNG on the Windows clipboard as CF_DIB (what every app pastes),
    plus the registered "PNG" format (lossless, for apps that prefer it)."""
    from PIL import Image

    with Image.open(png_path) as image:
        bmp = io.BytesIO()
        image.convert("RGB").save(bmp, "BMP")
    dib = bmp.getvalue()[14:]  # CF_DIB = the BMP file without its BITMAPFILEHEADER
    with open(png_path, "rb") as f:
        png = f.read()

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
    user32.RegisterClipboardFormatW.restype = wintypes.UINT
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    GMEM_MOVEABLE, CF_DIB = 0x0002, 8

    def put(fmt: int, data: bytes) -> bool:
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            return False
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            kernel32.GlobalFree(handle)
            return False
        ctypes.memmove(pointer, data, len(data))
        kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(fmt, handle):
            kernel32.GlobalFree(handle)  # still ours: the clipboard didn't take it
            return False
        return True  # owned by the clipboard now

    for _attempt in range(20):  # another app may hold the clipboard for a moment
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        raise HostError("the clipboard is busy; try again")
    try:
        user32.EmptyClipboard()
        if not put(CF_DIB, dib):
            raise HostError(f"couldn't copy the image (error {ctypes.get_last_error()})")
        png_format = user32.RegisterClipboardFormatW("PNG")
        if png_format:
            put(png_format, png)
    finally:
        user32.CloseClipboard()


# ---------- closing ----------

def close(plugin_id: int) -> None:
    with _sessions_lock:
        s = _sessions.pop(plugin_id, None)
    if s and s.alive():
        s.closing = True
        try:
            s.proc.stdin.write(json.dumps({"cmd": "quit"}) + "\n")
            s.proc.stdin.flush()
            s.proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            kill_tree(s.proc)
    if s:
        _changed()


def close_all() -> None:
    for pid in list(_sessions):
        close(pid)
    check_cancel()
    pictures_cancel()
    stop_standby()


# ---------- audio output devices ----------

_devices_lock = host_worker.PORTAUDIO_LOCK  # shared with app/audio_setup.py


def audio_devices() -> list[dict]:
    """Output devices the worker can play to (see host_worker.output_devices).
    PortAudio only sees the devices present when it starts, so it restarts
    here each time: a device plugged in since shows up."""
    import sounddevice as sd

    with _devices_lock:
        try:
            sd._terminate()
            sd._initialize()
        except Exception:  # noqa: BLE001 - a stale list beats no list
            pass
        return host_worker.output_devices(sd)


atexit.register(close_all)
