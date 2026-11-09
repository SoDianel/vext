"""Opt-in Newtime/Newtone monitor tests; generated clips and a muted output sink."""
import argparse
import ctypes as C
import os
from pathlib import Path
import sys
import tempfile
import time

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))


def worker():
    from app import fl_native, fl_native_worker, host_worker as hw
    from app.hosting.vst2 import AEffect
    from fl_native_fixtures import write_rhythm_wav

    class Output:
        latency = .01
        def start(self): pass
        def abort(self): pass
        def stop(self): pass
        def close(self): pass
        def write(self, data):
            time.sleep(len(data) / int(os.environ["VEXT_AUDITION_TEST_RATE"]))
            return False

    rate = int(os.environ["VEXT_AUDITION_TEST_RATE"])
    hw.sounddevice_module = lambda *a, **k: None
    hw.Player._open_stream = staticmethod(lambda *a: (
        Output(), rate, 512, dict(rate=rate, block=512, driver="test", latency_ms=10)))

    class Host(C.Structure):
        _fields_ = [("vt", C.c_void_p), ("version", C.c_int32), ("flags", C.c_int32),
                    ("app", C.c_void_p), ("waves", C.c_void_p * 10),
                    ("buffers", C.c_void_p * 4), ("reserved", C.c_int32 * 30)]

    class Prefix(C.Structure):
        _fields_ = [("effect", AEffect), ("host", Host), ("paths", C.c_void_p),
                    ("hostvt", C.c_void_p * 64), ("pathvt", C.c_void_p * 64),
                    ("lib", C.c_void_p), ("native", C.c_void_p)]

    original = fl_native_worker._wait_ready

    def with_fixture(plugin):
        original(plugin)
        if plugin is not None:
            # Both exact-hash dispatchers read the selected path from Value for 18.
            binary = os.environ["VEXT_FL_NATIVE_DLL"]
            entry = fl_native.resolve(binary, refresh=True)[2]
            assert entry["name"] in ("Newtime", "Newtone") and entry.get("audition")
            path = Path(os.environ["VEXT_FL_NATIVE_DATA"]) / "generated-clip.wav"
            write_rhythm_wav(path, seconds=8, rate=rate, hz=440)
            native = Prefix.from_address(plugin._eff.object).native
            table = C.cast(native, C.POINTER(C.POINTER(C.c_void_p))).contents
            load = C.CFUNCTYPE(C.c_ssize_t, C.c_void_p, C.c_ssize_t,
                              C.c_ssize_t, C.c_ssize_t)(table[1])
            filename = C.c_char_p(str(path).encode("mbcs"))
            load(native, 18, 0, C.cast(filename, C.c_void_p).value)

    fl_native_worker._wait_ready = with_fixture
    return hw.main(*sys.argv[2:])


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        return worker()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installed-root", type=Path)
    parser.add_argument("--rate", type=int, action="append")
    args = parser.parse_args()
    if args.installed_root is None:
        print("SKIP: supply --installed-root explicitly")
        return 0
    with tempfile.TemporaryDirectory(prefix="vext-editor-audition-") as temporary:
        for name in ("VEXT_ROOT", "APPDATA", "LOCALAPPDATA", "USERPROFILE"):
            os.environ[name] = str(Path(temporary) / name)
        os.environ.update(VEXT_TEST_NATIVE_HIDDEN="1", VEXT_VIRTUAL_MIDI="0", VEXT_NO_STANDBY="1")
        from app import plugin_host as ph, tempo
        from fl_native_fixtures import click
        ph.worker_command = lambda path, options: [sys.executable, str(Path(__file__).resolve()),
                                                    "--worker", path, *options]

        def wait(predicate, timeout=15):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                state = ph.state(900001)
                assert state["state"] != "closed", state
                if predicate(state):
                    return state
                time.sleep(.05)
            raise AssertionError(ph.state(900001))

        for name in ("Newtime", "Newtone"):
            for rate in args.rate or (44100, 48000, 96000):
                os.environ["VEXT_AUDITION_TEST_RATE"] = str(rate)
                path = args.installed_root / "Effects" / name
                ph._providers["audio"] = lambda: dict(samplerate=rate, blocksize=512)
                ph.open_plugin(900001, str(path), "FL-NATIVE", name, framed=True)
                session = ph._sessions[900001]
                process = session.proc
                workspace = Path(session.launch_args[3])
                try:
                    state = wait(lambda s: s["state"] == "open" and s["editor_hwnd"])
                    assert state["native_audition"] and not state["editor_only"]
                    assert not state["is_instrument"] and not state["accepts_midi"]
                    assert state["audio"] is None and not ph.sessions_busy()
                    assert state["native_limitations"]
                    time.sleep(1)  # GUI analysis continues after the loader returns.
                    for action in (lambda: ph.set_source(900001, "browser"),
                                   lambda: ph.play(900001, "not-a-real-file.wav"),
                                   lambda: ph.send_midi(900001, [[0x90, 69, 100]])):
                        try:
                            action()
                        except ph.HostError:
                            pass
                        else:
                            raise AssertionError("audition editor accepted another source or MIDI")
                    before = tempo.transport_state()
                    ph.set_audition(900001, True)
                    wait(lambda s: s["source_kind"] == "editor" and s["audio"])
                    ph.reveal(900001)
                    time.sleep(.3)
                    click(state["editor_hwnd"], 48, 24)
                    session.command("capture", path=str(REPO / ".scratch" / (name + "-monitor-trial.png")))
                    wait(lambda s: (s["audio"] or {}).get("peak_out", 0) > .01)
                    from app import analysis_tap
                    import numpy as np
                    reader = analysis_tap.TapReader(session.tap_path)
                    try:
                        frames = reader.read(None, 0, 8192, newest=True)
                        assert float(np.abs(frames[6]).max()) > .01
                    finally:
                        reader.close()
                    assert tempo.transport_state() == before, "monitor claimed page transport"
                    assert not ph.open_midi_inputs(), "monitor opened musical MIDI inputs"
                    ph.set_audition(900001, False)
                    wait(lambda s: s["audio"] is None and s["source_kind"] is None)
                    time.sleep(.35)
                    assert ph.state(900001)["audio"] is None
                    ph.set_audition(900001, True)
                    wait(lambda s: (s["audio"] or {}).get("peak_out", 0) > .01)
                    ph.bypass(900001, True)
                    wait(lambda s: (s["audio"] or {}).get("peak_out", 1) == 0)
                    ph.bypass(900001, False)
                    wait(lambda s: (s["audio"] or {}).get("peak_out", 0) > .01)
                    session.command("capture", path=str(Path(temporary) / (name + str(rate) + ".png")))
                finally:
                    ph.close(900001)
                    process.wait(timeout=15)
                    ph.stop_standby()
                assert process.returncode == 0, ph._stderr_tails.get(process.pid)
                deadline = time.monotonic() + 10
                while workspace.exists() and time.monotonic() < deadline:
                    time.sleep(.05)
                assert not workspace.exists(), workspace
                print("PASS", name, rate, "monitor on/off/restart, mute, no MIDI/transport, capture and cleanup")
    return 0


if __name__ == "__main__":
    sys.exit(main())
