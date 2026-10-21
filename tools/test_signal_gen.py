"""Tests for the Playground's Signal renderer (app/signal_gen.py) and its endpoints (app/web.py).
No audio device, no plugin. The cache goes to a temporary folder (VEXT_ROOT, read by app/config.py).

Run:  .venv-dev\\Scripts\\python.exe -m pytest tools/test_signal_gen.py -q
"""
import os
import struct
import sys
import tempfile
import time

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
WORK = tempfile.mkdtemp(prefix="vext-signal-test-")
os.environ["VEXT_ROOT"] = WORK  # set before app.config is imported

from app import host_worker
from app import signal_gen as sg

RATE = 48000


def osc(**kw) -> dict:
    """An oscillator patch: a 1 kHz sine at 0 dB unless told otherwise."""
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


def write_wav(path: str, samples, fmt: int = 3, bits: int = 32, channels: int = 1) -> None:
    if fmt == 3:
        data = np.asarray(samples, dtype="<f4").tobytes()
    else:
        data = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
    block = channels * bits // 8
    header = (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE" + b"fmt "
              + struct.pack("<IHHIIHH", 16, fmt, channels, RATE, RATE * block, block, bits)
              + b"data" + struct.pack("<I", len(data)))
    with open(path, "wb") as f:
        f.write(header + data)


# ---------- the patch ----------

def test_clean_patch_clamps_and_fills_defaults():
    c = sg.clean_patch({"seconds": 500, "rate": 12345, "level_db": 9, "seam_ms": -4, "channels": "nope",
                        "oscillators": [{"freq": 99999, "unison": 20, "wave": "nope", "pan": -7}]})
    assert c["seconds"] == 60 and c["rate"] == 48000 and c["level_db"] == 0 and c["seam_ms"] == 0
    assert c["channels"] == "stereo"
    o = c["oscillators"][0]
    assert o["freq"] == 24000 and o["unison"] == 8 and o["wave"] == "sine" and o["pan"] == -1
    d = sg.clean_patch({})
    assert d["seconds"] == 10 and d["normalize"] is True and d["noise"]["on"] is False
    assert len(d["oscillators"]) == 1 and d["oscillators"][0]["freq"] == 1000  # the default sine
    assert sg.clean_patch({"oscillators": []})["oscillators"] == []


def test_clean_patch_drops_unknown_keys_and_rejects_non_objects():
    c = sg.clean_patch({"bogus": 1, "seconds": 2})
    assert "bogus" not in c
    assert sg.patch_key(c) == sg.patch_key(sg.clean_patch({"seconds": 2}))
    for bad in ([1], "x", 3, None):
        with pytest.raises(ValueError):
            sg.clean_patch(bad)


# ---------- the oscillators ----------

def test_sine_level_and_frequency():
    audio, info = render(plain(oscillators=[osc(freq=1000)], level_db=-18))
    assert audio.shape == (2, 2 * RATE) and audio.dtype == np.float32
    assert abs(info["peak_db"] - (-18.0)) <= 0.1
    x = audio[0].astype(np.float64)
    spec = np.abs(np.fft.rfft(x))
    peak_hz = np.argmax(spec) * RATE / len(x)
    assert abs(peak_hz - 1000) < 1
    assert info["summary"] == "Sine 1 kHz" and info["clipped"] is False


def test_saw_is_band_limited():
    audio, _ = render(plain(oscillators=[osc(wave="saw", freq=5000)]))
    x = audio[0].astype(np.float64)
    spec = np.abs(np.fft.rfft(x))
    freqs = np.fft.rfftfreq(len(x), 1 / RATE)
    fund = spec[np.argmin(np.abs(freqs - 5000))]
    assert np.argmax(spec[freqs < 6000]) == np.argmin(np.abs(freqs - 5000))
    assert spec[freqs >= 0.46 * RATE].max() < fund * 10 ** (-60 / 20)


def test_unison_spread_and_centre():
    base = plain(seconds=1, level_db=-12, oscillators=[osc(freq=220, unison=7, unison_detune=15)])
    wide, _ = render({**base, "oscillators": [osc(freq=220, unison=7, unison_detune=15, unison_spread=0.8)]})
    assert not np.allclose(wide[0], wide[1])
    centre, _ = render({**base, "oscillators": [osc(freq=220, unison=7, unison_detune=15, unison_spread=0)]})
    assert np.allclose(centre[0], centre[1], atol=1e-6)


