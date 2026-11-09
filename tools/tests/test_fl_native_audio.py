"""Opt-in installed-binary checks; no audio device or real FL settings are used.

Run with --installed-root pointing to FL's Plugins/Fruity folder. Each effect gets
its own process, disposable profile/HKCU and hidden editor. Vendor files are read only.
"""
import argparse
import ctypes as C
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import uuid

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))


def child(binary, root):
    import numpy as np
    from app import analysis_tap, fl_native, host_worker
    from app.fl_native_worker import _isolate, _no_children
    from app.hosting import win_editor
    from app.hosting.vst2 import AEffect, Vst2Plugin

    host_worker._prepare()
    root = Path(root)
    root.mkdir(parents=True)
    fl_native.prepare_resources(root, Path(binary).parents[2], fl_native.resolve(binary, refresh=True)[2])
    _job = _no_children()  # keep the single-process job for the child lifetime
    api, key = _isolate(root)
    os.chdir(root)
    os.environ.update(VEXT_FL_NATIVE_DLL=binary, VEXT_FL_NATIVE_ROOT=str(Path(binary).parents[2]),
                      VEXT_FL_NATIVE_DATA=str(root), VEXT_FL_NATIVE_AUDIO="1")
    C.windll.kernel32.SetErrorMode(0x8003)
    ole = C.OleDLL("ole32")
    ole.OleUninitialize.restype = None  # void: not an HRESULT
    ole.OleInitialize(None)
    main = win_editor.MainThread()
    window = win_editor.EditorWindow("VEXT audio regression", 1200, 1000, False)
    os.environ["VEXT_FL_NATIVE_PARENT_HWND"] = str(window.hwnd)
    plugin = Vst2Plugin(str(fl_native.adapter_path()), main, 48000, 1024)
    plugin._dispatch(14, ptr=C.c_void_p(window.hwnd))
    get_editor = plugin._dll.VEXTNativeEditorHandle
    get_editor.argtypes, get_editor.restype = [C.c_void_p], C.c_void_p
    hwnd = get_editor(plugin._effp)
    assert hwnd and win_editor.user32.IsWindow(hwnd)
    if Path(binary).parent.name == "Vocodex":
        # This exact allowlisted editor's L-R switch is not an automatable
        # parameter. Exercise the verified control without system mouse input.
        from ctypes import wintypes as W
        user = C.WinDLL("user32")
        user.ChildWindowFromPointEx.argtypes = [W.HWND, W.POINT, W.UINT]
        user.ChildWindowFromPointEx.restype = W.HWND
        user.MapWindowPoints.argtypes = [W.HWND, W.HWND, C.POINTER(W.POINT), W.UINT]
        user.GetClientRect.argtypes = [W.HWND, C.POINTER(W.RECT)]
        user.SendMessageW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
        rect = W.RECT()
        assert user.GetClientRect(hwnd, C.byref(rect))
        point = W.POINT(round(226 * rect.right / 593), round(16 * rect.bottom / 603))
        child = user.ChildWindowFromPointEx(hwnd, point, 0)
        assert child
        user.MapWindowPoints(hwnd, child, C.byref(point), 1)
        packed = (point.x & 65535) | ((point.y & 65535) << 16)
        main.call(lambda: user.SendMessageW(child, 0x201, 1, packed))
        main.call(lambda: user.SendMessageW(child, 0x202, 0, packed))
    if Path(binary).parent.name == "Fruity Scratcher":
        from fl_native_fixtures import click as editor_click, pick_file, reject_file, write_rhythm_wav
        write_rhythm_wav(Path(root) / "deck.wav", seconds=30)
        reject_file(hwnd, 22, 215, root)
        pick_file(hwnd, 22, 215, Path(root) / "deck.wav")
    if Path(binary).parent.name in ("Fruity Convolver", "Transmitter", "Transporter", "Effector"):
        class NativeHost(C.Structure):
            _fields_ = [("vt", C.c_void_p), ("version", C.c_int32), ("flags", C.c_int32),
                        ("app", C.c_void_p), ("waves", C.c_void_p * 10),
                        ("buffers", C.c_void_p * 4), ("reserved", C.c_int32 * 30)]
        class NativeContext(C.Structure):
            _fields_ = [("effect", AEffect), ("host", NativeHost), ("paths", C.c_void_p),
                        ("hostvt", C.c_void_p * 64), ("pathvt", C.c_void_p * 64),
                        ("lib", C.c_void_p), ("native", C.c_void_p)]
        native_pointer = NativeContext.from_address(plugin._eff.object).native
        table = C.cast(native_pointer, C.POINTER(C.POINTER(C.c_void_p))).contents
        process_param = C.CFUNCTYPE(C.c_int32, C.c_void_p, C.c_int32, C.c_int32, C.c_int32)(table[6])
    if Path(binary).parent.name == "Fruity Convolver":
        import wave
        convolver_native = native_pointer
        loader = C.CFUNCTYPE(C.c_ssize_t, C.c_void_p, C.c_ssize_t, C.c_ssize_t, C.c_ssize_t)(table[1])
        impulse = np.zeros((4410, 2), dtype="<i2")
        impulse[0] = 16384
        impulse[2205] = 8192
        impulse_file = root / "impulse.wav"
        with wave.open(str(impulse_file), "wb") as stream:
            stream.setnchannels(2)
            stream.setsampwidth(2)
            stream.setframerate(44100)
            stream.writeframes(impulse.tobytes())
        argument = C.c_char_p(str(impulse_file).encode("mbcs"))
        main.call(lambda: loader(convolver_native, 18, 0, C.cast(argument, C.c_void_p).value))
        main.call(lambda: process_param(convolver_native, 0, 0, 17))
        main.call(lambda: process_param(convolver_native, 1, 1000, 17))
    if Path(binary).parent.name == "Transmitter":
        # Both native level faders start muted. Leave live instances unchanged.
        main.call(lambda: process_param(native_pointer, 3, 10000, 17))
        main.call(lambda: process_param(native_pointer, 4, 10000, 17))
    if Path(binary).parent.name == "Transporter":
        # Hear the captured loop rather than the independently mixed dry signal.
        for index, value in ((0, 0), (1, 12800), (2, 0), (7, 250)):
            main.call(lambda i=index, v=value: process_param(native_pointer, i, v, 17))
    if Path(binary).parent.name == "Effector":
        # Exact native values observed for FILTER, bypass off and the edited pad.
        for index, value in ((0, 93), (1, 0), (2, 256), (3, 90), (4, 162)):
            main.call(lambda i=index, v=value: process_param(native_pointer, i, v, 17))
    main.add_timer(1, 20, lambda: plugin._dispatch(19))
    player = host_worker.Player(plugin, None)
    done, errors = threading.Event(), []

    def render():
        try:
            for rate in (44100, 48000, 96000):
                n = rate // 2 + 17  # multiple full blocks plus a partial block
                if Path(binary).parent.name == "Transporter":
                    n = rate * 2 + 17
                source = np.array([.1 * np.sin(np.arange(n) * 2 * np.pi * 997 / rate),
                                   .07 * np.cos(np.arange(n) * 2 * np.pi * 211 / rate)], dtype=np.float32)
                if Path(binary).parent.name == "Vocodex":
                    t = np.arange(n) / rate
                    source = np.asarray([
                        .12 * np.sin(2 * np.pi * 220 * t) * (.2 + .8 * np.exp(-((t * 4) % 1) * 8)),
                        .04 * sum(np.sin(2 * np.pi * 110 * k * t) / k for k in range(1, 25)),
                    ], dtype=np.float32)
                plugin.reset()
                wet = player._through(source, rate, (), False)
                if Path(binary).parent.name == "Fruity Scratcher":
                    # With silent input every sample comes from the loaded deck.
                    silence = np.zeros_like(source)
                    main.call(lambda: editor_click(hwnd, 175, 183))
                    player._through(silence, rate, (), False)
                    deck = player._through(silence, rate, (), False)
                    assert float(np.abs(deck).max()) > 1e-3, ("deck silent", rate)
                    spectrum = np.abs(np.fft.rfft(deck[0] * np.hanning(deck.shape[1])))
                    pitch = int(np.argmax(spectrum)) * rate / deck.shape[1]
                    # Its loader asks for 44.1 kHz and steps one frame per output sample.
                    assert abs(pitch - 220 * rate / 44100) < 16, ("deck pitch", rate, pitch)
                    main.call(lambda: editor_click(hwnd, 125, 183))
                    # The platter brakes: allow its stop ramp before requiring silence.
                    for _ in range(8):
                        paused = player._through(silence, rate, (), False)
                    assert float(np.abs(paused).max()) < 1e-5, ("paused deck not silent", rate, float(np.abs(paused).max()))
                if Path(binary).parent.name == "Effector":
                    assert not np.allclose(wet, source, atol=1e-5), "Effector only passed dry audio"
                    main.call(lambda: process_param(native_pointer, 1, 256, 17))
                    native_bypass = player._through(source, rate, (), False)
                    np.testing.assert_allclose(native_bypass[:, -1024:], source[:, -1024:], atol=1e-6)
                    main.call(lambda: process_param(native_pointer, 1, 0, 17))
                if Path(binary).parent.name in ("Transmitter", "Transporter"):
                    assert not np.allclose(wet, source, atol=1e-5), "native effect only passed dry audio"
                    indices = (3, 4) if Path(binary).parent.name == "Transmitter" else (1, 2)
                    for index in indices:
                        main.call(lambda i=index: process_param(native_pointer, i, 0, 17))
                    player._through(np.zeros((2, rate * 2), np.float32), rate, (), False)
                    muted = player._through(source, rate, (), False)
                    floor = float(np.abs(muted).max())
                    # Transporter retains a repeatable low-level floor with all
                    # three levels at -INF; require both an absolute bound and
                    # 60 dB attenuation relative to the captured-loop signal.
                    bound = 1e-5 if Path(binary).parent.name == "Transporter" else 1e-6
                    assert floor < bound, (
                        "native level mute failed", Path(binary).parent.name, rate,
                        floor, float(np.abs(wet).max()))
                    if Path(binary).parent.name == "Transporter":
                        assert floor < float(np.abs(wet).max()) * .001
                    for index in indices:
                        value = 10000 if Path(binary).parent.name == "Transmitter" else 12800 if index == 1 else 0
                        main.call(lambda i=index, v=value: process_param(native_pointer, i, v, 17))
                    if Path(binary).parent.name == "Transporter":
                        for mode in (1, 2, 3):
                            main.call(lambda m=mode: process_param(native_pointer, 13, m, 17))
                            plugin.reset()
                            looped = player._through(source, rate, (), False)
                            assert np.isfinite(looped).all() and 1e-6 < float(np.abs(looped).max()) < 8
                        main.call(lambda: process_param(native_pointer, 13, 0, 17))
                if Path(binary).parent.name == "Vocodex":
                    assert float(np.abs(wet).max()) > 1e-4
                    assert not np.allclose(wet, source, atol=1e-5), "Vocodex only passed dry audio"
                    for channel in (0, 1):
                        plugin.reset()
                        player._through(np.zeros((2, rate * 2), np.float32), rate, (), False)
                        missing = source.copy()
                        missing[channel] = 0
                        quiet = player._through(missing, rate, (), False)
                        assert float(np.abs(quiet).max()) < 1e-5, ("Vocodex ignored a missing input", channel)
                if Path(binary).parent.name == "Fruity Convolver":
                    assert not np.allclose(wet, source, atol=1e-5), "Convolver only passed dry audio"
                    main.call(lambda: process_param(convolver_native, 1, 0, 17))
                    player._through(source, rate, (), False)
                    muted = player._through(source, rate, (), False)
                    assert float(np.abs(muted).max()) < 1e-6, "Convolver wet mute failed"
                    main.call(lambda: process_param(convolver_native, 1, 1000, 17))
                assert wet.shape == source.shape and np.isfinite(wet).all()
                assert 1e-6 < float(np.abs(wet).max()) < 8
                dry = player._through(source, rate, (), True)
                np.testing.assert_allclose(dry, source, rtol=0, atol=1e-7)
                player.set_tap(str(root / "analysis.ring"))
                tap = player._tap_start(rate, analysis_tap.FILE)
                tap.write(source, wet)
                with_reader = analysis_tap.TapReader(str(root / "analysis.ring"))
                try:
                    read = with_reader.read(None, 0, n)
                    assert read is not None
                    np.testing.assert_allclose(read[6], wet.T)
                    np.testing.assert_allclose(read[5], source.T)
                finally:
                    with_reader.close()
                player.record_on(str(root / "recording.wav"), n / rate)
                player.recorder.ensure(rate)
                player.recorder.write(0, wet)
                player.record_off()
                recorded = np.fromfile(root / "recording.wav", dtype="<f4", offset=host_worker.WAV_FLOAT_HEADER)
                np.testing.assert_allclose(recorded.reshape(-1, 2)[:n], wet.T)
            # A real native parameter change must reach the DSP (Balance's volume).
            if Path(binary).name.casefold() == "fruity balance_x64.dll":
                class Host(C.Structure):
                    _fields_ = [("vt", C.c_void_p), ("version", C.c_int32), ("flags", C.c_int32),
                                ("app", C.c_void_p), ("waves", C.c_void_p * 10),
                                ("buffers", C.c_void_p * 4), ("reserved", C.c_int32 * 30)]

                class Prefix(C.Structure):
                    _fields_ = [("effect", AEffect), ("host", Host), ("paths", C.c_void_p),
                                ("hostvt", C.c_void_p * 64), ("pathvt", C.c_void_p * 64),
                                ("library", C.c_void_p), ("native", C.c_void_p)]

                native = Prefix.from_address(plugin._eff.object).native
                vt = C.cast(native, C.POINTER(C.POINTER(C.c_void_p))).contents
                param = C.CFUNCTYPE(C.c_int32, C.c_void_p, C.c_int32, C.c_int32, C.c_int32)(vt[6])
                source = np.full((2, 8193), .125, dtype=np.float32)
                for value in (0, 128, 256):
                    main.call(lambda: param(native, 1, value, 1 | 16))
                    plugin.reset()
                    sound = player._process(source, 48000)
                    tail = sound[:, -1024:]
                    if value == 128:  # native volume has a nonlinear taper
                        assert 0 < float(tail.min()) <= float(tail.max()) < .125
                        np.testing.assert_allclose(tail[0], tail[1], atol=1e-7)
                    else:
                        np.testing.assert_allclose(tail, source[:, -1024:] * (value / 256), atol=1e-6)
                source[1] = 0
                sound = player._process(source, 48000)
                np.testing.assert_allclose(sound[1], 0, atol=1e-7)
                source[1] = source[0]
                for pan in (-64, 64):
                    main.call(lambda: param(native, 0, pan, 1 | 16))
                    plugin.reset()
                    sound = player._process(source, 48000)[:, -1024:]
                    left, right = sound.mean(axis=1)
                    assert (left > right if pan < 0 else right > left), (pan, left, right)
                main.call(lambda: param(native, 0, 0, 1 | 16))
        except BaseException as exc:
            errors.append(exc)
        finally:
            done.set()

    threading.Thread(target=render, daemon=True).start()
    main.pump(lambda: not done.is_set())
    player.close_tap()
    player.midi.shutdown()
    plugin._dispatch(15)
    plugin.close()
    window.destroy()
    ole.OleUninitialize()
    api.RegOverridePredefKey(C.c_void_p(-2147483647), None)
    key.Close()
    if errors:
        raise errors[0]
    print("PASS", flush=True)


