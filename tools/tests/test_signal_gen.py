"""Tests for the Playground's Signal renderer (app/signal_gen.py) and its endpoints (app/web.py).
No audio device, no plugin. The cache goes to a temporary folder (VEXT_ROOT, read by app/config.py).

Run:  .venv-dev\\Scripts\\python.exe -m pytest tools/tests/test_signal_gen.py -q
"""
import os
import sys
import tempfile
import time

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
WORK = tempfile.mkdtemp(prefix="vext-signal-test-")
os.environ["VEXT_ROOT"] = WORK  # set before app.config is imported

from app import host_worker
from app import signal_gen as sg

RATE = 48000
OFF = {"on": False}  # the oscillator switched off


def osc(**kw) -> dict:
    """The oscillator of a patch: a 1 kHz sine at 0 dB unless told otherwise."""
    base = {"wave": "sine", "freq": 1000, "level_db": 0}
    base.update(kw)
    return base


def render(patch: dict):
    return sg.render(sg.clean_patch(patch))


def plain(**kw) -> dict:
    """A patch with no normalize, no seam and level 0 dB: the samples are what the sources make."""
    base = {"seconds": 2, "seam_ms": 0, "normalize": False, "level_db": 0}
    base.update(kw)
    return base


# ---------- the patch ----------

def test_clean_patch_clamps_and_fills_defaults():
    c = sg.clean_patch({"seconds": 500, "rate": 12345, "level_db": 9, "seam_ms": -4, "channels": "nope",
                        "osc": {"freq": 99999, "unison": 20, "wave": "nope", "pan": -7}})
    assert c["seconds"] == 60 and c["rate"] == 48000 and c["level_db"] == 0 and c["seam_ms"] == 0
    assert c["channels"] == "stereo"
    o = c["osc"]
    assert o["freq"] == 24000 and o["unison"] == 8 and o["wave"] == "sine" and o["pan"] == -1
    d = sg.clean_patch({})
    assert d["seconds"] == 10 and d["normalize"] is True and d["noise"]["on"] is False
    assert d["osc"]["on"] is True and d["osc"]["freq"] == 1000  # missing: the default sine
    assert d["impulse"]["on"] is False and d["dc"] == 0
    assert sg.clean_patch({"osc": {"on": False}})["osc"]["on"] is False


def test_clean_patch_drops_unknown_keys_and_rejects_non_objects():
    c = sg.clean_patch({"bogus": 1, "seconds": 2})
    assert "bogus" not in c
    assert sg.patch_key(c) == sg.patch_key(sg.clean_patch({"seconds": 2}))
    for bad in ([1], "x", 3, None):
        with pytest.raises(ValueError):
            sg.clean_patch(bad)


def test_wavetable_wave_and_table_are_gone_so_wavetable_is_a_sine():
    o = sg.clean_patch({"osc": {"wave": "wavetable", "table": "basic", "position": 0.5}})["osc"]
    assert o["wave"] == "sine"
    assert "table" not in o and "position" not in o


def test_master_db_is_clamped_to_plus_minus_24_and_defaults_to_0():
    assert sg.clean_patch({})["master_db"] == 0
    assert sg.clean_patch({"master_db": 100})["master_db"] == 24
    assert sg.clean_patch({"master_db": -100})["master_db"] == -24
    assert sg.clean_patch({"master_db": 6.5})["master_db"] == 6.5


def test_clean_patch_drops_the_removed_features():
    c = sg.clean_patch({
        "oscillators": [{"freq": 440}], "gate": {"on": True}, "steps": {"on": True},
        "osc": {"freq": 440, "sweep": {"on": True, "to": 20000}, "vibrato": {"rate": 5, "depth": 20},
                "tremolo": {"rate": 3, "depth": 0.5}, "table": "basic", "position": 0.5},
    })
    assert "oscillators" not in c and "gate" not in c and "steps" not in c
    assert not {"sweep", "vibrato", "tremolo"} & set(c["osc"])
    assert c["osc"]["freq"] == 440
    assert not {"table", "position"} & set(c["osc"])


# ---------- the oscillator ----------

def test_sine_level_and_frequency():
    audio, info = render(plain(osc=osc(freq=1000), level_db=-18))
    assert audio.shape == (2, 2 * RATE) and audio.dtype == np.float32
    assert abs(info["peak_db"] - (-18.0)) <= 0.1
    x = audio[0].astype(np.float64)
    spec = np.abs(np.fft.rfft(x))
    peak_hz = np.argmax(spec) * RATE / len(x)
    assert abs(peak_hz - 1000) < 1
    assert info["summary"] == "Sine 1 kHz" and info["clipped"] is False


def test_saw_is_band_limited():
    audio, _ = render(plain(osc=osc(wave="saw", freq=5000)))
    x = audio[0].astype(np.float64)
    spec = np.abs(np.fft.rfft(x))
    freqs = np.fft.rfftfreq(len(x), 1 / RATE)
    fund = spec[np.argmin(np.abs(freqs - 5000))]
    assert np.argmax(spec[freqs < 6000]) == np.argmin(np.abs(freqs - 5000))
    assert spec[freqs >= 0.46 * RATE].max() < fund * 10 ** (-60 / 20)


