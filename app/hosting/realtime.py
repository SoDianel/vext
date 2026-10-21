"""Makes a VST3 loaded through pedalboard run in *realtime* mode.

pedalboard 0.9.26 puts every plugin in JUCE's non-realtime mode, so the
plugin's IAudioProcessor::setupProcessing and process() get
processMode = kOffline. Analyzers take that as "rendering a bounce": Voxengo
SPAN shows "Off-Line Render" and stops drawing its spectrum, TDR Prism and
Waves PAZ draw nothing. VEXT plays in real time, so before pedalboard loads
the file, the plugin's own IAudioProcessor vtable (one per class, in the
plugin's DLL) gets setupProcessing (7) and process (9) pointed at thunks that
set processMode = kRealtime and call the originals. pedalboard then loads the
same, already loaded, module (LoadLibrary hands back the same handle) and its
instance uses the patched vtable.

Best effort: anything unexpected leaves the plugin as pedalboard runs it.
"""

import ctypes
from ctypes import POINTER, byref, c_char_p, c_int32, c_uint32, c_void_p

from app.hosting.vst3 import (IID_IAudioProcessor, IID_IComponent, PClassInfo, ProcessData, ProcessSetup, _method,
                              _query, _release, kResultOk)

kRealtime = 0
_SETUP, _PROCESS = 7, 9  # IAudioProcessor vtable slots
_PAGE_READWRITE = 0x04
_MAX_CLASSES = 8  # no name given: patch every audio class only in small files

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.VirtualProtect.argtypes = [c_void_p, ctypes.c_size_t, c_uint32, POINTER(c_uint32)]
_kernel32.VirtualProtect.restype = ctypes.c_int

_SetupFn = ctypes.WINFUNCTYPE(c_int32, c_void_p, POINTER(ProcessSetup))
_ProcessFn = ctypes.WINFUNCTYPE(c_int32, c_void_p, POINTER(ProcessData))

_patched: dict[int, tuple] = {}  # vtable address -> (originals, thunks): kept alive for the process
_modules: list = []  # the plugin DLLs, kept loaded (the patch lives in their memory)
_instances: list = []  # (component, processor) probe instances kept until release_probes()


def _thunks(vtable):
    setup, process = _SetupFn(vtable[_SETUP]), _ProcessFn(vtable[_PROCESS])

    def realtime_setup(this, s):
        if s:
            s.contents.processMode = kRealtime
        return setup(this, s)

    def realtime_process(this, d):
        if d:
            d.contents.processMode = kRealtime
        return process(this, d)

    return (setup, process), (_SetupFn(realtime_setup), _ProcessFn(realtime_process))


def _patch(processor) -> bool:
    vtable = ctypes.cast(ctypes.cast(processor, POINTER(c_void_p))[0], POINTER(c_void_p))
    address = ctypes.addressof(vtable.contents)
    if address in _patched:
        return True
    originals, thunks = _thunks(vtable)
    size = ctypes.sizeof(c_void_p) * (_PROCESS + 1)
    old = c_uint32()
    if not _kernel32.VirtualProtect(address, size, _PAGE_READWRITE, byref(old)):
        return False
    try:
        vtable[_SETUP] = ctypes.cast(thunks[0], c_void_p).value
        vtable[_PROCESS] = ctypes.cast(thunks[1], c_void_p).value
    finally:
        _kernel32.VirtualProtect(address, size, old.value, byref(old))
    _patched[address] = (originals, thunks)
    return True


def force_realtime(binary: str, name: str | None = None) -> int:
    """Patches the audio class called `name` (else every audio class of a
    small file) in the VST3 `binary`. Returns how many classes were patched.
    The instances made to reach the vtables are kept until release_probes()."""
    name = name or None
    try:
        dll = ctypes.WinDLL(binary)
    except OSError:
        return 0
    _modules.append(dll)
    try:
        dll.InitDll()  # optional entry point; pedalboard (JUCE) calls it too
    except AttributeError:
        pass
    try:
        get_factory = dll.GetPluginFactory
    except AttributeError:
        return 0
    get_factory.restype = c_void_p
    factory = get_factory()
    if not factory:
        return 0
    wanted = []
    for i in range(_method(factory, 4, c_int32)()):
        info = PClassInfo()
        if _method(factory, 5, c_int32, c_int32, POINTER(PClassInfo))(i, byref(info)) != kResultOk:
            continue
        if info.category != b"Audio Module Class":
            continue
        if name is None or info.name.decode("utf-8", "replace") == name:
            wanted.append(bytes(info.cid))
    if name is None and len(wanted) > _MAX_CLASSES:
        return 0
    create = _method(factory, 6, c_int32, c_char_p, c_char_p, POINTER(c_void_p))
    done = 0
    for cid in wanted:
        component = c_void_p()
        if create(cid, IID_IComponent, byref(component)) != kResultOk or not component.value:
            continue
        processor = _query(component.value, IID_IAudioProcessor)
        _instances.append((component.value, processor))
        if processor and _patch(processor):
            done += 1
        # Kept alive until pedalboard has loaded the plugin, not released here: Xfer's OTT
        # and DJMFilter crash pedalboard's own load with an access violation when the
        # first instance of the class is destroyed before the next is made (their
        # destructor tears down something the next constructor doesn't rebuild).
    return done


def release_probes() -> None:
    """Releases the probe instances force_realtime kept. Call it after the plugin
    has loaded (or failed to): releasing one before pedalboard's load is what
    crashes OTT and DJMFilter, after it is safe."""
    while _instances:
        component, processor = _instances.pop()
        try:
            _release(processor)
            _release(component)
        except Exception:  # noqa: BLE001 - a probe that won't go is just one leaked instance
            pass