def main():
    if len(sys.argv) == 4 and sys.argv[1] == "--child":
        child(sys.argv[2], sys.argv[3])
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installed-root", type=Path)
    parser.add_argument("--plugin", action="append", help="Exact relative binary path; default: audio-enabled effects")
    args = parser.parse_args()
    if args.installed_root is None:
        print("SKIP: supply --installed-root explicitly to test installed native effects")
        return
    with tempfile.TemporaryDirectory(prefix="vext-native-audio-test-") as temp:
        for name in ("VEXT_ROOT", "USERPROFILE", "APPDATA", "LOCALAPPDATA"):
            os.environ[name] = str(Path(temp) / name)
        os.environ["VEXT_VIRTUAL_MIDI"] = "0"
        from app import fl_native

        entries = list(fl_native.compatibility().values())
        if args.plugin:
            wanted = {p.casefold() for p in args.plugin}
            selected = {e["relative"].casefold() for e in entries} & wanted
            assert selected == wanted, f"Unknown native binary paths: {wanted - selected}"
            entries = [e for e in entries if e["relative"].casefold() in wanted]
        else:
            entries = [e for e in entries if e.get("audio") and not e["instrument"] and not e.get("audition")]
        assert entries, "No matching effects selected"
        failures = []
        for entry in entries:
            binary = args.installed_root / entry["relative"]
            fl_native.resolve(str(binary), refresh=True)
            root = fl_native.WORKSPACES / uuid.uuid4().hex
            try:
                result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--child", str(binary), str(root)],
                                        capture_output=True, timeout=35)
                passed = result.returncode == 0 and b"PASS" in result.stderr
                print("PASS" if passed else "FAIL", entry["name"], flush=True)
                if not passed:
                    failures.append(entry["name"])
                    print(result.stderr.decode("utf-8", "replace")[-3500:])
            except subprocess.TimeoutExpired:
                failures.append(entry["name"])
                print("TIMEOUT", entry["name"], flush=True)
            finally:
                fl_native.cleanup(str(root))
        assert not failures, failures


if __name__ == "__main__":
    main()
