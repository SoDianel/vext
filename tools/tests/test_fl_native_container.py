"""Opt-in native container/capture protocol check, with no audio device.

Use --installed-root <Plugins/Fruity>; all settings, pictures and taps are disposable.
"""
import argparse
import os
from pathlib import Path
import sys
import tempfile
import time

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))


def worker():
    from app import host_worker as hw

    class Output:
        latency = .01
        def start(self): pass
        def abort(self): pass
        def stop(self): pass
        def close(self): pass
        def write(self, data):
            time.sleep(len(data) / 44100)
            return False

    hw.sounddevice_module = lambda *a, **k: None
    hw.Player._open_stream = staticmethod(lambda *a: (
        Output(), 44100, 512, dict(rate=44100, block=512, driver="test", latency_ms=10)))
    return hw.main(*sys.argv[2:])


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        return worker()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installed-root", type=Path)
    parser.add_argument("--plugin", action="append", help="Exact Effects/Generators folder")
    args = parser.parse_args()
    if args.installed_root is None:
        print("SKIP: supply --installed-root explicitly")
        return 0
    with tempfile.TemporaryDirectory(prefix="vext-native-container-") as temp:
        for name in ("VEXT_ROOT", "APPDATA", "LOCALAPPDATA", "USERPROFILE"):
            os.environ[name] = str(Path(temp) / name)
        os.environ.update(VEXT_TEST_NATIVE_HIDDEN="1", VEXT_VIRTUAL_MIDI="0", VEXT_NO_STANDBY="1")
        import numpy as np
        import ctypes as C
        from ctypes import wintypes as W
        from PIL import Image
        from app import plugin_host as ph, analysis_tap, fl_native

        ph.worker_command = lambda path, options: [sys.executable, str(Path(__file__).resolve()), "--worker", path, *options]
        for folder in ("Generators/3x Osc", "Generators/FL Keys", "Generators/Kepler",
                       "Generators/Kepler Exo", "Generators/Transistor Bass", "Generators/Morphine",
                       "Generators/Drumaxx", "Generators/Soundfont Player", "Generators/DirectWave", "Generators/FPC",
                       "Generators/Fruity Slicer", "Generators/Fruity Slicer 2", "Generators/Fruity Granulizer",
                       "Generators/Slicex",
                       "Generators/Plucked!", "Generators/SimSynth",
                       "Generators/Fruity DrumSynth Live", "Generators/GMS", "Generators/Minisynth",
                       "Generators/Sakura", "Generators/Sawer",
                       "Generators/BeepMap", "Generators/Fruit Kick",
                       "Effects/Fruity Balance", "Effects/Vocodex", "Effects/Fruity Scratcher",
                       "Generators/Wave Traveller", "Effects/Newtone", "Effects/Fruity Big Clock",
                       "Effects/Fruity Formula Controller", "Effects/Fruity Peak Controller",
                       "Generators/Fruity Envelope Controller", "Generators/Fruity Keyboard Controller",
                       "Effects/Fruity X-Y-Z Controller", "Effects/Newtime", "Effects/Effector",
                       "Effects/Transmitter", "Effects/Transporter", "Effects/Patcher", "Generators/Patcher",
                       "Generators/FL Studio Mobile", "Generators/FL Studio Mobile Rack",
                       "Effects/FL Studio Mobile Rack FX", "Effects/VFX Script",
                       "Effects/VFX Color Mapper", "Effects/VFX Envelope", "Effects/VFX Key Mapper",
                       "Effects/VFX Keyboard Splitter", "Effects/VFX Level Scaler",
                       "Effects/Control Surface", "Generators/Dashboard", "Effects/Fruity X-Y Controller",
                       "Effects/VFX Sequencer", "Generators/MIDI Out"):
            if args.plugin and folder not in args.plugin:
                continue
            path = str(args.installed_root / folder)
            if folder == "Effects/VFX Script":
                before = set(ph._sessions)
                try:
                    ph.open_plugin(900001, path, "FL-NATIVE", "VFX Script", framed=True)
                except ph.HostError as exc:
                    assert "embedded Python" in str(exc) and "not loaded" in str(exc), str(exc)
                else:
                    raise AssertionError("VFX Script opened despite its unsafe script runtime")
                assert set(ph._sessions) == before
                print("PASS VFX Script refused before worker startup")
                continue
            entry = fl_native.resolve(path)[2]
            ph.open_plugin(900001, path, "FL-NATIVE", entry["name"], framed=True)
            session = ph._sessions[900001]
            proc = session.proc
            workspace = Path(session.launch_args[3])
            try:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    state = ph.state(900001)
                    if state["state"] == "open" and state["editor_hwnd"]:
                        if not entry.get("midi") or state["audio"]:
                            break
                    assert state["state"] != "closed", state
                    time.sleep(.05)
                else:
                    raise AssertionError(state)
                assert not state["external_editor"] and state["editor_hidden"]
                assert state["clock_only"] == bool(entry.get("clock"))
                if entry["name"] in ("VFX Color Mapper", "VFX Envelope", "VFX Key Mapper",
                                     "VFX Keyboard Splitter", "VFX Level Scaler", "VFX Sequencer"):
                    assert state["editor_only"] and state["audio"] is None
                    assert not state["accepts_midi"] and not state["clock_only"]
                    assert state["native_hint"] == "Note and controller routing are not available in VEXT yet."
                    assert not session.tap_path or not Path(session.tap_path).exists()
                    try:
                        ph.set_source(900001, "browser")
                    except ph.HostError:
                        pass
                    else:
                        raise AssertionError("VFX editor accepted an audio source")
                if entry["name"] in ("Control Surface", "Dashboard", "Fruity X-Y controller", "MIDI Out"):
                    assert state["editor_only"] and state["audio"] is None
                    assert not state["accepts_midi"] and not state["clock_only"]
                    expected = ("External MIDI output is not available in VEXT yet." if entry["name"] == "MIDI Out"
                                else "Controller output is not available in VEXT yet.")
                    assert state["native_hint"] == expected
                    assert not session.tap_path or not Path(session.tap_path).exists()
                    for action in (lambda: ph.set_source(900001, "browser"),
                                   lambda: ph.send_midi(900001, [[0x90, 60, 100]])):
                        try:
                            action()
                        except ph.HostError:
                            pass
                        else:
                            raise AssertionError("Disconnected controller accepted audio or MIDI")
                    if entry["name"] == "Dashboard":
                        user = C.WinDLL("user32", use_last_error=True)
                        user.GetClientRect.argtypes = [W.HWND, C.POINTER(W.RECT)]
                        rectangle = W.RECT()
                        assert user.GetClientRect(state["editor_hwnd"], C.byref(rectangle))
                        assert rectangle.right >= 620 and rectangle.bottom >= 420, (rectangle.right, rectangle.bottom)
                if entry["name"] == "Patcher" or "FL Studio Mobile" in folder:
                    if entry["name"] == "Patcher":
                        assert state["editor_only"] and state["audio"] is None
                        assert not state["accepts_midi"] and not state["clock_only"]
                    expected = {"Patcher": "Nested plugin hosting is not available in VEXT yet.",
                                "FL Studio Mobile": "Add a synth track in FL Studio Mobile to play it.",
                                "Mobile Rack": "Add a module in Mobile Rack to play it.",
                                "Mobile Rack FX": "Add an effect module in Mobile Rack FX to hear it."}[entry["name"]]
                    assert state["native_hint"] == expected
                    if "FL Studio Mobile" in folder:
                        resources = workspace / "path-selector-1048"
                        assert len(list(resources.glob("*.cso"))) == 6
                        assert not list(resources.glob("*.dll"))
                        source = args.installed_root / "Generators/FL Studio Mobile/Installed"
                        catalog = resources / "samplesList.data"
                        if catalog.exists():
                            assert catalog.read_bytes() != (source / "samplesList.data").read_bytes()
                        for name in ("SampleVertexShader.cso", "SamplePixelShader.cso", "TexturePixelShader.cso",
                                     "CirclePixelShader.cso", "FeatherPixelShader.cso", "FontPixelShader.cso",
                                     "title.png", "wrap.png", "gamepad.png", "gamepad_fill.png",
                                     "oscdata.dat", "FilterTable_Ctf_Res.data"):
                            asset = resources / name
                            assert asset.is_file() and asset.stat().st_size <= 1024 * 1024
                            assert asset.read_bytes() == (source / asset.name).read_bytes()
                if entry["name"] == "Transmitter":
                    assert state["native_hint"] == "Raise the transient or sustain level in Transmitter to hear it."
                if entry["name"] in ("Newtime", "Newtone"):
                    assert not state["editor_only"] and state["native_audition"]
                    assert state["audio"] is None and not state["accepts_midi"]
                    assert "Enable Monitor" in state["native_hint"]
                if folder.endswith(("Formula Controller", "Peak Controller", "Envelope Controller",
                                    "Keyboard Controller", "X-Y-Z Controller")):
                    assert state["editor_only"] and state["audio"] is None
                    assert state["native_hint"] == "Controller output is not available in VEXT yet."
                    user = C.WinDLL("user32", use_last_error=True)
                    user.GetWindow.argtypes, user.GetWindow.restype = [W.HWND, W.UINT], W.HWND
                    user.GetWindowRect.argtypes = [W.HWND, C.POINTER(W.RECT)]
                    user.ClientToScreen.argtypes = [W.HWND, C.POINTER(W.POINT)]
                    child = user.GetWindow(state["editor_hwnd"], 5)  # first direct child
                    assert child, (folder, state)
                    origin, rectangle = W.POINT(), W.RECT()
                    assert user.ClientToScreen(state["editor_hwnd"], C.byref(origin))
                    assert user.GetWindowRect(child, C.byref(rectangle))
                    assert (rectangle.left, rectangle.top) == (origin.x, origin.y), (
                        "native editor controls clipped by a retained desktop offset", folder,
                        rectangle.left, rectangle.top, origin.x, origin.y)
                if entry["name"] == "Vocodex":
                    assert state["native_hint"] == "Enable L-R encoding for stereo input. Extra sidechain buses are unavailable."
                if entry["name"] == "Fruity Scratcher":
                    assert state["native_hint"] == "Load a WAV with the folder button, then press play on the deck."
                if entry["name"] == "Fruity granulizer":
                    assert state["native_hint"] == "Click the sample area to load a WAV."
                if entry["name"] == "Wave Traveller":
                    assert state["native_hint"] == "Load a WAV with the folder button to play it."
                if entry["name"] in ("Fruity Slicer", "Fruity Slicer 2"):
                    assert state["native_hint"] == "Load a WAV to slice it; notes from MIDI 60 up play the slices."
                ph.reveal(900001)
                time.sleep(.25)
                if entry.get("audio"):
                    ph.set_gain(900001, in_db=0, out_db=0, auto_balance=False)
                    ph.bypass(900001, True)
                    ph.bypass(900001, False)
                if entry.get("midi"):
                    assert ph.sessions_busy()
                    if entry["name"] == "Morphine":
                        assert state["native_hint"] == "Load a preset in Morphine to play it."
                    elif entry["name"] == "PLUCKED!":
                        assert state["native_hint"] == "Enable Gate in Plucked! to stop notes on release."
                    elif entry["name"] == "SoundFont Player":
                        assert state["native_hint"] == "Load a soundfont in SoundFont Player to play it."
                    elif entry["name"] == "FPC":
                        assert state["native_hint"] == "Load samples onto the FPC pads to play them. Pad 1 is MIDI note 60."
                    elif entry["name"] == "DirectWave Player":
                        assert state["native_hint"] == "Load a program in DirectWave to play it."
                    elif entry["name"] == "Drumaxx":
                        assert state["native_hint"] == "Assign pad keys in Drumaxx to match your MIDI notes."
                    elif entry["name"] == "Slicex":
                        assert state["native_hint"] == "Load audio in Slicex and play its mapped slice notes. Piano-roll export is unavailable."
                    midi_key = 0 if entry["name"] == "Drumaxx" else 69
                    ph.send_midi(900001, [[0x90, midi_key, 100]])
                    time.sleep(.4)
                    reader = analysis_tap.TapReader(session.tap_path)
                    try:
                        frames = reader.read(None, 0, 8192, newest=True)
                        peak = float(np.max(np.abs(frames[6])))
                        if entry["name"] in ("Morphine", "SoundFont Player", "DirectWave Player", "FPC", "Slicex",
                                             "Wave Traveller", "Fruity Slicer", "Fruity Slicer 2", "Fruity granulizer",
                                             "FL Studio Mobile", "Mobile Rack"):
                            assert peak <= 1e-6, ("unloaded sampler unexpectedly rendered", entry["name"], peak)
                        else:
                            assert peak > 0, (entry["name"], peak)
                    finally:
                        reader.close()
                    ph.send_midi(900001, [[0x80, midi_key, 0], [0xb0, 120, 0]])
                picture = ph.capture(900001, path, entry["name"])
                with Image.open(picture) as image:
                    assert image.width >= 32 and image.height >= 32, (
                        image.size, state.get("editor_width"), state.get("editor_height"))
                    assert any(lo != hi for lo, hi in image.getextrema())
            finally:
                ph.close(900001)
                proc.wait(timeout=15)
                deadline = time.monotonic() + 10
                while workspace.exists() and time.monotonic() < deadline:
                    time.sleep(.05)
                assert not workspace.exists(), workspace
                assert proc.returncode == 0, ph._stderr_tails.get(proc.pid)
                ph.stop_standby()
            print("PASS", entry["name"], "container handoff, controls, capture and cleanup")
    return 0


if __name__ == "__main__":
    sys.exit(main())
