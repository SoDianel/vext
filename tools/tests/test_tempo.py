"""Deterministic tempo, fallback, persistence, and authenticated API checks."""
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
scratch = tempfile.TemporaryDirectory(prefix="vext-tempo-test-")
for name in ("VEXT_ROOT", "APPDATA", "LOCALAPPDATA", "USERPROFILE"):
    os.environ[name] = str(Path(scratch.name) / name)
os.environ["VEXT_TOKEN"] = "tempo-test"
os.environ["VEXT_VIRTUAL_MIDI"] = "0"

import numpy as np
from app import tempo

rate = 48000
for bpm in (60, 90, 100, 120, 140, 180):
    sound = np.zeros(rate * 12, dtype=np.float32)
    for start in np.arange(0, len(sound), rate * 60 / bpm).astype(int):
        sound[start:start + 480] = np.hanning(960)[:min(480, len(sound) - start)]
    detected, confidence = tempo.estimate(tempo.envelope(sound, rate))
    assert abs(detected - bpm) < 1 and confidence > .35, (bpm, detected, confidence)

for sound in (np.zeros(rate * 12), np.random.default_rng(2).normal(0, .1, rate * 12),
              *(.1 * np.sin(np.arange(rate * 12) * 2 * np.pi * hz / rate) for hz in (20, 80, 440, 16000)), np.ones(rate * 12) * .1):
    assert tempo.estimate(tempo.envelope(sound, rate)) == (100, 0)
assert tempo.estimate([0, float("nan")]) == (100, 0)
assert tempo.state()["bpm"] == 100
tempo.set_value(137, "manual")
assert tempo.set_value(90, source="live", confidence=.9)["bpm"] == 137
tempo._loaded = False
assert tempo.state()["bpm"] == 137

tempo.set_transport(True, 12, owner="first-worker")
tempo.set_transport(True, 20, owner="second-worker")
tempo.release_transport("first-worker")
assert tempo.transport_state()["playing"]
tempo.release_transport("second-worker", 22)
assert tempo.transport_state() == {"playing": False, "position": 22}
assert tempo.set_value(mode="auto")["bpm"] == 100
for bad in (0, 301, float("nan"), float("inf")):
    try:
        tempo.set_value(bad, "manual")
    except ValueError:
        pass
    else:
        raise AssertionError(bad)

# Analyze a decoded float WAV rather than guessing from its duration.
from app.host_worker import float_wav_header
path = Path(scratch.name) / "pulse.wav"
click = np.zeros(rate * 12, dtype=np.float32)
for start in range(0, len(click), rate // 2):
    click[start:start + 480] = np.hanning(960)[:min(480, len(click) - start)]
path.write_bytes(float_wav_header(1, rate, len(click)) + click.tobytes())
result = tempo.analyze_file(path)
assert abs(result["bpm"] - 120) < 1 and result["source"] == "file"
assert tempo.analyze_file(path, lambda: False) is None

# The browser path receives uneven stereo blocks; opposite channel polarity
# must not cancel the onset envelope, and a manual override must remain stable.
import threading
news = threading.Event()
tempo.listen(lambda result: news.set() if result["source"] == "live" else None)
live = tempo.Live()
stereo = np.vstack((click, -click))
for start in range(0, stereo.shape[1], 1133):
    live.feed(stereo[:, start:start + 1133], rate, "generated-browser")
assert news.wait(5), "live tempo was not published"
assert abs(tempo.state()["bpm"] - 120) < 1
tempo.set_value(137, "manual")
live.feed(stereo, rate, "generated-browser")
assert tempo.state()["bpm"] == 137

from fastapi.testclient import TestClient
from app import web

client = TestClient(web.app, base_url="http://127.0.0.1:8000")
assert client.post("/api/playground/tempo", json={"bpm": 130}).status_code == 403
headers = {"X-VEXT-Token": "tempo-test"}
result = client.post("/api/playground/tempo", json={"bpm": 130}, headers=headers)
assert result.status_code == 200 and result.json()["mode"] == "manual", result.text
assert client.post("/api/playground/tempo", json={"bpm": 500}, headers=headers).status_code == 400
assert client.get("/api/playground/tempo").json()["bpm"] == 130
assert client.post("/api/playground/transport", json={"playing": True, "position": 10}).status_code == 403
assert client.post("/api/playground/transport", json={"playing": True, "position": 10}, headers=headers).status_code == 200
assert tempo.transport_state()["playing"]
client.post("/api/playground/transport", json={"playing": False, "position": 12.5}, headers=headers)
assert tempo.transport_state() == {"playing": False, "position": 12.5}
client.post("/api/playground/transport", json={"playing": False, "position": 7}, headers=headers)
assert tempo.transport_state()["position"] == 7, "paused page seek was lost"
assert client.post("/api/playground/transport", json={"playing": True, "position": -1}, headers=headers).status_code == 400

# Returning to Auto re-evaluates the already loaded file.
from unittest import mock
from app import playground_audio
detected = threading.Event()
tempo.listen(lambda result: detected.set() if result["source"] == "file" else None)
with mock.patch.object(playground_audio, "source_for_play", return_value=(str(path), [], 12)), \
        mock.patch.object(playground_audio, "state", return_value={"source_path": str(path)}):
    client.post("/api/playground/tempo", json={"mode": "auto"}, headers=headers)
    assert detected.wait(5), "Auto did not re-evaluate the loaded file"
assert abs(tempo.state()["bpm"] - 120) < 1
client.close()
print("PASS: pulse tempos, noise/tone/silence fallback, manual override, persistence and API token")