def test_invert_and_one_side_and_mono_channels():
    src = [osc(freq=440, pan=0.7)]
    inv, _ = render(plain(seconds=1, channels="invert", oscillators=src))
    assert np.array_equal(inv[1], -inv[0]) and np.any(inv[0] != 0)
    left, _ = render(plain(seconds=1, channels="left", oscillators=src))
    assert np.all(left[1] == 0) and np.any(left[0] != 0)
    mono, _ = render(plain(seconds=1, channels="mono", oscillators=src))
    assert np.array_equal(mono[0], mono[1])


def test_wavetable_frames_and_mip_levels():
    names = [t["name"] for t in sg.tables() if not t["user"]]
    assert names == ["basic", "harmonics", "pwm", "formant", "sync", "fm", "bitcrush"]
    for name in names:
        o = sg.clean_patch({"oscillators": [{"wave": "wavetable", "table": name}]})["oscillators"][0]
        stack = sg._stack(o)
        assert stack.shape == (sg.LEVELS, sg.TABLE_FRAMES, sg.N), name
        assert np.isfinite(stack).all(), name
        # the top level keeps only the fundamental: nothing above bin 1 in any frame
        spec = np.abs(np.fft.rfft(stack[sg.LEVELS - 1, 5].astype(np.float64)))
        assert spec[2:].max() < 1e-3 * spec[1], name


# ---------- noise, impulses, gate, seam ----------

def test_pink_noise_slope_is_minus_3_db_per_octave():
    audio, _ = render(plain(seconds=10, oscillators=[], noise={"on": True, "color": "pink"}))
    x = audio[0].astype(np.float64)
    power = np.abs(np.fft.rfft(x)) ** 2
    freqs = np.fft.rfftfreq(len(x), 1 / RATE)
    centres = [100 * 2 ** k for k in range(7)]  # 100 Hz .. 6.4 kHz, octave bands
    levels = [10 * np.log10(power[(freqs >= c / 2 ** 0.5) & (freqs < c * 2 ** 0.5)].mean()) for c in centres]
    slope = np.polyfit(np.arange(len(centres)), levels, 1)[0]
    assert -4.5 <= slope <= -1.5


def test_impulse_train_every_100_ms():
    audio, _ = render(plain(seconds=1, oscillators=[], impulse={"on": True, "every_ms": 100}))
    assert np.count_nonzero(audio[0]) == 10
    assert audio[0][0] == 1.0


def test_gate_closed_parts_are_silent():
    audio, _ = render(plain(oscillators=[osc(freq=1000)], gate={"on": True, "on_ms": 200, "off_ms": 800}))
    x = audio[0].astype(np.float64)
    ms = (np.arange(len(x)) / RATE * 1000) % 1000
    closed = x[(ms > 210) & (ms < 990)]
    opened = x[(ms > 20) & (ms < 180)]
    rms = lambda v: float(np.sqrt(np.mean(v ** 2)))
    assert rms(closed) < rms(opened) * 10 ** (-80 / 20)


def test_same_patch_same_bytes_and_seam_loops_cleanly():
    patch = {"seconds": 1, "oscillators": [osc(freq=997, unison=4, random_phase=True, unison_spread=0.9)],
             "noise": {"on": True, "color": "brown", "stereo": "independent", "level_db": -40}}
    first, _ = render(patch)
    again, _ = render(patch)
    assert first.tobytes() == again.tobytes()
    x = first[0].astype(np.float64)
    assert abs(x[0] - x[-1]) <= 1.5 * np.abs(np.diff(x)).max() + 1e-12


def test_seam_jump_of_a_tone():
    audio, _ = render({"seconds": 1, "normalize": False, "level_db": 0, "oscillators": [osc(freq=997)]})
    x = audio[0].astype(np.float64)
    assert abs(x[0] - x[-1]) <= 1.5 * np.abs(np.diff(x)).max()


# ---------- speed ----------

