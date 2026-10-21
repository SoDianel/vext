"""A minimal CLAP host (the CLAP C ABI, driven through ctypes): load, audio
processing and the embedded editor. Used by the host worker (app/host_worker.py)
for `.clap` plugins; it mirrors the part of pedalboard's plugin object the
worker uses (`name`, `is_instrument`, `__call__(audio, rate, reset)`, `reset()`,
`show_editor(close_event)`), plus `editor_hwnd`, `close()`, and MIDI:
`accepts_midi` (a note input port) and `__call__(..., midi=[(frame offset,
bytes)])`, delivered in process()'s input events as clap_event_midi, or as
clap_event_note when the port prefers CLAP's own note dialect.

CLAP is a plain C ABI (MIT-licensed headers, github.com/free-audio/clap); the
structs below mirror clap/entry.h, factory/plugin-factory.h, plugin.h,
process.h, audio-buffer.h, events.h, host.h and the extensions used
(audio-ports, note-ports, gui, timer-support, thread-check, log, params,
latency, state, tail).

Threads (CLAP's rules): the thread that created the plugin is its main thread
(init, activate, GUI, timers, on_main_thread); it runs the editor's message
loop. The playback thread is the audio thread (start_processing, process,
reset). A sample-rate change requested by the audio thread is applied on the
main thread (deactivate/activate), as CLAP requires.
"""

import ctypes
import os
import sys
import threading
from ctypes import POINTER, c_bool, c_char, c_char_p, c_double, c_int32, c_int64, c_uint32, c_uint64, c_void_p

from app.hosting import win_editor

CLAP_NAME_SIZE = 256
CLAP_PROCESS_ERROR = 0
CLAP_AUDIO_PORT_IS_MAIN = 1
CLAP_WINDOW_API_WIN32 = b"win32"
# events (events.h)
CLAP_CORE_EVENT_SPACE_ID = 0
CLAP_EVENT_NOTE_ON, CLAP_EVENT_NOTE_OFF, CLAP_EVENT_MIDI = 0, 1, 10
CLAP_EVENT_IS_LIVE = 1
# note dialects (note-ports.h)
CLAP_NOTE_DIALECT_CLAP, CLAP_NOTE_DIALECT_MIDI = 1 << 0, 1 << 1
MAX_EVENTS = 512  # input events per processing block


class ClapError(RuntimeError):
    pass


def _log(msg: str) -> None:
    try:
        sys.stderr.write(f"[clap] {msg}\n")
        sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass


# ---------- ABI ----------

class clap_version(ctypes.Structure):
    _fields_ = [("major", c_uint32), ("minor", c_uint32), ("revision", c_uint32)]


class clap_plugin_entry(ctypes.Structure):
    _fields_ = [("clap_version", clap_version),
                ("init", ctypes.CFUNCTYPE(c_bool, c_char_p)),
                ("deinit", ctypes.CFUNCTYPE(None)),
                ("get_factory", ctypes.CFUNCTYPE(c_void_p, c_char_p))]


class clap_plugin_descriptor(ctypes.Structure):
    _fields_ = [("clap_version", clap_version), ("id", c_char_p), ("name", c_char_p), ("vendor", c_char_p),
                ("url", c_char_p), ("manual_url", c_char_p), ("support_url", c_char_p), ("version", c_char_p),
                ("description", c_char_p), ("features", POINTER(c_char_p))]


class clap_host(ctypes.Structure):
    pass


class clap_plugin(ctypes.Structure):
    pass


class clap_process(ctypes.Structure):
    pass


HostP = POINTER(clap_host)
PluginP = POINTER(clap_plugin)

clap_host._fields_ = [
    ("clap_version", clap_version), ("host_data", c_void_p), ("name", c_char_p), ("vendor", c_char_p),
    ("url", c_char_p), ("version", c_char_p),
    ("get_extension", ctypes.CFUNCTYPE(c_void_p, HostP, c_char_p)),
    ("request_restart", ctypes.CFUNCTYPE(None, HostP)),
    ("request_process", ctypes.CFUNCTYPE(None, HostP)),
    ("request_callback", ctypes.CFUNCTYPE(None, HostP)),
]

