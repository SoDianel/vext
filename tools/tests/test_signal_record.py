"""Tests for the recording of the Signal tab (app/browser_record.py, start("signal")) and the
browser-record route's source check. No audio device, no plugin: the rings are in-process
(analysis_tap.InProcessTap). The cache goes to a temporary folder (VEXT_ROOT, read by app/config.py).

Run:  .venv-dev\\Scripts\\python.exe -m pytest tools/tests/test_signal_record.py -q
"""
import os
import sys
import tempfile
import time

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
WORK = tempfile.mkdtemp(prefix="vext-record-test-")
os.environ["VEXT_ROOT"] = WORK  # set before app.config is imported

from app import analysis_tap, browser_record, host_worker

RATE = 48000
BLOCK = 2048


@pytest.fixture
def rings(monkeypatch):
    """A fake plugin ring playing a file (mode FILE) and the browser's ring (mode BROWSER).
    analysis_tap.pick() runs for real over them."""
    plugin = analysis_tap.InProcessTap(capacity=1 << 16)
    plugin.start(RATE, analysis_tap.FILE)
    browser = analysis_tap.InProcessTap(capacity=1 << 16)
    browser.start(RATE, analysis_tap.BROWSER)
    monkeypatch.setattr(analysis_tap, "rings", lambda: [(plugin, "Fake Plugin"), (browser, None)])
    yield plugin, browser
    try:
        browser_record.stop()
    except browser_record.RecordError:
        pass


def feed(pairs, blocks: int) -> int:
    """Writes `blocks` blocks of a constant level into each (ring, level) pair, a little apart
    (a ring that isn't written for 0.3 s stops counting as playing). Answers the frames per ring."""
    for _ in range(blocks):
        for ring, level in pairs:
            x = np.full((2, BLOCK), level, dtype=np.float32)
            ring.write(x, x)
        time.sleep(0.01)
    return blocks * BLOCK


def wait_frames(n: int, timeout: float = 10.0) -> None:
    end = time.time() + timeout
    while browser_record.status()["frames"] < n:
        assert time.time() < end, browser_record.status()
        time.sleep(0.01)


def recorded(path: str) -> np.ndarray:
    """The recorded frames, (n, 2) float32, read from the WAV the record wrote."""
    layout = host_worker.read_wav_layout(path)
    assert layout is not None and layout["channels"] == 2
    with open(path, "rb") as f:
        f.seek(layout["offset"])
        raw = f.read(layout["frames"] * 8)
    return np.frombuffer(raw, dtype="<f4").reshape(-1, 2)


def test_signal_records_the_file_ring_and_ignores_the_browser_ring(rings):
    plugin, browser = rings
    browser_record.start("signal")
    st = browser_record.status()
    assert st["on"] and st["source"] == "signal"
    assert not analysis_tap._recording  # a signal never asks the pages to send their frames
    total = feed([(plugin, 0.25), (browser, 0.75)], blocks=20)
    wait_frames(total)
    done = browser_record.stop()
    assert done["name"].startswith("Signal recording ") and done["name"].endswith(".wav")
    assert os.path.basename(done["path"]).startswith("signal-")
    assert done["status"]["source"] == "signal" and done["status"]["through"] == "Fake Plugin"
    data = recorded(done["path"])
    assert len(data) == total
    assert np.allclose(data, 0.25)  # the browser's 0.75 is not in it
    assert browser_record.status()["source"] is None


def test_browser_source_ignores_the_file_ring(rings):
    plugin, browser = rings
    browser_record.start("browser")
    st = browser_record.status()
    assert st["source"] == "browser" and st["through"] is None
    assert analysis_tap._recording  # the pages are asked to send their frames
    total = feed([(plugin, 0.25), (browser, 0.75)], blocks=20)
    wait_frames(total)
    done = browser_record.stop()
    assert done["name"].startswith("Browser recording ")
    assert os.path.basename(done["path"]).startswith("browser-")
    data = recorded(done["path"])
    assert len(data) == total
    assert np.allclose(data, 0.75)  # the plugin's file is not in it
    assert not analysis_tap._recording


def test_signal_with_only_the_browser_playing_records_nothing(rings):
    _plugin, browser = rings
    browser_record.start("signal")
    feed([(browser, 0.75)], blocks=5)
    with pytest.raises(browser_record.RecordError) as err:
        browser_record.stop()
    assert str(err.value) == "Nothing was recorded: play the signal through a plugin"
    assert browser_record.status()["on"] is False


def test_a_restarted_ring_goes_on_recording(rings):
    plugin, _browser = rings
    browser_record.start("signal")
    first = feed([(plugin, 0.25)], blocks=8)
    wait_frames(first)
    plugin.start(RATE, analysis_tap.FILE)  # the signal rendered again: a new gen, from frame 0
    second = feed([(plugin, 0.5)], blocks=8)
    wait_frames(first + second)
    done = browser_record.stop()
    assert done["status"]["error"] is None
    data = recorded(done["path"])
    assert len(data) == first + second
    assert np.allclose(data[:first], 0.25) and np.allclose(data[first:], 0.5)


def test_a_rate_change_still_ends_the_recording(rings):
    plugin, _browser = rings
    browser_record.start("signal")
    first = feed([(plugin, 0.25)], blocks=4)
    wait_frames(first)
    plugin.start(44100, analysis_tap.FILE)
    feed([(plugin, 0.5)], blocks=2)
    end = time.time() + 10
    while browser_record.status()["on"] and time.time() < end:
        time.sleep(0.01)
    st = browser_record.status()
    assert st["on"] is False and st["pending"] is True and "sample rate changed" in (st["error"] or "")
    done = browser_record.stop()  # collects the recording that stopped by itself
    assert len(recorded(done["path"])) == first
    assert browser_record.status()["source"] is None


def test_a_second_start_says_what_already_records(rings):
    browser_record.start("signal")
    with pytest.raises(browser_record.RecordError) as err:
        browser_record.start("browser")
    assert str(err.value) == "Already recording the signal."
    try:
        browser_record.stop()
    except browser_record.RecordError:
        pass


def test_route_checks_the_source():
    from fastapi.testclient import TestClient

    from app import security
    from app.web import app

    c = TestClient(app, base_url="http://127.0.0.1")
    headers = {security.TOKEN_HEADER: security.TOKEN}
    r = c.post("/api/playground/browser-record", json={"on": True, "source": "nope"}, headers=headers)
    assert r.status_code == 400
    # a signal with no plugin playing: it starts, and stopping it with nothing recorded is a 409
    r = c.post("/api/playground/browser-record", json={"on": True, "source": "signal"}, headers=headers)
    assert r.status_code == 200 and r.json()["source"] == "signal"
    r = c.post("/api/playground/browser-record", json={"on": False}, headers=headers)
    assert r.status_code == 409
    assert "play the signal through a plugin" in r.json()["detail"]