def test_unison_spread_and_centre():
    base = plain(seconds=1, level_db=-12, osc=osc(freq=220, unison=7, unison_detune=15))
    wide, _ = render({**base, "osc": osc(freq=220, unison=7, unison_detune=15, unison_spread=0.8)})
    assert not np.allclose(wide[0], wide[1])
    centre, _ = render({**base, "osc": osc(freq=220, unison=7, unison_detune=15, unison_spread=0)})
    assert np.allclose(centre[0], centre[1], atol=1e-6)


def test_invert_and_one_side_and_mono_channels():
    src = osc(freq=440, pan=0.7)
    inv, _ = render(plain(seconds=1, channels="invert", osc=src))
    assert np.array_equal(inv[1], -inv[0]) and np.any(inv[0] != 0)
    left, _ = render(plain(seconds=1, channels="left", osc=src))
    assert np.all(left[1] == 0) and np.any(left[0] != 0)
    mono, _ = render(plain(seconds=1, channels="mono", osc=src))
    assert np.array_equal(mono[0], mono[1])


def test_every_waveform_has_mip_levels_and_the_top_one_is_a_sine():
    for wave in ("sine", "triangle", "saw", "square", "pulse"):
        o = sg.clean_patch({"osc": {"wave": wave, "width": 0.2}})["osc"]
        stack = sg._stack(o)
        assert stack.shape == (sg.LEVELS, sg.N), wave
        assert np.isfinite(stack).all(), wave
        # the top level keeps only the fundamental
        spec = np.abs(np.fft.rfft(stack[sg.LEVELS - 1].astype(np.float64)))
        assert spec[2:].max() < 1e-3 * spec[1], wave


# ---------- noise, impulses, seam, silence, names ----------

def test_pink_noise_slope_is_minus_3_db_per_octave():
    audio, _ = render(plain(seconds=10, osc=OFF, noise={"on": True, "color": "pink"}))
    x = audio[0].astype(np.float64)
    power = np.abs(np.fft.rfft(x)) ** 2
    freqs = np.fft.rfftfreq(len(x), 1 / RATE)
    centres = [100 * 2 ** k for k in range(7)]  # 100 Hz .. 6.4 kHz, octave bands
    levels = [10 * np.log10(power[(freqs >= c / 2 ** 0.5) & (freqs < c * 2 ** 0.5)].mean()) for c in centres]
    slope = np.polyfit(np.arange(len(centres)), levels, 1)[0]
    assert -4.5 <= slope <= -1.5


def test_impulse_train_every_100_ms():
    audio, _ = render(plain(seconds=1, osc=OFF, impulse={"on": True, "every_ms": 100}))
    assert np.count_nonzero(audio[0]) == 10
    assert audio[0][0] == 1.0


def test_same_patch_same_bytes_and_seam_loops_cleanly():
    patch = {"seconds": 1, "osc": osc(freq=997, unison=4, random_phase=True, unison_spread=0.9),
             "noise": {"on": True, "color": "brown", "stereo": "independent", "level_db": -40}}
    first, _ = render(patch)
    again, _ = render(patch)
    assert first.tobytes() == again.tobytes()
    x = first[0].astype(np.float64)
    assert abs(x[0] - x[-1]) <= 1.5 * np.abs(np.diff(x)).max() + 1e-12


def test_seam_jump_of_a_tone():
    audio, _ = render({"seconds": 1, "normalize": False, "level_db": 0, "osc": osc(freq=997)})
    x = audio[0].astype(np.float64)
    assert abs(x[0] - x[-1]) <= 1.5 * np.abs(np.diff(x)).max()


def test_seam_is_always_applied_when_set():
    # 997.3 Hz does not end on a whole cycle, so the crossfade changes the first 20 ms and nothing after
    base = plain(seconds=1, osc=osc(freq=997.3))
    plain_out, _ = render({**base, "seam_ms": 0})
    seamed, _ = render({**base, "seam_ms": 20})
    s = 20 * RATE // 1000
    assert not np.allclose(plain_out[:, :s], seamed[:, :s])
    assert np.allclose(plain_out[:, s:], seamed[:, s:], atol=1e-6)


def test_master_db_scales_the_level_and_clips_past_full_scale():
    _, info = render(plain(osc=osc(freq=1000), level_db=-18, master_db=6))
    assert abs(info["peak_db"] - (-12.0)) <= 0.1 and info["clipped"] is False
    _, info = render(plain(osc=osc(freq=1000), level_db=0, master_db=24))
    assert info["clipped"] is True and info["peak_db"] == 0.0  # the sine is clipped at full scale
    _, info = render(plain(osc=osc(freq=1000), level_db=-18, master_db=-6))
    assert abs(info["peak_db"] - (-24.0)) <= 0.1


def test_master_db_follows_normalize():
    _, info = render({"seconds": 1, "seam_ms": 0, "level_db": -18, "normalize": True, "master_db": 6,
                          "osc": osc(freq=1000)})
    assert abs(info["peak_db"] - (-12.0)) <= 0.1