clap_plugin._fields_ = [
    ("desc", POINTER(clap_plugin_descriptor)), ("plugin_data", c_void_p),
    ("init", ctypes.CFUNCTYPE(c_bool, PluginP)),
    ("destroy", ctypes.CFUNCTYPE(None, PluginP)),
    ("activate", ctypes.CFUNCTYPE(c_bool, PluginP, c_double, c_uint32, c_uint32)),
    ("deactivate", ctypes.CFUNCTYPE(None, PluginP)),
    ("start_processing", ctypes.CFUNCTYPE(c_bool, PluginP)),
    ("stop_processing", ctypes.CFUNCTYPE(None, PluginP)),
    ("reset", ctypes.CFUNCTYPE(None, PluginP)),
    ("process", ctypes.CFUNCTYPE(c_int32, PluginP, POINTER(clap_process))),
    ("get_extension", ctypes.CFUNCTYPE(c_void_p, PluginP, c_char_p)),
    ("on_main_thread", ctypes.CFUNCTYPE(None, PluginP)),
]


class clap_plugin_factory(ctypes.Structure):
    pass


clap_plugin_factory._fields_ = [
    ("get_plugin_count", ctypes.CFUNCTYPE(c_uint32, POINTER(clap_plugin_factory))),
    ("get_plugin_descriptor", ctypes.CFUNCTYPE(POINTER(clap_plugin_descriptor), POINTER(clap_plugin_factory),
                                               c_uint32)),
    ("create_plugin", ctypes.CFUNCTYPE(PluginP, POINTER(clap_plugin_factory), HostP, c_char_p)),
]


class clap_audio_buffer(ctypes.Structure):
    _fields_ = [("data32", POINTER(POINTER(ctypes.c_float))), ("data64", POINTER(POINTER(c_double))),
                ("channel_count", c_uint32), ("latency", c_uint32), ("constant_mask", c_uint64)]


class clap_event_header(ctypes.Structure):
    _fields_ = [("size", c_uint32), ("time", c_uint32), ("space_id", ctypes.c_uint16), ("type", ctypes.c_uint16),
                ("flags", c_uint32)]


class clap_event_note(ctypes.Structure):
    _fields_ = [("header", clap_event_header), ("note_id", c_int32), ("port_index", ctypes.c_int16),
                ("channel", ctypes.c_int16), ("key", ctypes.c_int16), ("velocity", c_double)]


class clap_event_midi(ctypes.Structure):
    _fields_ = [("header", clap_event_header), ("port_index", ctypes.c_uint16), ("data", ctypes.c_uint8 * 3)]


class clap_note_port_info(ctypes.Structure):
    _fields_ = [("id", c_uint32), ("supported_dialects", c_uint32), ("preferred_dialect", c_uint32),
                ("name", c_char * CLAP_NAME_SIZE)]


class clap_input_events(ctypes.Structure):
    pass


class clap_output_events(ctypes.Structure):
    pass


clap_input_events._fields_ = [
    ("ctx", c_void_p),
    ("size", ctypes.CFUNCTYPE(c_uint32, POINTER(clap_input_events))),
    # Returns a clap_event_header*; c_void_p because ctypes callbacks can't return pointer types.
    ("get", ctypes.CFUNCTYPE(c_void_p, POINTER(clap_input_events), c_uint32)),
]
clap_output_events._fields_ = [
    ("ctx", c_void_p),
    ("try_push", ctypes.CFUNCTYPE(c_bool, POINTER(clap_output_events), POINTER(clap_event_header))),
]

clap_process._fields_ = [
    ("steady_time", c_int64), ("frames_count", c_uint32), ("transport", c_void_p),
    ("audio_inputs", POINTER(clap_audio_buffer)), ("audio_outputs", POINTER(clap_audio_buffer)),
    ("audio_inputs_count", c_uint32), ("audio_outputs_count", c_uint32),
    ("in_events", POINTER(clap_input_events)), ("out_events", POINTER(clap_output_events)),
]


class clap_audio_port_info(ctypes.Structure):
    _fields_ = [("id", c_uint32), ("name", c_char * CLAP_NAME_SIZE), ("flags", c_uint32),
                ("channel_count", c_uint32), ("port_type", c_char_p), ("in_place_pair", c_uint32)]


class clap_plugin_audio_ports(ctypes.Structure):
    _fields_ = [("count", ctypes.CFUNCTYPE(c_uint32, PluginP, c_bool)),
                ("get", ctypes.CFUNCTYPE(c_bool, PluginP, c_uint32, c_bool, POINTER(clap_audio_port_info)))]


class clap_plugin_latency(ctypes.Structure):
    _fields_ = [("get", ctypes.CFUNCTYPE(c_uint32, PluginP))]


class clap_plugin_note_ports(ctypes.Structure):
    _fields_ = [("count", ctypes.CFUNCTYPE(c_uint32, PluginP, c_bool)),
                ("get", ctypes.CFUNCTYPE(c_bool, PluginP, c_uint32, c_bool, c_void_p))]


