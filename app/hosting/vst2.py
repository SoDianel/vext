"""A minimal VST2 host (the AEffect C ABI, driven through ctypes): load, audio
processing and the embedded editor. Used by the host worker (app/host_worker.py)
for `.dll` plugins; it mirrors the part of pedalboard's plugin object the worker
uses (`name`, `is_instrument`, `__call__(audio, rate, reset)`, `reset()`,
`show_editor(close_event)`), plus `editor_hwnd`, `close()`, and MIDI:
`accepts_midi` and `__call__(..., midi=[(frame offset, bytes)])`, delivered as
VstMidiEvents (effProcessEvents) right before each processReplacing.

No VST SDK is needed: the ABI is plain C (struct AEffect + two callbacks), the
numbers below are the published opcode values.

Threads: everything except processing runs on the thread that created the
plugin (the worker's main thread, which also runs the editor's message loop);
the playback thread only calls process. A sample-rate change requested by the
playback thread is applied on the main thread (MainThread.call).
"""

import ctypes
import os
import sys
import threading
from ctypes import POINTER, c_char_p, c_double, c_float, c_int16, c_int32, c_void_p

from app.hosting import win_editor

VstIntPtr = ctypes.c_ssize_t
kVstMagic = 0x56737450  # 'VstP'

# AEffect flags
effFlagsHasEditor, effFlagsCanReplacing, effFlagsIsSynth = 1 << 0, 1 << 4, 1 << 8

# dispatcher opcodes (plugin side)
effOpen, effClose, effSetSampleRate, effSetBlockSize, effMainsChanged = 0, 1, 10, 11, 12
effEditGetRect, effEditOpen, effEditClose, effEditIdle = 13, 14, 15, 19
effGetPlugCategory, effGetEffectName, effGetVendorString, effGetProductString = 35, 45, 47, 48
effCanDo, effShellGetNextPlugin, effStartProcess, effStopProcess = 51, 70, 71, 72
effProcessEvents = 25
kPlugCategShell = 10
kVstMidiType, kVstMidiEventIsRealtime = 1, 1
MAX_EVENTS = 512  # MIDI events per processing block

# audioMaster opcodes (host side)
amAutomate, amVersion, amCurrentId, amIdle, amWantMidi, amGetTime, amProcessEvents = 0, 1, 2, 3, 6, 7, 8
amIOChanged, amNeedIdle, amSizeWindow, amGetSampleRate, amGetBlockSize = 13, 14, 15, 16, 17
amGetInputLatency, amGetOutputLatency, amGetCurrentProcessLevel, amGetAutomationState = 18, 19, 23, 24
amGetVendorString, amGetProductString, amGetVendorVersion, amCanDo, amGetLanguage = 32, 33, 34, 37, 38
amGetDirectory, amUpdateDisplay, amBeginEdit, amEndEdit = 41, 42, 43, 44

HOST_CAN_DO = {b"sendVstEvents", b"sendVstMidiEvent", b"sendVstTimeInfo", b"receiveVstEvents",
               b"receiveVstMidiEvent", b"sizeWindow", b"supplyIdle", b"startStopProcess", b"supportShell",
               b"shellCategory"}

AudioMasterCallback = ctypes.CFUNCTYPE(VstIntPtr, c_void_p, c_int32, c_int32, VstIntPtr, c_void_p, c_float)
DispatcherProc = ctypes.CFUNCTYPE(VstIntPtr, c_void_p, c_int32, c_int32, VstIntPtr, c_void_p, c_float)
ProcessProc = ctypes.CFUNCTYPE(None, c_void_p, POINTER(POINTER(c_float)), POINTER(POINTER(c_float)), c_int32)
SetParameterProc = ctypes.CFUNCTYPE(None, c_void_p, c_int32, c_float)
GetParameterProc = ctypes.CFUNCTYPE(c_float, c_void_p, c_int32)


class AEffect(ctypes.Structure):
    _fields_ = [
        ("magic", c_int32), ("dispatcher", DispatcherProc), ("process", ProcessProc),
        ("setParameter", SetParameterProc), ("getParameter", GetParameterProc),
        ("numPrograms", c_int32), ("numParams", c_int32), ("numInputs", c_int32), ("numOutputs", c_int32),
        ("flags", c_int32), ("resvd1", VstIntPtr), ("resvd2", VstIntPtr), ("initialDelay", c_int32),
        ("realQualities", c_int32), ("offQualities", c_int32), ("ioRatio", c_float),
        ("object", c_void_p), ("user", c_void_p), ("uniqueID", c_int32), ("version", c_int32),
        ("processReplacing", ProcessProc), ("processDoubleReplacing", c_void_p), ("future", ctypes.c_char * 56),
    ]


