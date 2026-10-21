"""Tests for Configuration > Storage (app/storage.py, /api/storage): measuring and clearing
caches and VEXT's FL Studio backups. Everything runs in a temporary VEXT_ROOT, a temporary
LOCALAPPDATA (the web view's profile) and a temporary "Plugin database"; never the real ones.

Run:  .venv-dev\\Scripts\\python.exe -m pytest tools/test_storage.py -q
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
WORK = Path(tempfile.mkdtemp(prefix="vext-storage-test-"))
os.environ["VEXT_ROOT"] = str(WORK / "app")            # set before app.config is imported
os.environ["LOCALAPPDATA"] = str(WORK / "local")

import pytest

from app import storage
from app.config import PROJECT_ROOT

assert Path(PROJECT_ROOT).resolve() == (WORK / "app").resolve(), "app.config was imported before VEXT_ROOT was set"

FL = WORK / "fl" / "Plugin database"
PG = WORK / "app" / "cache" / "playground"
WEB = WORK / "local" / "com.vext.app" / "EBWebView"


def put(path: Path, size: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


@pytest.fixture()
def tree(monkeypatch):
    for p in (FL, PG, WEB, WORK / "app" / "cache" / "pictures"):
        if p.exists():
            import shutil
            shutil.rmtree(p)
    put(WEB / "Default" / "Cache" / "Cache_Data" / "f_000001", 1000)
    put(WEB / "Default" / "Cookies", 50)                               # a sign-in: never touched
    put(PG / "state.json", 10)
    put(PG / "source-cur.wav", 100)                                      # the open file
    put(PG / "source-old.wav", 300)
    put(PG / "heard-old-v1.wav", 200)
    put(PG / "signals" / "signal-aaa.wav", 400)
    put(PG / "wavetables" / "mine.npy", 70)                              # user data: never touched
    put(WORK / "app" / "cache" / "pictures" / "suggestions" / "1" / "a.png", 500)
    put(FL / "Thumbnails.vext-backup-20261020" / "Effects" / "A.png", 20)
    put(FL / "Removed.vext-20261020T1Z" / "Effects" / "B.fst", 30)
    put(FL / "Effects.vext-backup-20261020T1Z" / "C.fst", 40)
    put(FL / "Effects" / "D.fst", 60)                                    # FL's browse tree: never touched
    monkeypatch.setattr(storage, "_playground_keep",
                        lambda: {str(PG / "state.json").lower(), str(PG / "source-cur.wav").lower()})
    return FL


def sizes(result):
    return {i["key"]: i["size"] for i in result["items"]}


def test_measure_counts_only_what_can_go(tree):
    got = sizes(storage.measure(tree))
    assert got == {"web-cache": 1000, "playground-temp": 900, "picture-suggestions": 500,
                   "fl-thumbnail-backups": 20, "fl-removed": 30, "fl-tree-backups": 40}
    assert storage.measure(tree)["total"] == 2490


def test_clear_frees_the_picked_items_and_nothing_else(tree):
    r = storage.clear(["web-cache", "playground-temp", "fl-thumbnail-backups"], tree)
    assert r["freed"] == 1000 + 900 + 20
    assert (WEB / "Default" / "Cookies").exists()
    assert (PG / "source-cur.wav").exists() and (PG / "state.json").exists()
    assert (PG / "wavetables" / "mine.npy").exists()
    assert not (PG / "source-old.wav").exists() and not (PG / "signals" / "signal-aaa.wav").exists()
    assert not (FL / "Thumbnails.vext-backup-20261020").exists()
    assert (FL / "Removed.vext-20261020T1Z").exists() and (FL / "Effects" / "D.fst").exists()
    after = sizes(storage.measure(tree))
    assert after["web-cache"] == 0 and after["playground-temp"] == 0 and after["fl-removed"] == 30


def test_clear_rejects_an_unknown_item(tree):
    with pytest.raises(ValueError):
        storage.clear(["everything"], tree)


def test_no_fl_folder_means_no_fl_items(tree):
    got = sizes(storage.measure(None))
    assert got["fl-removed"] == 0 and got["web-cache"] == 1000


def test_endpoints(tree):
    from fastapi.testclient import TestClient

    from app import db, security
    from app.config import DB_PATH
    from app.web import app

    db.init_db(DB_PATH)
    with db.connect(DB_PATH) as conn:
        db.set_setting(conn, "fl_plugin_db_root", str(FL / "Installed"))
        conn.commit()
    c = TestClient(app, base_url="http://127.0.0.1")
    headers = {security.TOKEN_HEADER: security.TOKEN}
    assert c.get("/api/storage").json()["total"] > 0
    r = c.post("/api/storage/clear", json={"keys": ["fl-removed"]}, headers=headers)
    assert r.status_code == 200 and r.json()["freed"] == 30 and "now" in r.json()
    assert c.post("/api/storage/clear", json={"keys": ["nope"]}, headers=headers).status_code == 400
