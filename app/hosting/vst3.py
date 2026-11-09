"""A minimal VST3 host (ctypes, the VST3 C++ ABI as COM vtables; no SDK, no
build step), for the one case pedalboard can't load: plugins without an audio
output (analyzers, meters: Blue Cat's FreqAnalyst 2). pedalboard 0.9.26 insists
on as many output channels as input channels and refuses them ("... 2 input
channels and 0 output channels"). Every other VST3 still goes through
pedalboard (app/host_worker.py).

The plugin gets the audio (stereo or mono input bus, no output bus) so it
analyses it; what VEXT plays is the input unchanged (`__call__` returns it):
the worker's "analyzer routing". Like VEXT's VST2/CLAP hosts it mimics the
part of pedalboard's plugin object the worker uses (`name`, `is_instrument`,
`__call__`, `reset`, `show_editor`, `editor_hwnd`, `has_editor`, `close`) and
embeds the editor (IPlugView) in a VEXT window (win_editor.EditorWindow).

Supported: one "Audio Module Class" per file; the controller is the component
itself (single-component plugins) or the controller class it names, connected
through IConnectionPoint with host-made IMessage / IAttributeList objects.
Not supported (not needed by analyzers): parameter automation from the host,
MIDI, presets, IBStream state.

Threading: set-up and the editor on the worker's main thread (it runs the
message loop); process() on the playback thread after setActive/setProcessing.
"""

import ctypes
import threading
import uuid
from ctypes import POINTER, byref, c_char_p, c_double, c_int32, c_int64, c_uint32, c_uint64, c_void_p

from app.hosting import win_editor

kResultOk, kResultFalse, kNoInterface, kNotImplemented = 0, 1, 0x80004002, 0x80004001
kAudio, kEvent = 0, 1
kInput, kOutput = 0, 1
kRealtime, kSample32 = 0, 0
kSpeakerMono, kSpeakerStereo = 1 << 19, 0x3  # kSpeakerM, kSpeakerL | kSpeakerR
kPlatformTypeHWND = b"HWND"


def _iid(text: str) -> bytes:
    return uuid.UUID(text).bytes_le  # COM-compatible layout (Windows)


IID_FUnknown = _iid("00000000-0000-0000-C000-000000000046")
IID_IComponent = _iid("E831FF31-F2D5-4301-928E-BBEE25697802")
IID_IAudioProcessor = _iid("42043F99-B7DA-453C-A569-E79D9AAEC33D")
IID_IEditController = _iid("DCD7BBE3-7742-448D-A874-AACC979C759E")
IID_IConnectionPoint = _iid("70A4156F-6E6E-4026-9891-48BFAA60D8D1")
IID_IHostApplication = _iid("58E595CC-DB2D-4969-8B6A-AF8C36A664E5")
IID_IMessage = _iid("936F033B-C6C0-47DB-BB08-82F813C1E613")
IID_IAttributeList = _iid("1E5F0AEB-CC7F-4533-A254-401138AD5EE4")
IID_IComponentHandler = _iid("93A0BEA3-0BD0-45DB-8E89-0B0CC1E46AC6")
IID_IPlugFrame = _iid("367FAF01-AFA9-4693-8D4D-A2A0ED0882A3")


class Vst3Error(Exception):
    pass


class PClassInfo(ctypes.Structure):
    _fields_ = [("cid", ctypes.c_ubyte * 16), ("cardinality", c_int32), ("category", ctypes.c_char * 32),
                ("name", ctypes.c_char * 64)]


class BusInfo(ctypes.Structure):
    _fields_ = [("mediaType", c_int32), ("direction", c_int32), ("channelCount", c_int32),
                ("name", ctypes.c_wchar * 128), ("busType", c_int32), ("flags", c_uint32)]


class ProcessSetup(ctypes.Structure):
    _fields_ = [("processMode", c_int32), ("symbolicSampleSize", c_int32), ("maxSamplesPerBlock", c_int32),
                ("sampleRate", c_double)]


class AudioBusBuffers(ctypes.Structure):
    _fields_ = [("numChannels", c_int32), ("silenceFlags", c_uint64), ("channelBuffers32", POINTER(POINTER(ctypes.c_float)))]