def test_oscillator_off_with_noise_and_impulse_off_is_silence():
    audio, info = render(plain(osc=OFF))
    assert not audio.any()
    assert info["peak_db"] is None and info["rms_db"] is None and info["summary"] == "Silence"


def test_summary_names():
    cases = [
        (plain(osc=osc(wave="saw", freq=110, unison=7)), "Saw 110 Hz ×7"),
        (plain(osc=OFF, noise={"on": True, "color": "pink"}), "Pink noise"),
        (plain(osc=osc(freq=1000), noise={"on": True, "color": "pink"}), "Sine 1 kHz + pink noise"),
        (plain(osc=OFF, impulse={"on": True, "every_ms": 1000}), "Impulses every 1 s"),
        (plain(osc=OFF), "Silence"),
    ]
    for patch, text in cases:
        assert render(patch)[1]["summary"] == text


# ---------- speed ----------

def test_render_time_one_oscillator_8_voices_10_seconds():
    clean = sg.clean_patch({"seconds": 10, "rate": 48000, "normalize": True,
                            "osc": osc(wave="saw", freq=110, unison=8, unison_spread=0.7, random_phase=True),
                            "noise": {"on": True}})
    sg.render(clean)  # the first render builds the mip stacks
    start = time.perf_counter()
    audio, info = sg.render(clean)
    took = time.perf_counter() - start
    print(f"\n10 s @ 48 kHz, 1 oscillator x 8 voices + noise: {took:.2f} s")
    assert audio.shape == (2, 10 * RATE)
    assert "note" not in info
    assert took < 2.0


def test_unison_is_cut_for_very_long_renders_and_says_so():
    _, info = render({"seconds": 60, "rate": 96000, "osc": osc(unison=8)})
    assert "note" in info and info["seconds"] == 60.0


# ---------- files ----------

def test_save_writes_a_float_wav_reuses_it_and_keeps_six():
    folder = sg._signals_dir()
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("signal-*.wav"):
        old.unlink()
    paths = []
    for k in range(8):
        clean = sg.clean_patch({"seconds": 0.5, "osc": osc(freq=100 + k)})
        audio, _ = sg.render(clean)
        paths.append(sg.save(clean, audio, RATE))
    assert len(list(folder.glob("signal-*.wav"))) == sg.KEEP_SIGNALS
    assert paths[-1].exists() and not paths[0].exists()
    layout = host_worker.read_wav_layout(str(paths[-1]))
    assert layout["channels"] == 2 and layout["rate"] == RATE and layout["frames"] == RATE // 2
    clean = sg.clean_patch({"seconds": 0.5, "osc": osc(freq=107)})
    audio, _ = sg.render(clean)
    before = paths[-1].read_bytes()
    assert sg.save(clean, audio, RATE) == paths[-1]
    assert paths[-1].read_bytes() == before


def test_preview_is_4096_frames_from_half_a_second():
    audio, _ = render(plain(seconds=2, osc=osc(freq=1000)))
    p = sg.preview(audio, RATE)
    assert p["rate"] == RATE and len(p["l"]) == 4096 and len(p["r"]) == 4096
    assert p["l"][0] == round(float(audio[0][RATE // 2]), 5)


# ---------- the endpoints (app/web.py) ----------

@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    from app import security
    from app.web import app

    return TestClient(app, base_url="http://127.0.0.1"), {security.TOKEN_HEADER: security.TOKEN}


def test_render_endpoint_answers_audio_preview_and_info(client):
    c, headers = client
    r = c.post("/api/playground/signal", json={"patch": {"seconds": 2, "osc": osc(freq=1000)}},
               headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["preview"]["l"]) == 4096 and len(body["preview"]["r"]) == 4096
    assert body["info"]["summary"] == "Sine 1 kHz" and body["info"]["peak_db"] == -18.0
    state = None
    for _ in range(100):  # the Audio tab decodes the rendered file in the background
        state = c.get("/api/playground/audio").json()
        if state["status"] in ("ready", "error"):
            break
        time.sleep(0.1)
    assert state["status"] == "ready", state
    assert state["name"] == "Signal – Sine 1 kHz.wav"
    assert state["info"]["channels"] == 2 and state["info"]["rate"] == RATE


def test_errors_and_the_removed_table_endpoints(client):
    c, headers = client
    assert c.post("/api/playground/signal", json={"patch": {}}).status_code == 403  # no token
    assert c.post("/api/playground/signal", json={"patch": [1, 2]}, headers=headers).status_code == 409
    # the wavetable wave is gone: the oscillator renders a sine
    r = c.post("/api/playground/signal", json={"patch": {"osc": {"wave": "wavetable"}}}, headers=headers)
    assert r.status_code == 200 and r.json()["info"]["summary"] == "Sine 1 kHz"
    assert c.get("/api/playground/signal/tables").status_code == 404
    assert c.post("/api/playground/signal/wavetable", json={"path": "x.wav"}, headers=headers).status_code == 404
