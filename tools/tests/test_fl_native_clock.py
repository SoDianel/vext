"""Opt-in clock-only native check; no audio stream, MIDI port or user data."""
import argparse
import ctypes as C
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))


def child(binary, root):
    from app import fl_native, host_worker
    from app.fl_native_worker import _isolate, _no_children
    from app.hosting import win_editor
    from app.hosting.vst2 import Vst2Plugin

    host_worker._prepare()
    root = Path(root)
    root.mkdir(parents=True)
    job = _no_children()
    api, key = _isolate(root)
    os.chdir(root)
    os.environ.update(VEXT_FL_NATIVE_DLL=binary, VEXT_FL_NATIVE_ROOT=str(Path(binary).parents[2]),
                      VEXT_FL_NATIVE_DATA=str(root), VEXT_FL_NATIVE_CLOCK="1")
    os.environ.pop("VEXT_FL_NATIVE_AUDIO", None)
    os.environ.pop("VEXT_FL_NATIVE_MIDI", None)
    ole = C.OleDLL("ole32")
    ole.OleUninitialize.restype = None  # void: not an HRESULT
    ole.OleInitialize(None)
    main = win_editor.MainThread()
    window = win_editor.EditorWindow("VEXT clock regression", 800, 400, False)
    os.environ["VEXT_FL_NATIVE_PARENT_HWND"] = str(window.hwnd)
    plugin = Vst2Plugin(str(fl_native.adapter_path()), main)
    try:
        plugin._dispatch(14, ptr=C.c_void_p(window.hwnd))
        host_address = plugin._eff.object + 192
        table = C.cast(host_address, C.POINTER(C.POINTER(C.c_void_p))).contents
        ticks = C.CFUNCTYPE(C.c_double, C.c_void_p)(table[17])
        plugin.set_transport(bpm=120, playing=False, position=1)
        assert abs(ticks(host_address) - 192) < .01
        plugin.set_transport(playing=True)
        time.sleep(.15)
        assert 215 < ticks(host_address) < 235
        plugin.set_transport(playing=False)
        stopped = ticks(host_address)
        time.sleep(.15)
        assert ticks(host_address) == stopped
        plugin.set_transport(bpm=90, position=0, playing=True)
        time.sleep(.15)
        assert 17 < ticks(host_address) < 30
        plugin.set_transport(playing=False)
    finally:
        plugin._dispatch(15)
        plugin.close()
        window.destroy()
        ole.OleUninitialize()
        api.RegOverridePredefKey(C.c_void_p(-2147483647), None)
        key.Close()
    assert job
    print("PASS", flush=True)


def main():
    if len(sys.argv) == 4 and sys.argv[1] == "--child":
        child(sys.argv[2], sys.argv[3])
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installed-root", type=Path)
    args = parser.parse_args()
    if args.installed_root is None:
        print("SKIP: supply --installed-root explicitly")
        return
    with tempfile.TemporaryDirectory(prefix="vext-native-clock-") as temp:
        for name in ("VEXT_ROOT", "APPDATA", "LOCALAPPDATA", "USERPROFILE"):
            os.environ[name] = str(Path(temp) / name)
        from app import fl_native

        binary, _, entry = fl_native.resolve(str(args.installed_root / "Effects/Fruity Big Clock"))
        assert entry.get("clock") and not entry.get("audio")
        root = fl_native.WORKSPACES / uuid.uuid4().hex
        try:
            result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--child", str(binary), str(root)],
                                    capture_output=True, timeout=25)
            assert result.returncode == 0, result.stderr.decode("utf-8", "replace")[-3000:]
        finally:
            fl_native.cleanup(str(root))
    print("PASS: clock-only native tempo, elapsed time, pause and relocation")


if __name__ == "__main__":
    main()