class ERect(ctypes.Structure):
    _fields_ = [("top", c_int16), ("left", c_int16), ("bottom", c_int16), ("right", c_int16)]


class VstMidiEvent(ctypes.Structure):
    _fields_ = [
        ("type", c_int32), ("byteSize", c_int32), ("deltaFrames", c_int32), ("flags", c_int32),
        ("noteLength", c_int32), ("noteOffset", c_int32), ("midiData", ctypes.c_ubyte * 4),
        ("detune", ctypes.c_byte), ("noteOffVelocity", ctypes.c_byte), ("reserved1", ctypes.c_byte),
        ("reserved2", ctypes.c_byte),
    ]


class VstEvents(ctypes.Structure):  # VstEvent* events[numEvents]: sized for MAX_EVENTS here
    _fields_ = [("numEvents", c_int32), ("reserved", VstIntPtr), ("events", c_void_p * MAX_EVENTS)]


class VstTimeInfo(ctypes.Structure):
    _fields_ = [
        ("samplePos", c_double), ("sampleRate", c_double), ("nanoSeconds", c_double), ("ppqPos", c_double),
        ("tempo", c_double), ("barStartPos", c_double), ("cycleStartPos", c_double), ("cycleEndPos", c_double),
        ("timeSigNumerator", c_int32), ("timeSigDenominator", c_int32), ("smpteOffset", c_int32),
        ("smpteFrameRate", c_int32), ("samplesToNextClock", c_int32), ("flags", c_int32),
    ]


# VstTimeInfo flags: playing, and which fields are valid
kVstTransportPlaying, kVstPpqPosValid, kVstTempoValid, kVstBarsValid, kVstTimeSigValid = 2, 512, 1024, 2048, 8192
TEMPO = 120.0

LOAD_WITH_ALTERED_SEARCH_PATH = 0x00000008


class Vst2Error(RuntimeError):
    pass


def _log(msg: str) -> None:
    try:
        sys.stderr.write(f"[vst2] {msg}\n")
        sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass


def _copy_str(ptr, text: bytes, size: int) -> None:
    if ptr:
        ctypes.memmove(ptr, text[: size - 1] + b"\0", min(len(text), size - 1) + 1)


