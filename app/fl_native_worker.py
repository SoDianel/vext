"""Isolated native editors and validated effects using the Playground audio engine."""

import ctypes as C
from ctypes import wintypes as W
import json
import os
from pathlib import Path
import queue
import sys
import threading
import time
import winreg

from app import fl_native
from app.hosting.native_dialogs import error_text, owned_dialogs
from app.hosting.vst2 import AEffect, AudioMasterCallback


def _isolate(root: Path):
    for name, sub in (("USERPROFILE", "profile"), ("APPDATA", "profile/roaming"),
                      ("LOCALAPPDATA", "profile/local"), ("TEMP", "temp"), ("TMP", "temp"), ("VEXT_ROOT", "data")):
        folder = root / sub
        folder.mkdir(parents=True, exist_ok=True)
        os.environ[name] = str(folder)
    folders = {"AppData": os.environ["APPDATA"], "Local AppData": os.environ["LOCALAPPDATA"],
               "Personal": str(root / "profile/Documents"), "Desktop": str(root / "profile/Desktop"),
               "My Music": str(root / "profile/Music"), "Profile": os.environ["USERPROFILE"]}
    key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, fl_native.REGISTRY_ROOT + "\\" + root.name,
                             0, winreg.KEY_ALL_ACCESS)
    for sub in (r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders",
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"):
        with winreg.CreateKeyEx(key, sub) as handle:
            for name, value in folders.items():
                Path(value).mkdir(parents=True, exist_ok=True)
                winreg.SetValueEx(handle, name, 0, winreg.REG_SZ, value)
    api = C.WinDLL("advapi32", use_last_error=True)
    api.RegOverridePredefKey.argtypes = [W.HKEY, W.HKEY]
    api.RegOverridePredefKey.restype = W.LONG
    status = api.RegOverridePredefKey(W.HKEY(-2147483647), W.HKEY(int(key)))
    if status:
        key.Close()
        raise OSError(status, "native editor registry isolation failed")
    return api, key


def _no_children():
    kernel = C.WinDLL("kernel32", use_last_error=True)

    class Basic(C.Structure):
        _fields_ = [("process_time", C.c_int64), ("job_time", C.c_int64), ("flags", W.DWORD),
                    ("min_ws", C.c_size_t), ("max_ws", C.c_size_t), ("active", W.DWORD),
                    ("affinity", C.c_size_t), ("priority", W.DWORD), ("scheduling", W.DWORD)]

    class Extended(C.Structure):
        _fields_ = [("basic", Basic), ("io", C.c_uint64 * 6), ("process_memory", C.c_size_t),
                    ("job_memory", C.c_size_t), ("peak_process", C.c_size_t), ("peak_job", C.c_size_t)]

    kernel.CreateJobObjectW.argtypes = [C.c_void_p, W.LPCWSTR]
    kernel.CreateJobObjectW.restype = W.HANDLE
    kernel.SetInformationJobObject.argtypes = [W.HANDLE, C.c_int, C.c_void_p, W.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [W.HANDLE, W.HANDLE]
    kernel.GetCurrentProcess.restype = W.HANDLE
    job = kernel.CreateJobObjectW(None, None)
    limits = Extended()
    limits.basic.flags, limits.basic.active = 0x2000 | 0x8, 1
    if (not job or not kernel.SetInformationJobObject(job, 9, C.byref(limits), C.sizeof(limits))
            or not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess())):
        raise C.WinError(C.get_last_error())
    return job  # keep the handle until process exit; no helper/FL launches


def _wait_ready(plugin) -> None:
    """Keep the creator thread pumping while a verified native sample loader runs."""
    loading = plugin._dll.VEXTNativeLoading
    loading.argtypes, loading.restype = [C.c_void_p], C.c_int32
    deadline = time.monotonic() + 10

    def pending():
        plugin._dispatch(19)
        if not loading(plugin._effp):
            return False
        if time.monotonic() >= deadline:
            raise RuntimeError("Native factory sample loading timed out")
        return True

    plugin.main.pump(pending)


def run(host_path: str, opts: dict, emit) -> int:
    """The backend supplies the workspace; clients cannot select data or DLL paths."""
    from app import host_worker
    from types import SimpleNamespace

    root = fl_native.workspace_path(opts.get("native-data") or "")
    binary, factory_root, entry = fl_native.resolve(host_path, refresh=True)
    capture_only = bool(opts.get("native_capture") and opts.get("capture"))
    midi = not capture_only and bool(entry.get("midi")) and entry["instrument"]
    audio = not capture_only and bool(entry.get("audio")) and (not entry["instrument"] or midi)
    clock = not capture_only and bool(entry.get("clock"))
    audition = audio and bool(entry.get("audition"))
    note = fl_native.CLOCK_NOTE if clock else fl_native.MIDI_NOTE if midi else fl_native.AUDIO_NOTE if audio else fl_native.EDITOR_NOTE
    if audition:
        note = fl_native.AUDITION_NOTE
    hint = ""
    if midi and entry["name"] == "Morphine":
        hint = "Load a preset in Morphine to play it."
    if midi and entry["name"] == "PLUCKED!":
        hint = "Enable Gate in Plucked! to stop notes on release."
    if midi and entry["name"] == "SoundFont Player":
        hint = "Load a soundfont in SoundFont Player to play it."
    if midi and entry["name"] == "FPC":
        hint = "Load samples onto the FPC pads to play them. Pad 1 is MIDI note 60."
    if midi and entry["name"] == "DirectWave Player":
        hint = "Load a program in DirectWave to play it."
    if midi and entry["name"] == "Drumaxx":
        hint = "Assign pad keys in Drumaxx to match your MIDI notes."
    if midi and entry["name"] == "Slicex":
        hint = "Load audio in Slicex and play its mapped slice notes. Piano-roll export is unavailable."
    if audio and entry["name"] == "Vocodex":
        hint = "Enable L-R encoding for stereo input. Extra sidechain buses are unavailable."
    if audio and entry["name"] == "Transmitter":
        hint = "Raise the transient or sustain level in Transmitter to hear it."
    if audition:
        hint = "Enable Monitor, load audio in the editor, then use its Play/Stop controls. Monitor off silences the output."
    if not audio and entry["name"] in ("Newtime", "Newtone"):
        hint = "Audio editing only; playback is not available in VEXT yet."
    if not audio and entry["name"] == "Patcher":
        hint = "Nested plugin hosting is not available in VEXT yet."
    if midi and entry["name"] == "FL Studio Mobile":
        hint = "Add a synth track in FL Studio Mobile to play it."
    if midi and entry["name"] == "Mobile Rack":
        hint = "Add a module in Mobile Rack to play it."
    if audio and entry["name"] == "Mobile Rack FX":
        hint = "Add an effect module in Mobile Rack FX to hear it."
    if not audio and entry["name"] in (
            "VFX Color Mapper", "VFX Envelope", "VFX Key Mapper",
            "VFX Keyboard Splitter", "VFX Level Scaler", "VFX Sequencer"):
        hint = "Note and controller routing are not available in VEXT yet."
    if not audio and entry["name"] in ("Control Surface", "Dashboard", "Fruity X-Y controller"):
        hint = "Controller output is not available in VEXT yet."
    if not audio and entry["name"] == "MIDI Out":
        hint = "External MIDI output is not available in VEXT yet."
    if audio and entry["name"] == "Fruity Scratcher":
        hint = "Load a WAV with the folder button, then press play on the deck."
    if midi and entry["name"] == "Fruity granulizer":
        hint = "Click the sample area to load a WAV."
    if midi and entry["name"] == "Wave Traveller":
        hint = "Load a WAV with the folder button to play it."
    if midi and entry["name"] in ("Fruity Slicer", "Fruity Slicer 2"):
        hint = "Load a WAV to slice it; notes from MIDI 60 up play the slices."
    if not audio and entry["name"].casefold() in (
            "fruity formula controller", "fruity peak controller", "fruity envelope controller",
            "fruity keyboard controller", "fruity x-y-z controller"):
        hint = "Controller output is not available in VEXT yet."
    note = " ".join(filter(None, (note, hint)))
    root.mkdir(parents=True, exist_ok=False)
    job = _no_children()
    api, key = _isolate(root)
    os.chdir(root)
    fl_native.prepare_resources(root, factory_root, entry)
    os.environ.update(VEXT_FL_NATIVE_DLL=str(binary), VEXT_FL_NATIVE_ROOT=str(factory_root),
                      VEXT_FL_NATIVE_DATA=str(root))
    os.environ.pop("VEXT_FL_NATIVE_AUDIO", None)
    os.environ.pop("VEXT_FL_NATIVE_MIDI", None)
    os.environ.pop("VEXT_FL_NATIVE_CLOCK", None)
    if clock:
        os.environ["VEXT_FL_NATIVE_CLOCK"] = "1"
    if midi:
        os.environ["VEXT_FL_NATIVE_MIDI"] = "1"
    if audio:
        os.environ["VEXT_FL_NATIVE_AUDIO"] = "1"
    kernel = C.WinDLL("kernel32", use_last_error=True)
    kernel.SetErrorMode(0x8003)
    ole = C.OleDLL("ole32")
    ole.OleInitialize.argtypes = [C.c_void_p]
    ole.OleInitialize(None)
    done = threading.Event()
    commands = queue.Queue()
    user = C.WinDLL("user32", use_last_error=True)
    callback_type = C.WINFUNCTYPE(C.c_ssize_t, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
    user.DefWindowProcW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
    user.DefWindowProcW.restype = C.c_ssize_t

    @callback_type
    def window_proc(hwnd, message, wparam, lparam):
        if message == 0x10:  # close through the native lifecycle, not DestroyWindow
            done.set()
            return 0
        return user.DefWindowProcW(hwnd, message, wparam, lparam)

    class WindowClass(C.Structure):
        _fields_ = [("style", W.UINT), ("proc", callback_type), ("class_extra", C.c_int),
                    ("window_extra", C.c_int), ("instance", W.HINSTANCE), ("icon", W.HICON),
                    ("cursor", W.HANDLE), ("background", W.HBRUSH), ("menu", W.LPCWSTR), ("name", W.LPCWSTR)]

    kernel.GetModuleHandleW.argtypes = [W.LPCWSTR]
    kernel.GetModuleHandleW.restype = W.HMODULE
    instance = kernel.GetModuleHandleW(None)
    klass = WindowClass(proc=window_proc, instance=instance, background=W.HBRUSH(6), name="VEXTNativeEditor")
    user.RegisterClassW.argtypes = [C.POINTER(WindowClass)]
    if not user.RegisterClassW(C.byref(klass)):
        raise C.WinError(C.get_last_error())
    user.CreateWindowExW.argtypes = [W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD, C.c_int, C.c_int,
                                   C.c_int, C.c_int, W.HWND, W.HMENU, W.HINSTANCE, C.c_void_p]
    user.CreateWindowExW.restype = W.HWND
    user.IsWindow.argtypes = [W.HWND]
    user.GetClientRect.argtypes = [W.HWND, C.POINTER(W.RECT)]
    user.GetWindowThreadProcessId.argtypes = [W.HWND, C.POINTER(W.DWORD)]
    user.GetParent.argtypes, user.GetParent.restype = [W.HWND], W.HWND
    user.ShowWindow.argtypes = [W.HWND, C.c_int]
    user.MoveWindow.argtypes = [W.HWND, C.c_int, C.c_int, C.c_int, C.c_int, W.BOOL]
    user.DestroyWindow.argtypes = [W.HWND]
    user.AdjustWindowRectEx.argtypes = [C.POINTER(W.RECT), W.DWORD, W.BOOL, W.DWORD]
    user.SystemParametersInfoW.argtypes = [W.UINT, W.UINT, C.c_void_p, W.UINT]
    style = 0x00C80000 | 0x02000000
    title = f"{entry['name']} — VEXT" + ("" if audio else " · Clock only" if clock else " · Editor only")
    if opts.get("hidden"):
        host_worker._hold.start()
    if capture_only:
        host_worker._hold.start(forever=True)
    parent = user.CreateWindowExW(0, klass.name, title, style,
                                  -32000, -32000, 1400, 1000, None, None, instance, None)
    if not parent:
        raise C.WinError(C.get_last_error())
    os.environ["VEXT_FL_NATIVE_PARENT_HWND"] = str(int(parent))

    def read_commands():
        try:
            for line in sys.stdin:
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if isinstance(value, dict):
                    commands.put(value)
        finally:
            done.set()

    def watch_dialogs():
        waiting = False
        while not done.wait(0.2):
            dialogs = owned_dialogs(os.getpid())
            for dialog in dialogs:
                text = error_text(dialog)
                if text:
                    emit("error", code="failed", message=f"Native editor error: {text}")
                    os._exit(1)  # a blocked GUI callback cannot be closed cooperatively
            if bool(dialogs) != waiting:
                waiting = bool(dialogs)
                emit("status", dialog=waiting, message="The native editor opened a dialog that needs an answer." if waiting else note)

    if not audio:
        threading.Thread(target=read_commands, daemon=True).start()
    threading.Thread(target=watch_dialogs, daemon=True).start()
    started = time.monotonic()
    emit("status", message="Opening the experimental FL native editor…")
    effect, pointer, editor, plugin, player, live_input = None, None, None, None, None, None
    try:
        library = C.CDLL(str(fl_native.adapter_path()))
        version = library.VEXTNativeAdapterVersion
        version.restype = C.c_int
        if version() != fl_native.ADAPTER_VERSION:
            raise RuntimeError("Native editor adapter version mismatch")

        @AudioMasterCallback
        def vst_host(effect, opcode, index, value, ptr, opt):
            return 2400 if opcode == 1 else 0

        entry_point = library.VSTPluginMain
        entry_point.argtypes, entry_point.restype = [AudioMasterCallback], C.c_void_p
        if audio:
            from app.hosting.vst2 import Vst2Plugin
            from app.hosting.win_editor import MainThread

            plugin = Vst2Plugin(str(fl_native.adapter_path()), MainThread(),
                                int(opts.get("rate") or 48000), int(opts.get("block") or 1024))
            plugin.native_effect = not midi
            plugin.native_instrument = midi
            plugin.native_audition = audition
            pointer = plugin._effp
        else:
            pointer = entry_point(vst_host)
        if not pointer:
            raise RuntimeError("The native plugin refused to create its editor")
        clock_tempo, clock_playing = 100.0, False
        clock_transport = library.VEXTNativeSetTransport
        clock_transport.argtypes = [C.c_void_p, C.c_double, C.c_int32, C.c_int64]
        clock_transport.restype = None
        effect = C.cast(pointer, C.POINTER(AEffect)).contents
        if effect.magic != 0x56737450:
            raise RuntimeError("Invalid native editor adapter interface")
        if not audio:
            effect.dispatcher(pointer, 0, 0, 0, None, 0)
        effect.dispatcher(pointer, 14, 0, 0, parent, 0)
        if plugin is not None:
            _wait_ready(plugin)
        editor_handle = library.VEXTNativeEditorHandle
        editor_handle.argtypes, editor_handle.restype = [C.c_void_p], W.HWND
        editor = editor_handle(pointer)
        pid, rect = W.DWORD(), W.RECT()
        if editor:
            user.GetWindowThreadProcessId(editor, C.byref(pid))
        if not editor or not user.IsWindow(editor) or pid.value != os.getpid():
            raise RuntimeError("The native plugin did not create an owned editor window")
        user.GetClientRect(editor, C.byref(rect))
        width, height = rect.right - rect.left, rect.bottom - rect.top
        if not 0 < width <= 16384 or not 0 < height <= 16384:
            raise RuntimeError("The native plugin returned an invalid editor size")
        # Dashboard's empty panel starts too small for its toolbar/property pane.
        if entry["name"] == "Dashboard":
            width, height = max(width, 620), max(height, 420)
        # Some Delphi editors retain their former desktop position when attached.
        # Keep an existing child at the host origin so its controls are not clipped.
        if user.GetParent(editor) == parent:
            if not user.MoveWindow(editor, 0, 0, width, height, True):
                raise C.WinError(C.get_last_error())
        outer, work = W.RECT(0, 0, width, height), W.RECT(0, 0, 1920, 1080)
        user.AdjustWindowRectEx(C.byref(outer), style, False, 0)
        user.SystemParametersInfoW(48, 0, C.byref(work), 0)
        w, h = min(outer.right - outer.left, work.right - work.left), min(outer.bottom - outer.top, work.bottom - work.top)
        user.MoveWindow(parent, work.left + max(0, (work.right - work.left - w) // 2),
                        work.top + max(0, (work.bottom - work.top - h) // 2), w, h, True)
        host_worker._editor.owner = SimpleNamespace(editor_hwnd=int(parent))
        host_worker._editor.hwnd = int(parent)
        user.ShowWindow(editor, 5)
        # Used only by isolated regression tests; desktop opens always show the editor.
        if os.environ.get("VEXT_TEST_NATIVE_HIDDEN") != "1":
            user.ShowWindow(parent, 5)
        extra = {}
        if audio:
            from app import host_worker

            live_input = host_worker._LiveInput()
            player = host_worker.Player(plugin, live_input)
            host_worker._editor.ready.set()
            extra.update(live_port=live_input.port, live_key=live_input.key)
            threading.Thread(target=host_worker._commands,
                             args=(plugin, player, done, lambda: commands.put({"cmd": "reveal"})), daemon=True).start()
        emit("loaded", format="FL-NATIVE", plugin_name=entry["name"], is_instrument=entry["instrument"],
             accepts_midi=midi, editor_only=not audio, clock_only=clock, external_editor=False,
             note=note, native_hint=hint, native_limitations=fl_native.limitations(entry), native_audition=audition,
             load_s=round(time.monotonic() - started, 2), **extra)
        emit("editor", hwnd=int(parent), width=width, height=height, hidden=host_worker._hold.active)
        threading.Thread(target=host_worker._watch_editor, args=(done,), daemon=True).start()
        if capture_only:
            threading.Thread(target=host_worker._auto_capture,
                             args=(opts["capture"], done, bool(opts.get("capture_screen"))), daemon=True).start()
        user.PeekMessageW.argtypes = [C.POINTER(W.MSG), W.HWND, W.UINT, W.UINT, W.UINT]
        user.TranslateMessage.argtypes = [C.POINTER(W.MSG)]
        user.DispatchMessageW.argtypes = [C.POINTER(W.MSG)]
        user.DispatchMessageW.restype = C.c_ssize_t
        while not done.is_set() and user.IsWindow(parent) and user.IsWindow(editor):
            message = W.MSG()
            until = time.monotonic() + 0.01
            while time.monotonic() < until and user.PeekMessageW(C.byref(message), None, 0, 0, 1):
                user.TranslateMessage(C.byref(message))
                user.DispatchMessageW(C.byref(message))
            for _ in range(32):
                try:
                    cmd = commands.get_nowait()
                except queue.Empty:
                    break
                action = cmd.get("cmd")
                error = None
                if action == "quit":
                    done.set()
                elif action in ("tempo", "transport"):
                    if clock:
                        clock_tempo = float(cmd.get("bpm", clock_tempo))
                        clock_playing = bool(cmd.get("playing", clock_playing))
                        position = int(max(0, float(cmd["position"])) * 48000) if "position" in cmd else -1
                        clock_transport(pointer, clock_tempo, clock_playing, position)
                elif action == "capture":
                    try:
                        method = host_worker.capture_editor(cmd["path"])
                        emit("captured", path=cmd["path"], method=method)
                    except Exception as exc:
                        error = str(exc)
                elif action == "reveal":
                    host_worker._hold.request_reveal("command")
                    user.ShowWindow(parent, 9)
                    emit("revealed")
                else:
                    error = fl_native.EDITOR_NOTE
                if "id" in cmd:
                    emit("ack", id=cmd["id"], error=error)
            if plugin is not None:
                plugin.main.run_pending()
            effect.dispatcher(pointer, 19, 0, 0, None, 0)
            done.wait(0.01)
    finally:
        done.set()
        if plugin is not None:
            plugin.main.run_pending()
        if player is not None:
            player.stop()
            player.record_off()
            player.close_tap()
            player.midi.shutdown()
        if effect is not None:
            effect.dispatcher(pointer, 15, 0, 0, None, 0)
            if plugin is not None:
                plugin.close()
            else:
                effect.dispatcher(pointer, 1, 0, 0, None, 0)
        if live_input is not None:
            live_input.close()
        user.DestroyWindow(parent)
        ole.OleUninitialize()
        api.RegOverridePredefKey(W.HKEY(-2147483647), None)
        key.Close()
    emit("closed")
    return 0
