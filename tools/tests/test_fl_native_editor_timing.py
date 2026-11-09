"""Opt-in Newtone editor metadata regression; no stream, voices or clocks run."""
import argparse
import ctypes as C
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))


def child(binary, root):
    from app import fl_native, host_worker
    from app.fl_native_worker import _isolate, _no_children
    from app.hosting import win_editor
    from app.hosting.vst2 import AEffect, Vst2Plugin

    host_worker._prepare()
    _job = _no_children()
    api, key = _isolate(root)
    os.chdir(root)
    for name in ("AUDIO", "MIDI", "CLOCK"):
        os.environ.pop("VEXT_FL_NATIVE_" + name, None)
    os.environ.update(VEXT_FL_NATIVE_DLL=str(binary), VEXT_FL_NATIVE_DATA=str(root),
                      VEXT_FL_NATIVE_ROOT=str(binary.parents[2]))
    C.oledll.ole32.OleInitialize(None)
    main = win_editor.MainThread()
    window = win_editor.EditorWindow("VEXT editor timing regression", 1200, 900, False)
    os.environ["VEXT_FL_NATIVE_PARENT_HWND"] = str(window.hwnd)
    plugin = None
    try:
        class Host(C.Structure):
            _fields_ = [("vt", C.c_void_p), ("version", C.c_int32), ("flags", C.c_int32),
                        ("app", C.c_void_p), ("waves", C.c_void_p * 10),
                        ("buffers", C.c_void_p * 4), ("reserved", C.c_int32 * 30)]

        class Prefix(C.Structure):
            _fields_ = [("effect", AEffect), ("host", Host), ("paths", C.c_void_p),
                        ("hostvt", C.c_void_p * 64), ("pathvt", C.c_void_p * 64),
                        ("lib", C.c_void_p), ("native", C.c_void_p)]

        plugin = Vst2Plugin(str(fl_native.adapter_path()), main)
        plugin._dispatch(14, ptr=C.c_void_p(window.hwnd))
        native = Prefix.from_address(plugin._eff.object).native
        table = C.cast(native, C.POINTER(C.POINTER(C.c_void_p))).contents
        dispatch = C.CFUNCTYPE(C.c_ssize_t, C.c_void_p, C.c_ssize_t,
                              C.c_ssize_t, C.c_ssize_t)(table[1])
        # Exact allowlisted wrapper layout, established by the dispatcher and
        # REA time-provider dossier. Its clock period occupies bytes 16..19 of
        # the 44-byte request, not the host's separate 16-byte TFPTime record.
        inner = C.c_void_p.from_address(native + 0x1d4).value
        assert inner
        service = C.c_void_p.from_address(inner + 0x78).value
        assert service
        methods = C.cast(service, C.POINTER(C.POINTER(C.c_void_p))).contents
        get_time = C.CFUNCTYPE(None, C.c_void_p, C.c_uint32, C.c_void_p)(methods[3])
        for rate in (48000, 44100, 96000):
            if rate != 48000:
                dispatch(native, 4, 0, rate)
            record = (C.c_ubyte * 44)()
            get_time(service, 2, record)
            period = C.c_uint32.from_buffer(record, 16).value
            assert period == round(rate * 60 / 100), (rate, period)
            print("PASS: Newtone static clock-period metadata", rate, period, flush=True)
    finally:
        if plugin:
            plugin._dispatch(15)
            plugin.close()
        window.destroy()
        C.windll.ole32.OleUninitialize()
        api.RegOverridePredefKey(C.c_void_p(-2147483647), None)
        key.Close()


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        child(Path(sys.argv[2]), Path(sys.argv[3]))
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installed-root", type=Path)
    args = parser.parse_args()
    if args.installed_root is None:
        print("SKIP: supply --installed-root explicitly")
        return
    with tempfile.TemporaryDirectory(prefix="vext-editor-timing-") as temporary:
        for name in ("VEXT_ROOT", "USERPROFILE", "APPDATA", "LOCALAPPDATA"):
            os.environ[name] = str(Path(temporary) / name)
        os.environ["VEXT_VIRTUAL_MIDI"] = "0"
        from app import fl_native
        binary = args.installed_root / "Effects/Newtone/Newtone_x64.dll"
        entry = fl_native.resolve(str(binary), refresh=True)[2]
        assert entry["sha256"] == "3a47d9942d1d594270c064d7d819c09115c02f73fa11a6ab5e3e6ba18ff4911c"
        assert entry.get("audition") and not entry.get("midi") and not entry.get("clock")
        root = fl_native.WORKSPACES / uuid.uuid4().hex
        root.mkdir(parents=True)
        try:
            subprocess.run([sys.executable, str(Path(__file__).resolve()), "--child",
                            str(binary), str(root)], check=True, timeout=30)
        finally:
            fl_native.cleanup(str(root))


if __name__ == "__main__":
    main()