class clap_window(ctypes.Structure):
    _fields_ = [("api", c_char_p), ("handle", c_void_p)]  # union {cocoa, x11, win32, ptr}: one pointer


class clap_gui_resize_hints(ctypes.Structure):
    _fields_ = [("can_resize_horizontally", c_bool), ("can_resize_vertically", c_bool),
                ("preserve_aspect_ratio", c_bool), ("aspect_ratio_width", c_uint32),
                ("aspect_ratio_height", c_uint32)]


class clap_plugin_gui(ctypes.Structure):
    _fields_ = [
        ("is_api_supported", ctypes.CFUNCTYPE(c_bool, PluginP, c_char_p, c_bool)),
        ("get_preferred_api", ctypes.CFUNCTYPE(c_bool, PluginP, POINTER(c_char_p), POINTER(c_bool))),
        ("create", ctypes.CFUNCTYPE(c_bool, PluginP, c_char_p, c_bool)),
        ("destroy", ctypes.CFUNCTYPE(None, PluginP)),
        ("set_scale", ctypes.CFUNCTYPE(c_bool, PluginP, c_double)),
        ("get_size", ctypes.CFUNCTYPE(c_bool, PluginP, POINTER(c_uint32), POINTER(c_uint32))),
        ("can_resize", ctypes.CFUNCTYPE(c_bool, PluginP)),
        ("get_resize_hints", ctypes.CFUNCTYPE(c_bool, PluginP, POINTER(clap_gui_resize_hints))),
        ("adjust_size", ctypes.CFUNCTYPE(c_bool, PluginP, POINTER(c_uint32), POINTER(c_uint32))),
        ("set_size", ctypes.CFUNCTYPE(c_bool, PluginP, c_uint32, c_uint32)),
        ("set_parent", ctypes.CFUNCTYPE(c_bool, PluginP, POINTER(clap_window))),
        ("set_transient", ctypes.CFUNCTYPE(c_bool, PluginP, POINTER(clap_window))),
        ("suggest_title", ctypes.CFUNCTYPE(None, PluginP, c_char_p)),
        ("show", ctypes.CFUNCTYPE(c_bool, PluginP)),
        ("hide", ctypes.CFUNCTYPE(c_bool, PluginP)),
    ]


class clap_plugin_timer_support(ctypes.Structure):
    _fields_ = [("on_timer", ctypes.CFUNCTYPE(None, PluginP, c_uint32))]


# host extensions
class clap_host_log(ctypes.Structure):
    _fields_ = [("log", ctypes.CFUNCTYPE(None, HostP, c_int32, c_char_p))]


class clap_host_thread_check(ctypes.Structure):
    _fields_ = [("is_main_thread", ctypes.CFUNCTYPE(c_bool, HostP)),
                ("is_audio_thread", ctypes.CFUNCTYPE(c_bool, HostP))]


class clap_host_gui(ctypes.Structure):
    _fields_ = [("resize_hints_changed", ctypes.CFUNCTYPE(None, HostP)),
                ("request_resize", ctypes.CFUNCTYPE(c_bool, HostP, c_uint32, c_uint32)),
                ("request_show", ctypes.CFUNCTYPE(c_bool, HostP)),
                ("request_hide", ctypes.CFUNCTYPE(c_bool, HostP)),
                ("closed", ctypes.CFUNCTYPE(None, HostP, c_bool))]


class clap_host_timer_support(ctypes.Structure):
    _fields_ = [("register_timer", ctypes.CFUNCTYPE(c_bool, HostP, c_uint32, POINTER(c_uint32))),
                ("unregister_timer", ctypes.CFUNCTYPE(c_bool, HostP, c_uint32))]


class clap_host_params(ctypes.Structure):
    _fields_ = [("rescan", ctypes.CFUNCTYPE(None, HostP, c_uint32)),
                ("clear", ctypes.CFUNCTYPE(None, HostP, c_uint32, c_uint32)),
                ("request_flush", ctypes.CFUNCTYPE(None, HostP))]


class clap_host_simple(ctypes.Structure):  # latency.changed, state.mark_dirty, tail.changed
    _fields_ = [("fn", ctypes.CFUNCTYPE(None, HostP))]


class clap_host_audio_ports(ctypes.Structure):
    _fields_ = [("is_rescan_flag_supported", ctypes.CFUNCTYPE(c_bool, HostP, c_uint32)),
                ("rescan", ctypes.CFUNCTYPE(None, HostP, c_uint32))]


class clap_host_note_ports(ctypes.Structure):
    _fields_ = [("supported_dialects", ctypes.CFUNCTYPE(c_uint32, HostP)),
                ("rescan", ctypes.CFUNCTYPE(None, HostP, c_uint32))]


