"""Tests for the Audio tab's reset (app/playground_audio.reset_session, run at startup and shutdown by
app/web.py): the Audio tab starts empty, VEXT's files under cache/playground go, and the user's own
audio file is never touched. Everything runs in a temporary VEXT_ROOT.

Run:  .venv-dev\\Scripts\\python.exe -m pytest tools/tests/test_playground_reset.py -q
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
WORK = Path(tempfile.mkdtemp(prefix="vext-reset-test-"))
os.environ["VEXT_ROOT"] = str(WORK / "app")  # set before app.config is imported

import numpy as np
import pytest

from app import host_worker
from app import playground_audio as pa
from app.config import PROJECT_ROOT

assert Path(PROJECT_ROOT).resolve() == (WORK / "app").resolve(), "app.config was imported before VEXT_ROOT was set"

CACHE = Path(pa.CACHE_DIR)
RATE = 48000
TOKEN = "abc123"


def float_wav(path: Path, frames: int = 4800) -> Path:
    """A 32-bit float stereo WAV, like the decoded copies VEXT writes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = np.full((frames, 2), 0.25, dtype="<f4").tobytes()
    path.write_bytes(host_worker.float_wav_header(2, RATE, frames) + data)
    return path


def put(path: Path, text: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text)
    return path


def save_loaded_file(user_file: Path) -> None:
    """What a session leaves behind: the decoded copy, a recording and the state file that names them."""
    float_wav(CACHE / f"source-{TOKEN}.wav")
    float_wav(CACHE / f"recording-{TOKEN}.wav")
    (CACHE / "state.json").write_text(json.dumps({
        "path": str(user_file), "name": "song.wav", "token": TOKEN, "info": None,
        "edits": [{"type": "fade_in", "start_s": 0.0, "end_s": 0.5, "gain": 1.0}],
    }), encoding="utf-8")


@pytest.fixture(autouse=True)
def fresh_cache():
    shutil.rmtree(CACHE, ignore_errors=True)
    pa._restored = False  # the Audio tab restores its saved file on the first read, as after a start
    pa._empty()
    yield
    shutil.rmtree(CACHE, ignore_errors=True)


def test_reset_clears_the_tab_and_deletes_vexts_files_but_not_the_user_file():
    user_file = put(WORK / "Music" / "song.wav", b"the user's own audio")
    save_loaded_file(user_file)
    assert pa.state()["status"] == "ready"  # the saved file was restored

    put(CACHE / "heard-abc123-v2.wav")
    put(CACHE / "upload-1a2b3c4d" / "song.mp3")
    put(CACHE / "captures" / "browser-20261022-101010.wav")
    put(CACHE / "signals" / "signal-0123456789ab.wav")
    put(CACHE / "wavetables" / "mine.npy")  # the old user wavetables
    put(CACHE / "source-old.part")
    put(CACHE / "state.tmp")
    put(CACHE / "notes.txt")  # not VEXT's: left alone

    pa.reset_session()

    state = pa.state()
    assert state["status"] == "empty" and state["path"] == "" and state["edits"] == []
    assert state["recording"]["exists"] is False
    assert not (CACHE / "state.json").exists()
    left = sorted(p.name for p in CACHE.iterdir())
    assert left == ["notes.txt"]
    assert user_file.read_bytes() == b"the user's own audio"


def test_the_saved_file_does_not_come_back_after_a_reset():
    user_file = put(WORK / "Music" / "song.wav", b"audio")
    save_loaded_file(user_file)
    pa.reset_session()  # before anything restored the tab (no state() call yet)
    # a stale state file that survived the reset (a file a worker held open) must not restore either
    save_loaded_file(user_file)
    state = pa.state()
    assert state["status"] == "empty" and state["edits"] == []
    with pytest.raises(ValueError):
        pa.source_for_play()
    with pytest.raises(ValueError):
        pa.heard_source_path()


def test_reset_on_an_empty_or_missing_cache_does_nothing_and_does_not_raise():
    shutil.rmtree(CACHE, ignore_errors=True)
    pa.reset_session()
    assert pa.state()["status"] == "empty"
    pa.reset_session()  # twice in a row is fine too


def test_a_file_still_open_is_left_for_the_next_start_and_the_rest_goes():
    held = put(CACHE / "recording-held.wav")
    gone = put(CACHE / "captures" / "browser-1.wav")
    with open(held, "rb"):
        pa.reset_session()
    assert gone.exists() is False
    if os.name == "nt":  # Windows won't delete a file that is open
        assert held.exists()
    else:
        assert held.exists() is False


def test_the_backend_resets_at_startup_before_the_other_startup_work_and_at_shutdown():
    from app.web import app

    startup = [f.__name__ for f in app.router.on_startup]
    shutdown = [f.__name__ for f in app.router.on_shutdown]
    assert startup.index("_reset_playground_audio") < startup.index("_ensure_schema_current")
    assert "_reset_playground_audio_at_shutdown" in shutdown