class ProcessData(ctypes.Structure):
    _fields_ = [("processMode", c_int32), ("symbolicSampleSize", c_int32), ("numSamples", c_int32),
                ("numInputs", c_int32), ("numOutputs", c_int32), ("inputs", POINTER(AudioBusBuffers)),
                ("outputs", POINTER(AudioBusBuffers)), ("inputParameterChanges", c_void_p),
                ("outputParameterChanges", c_void_p), ("inputEvents", c_void_p), ("outputEvents", c_void_p),
                ("processContext", c_void_p)]


class ViewRect(ctypes.Structure):
    _fields_ = [("left", c_int32), ("top", c_int32), ("right", c_int32), ("bottom", c_int32)]


def _method(obj, index: int, restype, *argtypes):
    """Method `index` of the COM interface pointer `obj` (this-call on x64 = first argument)."""
    vtable = ctypes.cast(ctypes.cast(obj, POINTER(c_void_p))[0], POINTER(c_void_p))
    fn = ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)(vtable[index])
    return lambda *args: fn(obj, *args)


def _query(obj, iid: bytes):
    out = c_void_p()
    if obj and _method(obj, 0, c_int32, c_char_p, POINTER(c_void_p))(iid, byref(out)) == kResultOk and out.value:
        return out.value
    return None


def _release(obj) -> None:
    if obj:
        _method(obj, 2, c_uint32)()


# ---------- objects VEXT implements (host side) ----------

class _ComObject:
    """A COM object made in Python: a vtable of ctypes callbacks. Reference
    counting is nominal (they live as long as the plugin)."""

    def __init__(self, iids: tuple, methods: list):
        self._iids = {IID_FUnknown, *iids}
        proto = ctypes.WINFUNCTYPE
        base = [
            proto(c_int32, c_void_p, c_char_p, POINTER(c_void_p))(self._qi),
            proto(c_uint32, c_void_p)(lambda _this: 1),
            proto(c_uint32, c_void_p)(lambda _this: 1),
        ]
        self._callbacks = base + methods  # kept alive here
        self._vtable = (c_void_p * len(self._callbacks))(*[ctypes.cast(f, c_void_p) for f in self._callbacks])
        self._object = (c_void_p * 1)(ctypes.addressof(self._vtable))
        self.ptr = ctypes.addressof(self._object)

    def _qi(self, _this, iid, out):
        if iid is not None and bytes(ctypes.string_at(iid, 16)) in self._iids:
            out[0] = self.ptr
            return kResultOk
        out[0] = None
        return kNoInterface