class Vst2Plugin:
    MAX_BLOCK = 4096

    def __init__(self, path: str, main: win_editor.MainThread, sample_rate: int = 48000, block_size: int = 1024,
                 shell_name=None):
        import numpy as np

        self._np = np
        self.path = path
        self.main = main
        self.sample_rate = int(sample_rate)
        self.block_size = int(min(block_size, self.MAX_BLOCK))
        self.editor_hwnd: int | None = None
        self._window: win_editor.EditorWindow | None = None
        self._audio_thread: int | None = None
        self._lock = threading.Lock()  # process vs. reconfigure
        self._shell_id = 0
        self._time = VstTimeInfo(sampleRate=self.sample_rate, tempo=TEMPO, timeSigNumerator=4, timeSigDenominator=4,
                                 flags=kVstTransportPlaying | kVstPpqPosValid | kVstTempoValid | kVstBarsValid
                                 | kVstTimeSigValid)
        self._dir = ctypes.create_string_buffer(os.path.dirname(path).encode("mbcs", "replace") + b"\0", 1024)
        self._callback = AudioMasterCallback(self._host)  # kept alive as long as the plugin
        self._eff = None
        self._effp = None
        try:
            self._dll = ctypes.CDLL(path, winmode=LOAD_WITH_ALTERED_SEARCH_PATH)
        except OSError as e:
            raise Vst2Error(describe_load_error(path, e)) from None
        self._entry = None
        for symbol in ("VSTPluginMain", "main", "main_plugin"):
            try:
                fn = getattr(self._dll, symbol)
            except AttributeError:
                continue
            fn.restype = c_void_p
            fn.argtypes = [AudioMasterCallback]
            self._entry = fn
            break
        if self._entry is None:
            raise Vst2Error("not a VST2 plugin (no VSTPluginMain entry point)")
        self._instantiate()
        if self._dispatch(effGetPlugCategory) == kPlugCategShell:
            names = self.shell_plugins()
            pick = _pick_shell(names, shell_name)
            if pick is None:
                if not names:
                    raise Vst2Error("this shell exposes no plugins (none of them is licensed or installed)")
                if shell_name:
                    what = "that plugin" if callable(shell_name) else f"'{shell_name}'"
                    raise Vst2Error(f"this shell doesn't expose {what} (not licensed or not installed)")
                raise Vst2Error("this is a shell with several plugins: " + ", ".join(n for n, _ in names[:20]))
            self._dispatch(effClose)
            self._shell_id = pick[1]
            self._instantiate()
        e = self._eff
        self.num_inputs, self.num_outputs = e.numInputs, e.numOutputs
        self.is_instrument = bool(e.flags & effFlagsIsSynth) or e.numInputs == 0
        # Effects that take MIDI too (e.g. MIDI-controlled filters, vocoders).
        self.accepts_midi = self.is_instrument or any(
            self._dispatch(effCanDo, ptr=ctypes.create_string_buffer(what)) > 0
            for what in (b"receiveVstMidiEvent", b"receiveVstEvents"))
        self._midi_pool = (VstMidiEvent * MAX_EVENTS)()
        self._events = VstEvents()
        for i in range(MAX_EVENTS):
            self._events.events[i] = ctypes.addressof(self._midi_pool[i])
        self.has_editor = bool(e.flags & effFlagsHasEditor)
        self.name = (self._string(effGetEffectName) or self._string(effGetProductString)
                     or os.path.splitext(os.path.basename(path))[0])
        self.vendor = self._string(effGetVendorString)
        self._alloc()
        self._resume()

    # ---- setup ----

    def _instantiate(self) -> None:
        ptr = self._entry(self._callback)
        if not ptr:
            raise Vst2Error("the plugin refused to start (VSTPluginMain returned nothing; "
                            "it may need to be authorized in its vendor's app)")
        eff = ctypes.cast(ptr, POINTER(AEffect)).contents
        if eff.magic != kVstMagic:
            raise Vst2Error("not a VST2 plugin (bad AEffect magic)")
        self._eff, self._effp = eff, ptr
        self._dispatch(effOpen)
        self._dispatch(effSetSampleRate, opt=float(self.sample_rate))
        self._dispatch(effSetBlockSize, value=self.block_size)

    @property
    def latency_samples(self) -> int:
        """The delay the plugin reports (AEffect.initialDelay, frames): it hands back what went in that long
        ago (host_worker._Align lines it up)."""
        e = self._eff
        return max(0, int(e.initialDelay)) if e is not None else 0

    def _dispatch(self, opcode: int, index: int = 0, value: int = 0, ptr=None, opt: float = 0.0) -> int:
        return self._eff.dispatcher(self._effp, opcode, index, value, ptr, opt)

    def _string(self, opcode: int) -> str:
        buf = ctypes.create_string_buffer(256)
        self._dispatch(opcode, ptr=buf)
        return buf.value.decode("utf-8", "replace").strip()

    def shell_plugins(self) -> list[tuple[str, int]]:
        out = []
        for _ in range(4096):
            buf = ctypes.create_string_buffer(256)
            uid = self._dispatch(effShellGetNextPlugin, ptr=buf)
            if not uid:
                break
            out.append((buf.value.decode("utf-8", "replace"), int(uid)))
        return out

    def _alloc(self) -> None:
        np = self._np
        n_in, n_out = max(self.num_inputs, 0), max(self.num_outputs, 0)
        self._in = np.zeros((max(n_in, 1), self.MAX_BLOCK), dtype=np.float32)
        self._out = np.zeros((max(n_out, 1), self.MAX_BLOCK), dtype=np.float32)
        self._in_ptrs = (POINTER(c_float) * max(n_in, 1))(
            *[row.ctypes.data_as(POINTER(c_float)) for row in self._in])
        self._out_ptrs = (POINTER(c_float) * max(n_out, 1))(
            *[row.ctypes.data_as(POINTER(c_float)) for row in self._out])
        self._replacing = bool(self._eff.flags & effFlagsCanReplacing)

    def _resume(self) -> None:
        self._dispatch(effMainsChanged, value=1)
        self._dispatch(effStartProcess)

    def _suspend(self) -> None:
        self._dispatch(effStopProcess)
        self._dispatch(effMainsChanged, value=0)

    def _reconfigure(self, rate: int) -> None:
        def apply():
            self._suspend()
            self.sample_rate = rate
            self._time.sampleRate = rate
            self._dispatch(effSetSampleRate, opt=float(rate))
            self._dispatch(effSetBlockSize, value=self.block_size)
            self._resume()

        self.main.call(apply)

    # ---- host callback ----

    def _host(self, effect, opcode, index, value, ptr, opt):
        try:
            if opcode == amVersion:
                return 2400
            if opcode == amCurrentId:
                return self._shell_id
            if opcode == amGetTime:
                return ctypes.addressof(self._time)
            if opcode == amGetSampleRate:
                return self.sample_rate
            if opcode == amGetBlockSize:
                return self.block_size
            if opcode == amGetCurrentProcessLevel:
                return 2 if win_editor.kernel32.GetCurrentThreadId() == self._audio_thread else 1
            if opcode == amSizeWindow:
                self._request_size(int(index), int(value))
                return 1
            if opcode == amCanDo:
                return 1 if ptr and ctypes.string_at(ptr) in HOST_CAN_DO else 0
            if opcode == amGetVendorString:
                _copy_str(ptr, b"VEXT", 64)
                return 1
            if opcode == amGetProductString:
                _copy_str(ptr, b"VEXT Plugin Host", 64)
                return 1
            if opcode == amGetVendorVersion:
                return 1000
            if opcode == amGetLanguage:
                return 1  # English
            if opcode == amGetDirectory:
                return ctypes.addressof(self._dir)
            if opcode in (amWantMidi, amIOChanged, amNeedIdle, amUpdateDisplay, amBeginEdit, amEndEdit,
                          amAutomate):
                return 1
            if opcode == amGetAutomationState:
                return 1  # off
            return 0
        except Exception as e:  # noqa: BLE001 - never unwind into the plugin
            _log(f"host callback {opcode} failed: {e!r}")
            return 0

    # ---- audio ----

    def reset(self) -> None:
        """Clears tails and state (suspend + resume, on the main thread)."""
        def apply():
            with self._lock:
                self._suspend()
                self._resume()

        self.main.call(apply)

    def _send_midi(self, midi, start: int, n: int) -> None:
        """The MIDI events [(offset, bytes)] falling in frames [start, start+n),
        handed to the plugin (effProcessEvents) just before it processes them.
        Audio thread; the events stay valid until the next call."""
        count = 0
        for offset, data in midi:
            if not start <= offset < start + n or count >= MAX_EVENTS:
                continue
            e = self._midi_pool[count]
            e.type, e.byteSize, e.deltaFrames, e.flags = kVstMidiType, ctypes.sizeof(VstMidiEvent), offset - start, \
                kVstMidiEventIsRealtime
            e.noteLength = e.noteOffset = 0
            b = bytes(data[:3]).ljust(3, b"\0")
            e.midiData[0], e.midiData[1], e.midiData[2], e.midiData[3] = b[0], b[1], b[2], 0
            e.detune = e.noteOffVelocity = 0
            count += 1
        if count:
            self._events.numEvents = count
            self._dispatch(effProcessEvents, ptr=ctypes.byref(self._events))

    def __call__(self, audio, sample_rate, reset: bool = False, midi=None):
        """(channels, n) float32 in, (2, n) float32 out; like pedalboard's plugin
        call. `midi`: [(frame offset, bytes)] channel messages for this block."""
        np = self._np
        audio = np.asarray(audio, dtype=np.float32)
        if audio.ndim == 1:
            audio = audio[None, :]
        rate = int(sample_rate)
        if rate != self.sample_rate:
            self._reconfigure(rate)
        if reset:
            self.reset()
        n_total = audio.shape[1]
        out = np.zeros((2, n_total), dtype=np.float32)
        self._audio_thread = win_editor.kernel32.GetCurrentThreadId()
        with self._lock:
            for start in range(0, n_total, self.block_size):
                n = min(self.block_size, n_total - start)
                chunk = audio[:, start:start + n]
                self._in[:, :n] = 0.0
                if self.num_inputs == 1:
                    self._in[0, :n] = chunk.mean(axis=0)
                elif self.num_inputs >= 2:
                    self._in[0, :n] = chunk[0]
                    self._in[1, :n] = chunk[1] if chunk.shape[0] > 1 else chunk[0]
                self._out[:, :n] = 0.0
                if midi:
                    self._send_midi(midi, start, n)
                if self._replacing:
                    self._eff.processReplacing(self._effp, self._in_ptrs, self._out_ptrs, n)
                else:
                    self._eff.process(self._effp, self._in_ptrs, self._out_ptrs, n)
                if self.num_outputs >= 2:
                    out[:, start:start + n] = self._out[:2, :n]
                elif self.num_outputs == 1:
                    out[0, start:start + n] = out[1, start:start + n] = self._out[0, :n]
                t = self._time
                t.samplePos += n
                t.ppqPos = t.samplePos / max(self.sample_rate, 1) * TEMPO / 60.0
                t.barStartPos = (t.ppqPos // 4.0) * 4.0
        return out

    # ---- editor ----

    def _request_size(self, width: int, height: int) -> None:
        if width <= 0 or height <= 0:
            return

        def apply():
            if self._window and not self._window.closed:
                self._window.set_client_size(width, height)

        if self.main.is_main():
            apply()
        else:
            self.main.post(apply)

    def _rect(self) -> tuple[int, int] | None:
        rect = POINTER(ERect)()
        self._dispatch(effEditGetRect, ptr=ctypes.byref(rect))
        if not rect:
            return None
        r = rect.contents
        w, h = r.right - r.left, r.bottom - r.top
        return (w, h) if w > 0 and h > 0 else None

    def show_editor(self, close_event: threading.Event) -> None:
        """Opens the editor and runs the message loop (main thread) until
        `close_event` is set or the window is closed."""
        if not self.has_editor:
            raise Vst2Error("this plugin has no editor of its own")
        width, height = self._rect() or (640, 480)
        window = win_editor.EditorWindow(self.name, width, height, resizable=False,
                                         on_idle=lambda: self._dispatch(effEditIdle),
                                         on_close=close_event.set)
        self._window = window
        try:
            if not self._dispatch(effEditOpen, ptr=c_void_p(window.hwnd)) and self._rect() is None:
                _log("effEditOpen returned 0")
            size = self._rect()  # many plugins only know their size once open
            if size:
                window.set_client_size(*size)
            window.show()
            self.editor_hwnd = int(window.hwnd)
            self.main.pump(lambda: not close_event.is_set() and not window.closed)
        finally:
            self.editor_hwnd = None
            if not window.closed:
                self._dispatch(effEditClose)
            window.destroy()
            self._window = None

    def close(self) -> None:
        """Stops and closes the plugin (main thread)."""
        if self._eff is None:
            return
        try:
            with self._lock:
                self._suspend()
                self._dispatch(effClose)
        finally:
            self._eff = None


def _pick_shell(names: list[tuple[str, int]], wanted):
    """`wanted`: a name, a function choosing one from the list of names, or None
    (only a single-plugin shell can be opened without saying which)."""
    if not names:
        return None
    if callable(wanted):
        chosen = wanted([n for n, _ in names])
        return next(((n, uid) for n, uid in names if n == chosen), None)
    if wanted:
        low = wanted.lower()
        for name, uid in names:
            if name.lower() == low:
                return name, uid
        for name, uid in names:
            if name.lower().startswith(low):
                return name, uid
        return None
    return names[0] if len(names) == 1 else None


def describe_load_error(path: str, error: OSError) -> str:
    """Why Windows couldn't load a plugin binary, in words."""
    code = getattr(error, "winerror", None)
    from app.hosting import deps as plugin_deps

    if code == 193:
        return plugin_deps.wrong_arch(path) or "it isn't a Windows binary this host can load"
    if code == 126:
        missing = plugin_deps.missing_dlls(path)
        if missing:
            return "a DLL it needs is missing: " + ", ".join(missing[:5])
        return "a DLL it needs couldn't be found"
    if code == 127:
        return "a DLL it needs is too old (missing function; update the Visual C++ runtime or the plugin)"
    if code == 1114:
        return "the plugin failed to start (its DllMain failed; it may need its vendor's app or authorization)"
    return f"Windows couldn't load it ({error})"
