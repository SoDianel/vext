"""Opt-in installed-binary checks; no audio device or real FL settings are used.

Run with --installed-root pointing to FL's Plugins/Fruity folder. Each instrument gets
its own process, disposable profile/HKCU and hidden editor. Vendor files are read only.
"""
import argparse
import json
import ctypes as C
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import uuid

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))


def check_mixing(host_address):
    import numpy as np

    table = C.cast(host_address, C.POINTER(C.POINTER(C.c_void_p))).contents
    pointer = C.POINTER(C.c_float)
    for slot, pan_type, edge in ((3, C.c_int32, 64), (31, C.c_float, 1)):
        levels = C.CFUNCTYPE(None, C.c_void_p, pointer, pointer, pan_type, C.c_float)(table[slot])
        for pan, expected in ((0, (.8, .8)), (-edge, (.8, 0)), (edge, (0, .8)),
                              (edge // 2 if slot == 3 else .5, (.4, .8))):
            left, right = C.c_float(), C.c_float()
            levels(host_address, C.byref(left), C.byref(right), pan_type(pan), .8)
            np.testing.assert_allclose((left.value, right.value), expected, atol=1e-7)
    for slot, channels in ((11, 1), (12, 2)):
        mix = C.CFUNCTYPE(None, C.c_void_p, C.c_void_p, C.c_void_p, C.c_int32,
                         C.c_float, C.c_float, pointer, pointer)(table[slot])
        source = np.ones(4 * channels, dtype=np.float32)
        left, right = C.c_float(), C.c_float()
        # DrumSynth calls this for an unavailable extra output before its main bus.
        mix(host_address, source.ctypes.data, None, 4, .25, .75, C.byref(left), C.byref(right))
        np.testing.assert_allclose((left.value, right.value), (.25, .75))
        output = np.zeros((4, 2), dtype=np.float32)
        mix(host_address, source.ctypes.data, output.ctypes.data, 4, .25, .75,
            C.byref(left), C.byref(right))
        np.testing.assert_allclose(output, np.tile((.25, .75), (4, 1)))
        left.value, right.value = 0, 0
        output.fill(0)
        mix(host_address, source.ctypes.data, output.ctypes.data, 4, .25, .75,
            C.byref(left), C.byref(right))
        np.testing.assert_allclose(output, np.arange(1, 5)[:, None] / 4 * (.25, .75))


# Each sampler's load button, in editor-client coordinates of the hash-pinned binary.
SAMPLE_BUTTONS = {"Wave Traveller": (17, 10), "Fruity Slicer": (31, 37), "Fruity Slicer 2": (334, 29),
                  "Fruity Granulizer": (192, 369)}


# Their racks and projects start empty and only accept real pointer input to add a
# module, so this run covers silence, stability and unload; sound is a GUI check.
EMPTY_AT_START = ("FL Studio Mobile", "FL Studio Mobile Rack")


def child(binary, root, block_size=1024):
    import numpy as np
    from fl_native_fixtures import pick_file, reject_file, write_rhythm_wav
    from app import fl_native, host_worker
    from app.fl_native_worker import _isolate, _no_children, _wait_ready
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
                      VEXT_FL_NATIVE_DATA=str(root), VEXT_FL_NATIVE_AUDIO="1", VEXT_FL_NATIVE_MIDI="1")
    C.windll.kernel32.SetErrorMode(0x8003)
    ole = C.OleDLL("ole32")
    ole.OleUninitialize.restype = None  # void: not an HRESULT
    ole.OleInitialize(None)
    main = win_editor.MainThread()
    window = win_editor.EditorWindow("VEXT audio regression", 1200, 1000, False)
    os.environ["VEXT_FL_NATIVE_PARENT_HWND"] = str(window.hwnd)
    plugin = Vst2Plugin(str(fl_native.adapter_path()), main, 48000, block_size)
    plugin._dispatch(14, ptr=C.c_void_p(window.hwnd))
    _wait_ready(plugin)

    class NativeHost(C.Structure):
        _fields_ = [("vt", C.POINTER(C.c_void_p)), ("version", C.c_int32),
                    ("flags", C.c_int32), ("app", C.c_void_p),
                    ("waves", C.c_void_p * 10), ("buffers", C.c_void_p * 4),
                    ("reserved", C.c_int32 * 30)]

    class NativeContext(C.Structure):
        _fields_ = [("effect", AEffect), ("host", NativeHost), ("paths", C.c_void_p),
                    ("hostvt", C.c_void_p * 64), ("pathvt", C.c_void_p * 64),
                    ("lib", C.c_void_p), ("native", C.c_void_p)]

    context = NativeContext.from_address(plugin._eff.object)
    native = context.native
    native_vt = C.cast(native, C.POINTER(C.POINTER(C.c_void_p))).contents
    if Path(binary).parent.name == "Morphine":
        native_dispatch = C.CFUNCTYPE(C.c_ssize_t, C.c_void_p, C.c_ssize_t,
                                      C.c_ssize_t, C.c_ssize_t)(native_vt[1])
        preset = (Path(binary).parents[4] / "Data" / "Patches" / "Plugin presets" /
                  "Generators" / "Morphine" / "Bass" / "BAS Acoustic 1 FG.mrp")
        assert preset.is_file(), "Morphine factory test preset missing"
        preset_arg = C.c_char_p(str(preset).encode("mbcs"))
        result = main.call(lambda: native_dispatch(
            native, 18, 0, C.cast(preset_arg, C.c_void_p).value))
        assert result == 1, ("Morphine factory preset load failed", result)
    elif Path(binary).parent.name == "Plucked!":
        process_param = C.CFUNCTYPE(C.c_int32, C.c_void_p, C.c_int32,
                                    C.c_int32, C.c_int32)(native_vt[6])
        main.call(lambda: process_param(native, 3, 1, 17))
    elif Path(binary).parent.name == "Soundfont Player":
        native_dispatch = C.CFUNCTYPE(C.c_ssize_t, C.c_void_p, C.c_ssize_t,
                                      C.c_ssize_t, C.c_ssize_t)(native_vt[1])
        preset = Path(binary).parents[4] / "Data/Patches/Soundfonts/STR_Ensemble.sf2"
        assert preset.is_file(), "SoundFont factory test fixture missing"
        preset_arg = C.c_char_p(str(preset).encode("mbcs"))
        result = main.call(lambda: native_dispatch(
            native, 18, 0, C.cast(preset_arg, C.c_void_p).value))
        assert result == 1, ("SoundFont test fixture load failed", result)
    elif Path(binary).parent.name == "DirectWave":
        native_dispatch = C.CFUNCTYPE(C.c_ssize_t, C.c_void_p, C.c_ssize_t,
                                      C.c_ssize_t, C.c_ssize_t)(native_vt[1])
        preset = Path(binary).parents[4] / "Data/Patches/Packs/Legacy/Instruments/Piano/Piano 1.dwp"
        assert preset.is_file(), "DirectWave factory test fixture missing"
        preset_arg = C.c_char_p(str(preset).encode("mbcs"))
        main.call(lambda: native_dispatch(
            native, 18, 0, C.cast(preset_arg, C.c_void_p).value))
    elif Path(binary).parent.name == "FPC":
        import wave

        rate = 44100
        t = np.arange(rate * 2) / rate
        samples = .3 * np.sin(2 * np.pi * 220 * t) * np.minimum(1, (2 - t) * 20)
        pcm = np.asarray(np.column_stack((samples, samples)) * 32767, dtype="<i2")
        fixture_path = root / "pad.wav"
        with wave.open(str(fixture_path), "wb") as stream:
            stream.setnchannels(2)
            stream.setsampwidth(2)
            stream.setframerate(rate)
            stream.writeframes(pcm.tobytes())
        native_dispatch = C.CFUNCTYPE(C.c_ssize_t, C.c_void_p, C.c_ssize_t,
                                      C.c_ssize_t, C.c_ssize_t)(native_vt[1])
        fixture_arg = C.c_char_p(str(fixture_path).encode("mbcs"))
        # Opcode 18 loads into the editor's selected pad and layer (REA: dispatcher
        # RVA 0x46a970 reads editor+0x9f8/+0x9fc). The GUI's pad click sets the same
        # fields; a test fixture on this hash-pinned binary selects each of 32 pads.
        editor_object = C.c_void_p.from_address(native + 0xc4).value
        selected_pad = C.c_int32.from_address(editor_object + 0x9f8)
        for pad in range(32):
            selected_pad.value = pad
            main.call(lambda: native_dispatch(native, 18, 0, C.cast(fixture_arg, C.c_void_p).value))
    elif Path(binary).parent.name == "Patcher":
        native_dispatch = C.CFUNCTYPE(C.c_ssize_t, C.c_void_p, C.c_ssize_t,
                                      C.c_ssize_t, C.c_ssize_t)(native_vt[1])
        preset = next((Path(binary).parents[4] / "Data/Patches/Plugin database/Generators").rglob("3x Osc.fst"))
        preset_arg = C.c_char_p(str(preset).encode("mbcs"))
        print("patcher load", main.call(lambda: native_dispatch(native, 18, 0, C.cast(preset_arg, C.c_void_p).value)), file=sys.stderr, flush=True)
    elif Path(binary).parent.name == "Drumaxx":
        process_param = C.CFUNCTYPE(C.c_int32, C.c_void_p, C.c_int32,
                                    C.c_int32, C.c_int32)(native_vt[6])
        for index, midi_key in ((4, 69), (48, 60), (92, 64)):
            main.call(lambda index=index, midi_key=midi_key: process_param(native, index, midi_key, 17))
            assert main.call(lambda index=index: process_param(native, index, 0, 2)) == midi_key
    elif Path(binary).parent.name == "Slicex":
        import wave

        rate = 44100
        t = np.arange(rate * 8) / rate
        samples = .15 * np.sin(2 * np.pi * 220 * t) * np.exp(-((t * 2) % 1) * 8)
        pcm = np.asarray(np.column_stack((samples, samples)) * 32767, dtype="<i2")
        fixture_path = root / "rhythm.wav"
        with wave.open(str(fixture_path), "wb") as stream:
            stream.setnchannels(2)
            stream.setsampwidth(2)
            stream.setframerate(rate)
            stream.writeframes(pcm.tobytes())
        loader = C.CFUNCTYPE(C.c_ssize_t, C.c_void_p, C.c_ssize_t,
                            C.c_ssize_t, C.c_ssize_t)(native_vt[1])
        file_arg = C.c_char_p(str(fixture_path).encode("mbcs"))
        # This binary's opcode 18 loads deck A, then auto-slices. Piano-roll
        # export is unavailable in the standalone host; no notes are replayed.
        main.call(lambda: loader(native, 18, 0, C.cast(file_arg, C.c_void_p).value))
    get_editor = plugin._dll.VEXTNativeEditorHandle
    get_editor.argtypes, get_editor.restype = [C.c_void_p], C.c_void_p
    hwnd = get_editor(plugin._effp)
    assert hwnd and win_editor.user32.IsWindow(hwnd)
    if Path(binary).parent.name in SAMPLE_BUTTONS:
        # These load only through their own picker, served by the host's WAV loader.
        write_rhythm_wav(root / "rhythm.wav")
        reject_file(hwnd, *SAMPLE_BUTTONS[Path(binary).parent.name], root)
        pick_file(hwnd, *SAMPLE_BUTTONS[Path(binary).parent.name], root / "rhythm.wav")
    main.add_timer(1, 20, lambda: plugin._dispatch(19))
    player = host_worker.Player(plugin, None)
    check_mixing(plugin._eff.object + 192)
    host_address = plugin._eff.object + 192
    host_vt = C.cast(host_address, C.POINTER(C.POINTER(C.c_void_p))).contents
    dispatch_host = C.CFUNCTYPE(C.c_ssize_t, C.c_void_p, C.c_ssize_t,
                               C.c_ssize_t, C.c_ssize_t, C.c_ssize_t)(host_vt[0])
    suspend = C.CFUNCTYPE(None, C.c_void_p, C.c_ssize_t)(host_vt[43])
    resume = C.CFUNCTYPE(None, C.c_void_p, C.c_ssize_t)(host_vt[44])
    clock = C.CFUNCTYPE(C.c_double, C.c_void_p)(host_vt[17])
    selected = root / "selected.wav"
    selected.write_bytes(b"selected local file")
    for filename, exists in ((str(selected), True), (str(root), False),
                             (str(root / "missing.wav"), False), ("relative.wav", False),
                             (r"\\server\share\missing.wav", False), (str(selected) + ":stream", False)):
        argument = C.c_char_p(filename.encode("mbcs"))
        result = dispatch_host(host_address, 0, 14, 0, C.cast(argument, C.c_void_p).value)
        if Path(binary).parent.name == "DirectWave" and not exists:
            assert result and C.string_at(result) == b"", ("unavailable DirectWave path", filename)
        else:
            assert bool(result) == exists, ("local file resolution", filename)
        if exists:
            assert C.string_at(result).decode("mbcs") == filename
    assert selected.read_bytes() == b"selected local file"
    assert dispatch_host(host_address, 0, 17, 0, 0) == 0
    done, errors = threading.Event(), []

    def render():
        try:
            for rate in (44100, 48000, 96000):
                plugin.reset()
                silence = np.zeros((2, rate // 2), dtype=np.float32)
                def play(events=()):
                    result = plugin(silence, rate, midi=events)
                    assert np.isfinite(result).all() and float(np.abs(result).max()) < 8
                    return result
                def decay():
                    # Native presets can retain long release/reverb tails. Require
                    # silence within 30 rendered seconds, without truncating them.
                    for _ in range(60):
                        result = play()
                        if float(np.abs(result).max()) < 1e-4:
                            break
                    return result
                np.testing.assert_allclose(play(), 0, atol=1e-6)
                if Path(binary).parent.name in EMPTY_AT_START:
                    np.testing.assert_allclose(play([(7, bytes((0x90, 69, 100)))]), 0, atol=1e-6)
                    np.testing.assert_allclose(play([(0, bytes((0xb0, 120, 0)))]), 0, atol=1e-6)
                    plugin.reset()
                    continue
                if Path(binary).parent.name in (*SAMPLE_BUTTONS, "FPC"):
                    # Host PCM is always 44.1 kHz, so the 220 Hz source must keep its
                    # pitch at every engine rate. Wave Traveller stretches its eight
                    # seconds over one bar (2.4 s at the default 100 BPM).
                    expected = 220 * 8 / 2.4 if Path(binary).parent.name == "Wave Traveller" else 220
                    tone = play([(0, bytes((0x90, 60, 100)))])
                    peak = int(np.argmax(np.abs(np.fft.rfft(tone[0] * np.hanning(tone.shape[1])))))
                    assert abs(peak * rate / tone.shape[1] - expected) < 12, ("sample pitch", rate, peak * rate / tone.shape[1])
                    play([(0, bytes((0xb0, 120, 0)))])
                    assert float(np.abs(decay()).max()) < 1e-4
                if Path(binary).parent.name == "Soundfont Player":
                    # Reload while a voice is active: the old engine must not
                    # leave host voice handles pointing into replaced data.
                    play([(0, bytes((0x90, 69, 100)))])
                    result = main.call(lambda: native_dispatch(
                        native, 18, 0, C.cast(preset_arg, C.c_void_p).value))
                    assert result == 1, "active SoundFont reload failed"
                    plugin.reset()
                    np.testing.assert_allclose(play(), 0, atol=1e-5)
                # A loader can pump GUI messages while output is suspended.
                # Neither MIDI source may call the engine or survive the load.
                main.call(lambda: plugin.set_transport(bpm=100, playing=False, position=2))
                main.call(lambda: suspend(host_address, 0))
                main.call(lambda: dispatch_host(host_address, 0, 20, 69, 100))
                np.testing.assert_allclose(play([(7, bytes((0x90, 69, 100)))]), 0, atol=1e-7)
                assert abs(clock(host_address) - 320) < .01
                main.call(lambda: plugin.set_transport(playing=True))
                np.testing.assert_allclose(play(), 0, atol=1e-7)
                assert abs(clock(host_address) - 400) < .02
                main.call(lambda: resume(host_address, 0))
                np.testing.assert_allclose(play(), 0, atol=1e-6)
                # A delayed note must leave the first samples silent.
                sound = play([(100, bytes((0x90, 69, 100)))])
                np.testing.assert_allclose(sound[:, :100], 0, atol=1e-7)
                assert float(np.abs(sound[:, 100:]).max()) > 1e-5, "silent note-on"
                sustained = play([(0, bytes((0xb0, 64, 127))), (10, bytes((0x80, 69, 0)))])
                if Path(binary).parent.name == "3x Osc":
                    assert float(np.abs(sustained[:, -1024:]).max()) > 1e-5, "sustain lost the voice"
                if Path(binary).parent.name in ("3x Osc", "FL Keys", "Harmless", "GMS", "SimSynth"):
                    def frequency(signal):
                        spectrum = np.abs(np.fft.rfft(signal[0] * np.hanning(signal.shape[1])))
                        return int(np.argmax(spectrum)) * rate / signal.shape[1]
                    base = frequency(play())
                    assert abs(base - 440) <= (5 if Path(binary).parent.name == "FL Keys" else 2), ("MIDI A4", rate, base)
                    play([(0, bytes((0xe0, 0, 127)))])
                    bent = frequency(play())
                    assert 1.10 < bent / base < 1.14, ("pitch bend", base, bent)
                play([(0, bytes((0xe0, 0, 80)))])
                play([(0, bytes((0xb0, 64, 0)))])
                tail = decay()
                assert float(np.abs(tail).max()) < 1e-4, "note did not release"
                # One mapped drum pad can make a two-note block look valid.
                # Verify both pitches independently before the combined trial.
                for midi_key in (60, 64):
                    sound = play([(0, bytes((0x91, midi_key, 100)))])
                    assert float(np.abs(sound).max()) > 1e-5, ("unmapped polyphony pitch", midi_key)
                    play([(0, bytes((0xb1, 120, 0)))])
                    assert float(np.abs(decay()).max()) < 1e-4, "single-pitch panic did not silence"
                sound = play([(0, bytes((0x91, 60, 100))), (0, bytes((0x91, 64, 100)))])
                assert float(np.abs(sound).max()) > 1e-5, "silent polyphony"
                killed = play([(0, bytes((0xb1, 120, 0)))])
                # Killing a voice can leave a native reverb/delay tail. It must decay.
                killed = decay()
                assert float(np.abs(killed[:, -1024:]).max()) < 1e-4, "panic did not silence"
                # CC120 must stop voices even while sustain is holding them.
                play([(0, bytes((0xb1, 64, 127))),
                      (0, bytes((0x91, 60, 100))), (0, bytes((0x91, 64, 100)))])
                play([(0, bytes((0xb1, 120, 0)))])
                killed = decay()
                assert float(np.abs(killed[:, -1024:]).max()) < 1e-4, "panic did not release sustained voices"
                play([(0, bytes((0xb1, 64, 0)))])
                # Replacing the same pitch must release the old native voice before killing it.
                play([(0, bytes((0x91, 60, 100)))])
                play([(0, bytes((0x91, 60, 100)))])
                play([(0, bytes((0xb1, 120, 0)))])
                killed = decay()
                assert float(np.abs(killed[:, -1024:]).max()) < 1e-4, "panic did not silence retriggered voice"
                # Exercise the exact callbacks used by native editor keyboards.
                class Host(C.Structure):
                    _fields_ = [("vt", C.POINTER(C.c_void_p))]
                # AEffect's ctypes view ends before its full reserved tail. The
                # adapter's host is after the complete ABI-sized AEffect (192 bytes).
                host = Host.from_address(host_address)
                preview = C.CFUNCTYPE(C.c_ssize_t, C.c_void_p, C.c_ssize_t, C.c_ssize_t, C.c_ssize_t, C.c_ssize_t)(host.vt[0])
                main.call(lambda: preview(host_address, 0, 20, 69, 100))
                heard = play()
                assert float(np.abs(heard).max()) > 1e-5, "native keyboard preview silent"
                main.call(lambda: preview(host_address, 0, 21, 69, -1))
                released = decay()
                assert float(np.abs(released).max()) < 1e-4, "native keyboard release stuck"
                main.call(lambda: preview(host_address, 0, 56, 69, 100))
                assert float(np.abs(play()).max()) > 1e-5, "combined native preview silent"
                main.call(lambda: preview(host_address, 0, 56, 69, 0))
                released = decay()
                assert float(np.abs(released).max()) < 1e-4, "combined native preview stuck"
                play([(0, bytes((0x90, 69, 100)))])
                plugin.reset()
                np.testing.assert_allclose(play(), 0, atol=1e-5)
                main.call(lambda: plugin.set_transport(bpm=125, playing=True, position=2))
                assert abs(clock(host_address) - 400) < .01
                play()
                assert abs(clock(host_address) - 500) < .02
                main.call(lambda: plugin.set_transport(playing=False))
                play()
                assert abs(clock(host_address) - 500) < .02, "paused clock advanced"
                main.call(lambda: plugin.set_transport(playing=True))
                play()
                assert abs(clock(host_address) - 600) < .02
                main.call(lambda: plugin.set_transport(bpm=100))
                if Path(binary).parent.name in ("Kepler", "Kepler Exo", "Transistor Bass", "Soundfont Player", "DirectWave"):
                    # Reset with an active voice, then queue a new note in the
                    # first block. Internal cache draining must preserve it and
                    # the requested clock, including a paused nonzero seek.
                    for playing in (False, True):
                        play([(0, bytes((0x90, 69, 100)))])
                        plugin.reset()
                        main.call(lambda: plugin.set_transport(playing=playing, position=2))
                        before = clock(host_address)
                        sound = play([(7, bytes((0x90, 69, 100)))])
                        np.testing.assert_allclose(sound[:, :7], 0, atol=1e-7)
                        assert float(np.abs(sound[:, 7:]).max()) > 1e-5, "reset lost queued note"
                        expected = before + (80 if playing else 0)
                        assert abs(clock(host_address) - expected) < .02, "reset drain changed transport"
                        plugin.reset()
                        np.testing.assert_allclose(play(), 0, atol=1e-5)
                    main.call(lambda: plugin.set_transport(playing=False))
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
    if len(sys.argv) in (4, 5) and sys.argv[1] == "--child":
        child(sys.argv[2], sys.argv[3], int(sys.argv[4]) if len(sys.argv) == 5 else 1024)
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installed-root", type=Path)
    parser.add_argument("--plugin", action="append", help="Exact relative binary path; default: MIDI-enabled instruments")
    parser.add_argument("--block-size", type=int, choices=(64, 512, 1024), default=1024)
    args = parser.parse_args()
    if args.installed_root is None:
        print("SKIP: supply --installed-root explicitly to test installed native instruments")
        return
    with tempfile.TemporaryDirectory(prefix="vext-native-midi-test-") as temp:
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
            entries = [e for e in entries if e.get("midi")]
        assert entries, "No matching instruments selected"
        failures = []
        results = []
        for entry in entries:
            binary = args.installed_root / entry["relative"]
            fl_native.resolve(str(binary), refresh=True)
            root = fl_native.WORKSPACES / uuid.uuid4().hex
            try:
                result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--child", str(binary), str(root), str(args.block_size)],
                                        capture_output=True, timeout=40)
                passed = result.returncode == 0 and b"PASS" in result.stderr
                print("PASS" if passed else "FAIL", entry["name"], flush=True)
                results.append({"relative": entry["relative"], "passed": passed, "diagnostic": result.stderr.decode("utf-8", "replace")[-3500:] if not passed else ""})
                if not passed:
                    failures.append(entry["name"])
                    print("Worker exit:", result.returncode)
                    print(result.stderr.decode("utf-8", "replace")[-3500:])
            except subprocess.TimeoutExpired:
                results.append({"relative": entry["relative"], "passed": False, "diagnostic": "timeout"})
                failures.append(entry["name"])
                print("TIMEOUT", entry["name"], flush=True)
            finally:
                fl_native.cleanup(str(root))
        (REPO / "build" / "fl-native" / "midi-results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
        assert not failures, failures


if __name__ == "__main__":
    main()