class _AttributeList(_ComObject):
    def __init__(self):
        p = ctypes.WINFUNCTYPE
        self.values: dict = {}
        self._buffers: dict = {}
        super().__init__((IID_IAttributeList,), [
            p(c_int32, c_void_p, c_char_p, c_int64)(lambda _t, k, v: self._set(k, int(v))),
            p(c_int32, c_void_p, c_char_p, POINTER(c_int64))(lambda _t, k, out: self._get(k, out, int)),
            p(c_int32, c_void_p, c_char_p, c_double)(lambda _t, k, v: self._set(k, float(v))),
            p(c_int32, c_void_p, c_char_p, POINTER(c_double))(lambda _t, k, out: self._get(k, out, float)),
            p(c_int32, c_void_p, c_char_p, ctypes.c_wchar_p)(lambda _t, k, v: self._set(k, v or "")),
            p(c_int32, c_void_p, c_char_p, c_void_p, c_uint32)(self._get_string),
            p(c_int32, c_void_p, c_char_p, c_void_p, c_uint32)(
                lambda _t, k, data, size: self._set(k, ctypes.string_at(data, size) if data and size else b"")),
            p(c_int32, c_void_p, c_char_p, POINTER(c_void_p), POINTER(c_uint32))(self._get_binary),
        ])

    def _set(self, key, value) -> int:
        self.values[key] = value
        return kResultOk

    def _get(self, key, out, kind) -> int:
        value = self.values.get(key)
        if not isinstance(value, (int, float)) or not out:
            return kResultFalse
        out[0] = kind(value)
        return kResultOk

    def _get_string(self, _t, key, out, size_bytes) -> int:
        value = self.values.get(key)
        if not isinstance(value, str) or not out or size_bytes < 2:
            return kResultFalse
        data = (value[: size_bytes // 2 - 1] + "\0").encode("utf-16-le")
        ctypes.memmove(out, data, len(data))
        return kResultOk

    def _get_binary(self, _t, key, data_out, size_out) -> int:
        value = self.values.get(key)
        if not isinstance(value, bytes) or not data_out or not size_out:
            return kResultFalse
        buf = self._buffers[key] = ctypes.create_string_buffer(value, len(value) or 1)
        data_out[0] = ctypes.addressof(buf)
        size_out[0] = len(value)
        return kResultOk


class _Message(_ComObject):
    def __init__(self):
        p = ctypes.WINFUNCTYPE
        self.id = b""
        self.attributes = _AttributeList()
        super().__init__((IID_IMessage,), [
            p(c_char_p, c_void_p)(lambda _t: self.id),
            p(None, c_void_p, c_char_p)(lambda _t, mid: setattr(self, "id", bytes(mid or b""))),
            p(c_void_p, c_void_p)(lambda _t: self.attributes.ptr),
        ])


class _HostApplication(_ComObject):
    """IHostApplication: the host's name, and messages for IConnectionPoint."""

    def __init__(self):
        p = ctypes.WINFUNCTYPE
        self._made: list = []
        super().__init__((IID_IHostApplication,), [
            p(c_int32, c_void_p, c_void_p)(self._name),
            p(c_int32, c_void_p, c_char_p, c_char_p, POINTER(c_void_p))(self._create),
        ])

    @staticmethod
    def _name(_t, out) -> int:
        data = "VEXT\0".encode("utf-16-le")
        ctypes.memmove(out, data, len(data))
        return kResultOk

    def _create(self, _t, cid, iid, out) -> int:
        wanted = {bytes(ctypes.string_at(cid, 16)) if cid else b"", bytes(ctypes.string_at(iid, 16)) if iid else b""}
        if IID_IMessage in wanted:
            obj = _Message()
        elif IID_IAttributeList in wanted:
            obj = _AttributeList()
        else:
            out[0] = None
            return kNoInterface
        self._made.append(obj)
        del self._made[:-256]  # plugins release them; a few hundred recent ones stay valid
        out[0] = obj.ptr
        return kResultOk


class _ComponentHandler(_ComObject):
    """IComponentHandler: edits from the editor (nothing to do: no automation)."""

    def __init__(self):
        p = ctypes.WINFUNCTYPE
        ok = lambda *_a: kResultOk  # noqa: E731
        super().__init__((IID_IComponentHandler,), [
            p(c_int32, c_void_p, c_uint32)(ok),  # beginEdit
            p(c_int32, c_void_p, c_uint32, c_double)(ok),  # performEdit
            p(c_int32, c_void_p, c_uint32)(ok),  # endEdit
            p(c_int32, c_void_p, c_int32)(ok),  # restartComponent
        ])


class _PlugFrame(_ComObject):
    """IPlugFrame: the editor asks for another size."""

    def __init__(self, on_resize):
        p = ctypes.WINFUNCTYPE
        self._on_resize = on_resize
        super().__init__((IID_IPlugFrame,), [p(c_int32, c_void_p, c_void_p, POINTER(ViewRect))(self._resize)])

    def _resize(self, _t, _view, rect) -> int:
        if rect:
            r = rect[0]
            self._on_resize(r.right - r.left, r.bottom - r.top)
        return kResultOk


# ---------- the plugin ----------

class Vst3Plugin:
    """A VST3 plugin without an audio output, analysing what it's given."""

    is_instrument = False
    accepts_midi = False
    no_output = True  # the worker plays the input unchanged (analyzer routing)
    floating_editor = False

    def __init__(self, binary: str, main_thread: "win_editor.MainThread", rate: int, block: int, hint: str | None = None):
        self.main = main_thread
        self.sample_rate, self.block_size = int(rate), int(block)
        self.editor_hwnd: int | None = None
        self._window = None
        self._view = None
        self._lock = threading.Lock()
        self._active = False
        self._dll = ctypes.WinDLL(binary)
        try:
            self._dll.InitDll()  # optional entry point (VST3 on Windows)
        except AttributeError:
            pass
        self._dll.GetPluginFactory.restype = c_void_p
        factory = self._dll.GetPluginFactory()
        if not factory:
            raise Vst3Error("the plugin has no VST3 factory")
        self._factory = factory
        classes = []
        count = _method(factory, 4, c_int32)()
        for i in range(count):
            info = PClassInfo()
            if _method(factory, 5, c_int32, c_int32, POINTER(PClassInfo))(i, byref(info)) == kResultOk:
                classes.append(info)
        audio = [c for c in classes if c.category == b"Audio Module Class"]
        if not audio:
            raise Vst3Error("the plugin file has no audio module")
        low = (hint or "").lower()
        chosen = next((c for c in audio if c.name.decode("utf-8", "replace").lower() == low), audio[0])
        self.name = chosen.name.decode("utf-8", "replace")
        self._host = _HostApplication()
        self._handler = _ComponentHandler()
        self._frame = _PlugFrame(self._request_size)
        create = _method(factory, 6, c_int32, c_char_p, c_char_p, POINTER(c_void_p))
        component = c_void_p()
        if create(bytes(chosen.cid), IID_IComponent, byref(component)) != kResultOk or not component.value:
            raise Vst3Error("the plugin couldn't create its component")
        self._component = component.value
        if _method(self._component, 3, c_int32, c_void_p)(self._host.ptr) != kResultOk:  # initialize
            raise Vst3Error("the plugin's component didn't initialize")
        self._processor = _query(self._component, IID_IAudioProcessor)
        if not self._processor:
            raise Vst3Error("the plugin has no audio processor")
        # The controller: the component itself, or the class it names.
        self._separate = False
        self._controller = _query(self._component, IID_IEditController)
        if not self._controller:
            cid = (ctypes.c_ubyte * 16)()
            if _method(self._component, 5, c_int32, c_void_p)(cid) == kResultOk and any(cid):
                ctrl = c_void_p()
                if create(bytes(cid), IID_IEditController, byref(ctrl)) == kResultOk and ctrl.value:
                    self._controller = ctrl.value
                    self._separate = True
                    _method(self._controller, 3, c_int32, c_void_p)(self._host.ptr)
        if self._controller:
            _method(self._controller, 16, c_int32, c_void_p)(self._handler.ptr)  # setComponentHandler
            if self._separate:
                a, b = _query(self._component, IID_IConnectionPoint), _query(self._controller, IID_IConnectionPoint)
                if a and b:
                    _method(a, 3, c_int32, c_void_p)(b)
                    _method(b, 3, c_int32, c_void_p)(a)
                self._connections = (a, b)
        self._setup_buses()
        self._activate(self.sample_rate, self.block_size)
        self.has_editor = self._controller is not None

    # ---- audio ----

    def _setup_buses(self) -> None:
        bus_count = _method(self._component, 7, c_int32, c_int32, c_int32)
        bus_info = _method(self._component, 8, c_int32, c_int32, c_int32, c_int32, POINTER(BusInfo))
        activate = _method(self._component, 10, c_int32, c_int32, c_int32, c_int32, ctypes.c_ubyte)
        self._in_buses = []
        for i in range(bus_count(kAudio, kInput)):
            info = BusInfo()
            bus_info(kAudio, kInput, i, byref(info))
            self._in_buses.append(max(1, info.channelCount))
            activate(kAudio, kInput, i, 1 if i == 0 else 0)
        outs = bus_count(kAudio, kOutput)
        for i in range(outs):
            activate(kAudio, kOutput, i, 0)
        if not self._in_buses:
            raise Vst3Error("the plugin has no audio input")
        self.channels = 2 if self._in_buses[0] >= 2 else 1
        arrangement = (c_uint64 * 1)(kSpeakerStereo if self.channels == 2 else kSpeakerMono)
        _method(self._processor, 3, c_int32, POINTER(c_uint64), c_int32, POINTER(c_uint64), c_int32)(
            arrangement, 1, None, 0)

    def _activate(self, rate: int, block: int) -> None:
        setup = ProcessSetup(processMode=kRealtime, symbolicSampleSize=kSample32, maxSamplesPerBlock=int(block),
                             sampleRate=float(rate))
        _method(self._processor, 7, c_int32, POINTER(ProcessSetup))(byref(setup))
        _method(self._component, 11, c_int32, ctypes.c_ubyte)(1)  # setActive
        _method(self._processor, 8, c_int32, ctypes.c_ubyte)(1)  # setProcessing
        self.sample_rate, self.block_size, self._active = int(rate), int(block), True

    def _deactivate(self) -> None:
        if self._active:
            _method(self._processor, 8, c_int32, ctypes.c_ubyte)(0)
            _method(self._component, 11, c_int32, ctypes.c_ubyte)(0)
            self._active = False

    def __call__(self, audio, sample_rate, reset=False, midi=None):
        """Feeds the plugin `audio` ((2, n) float32) and returns it unchanged:
        the plugin has no output; what's heard is its input."""
        import numpy as np

        n = int(audio.shape[1])
        with self._lock:
            if int(sample_rate) != self.sample_rate or n > self.block_size:
                # New rate or bigger blocks: set up again on the main thread (VST3 rule).
                rate, block = int(sample_rate), max(n, self.block_size)
                self.main.call(lambda: (self._deactivate(), self._activate(rate, block)), timeout=10)
            data = np.ascontiguousarray(audio[:2] if self.channels == 2 else audio.mean(axis=0, keepdims=True),
                                        dtype=np.float32)
            pointers = (POINTER(ctypes.c_float) * self.channels)(
                *[data[c].ctypes.data_as(POINTER(ctypes.c_float)) for c in range(self.channels)])
            inputs = AudioBusBuffers(numChannels=self.channels, silenceFlags=0, channelBuffers32=pointers)
            pd = ProcessData(processMode=kRealtime, symbolicSampleSize=kSample32, numSamples=n, numInputs=1,
                             numOutputs=0, inputs=ctypes.pointer(inputs), outputs=None)
            _method(self._processor, 9, c_int32, POINTER(ProcessData))(byref(pd))
        return audio

    def reset(self) -> None:
        pass

    # ---- editor ----

    def _request_size(self, width: int, height: int) -> None:
        window, view = self._window, self._view
        if window is None or view is None or width <= 0 or height <= 0:
            return

        def apply():
            window.set_client_size(width, height)
            rect = ViewRect(0, 0, width, height)
            _method(view, 10, c_int32, POINTER(ViewRect))(byref(rect))  # onSize

        if self.main.is_main():
            apply()
        else:
            self.main.post(apply)

    def show_editor(self, close_event: threading.Event) -> None:
        """Opens the editor embedded in a VEXT window and runs the message loop
        (main thread) until `close_event` is set or the window is closed."""
        if not self._controller:
            raise Vst3Error("this plugin has no editor")
        view = _method(self._controller, 17, c_void_p, c_char_p)(b"editor")  # createView
        if not view:
            raise Vst3Error("the plugin couldn't create its editor")
        if _method(view, 3, c_int32, c_char_p)(kPlatformTypeHWND) != kResultOk:
            _release(view)
            raise Vst3Error("the plugin's editor doesn't support Windows windows")
        rect = ViewRect()
        _method(view, 9, c_int32, POINTER(ViewRect))(byref(rect))
        width, height = rect.right - rect.left, rect.bottom - rect.top
        if width <= 0 or height <= 0:
            width, height = 640, 480
        resizable = _method(view, 13, c_int32)() == kResultOk
        window = win_editor.EditorWindow(self.name, width, height, resizable,
                                         on_resized=self._on_resized if resizable else None,
                                         on_close=close_event.set)
        self._window, self._view = window, view
        try:
            _method(view, 12, c_int32, c_void_p)(self._frame.ptr)  # setFrame
            if _method(view, 4, c_int32, c_void_p, c_char_p)(window.hwnd, kPlatformTypeHWND) != kResultOk:
                raise Vst3Error("the plugin couldn't embed its editor")
            window.show()
            self.editor_hwnd = int(window.hwnd)
            self.main.pump(lambda: not close_event.is_set() and not window.closed)
        finally:
            self.editor_hwnd = None
            try:
                _method(view, 5, c_int32)()  # removed
                _method(view, 12, c_int32, c_void_p)(None)
            finally:
                _release(view)
                self._view = None
                window.destroy()
                self._window = None

    def _on_resized(self, width: int, height: int):
        view = self._view
        if view is None:
            return width, height
        rect = ViewRect(0, 0, width, height)
        _method(view, 14, c_int32, POINTER(ViewRect))(byref(rect))  # checkSizeConstraint
        rect2 = ViewRect(0, 0, rect.right - rect.left, rect.bottom - rect.top)
        _method(view, 10, c_int32, POINTER(ViewRect))(byref(rect2))  # onSize
        return rect2.right, rect2.bottom

    def close(self) -> None:
        """Stops, terminates and releases the plugin (main thread)."""
        if getattr(self, "_component", None) is None:
            return
        try:
            self._deactivate()
            if self._separate and getattr(self, "_connections", None):
                a, b = self._connections
                if a and b:
                    _method(a, 4, c_int32, c_void_p)(b)
                    _method(b, 4, c_int32, c_void_p)(a)
            if self._controller and self._separate:
                _method(self._controller, 4, c_int32)()
                _release(self._controller)
            _method(self._component, 4, c_int32)()  # terminate
            _release(self._processor)
            if self._controller and not self._separate:
                _release(self._controller)
            _release(self._component)
        finally:
            self._component = None