def test_render_time_4_oscillators_8_voices_10_seconds():
    oscs = [osc(wave=w, freq=110 * (i + 1), unison=8, unison_spread=0.7, random_phase=True,
                tremolo={"rate": 3, "depth": 0.3}, vibrato={"rate": 5, "depth": 20})
            for i, w in enumerate(["sine", "saw", "pulse", "wavetable"])]
    clean = sg.clean_patch({"seconds": 10, "rate": 48000, "oscillators": oscs, "noise": {"on": True},
                            "gate": {"on": True}})
    sg.render(clean)  # the first render builds the wavetables
    start = time.perf_counter()
    audio, info = sg.render(clean)
    took = time.perf_counter() - start
    print(f"\n10 s @ 48 kHz, 4 oscillators x 8 voices: {took:.2f} s")
    assert audio.shape == (2, 10 * RATE)
    assert "note" not in info
    assert took < 2.0


def test_unison_is_cut_for_very_long_renders_and_says_so():
    oscs = [osc(unison=8) for _ in range(4)]
    _, info = render({"seconds": 60, "rate": 96000, "oscillators": oscs})
    assert "note" in info and info["seconds"] == 60.0


# ---------- wavetables from files ----------

def test_import_user_wavetable_and_use_it():
    src = os.path.join(WORK, "my table.wav")
    write_wav(src, np.sin(2 * np.pi * np.arange(2 * 2048 + 100) / 2048), fmt=1, bits=16)  # 2 cycles and a rest
    mine = [t for t in sg.import_wavetable(src) if t["name"] == "my table"]
    assert mine == [{"name": "my table", "label": "my table", "frames": 2, "user": True}]
    _audio, info = render(plain(oscillators=[{"wave": "wavetable", "table": "my table", "freq": 1000}]))
    assert info["summary"] == "Table my table 1 kHz"
    assert abs(info["peak_db"]) < 0.5


def test_short_float_wav_is_one_stretched_frame_and_builtin_names_are_kept():
    src = os.path.join(WORK, "basic.wav")
    write_wav(src, np.linspace(-1, 1, 100))
    mine = [t for t in sg.import_wavetable(src) if t["name"] == "basic (user)"]
    assert mine == [{"name": "basic (user)", "label": "basic (user)", "frames": 1, "user": True}]


def test_bad_wavetable_files_raise_value_error():
    text = os.path.join(WORK, "notes.wav")
    with open(text, "w") as f:
        f.write("not audio")
    with pytest.raises(ValueError):
        sg.import_wavetable(text)
    with pytest.raises(ValueError):
        sg.import_wavetable(os.path.join(WORK, "missing.wav"))


# ---------- files ----------

def test_save_writes_a_float_wav_reuses_it_and_keeps_six():
    folder = sg._signals_dir()
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("signal-*.wav"):
        old.unlink()
    paths = []
    for k in range(8):
        clean = sg.clean_patch({"seconds": 0.5, "oscillators": [osc(freq=100 + k)]})
        audio, _ = sg.render(clean)
        paths.append(sg.save(clean, audio, RATE))
    assert len(list(folder.glob("signal-*.wav"))) == sg.KEEP_SIGNALS
    assert paths[-1].exists() and not paths[0].exists()
    layout = host_worker.read_wav_layout(str(paths[-1]))
    assert layout["channels"] == 2 and layout["rate"] == RATE and layout["frames"] == RATE // 2
    clean = sg.clean_patch({"seconds": 0.5, "oscillators": [osc(freq=107)]})
    audio, _ = sg.render(clean)
    before = paths[-1].read_bytes()
    assert sg.save(clean, audio, RATE) == paths[-1]
    assert paths[-1].read_bytes() == before


def test_preview_is_4096_frames_from_half_a_second():
    audio, _ = render(plain(seconds=2, oscillators=[osc(freq=1000)]))
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
    r = c.post("/api/playground/signal", json={"patch": {"seconds": 2, "oscillators": [osc(freq=1000)]}},
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


def test_tables_and_errors_from_the_endpoints(client):
    c, headers = client
    names = [t["name"] for t in c.get("/api/playground/signal/tables").json()["tables"]]
    assert names[:2] == ["basic", "harmonics"]
    assert c.post("/api/playground/signal", json={"patch": {}}).status_code == 403  # no token
    assert c.post("/api/playground/signal", json={"patch": [1, 2]}, headers=headers).status_code == 409
    bad_table = {"oscillators": [{"wave": "wavetable", "table": "no such table"}]}
    assert c.post("/api/playground/signal", json={"patch": bad_table}, headers=headers).status_code == 409
    gone = os.path.join(WORK, "gone.wav")
    assert c.post("/api/playground/signal/wavetable", json={"path": gone}, headers=headers).status_code == 409