# ---------- the plugin ----------

def list_plugins(path: str) -> list[dict]:
    """The plugins a .clap file holds: [{id, name, vendor, features}] (loads the file)."""
    plugin = _Library(path)
    try:
        return plugin.descriptors()
    finally:
        plugin.close()


class _Library:
    """The .clap file: its entry, initialized once."""

    def __init__(self, path: str):
        try:
            self.dll = ctypes.CDLL(path, winmode=0x00000008)  # LOAD_WITH_ALTERED_SEARCH_PATH
        except OSError as e:
            from app.hosting.vst2 import describe_load_error

            raise ClapError(describe_load_error(path, e)) from None
        try:
            self.entry = clap_plugin_entry.in_dll(self.dll, "clap_entry")
        except ValueError:
            raise ClapError("not a CLAP plugin (no clap_entry)") from None
        if self.entry.clap_version.major < 1:
            raise ClapError(f"unsupported CLAP version {self.entry.clap_version.major}.{self.entry.clap_version.minor}")
        if not self.entry.init(path.encode("utf-8")):
            raise ClapError("the plugin refused to start (clap_entry.init failed; "
                            "it may need to be installed or authorized in its vendor's app)")
        self._initialized = True
        ptr = self.entry.get_factory(b"clap.plugin-factory")
        if not ptr:
            self.close()
            raise ClapError("the file has no plugin factory")
        self.factory_p = ctypes.cast(ptr, POINTER(clap_plugin_factory))
        self.factory = self.factory_p.contents

    def descriptors(self) -> list[dict]:
        out = []
        for i in range(self.factory.get_plugin_count(self.factory_p)):
            d = self.factory.get_plugin_descriptor(self.factory_p, i)
            if not d:
                continue
            d = d.contents
            features = []
            if d.features:
                for j in range(64):
                    f = d.features[j]
                    if f is None:
                        break
                    features.append(f.decode("utf-8", "replace"))
            out.append({"id": (d.id or b"").decode("utf-8", "replace"),
                        "name": (d.name or b"").decode("utf-8", "replace"),
                        "vendor": (d.vendor or b"").decode("utf-8", "replace"),
                        "version": (d.version or b"").decode("utf-8", "replace"), "features": features})
        return out

    def close(self) -> None:
        if getattr(self, "_initialized", False):
            self._initialized = False
            try:
                self.entry.deinit()
            except Exception:  # noqa: BLE001
                pass


class ClapPlugin:
    MAX_BLOCK = 4096

    def __init__(self, path: str, main: win_editor.MainThread, sample_rate: int = 48000, block_size: int = 1024,
                 plugin_name: str | None = None, hint: str | None = None):
        import numpy as np

        self._np = np
        self.path = path
        self.main = main
        self.sample_rate = int(sample_rate)
        self.editor_hwnd: int | None = None
        self.floating_editor = False  # the plugin shows its own window (no win32 embedding)
        self._window: win_editor.EditorWindow | None = None
        self._audio_thread: int | None = None
        self._lock = threading.Lock()  # process vs. reconfigure
        self._active = self._processing = False
        self._restart_requested = False
        self._gui = None
        self._gui_created = False
        self._close_event: threading.Event | None = None
        self._steady = 0
        self._timer_ids = iter(range(1, 1 << 30))
        self._lib = _Library(path)
        descriptors = self._lib.descriptors()
        if not descriptors:
            self._lib.close()
            raise ClapError("the file holds no plugins")
        if plugin_name:
            pick = _pick(descriptors, plugin_name)
        else:  # the catalog's name only hints at one of several
            pick = (hint and _pick(descriptors, hint)) or descriptors[0]
        if pick is None:
            self._lib.close()
            raise ClapError("the file holds several plugins: " + ", ".join(d["name"] for d in descriptors))
        self.descriptor = pick
        self.name = pick["name"] or os.path.splitext(os.path.basename(path))[0]
        self.vendor = pick["vendor"]
        features = set(pick["features"])
        self._build_host()
        p = self._lib.factory.create_plugin(self._lib.factory_p, ctypes.byref(self._host), pick["id"].encode("utf-8"))
        if not p:
            self._lib.close()
            raise ClapError("the plugin couldn't be created (it may need to be authorized in its vendor's app)")
        self._p = p
        self._plugin = p.contents
        if not self._plugin.init(p):
            self._plugin.destroy(p)
            self._lib.close()
            raise ClapError("the plugin failed to initialize")
        self._ports()
        self._note_ports()
        self.is_instrument = "instrument" in features or (not self._in_ports and self.accepts_midi)
        gui = self._extension(b"clap.gui", clap_plugin_gui)
        self._gui = gui
        self.has_editor = bool(gui and (gui.is_api_supported(p, CLAP_WINDOW_API_WIN32, False)
                                        or gui.is_api_supported(p, CLAP_WINDOW_API_WIN32, True)))
        self._timer_ext = self._extension(b"clap.timer-support", clap_plugin_timer_support)
        self._alloc()
        self._activate()

    # ---- host side ----

    def _build_host(self) -> None:
        H = clap_host
        f = H._fields_
        typ = {name: t for name, t in f}
        self._keep = []  # every callback/struct handed to the plugin stays alive here

        def cb(ftype, fn):
            def safe(*args):
                try:
                    return fn(*args)
                except Exception as e:  # noqa: BLE001 - never unwind into the plugin
                    _log(f"host callback {fn.__name__} failed: {e!r}")
                    return 0 if ftype._restype_ not in (None,) else None
            c = ftype(safe)
            self._keep.append(c)
            return c

        def ext_fields(struct, **fns):
            ftypes = dict(struct._fields_)
            return struct(**{name: cb(ftypes[name], fn) for name, fn in fns.items()})

        def is_main(_h):
            return self.main.is_main()

        def is_audio(_h):
            return win_editor.kernel32.GetCurrentThreadId() == self._audio_thread

        def log(_h, severity, msg):
            _log(f"plugin log [{severity}]: {(msg or b'').decode('utf-8', 'replace')}")

        def request_resize(_h, width, height):
            self._request_size(int(width), int(height))
            return True

        def gui_closed(_h, was_destroyed):
            if self._close_event is not None:
                self._close_event.set()

        def register_timer(_h, period_ms, timer_id_p):
            tid = next(self._timer_ids)
            if timer_id_p:
                timer_id_p[0] = tid
            on_timer = self._timer_ext
            p = self._p
            return self.main.add_timer(tid, period_ms, lambda: on_timer.on_timer(p, tid) if on_timer else None)

        def unregister_timer(_h, timer_id):
            return self.main.remove_timer(int(timer_id))

        def nothing(*_a):
            return None

        self._exts = {
            b"clap.log": ext_fields(clap_host_log, log=log),
            b"clap.thread-check": ext_fields(clap_host_thread_check, is_main_thread=is_main,
                                             is_audio_thread=is_audio),
            b"clap.gui": ext_fields(clap_host_gui, resize_hints_changed=nothing, request_resize=request_resize,
                                    request_show=lambda _h: True, request_hide=lambda _h: True, closed=gui_closed),
            b"clap.timer-support": ext_fields(clap_host_timer_support, register_timer=register_timer,
                                              unregister_timer=unregister_timer),
            b"clap.params": ext_fields(clap_host_params, rescan=nothing, clear=nothing, request_flush=nothing),
            b"clap.latency": ext_fields(clap_host_simple, fn=nothing),
            b"clap.state": ext_fields(clap_host_simple, fn=nothing),
            b"clap.tail": ext_fields(clap_host_simple, fn=nothing),
            b"clap.audio-ports": ext_fields(clap_host_audio_ports, is_rescan_flag_supported=lambda _h, _f: False,
                                            rescan=nothing),
            b"clap.note-ports": ext_fields(clap_host_note_ports,
                                           supported_dialects=lambda _h: CLAP_NOTE_DIALECT_CLAP | CLAP_NOTE_DIALECT_MIDI,
                                           rescan=nothing),
        }

        def get_extension(_h, ext_id):
            ext = self._exts.get(ext_id)
            return ctypes.addressof(ext) if ext is not None else None

        def request_restart(_h):
            self._restart_requested = True
            if not self._processing:
                self.main.post(self._restart)

        def request_callback(_h):
            self.main.post(lambda: self._plugin.on_main_thread(self._p))

        self._names = [ctypes.create_string_buffer(s) for s in (b"VEXT", b"VEXT", b"https://vext.local", b"1.0")]
        self._host = H(clap_version=clap_version(1, 2, 0), host_data=None,
                       name=ctypes.cast(self._names[0], c_char_p), vendor=ctypes.cast(self._names[1], c_char_p),
                       url=ctypes.cast(self._names[2], c_char_p), version=ctypes.cast(self._names[3], c_char_p),
                       get_extension=cb(typ["get_extension"], get_extension),
                       request_restart=cb(typ["request_restart"], request_restart),
                       request_process=cb(typ["request_process"], nothing),
                       request_callback=cb(typ["request_callback"], request_callback))
        # process()'s input events: the MIDI of the block being processed
        # (_fill_events), read back by the plugin through these two calls.
        self._ev_count = 0
        self._ev_ptrs = (c_void_p * MAX_EVENTS)()
        self._note_pool = (clap_event_note * MAX_EVENTS)()
        self._midi_pool = (clap_event_midi * MAX_EVENTS)()

        def events_size(_l):
            return self._ev_count

        def events_get(_l, index):
            return self._ev_ptrs[index] if 0 <= index < self._ev_count else None

        self._in_events = clap_input_events(ctx=None, size=cb(dict(clap_input_events._fields_)["size"], events_size),
                                            get=cb(dict(clap_input_events._fields_)["get"], events_get))
        self._out_events = clap_output_events(ctx=None, try_push=cb(dict(clap_output_events._fields_)["try_push"],
                                                                    lambda _l, _e: True))

    def _extension(self, ext_id: bytes, struct):
        ptr = self._plugin.get_extension(self._p, ext_id)
        return ctypes.cast(ptr, POINTER(struct)).contents if ptr else None

    def _ports(self) -> None:
        ports = self._extension(b"clap.audio-ports", clap_plugin_audio_ports)
        self._in_ports, self._out_ports = [], []
        if ports is None:
            return
        for is_input, target in ((True, self._in_ports), (False, self._out_ports)):
            for i in range(ports.count(self._p, is_input)):
                info = clap_audio_port_info()
                if ports.get(self._p, i, is_input, ctypes.byref(info)):
                    target.append({"channels": int(info.channel_count),
                                   "main": bool(info.flags & CLAP_AUDIO_PORT_IS_MAIN)})

    def _note_ports(self) -> None:
        """Its first note input port (where VEXT sends notes) and the dialects
        it understands there: CLAP note events and/or raw MIDI."""
        notes = self._extension(b"clap.note-ports", clap_plugin_note_ports)
        self.accepts_midi = bool(notes and notes.count(self._p, True))
        self._dialects, self._clap_notes = 0, False
        if not self.accepts_midi:
            return
        info = clap_note_port_info()
        if notes.get(self._p, 0, True, ctypes.byref(info)):
            self._dialects = int(info.supported_dialects)
        else:
            self._dialects = CLAP_NOTE_DIALECT_CLAP | CLAP_NOTE_DIALECT_MIDI
        midi = bool(self._dialects & CLAP_NOTE_DIALECT_MIDI)
        # Notes as CLAP note events when it prefers them or can't take MIDI; the
        # rest (CC, pitch bend, pressure) only reaches a MIDI-capable port.
        self._clap_notes = bool(self._dialects & CLAP_NOTE_DIALECT_CLAP) and (
            info.preferred_dialect == CLAP_NOTE_DIALECT_CLAP or not midi)

    def _fill_events(self, midi, start: int, n: int) -> None:
        """process()'s input events for frames [start, start+n): the MIDI
        [(offset, bytes)] in this port's dialect. Audio thread."""
        count = notes = raws = 0
        midi_ok = bool(self._dialects & CLAP_NOTE_DIALECT_MIDI)
        for offset, data in midi or ():
            if not start <= offset < start + n or count >= MAX_EVENTS or not data:
                continue
            kind, channel = data[0] & 0xF0, data[0] & 0x0F
            if self._clap_notes and kind in (0x80, 0x90) and len(data) >= 3:
                e = self._note_pool[notes]
                notes += 1
                on = kind == 0x90 and data[2] > 0
                e.header.size, e.header.time, e.header.space_id = ctypes.sizeof(clap_event_note), offset - start, 0
                e.header.type = CLAP_EVENT_NOTE_ON if on else CLAP_EVENT_NOTE_OFF
                e.header.flags = CLAP_EVENT_IS_LIVE
                e.note_id, e.port_index, e.channel, e.key = -1, 0, channel, data[1]
                e.velocity = data[2] / 127.0
            elif midi_ok:
                e = self._midi_pool[raws]
                raws += 1
                e.header.size, e.header.time, e.header.space_id = ctypes.sizeof(clap_event_midi), offset - start, 0
                e.header.type, e.header.flags = CLAP_EVENT_MIDI, CLAP_EVENT_IS_LIVE
                e.port_index = 0
                b = bytes(data[:3]).ljust(3, b"\0")
                e.data[0], e.data[1], e.data[2] = b[0], b[1], b[2]
            else:
                continue  # a CC or pitch bend for a port without MIDI: nothing to map it to
            self._ev_ptrs[count] = ctypes.addressof(e)
            count += 1
        self._ev_count = count

    def _alloc(self) -> None:
        np = self._np

        def buffers(ports):
            arrays, structs = [], (clap_audio_buffer * max(len(ports), 1))()
            for i, port in enumerate(ports):
                ch = max(port["channels"], 1)
                data = np.zeros((ch, self.MAX_BLOCK), dtype=np.float32)
                ptrs = (POINTER(ctypes.c_float) * ch)(*[row.ctypes.data_as(POINTER(ctypes.c_float)) for row in data])
                arrays.append((data, ptrs))
                structs[i].data32 = ptrs
                structs[i].channel_count = port["channels"]
            return arrays, structs

        self._in_arrays, self._in_bufs = buffers(self._in_ports)
        self._out_arrays, self._out_bufs = buffers(self._out_ports)
        main_in = next((i for i, p in enumerate(self._in_ports) if p["main"]), 0 if self._in_ports else None)
        main_out = next((i for i, p in enumerate(self._out_ports) if p["main"]), 0 if self._out_ports else None)
        self._main_in, self._main_out = main_in, main_out
        self._process = clap_process(audio_inputs=self._in_bufs, audio_outputs=self._out_bufs,
                                     audio_inputs_count=len(self._in_ports), audio_outputs_count=len(self._out_ports),
                                     in_events=ctypes.pointer(self._in_events),
                                     out_events=ctypes.pointer(self._out_events))

    def _activate(self) -> None:
        """Main thread."""
        if not self._plugin.activate(self._p, float(self.sample_rate), 1, self.MAX_BLOCK):
            raise ClapError(f"the plugin couldn't start at {self.sample_rate} Hz")
        self._active = True
        # Its delay (frames), asked while active on the main thread as CLAP requires: host_worker._Align lines
        # what it hands back up with what went in. A change is announced with a restart, which asks again.
        latency = self._extension(b"clap.latency", clap_plugin_latency)
        try:
            self.latency_samples = int(latency.get(self._p)) if latency else 0
        except (OSError, ValueError):
            self.latency_samples = 0

    def _deactivate(self) -> None:
        """Main thread. Processing must have stopped."""
        if self._active:
            self._plugin.deactivate(self._p)
            self._active = False

    def _stop_processing(self) -> None:
        if self._processing:
            self._plugin.stop_processing(self._p)
            self._processing = False

    def _restart(self) -> None:
        """The plugin asked for it (request_restart): deactivate + activate, on
        the main thread. While audio runs, the audio thread first stops
        processing (see __call__) and then asks for this."""
        with self._lock:
            if self._processing or not self._restart_requested:
                return
            self._restart_requested = False
            self._deactivate()
            self._ports()
            self._alloc()
            self._activate()

    # ---- audio ----

    def reset(self) -> None:
        with self._lock:
            if self._active:
                self._plugin.reset(self._p)

    def __call__(self, audio, sample_rate, reset: bool = False, midi=None):
        """(channels, n) float32 in, (2, n) float32 out; like pedalboard's plugin
        call. `midi`: [(frame offset, bytes)] channel messages for this block."""
        np = self._np
        audio = np.asarray(audio, dtype=np.float32)
        if audio.ndim == 1:
            audio = audio[None, :]
        rate = int(sample_rate)
        self._audio_thread = win_editor.kernel32.GetCurrentThreadId()
        if self._restart_requested:
            with self._lock:
                self._stop_processing()  # audio thread
            self.main.call(self._restart)
        if rate != self.sample_rate:
            with self._lock:
                self._stop_processing()  # audio thread, as CLAP wants

            def apply():
                with self._lock:
                    self._deactivate()
                    self.sample_rate = rate
                    self._activate()

            self.main.call(apply)
        n_total = audio.shape[1]
        out = np.zeros((2, n_total), dtype=np.float32)
        with self._lock:
            if not self._active:
                return out
            if not self._processing:
                self._processing = bool(self._plugin.start_processing(self._p))
            if reset:
                self._plugin.reset(self._p)
            for start in range(0, n_total, self.MAX_BLOCK):
                n = min(self.MAX_BLOCK, n_total - start)
                chunk = audio[:, start:start + n]
                for data, _ptrs in self._in_arrays:
                    data[:, :n] = 0.0
                if self._main_in is not None:
                    data = self._in_arrays[self._main_in][0]
                    if data.shape[0] == 1:
                        data[0, :n] = chunk.mean(axis=0)
                    else:
                        data[0, :n] = chunk[0]
                        data[1, :n] = chunk[1] if chunk.shape[0] > 1 else chunk[0]
                for data, _ptrs in self._out_arrays:
                    data[:, :n] = 0.0
                self._process.frames_count = n
                self._process.steady_time = self._steady
                if midi:
                    self._fill_events(midi, start, n)
                try:
                    status = self._plugin.process(self._p, ctypes.byref(self._process))
                finally:
                    self._ev_count = 0
                self._steady += n
                if status == CLAP_PROCESS_ERROR:
                    continue  # silence for this block
                if self._main_out is not None:
                    data = self._out_arrays[self._main_out][0]
                    out[0, start:start + n] = data[0, :n]
                    out[1, start:start + n] = data[1 if data.shape[0] > 1 else 0, :n]
        return out

    # ---- editor ----

    def _request_size(self, width: int, height: int) -> None:
        def apply():
            if self._window and not self._window.closed and width > 0 and height > 0:
                self._window.set_client_size(width, height)
                if self._gui is not None:
                    self._gui.set_size(self._p, width, height)

        if self.main.is_main():
            apply()
        else:
            self.main.post(apply)

    def _on_resized(self, width: int, height: int):
        """The window was resized from outside (user / full screen): the plugin
        adjusts to a size it supports, which the window then snaps to."""
        gui, p = self._gui, self._p
        w, h = c_uint32(width), c_uint32(height)
        if not gui.adjust_size(p, ctypes.byref(w), ctypes.byref(h)):
            w, h = c_uint32(width), c_uint32(height)
        gui.set_size(p, w.value, h.value)
        return int(w.value), int(h.value)

    def show_editor(self, close_event: threading.Event) -> None:
        """Opens the editor embedded in a VEXT window and runs the message loop
        (main thread) until `close_event` is set or the window is closed."""
        gui, p = self._gui, self._p
        if not self.has_editor:
            raise ClapError("this plugin has no editor VEXT can show (no win32 GUI)")
        self._close_event = close_event
        embedded = bool(gui.is_api_supported(p, CLAP_WINDOW_API_WIN32, False))
        if not gui.create(p, CLAP_WINDOW_API_WIN32, not embedded):
            raise ClapError("the plugin couldn't create its editor")
        self._gui_created = True
        window = None
        try:
            if embedded:
                width, height = c_uint32(0), c_uint32(0)
                resizable = bool(gui.can_resize(p))
                window = win_editor.EditorWindow(self.name, 640, 480, resizable,
                                                 on_resized=self._on_resized if resizable else None,
                                                 on_close=close_event.set)
                self._window = window
                gui.set_scale(p, window.dpi_scale())
                if not gui.get_size(p, ctypes.byref(width), ctypes.byref(height)) or not width.value:
                    width, height = c_uint32(640), c_uint32(480)
                window.set_client_size(width.value, height.value)
                parent = clap_window(api=CLAP_WINDOW_API_WIN32, handle=window.hwnd)
                if not gui.set_parent(p, ctypes.byref(parent)):
                    raise ClapError("the plugin couldn't embed its editor")
                gui.show(p)
                window.show()
                self.editor_hwnd = int(window.hwnd)
                alive = lambda: not close_event.is_set() and not window.closed  # noqa: E731
            else:
                # Floating: the plugin owns its window; the worker finds it
                # (biggest window of the process), like pedalboard's.
                self.floating_editor = True
                gui.suggest_title(p, self.name.encode("utf-8"))
                gui.show(p)
                alive = lambda: not close_event.is_set()  # noqa: E731
            self.main.pump(alive)
        finally:
            self.editor_hwnd = None
            self._close_event = None
            try:
                gui.hide(p)
                gui.destroy(p)
            finally:
                self._gui_created = False
                if window:
                    window.destroy()
                self._window = None

    def close(self) -> None:
        """Stops, destroys and unloads the plugin (main thread)."""
        if getattr(self, "_p", None) is None:
            return
        try:
            with self._lock:
                if self._processing:
                    # stop_processing belongs to the audio thread (some plugins
                    # abort otherwise); the playback thread is gone by now, so a
                    # short-lived thread takes that role.
                    def stop():
                        self._audio_thread = win_editor.kernel32.GetCurrentThreadId()
                        self._stop_processing()

                    t = threading.Thread(target=stop, daemon=True)
                    t.start()
                    t.join(timeout=5)
                self._deactivate()
                self._plugin.destroy(self._p)
        finally:
            self._p = None
            self._lib.close()


def _pick(descriptors: list[dict], wanted: str | None) -> dict | None:
    if len(descriptors) == 1 and not wanted:
        return descriptors[0]
    if wanted:
        low = wanted.lower()
        for key in ("name", "id"):
            for d in descriptors:
                if d[key].lower() == low:
                    return d
        for d in descriptors:
            if d["name"].lower().startswith(low) or low.startswith(d["name"].lower()):
                return d
        return None
    return descriptors[0]
