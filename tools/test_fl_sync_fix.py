"""Tests for the FL Studio Sync fixes: the standard thumbnail sizes, fixing pictures, manual
links (app/fl_reorganize.py, the fl_links helpers in app/db.py, the /api/fl-sync endpoints in
app/web.py).

Everything runs in a temporary VEXT_ROOT (the database, VEXT's pictures) and a temporary
"Plugin database" folder. Never the real FL Studio folder or the real VEXT data.

Run:  .venv-dev\\Scripts\\python.exe -m pytest tools/test_fl_sync_fix.py -q
"""
import hashlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
WORK = tempfile.mkdtemp(prefix="vext-flsync-test-")
os.environ["VEXT_ROOT"] = WORK  # set before app.config is imported

import pytest
from PIL import Image, PngImagePlugin

from app import db
from app import fl_reorganize as fr
from app.config import DB_PATH, IMAGES_DIR, PROJECT_ROOT

# If app.config had been imported before VEXT_ROOT was set, these tests would touch the real data.
assert Path(PROJECT_ROOT).resolve() == Path(WORK).resolve(), "app.config was imported before VEXT_ROOT was set"

PLUGIN_DB = Path(WORK) / "fl" / "Plugin database"
INSTALLED = PLUGIN_DB / "Installed"
EFFECTS = PLUGIN_DB / "Effects"

# name, vendor, format, host_path, has a VEXT picture
PLUGINS = [
    ("Std Large", "Acme", "VST3", r"C:\VST\Acme\Std Large.vst3", True),
    ("Std Medium", "Acme", "VST3", r"C:\VST\Acme\Std Medium.vst3", True),
    ("Std Compact", "Acme", "VST3", r"C:\VST\Acme\Std Compact.vst3", True),
    ("Odd Hundred", "Acme", "VST3", r"C:\VST\Acme\Odd Hundred.vst3", True),
    ("Nopic Hundred", "Acme", "VST3", r"C:\VST\Acme\Nopic Hundred.vst3", False),
    ("Fix Me", "Acme", "VST3", r"C:\VST\Acme\Fix Me.vst3", True),
    ("Legacy Plugin", "Acme", "VST3", r"C:\VST\Acme\Legacy Plugin.vst3", True),
    ("Legacy Two", "Acme", "VST3", r"C:\VST\Acme\Legacy Two.vst3", True),
    ("Linked Plugin", "Linkco", "VST3", r"C:\VST\Linkco\Linked Plugin.vst3", False),
    ("Apply Plugin", "Applyco", "VST3", r"C:\VST\Applyco\Apply Plugin.vst3", True),
    ("Import Target", "Importco", "VST3", r"C:\VST\Importco\Import Target.vst3", False),
    ("Pro-Q 3", "FabFilter", "VST3", r"C:\VST\FabFilter\Pro-Q 3.vst3", False),
    ("Pro-L 2", "FabFilter", "VST3", r"C:\VST\FabFilter\Pro-L 2.vst3", False),
    ("Pro-Q 3", "FabFilter", "AAX", r"C:\AAX\FabFilter\Pro-Q 3.aaxplugin", False),
]
HOST = {name: host for name, _vendor, _fmt, host, _pic in PLUGINS}


# ---------- helpers ----------

def make_png(path: Path, width: int, height: int, mark: str | None = None) -> None:
    if path.exists():
        fr._set_hidden(path, False)  # Windows won't overwrite a hidden file
    info = PngImagePlugin.PngInfo()
    if mark:
        info.add_text(fr._VEXT_MARK_KEY, mark)
    Image.new("RGB", (width, height), (120, 40, 40)).save(path, "PNG", pnginfo=info)


def vext_picture(host_path: str) -> str:
    """VEXT's own saved picture for a plugin, under the key picture_for_host_path() reads."""
    key = hashlib.sha1(host_path.lower().encode("utf-8")).hexdigest()[:16]
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    path = IMAGES_DIR / f"{key}.png"
    if not path.exists():
        make_png(path, 480, 300)
    return str(path)


def stamp_of(host_path: str) -> str:
    return str(int(os.path.getmtime(fr.picture_for_host_path(host_path))))


def fst_at(folder: str, stem: str) -> Path:
    """An .fst in the browse tree, Effects\\<folder>\\<stem>.fst."""
    path = EFFECTS / folder / f"{stem}.fst"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"FST")
    return path


def make_thumb(fst: Path, height: int, mark: str | None = None) -> Path:
    """A thumbnail beside fst the way FL has it: a hidden <stem>.png and the hidden .nfo that points at it."""
    png = fst.with_suffix(".png")
    make_png(png, height * 2, height, mark)
    fr._set_hidden(png, True)
    fr._write_bitmap_nfo(fst.with_suffix(".nfo"), png.name)
    return png


def pid(conn, name: str, fmt: str = "VST3") -> int:
    return conn.execute("SELECT id FROM plugins WHERE name=? AND format=?", (name, fmt)).fetchone()[0]


def check_entry(conn, stem: str) -> dict:
    inv = fr.inventory(conn)
    return next(e for e in inv["entries"] if e["name"].lower() == stem.lower())


# ---------- fixtures ----------

@pytest.fixture(scope="module")
def conn():
    """A database with the plugins above, and the FL Plugin database pointed at the temp folder."""
    db.init_db(DB_PATH)
    with db.connect(DB_PATH) as c:
        db.set_setting(c, "fl_plugin_db_root", str(INSTALLED))
        for name, vendor, fmt, host, picture in PLUGINS:
            c.execute(
                "INSERT INTO plugins (name, normalized_name, vendor, format, host_path, scanned_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (name, name.lower(), vendor, fmt, host, "2026-10-20T00:00:00+00:00"),
            )
            if picture:
                vext_picture(host)
        c.commit()
        yield c


@pytest.fixture(autouse=True)
def _commit_after_each_test(conn):
    yield
    conn.commit()  # the endpoints read through their own connection


@pytest.fixture(scope="module")
def client(conn):
    from fastapi.testclient import TestClient

    from app import security
    from app.web import app

    return TestClient(app, base_url="http://127.0.0.1"), {security.TOKEN_HEADER: security.TOKEN}


# ---------- standard sizes and the check ----------

@pytest.mark.parametrize("name, height, size_name", [
    ("Std Large", 256, "Large"), ("Std Medium", 128, "Medium"), ("Std Compact", 64, "Compact"),
])
def test_standard_heights_give_no_size_problem(conn, name, height, size_name):
    make_thumb(fst_at("Acme", name), height)  # FL's own picture, not VEXT's
    e = check_entry(conn, name)
    assert e["size"][1] == height and e["size_name"] == size_name
    assert e["can_standardize"] is False
    assert e["issues"] == []


def test_non_standard_height_with_a_vext_picture_can_be_standardized(conn):
    make_thumb(fst_at("Acme", "Odd Hundred"), 100)
    e = check_entry(conn, "Odd Hundred")
    assert e["size_name"] is None and e["can_standardize"] is True
    assert "picture is 100 px tall: not a standard size (Resize it)" in e["issues"]
    assert e["key"] == "odd hundred"


def test_non_standard_height_without_a_vext_picture_is_a_problem_too(conn):
    make_thumb(fst_at("Acme", "Nopic Hundred"), 100)
    e = check_entry(conn, "Nopic Hundred")
    assert e["can_standardize"] is True and not e["vext_picture"]   # Resize fixes it without VEXT's picture
    assert "picture is 100 px tall: not a standard size (Resize it)" in e["issues"]


def test_inventory_counts_the_new_fields(conn):
    make_thumb(fst_at("Acme", "Odd Hundred"), 100)
    counts = fr.inventory(conn)["counts"]
    assert counts["can_standardize"] >= 1
    assert counts["linked"] >= 0


# ---------- fix pictures ----------

def test_fix_pictures_at_128_writes_the_thumbnail_and_backs_up_a_foreign_one(conn):
    fst = fst_at("Acme", "Fix Me")
    make_thumb(fst, 100)  # not VEXT's: must be backed up before it's replaced
    result = fr.fix_pictures(conn, [str(fst)], 128)
    assert result["fixed"] == 1 and result["skipped"] == []
    backup = Path(result["backup"])
    assert "Thumbnails.vext-backup-" in backup.name
    assert fr._png_info(backup / "Effects" / "Acme" / "Fix Me.png")[0][1] == 100

    png, nfo = fst.with_suffix(".png"), fst.with_suffix(".nfo")
    size, mark = fr._png_info(png)
    assert size[1] == 128 and mark == f"{stamp_of(HOST['Fix Me'])}@128"
    assert fr._is_hidden(png) and fr._is_hidden(nfo) and fr._bitmap_of(nfo) == png.name
    e = result["entries"][0]
    assert e["size_name"] == "Medium" and e["source"] == "vext" and e["issues"] == []

    again = fr.fix_pictures(conn, [str(fst)], 128)  # already VEXT's, at this size: nothing to back up
    assert again["fixed"] == 1 and again["backup"] is None


def test_fix_pictures_skips_each_bad_path_with_a_reason(conn):
    outside = Path(WORK) / "outside" / "Loose.fst"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_bytes(b"FST")
    readme = EFFECTS / "Acme" / "readme.txt"
    readme.parent.mkdir(parents=True, exist_ok=True)
    readme.write_text("not a plugin", encoding="utf-8")
    newbie = INSTALLED / "Effects" / "New" / "Newbie.fst"
    newbie.parent.mkdir(parents=True, exist_ok=True)
    newbie.write_bytes(b"FST")
    mystery = fst_at("Unknown Co", "Mystery Thing")
    nopic = fst_at("Acme", "Nopic Hundred")
    traversal = str(EFFECTS / "Acme" / ".." / ".." / ".." / ".." / "outside" / "Loose.fst")  # back to WORK
    paths = [str(outside), str(EFFECTS / "Acme" / "Ghost.fst"), str(readme), str(newbie), str(mystery),
             str(nopic), traversal]

    result = fr.fix_pictures(conn, paths, 64)
    reasons = [s["reason"] for s in result["skipped"]]
    assert reasons.count("outside FL Studio's plugin database") == 2  # the path and the ..\ escape
    assert "file not found" in reasons
    assert "not an .fst file" in reasons
    assert "not in Effects or Generators" in reasons
    assert "not recognized by VEXT: link it first" in reasons
    assert "VEXT has no picture for Nopic Hundred" in reasons
    assert result["fixed"] == 0
    assert sorted(e["name"] for e in result["entries"]) == ["Mystery Thing", "Nopic Hundred"]


def test_fix_pictures_refuses_a_height_that_is_not_standard(conn):
    with pytest.raises(ValueError):
        fr.fix_pictures(conn, [], 100)


def test_legacy_mark_is_current_only_at_the_same_height(conn):
    for name, legacy_height in (("Legacy Plugin", 64), ("Legacy Two", 128)):
        fst = fst_at("Acme", name)
        pic = fr.picture_for_host_path(HOST[name])
        make_thumb(fst, legacy_height, mark=stamp_of(HOST[name]))  # written before the sizes: no "@height"
        pics = fr._Pictures(PLUGIN_DB.resolve(), True, False, 64)
        pics.place(fst, pic)
        if legacy_height == 64:
            assert pics.counts["current"] == 1
        else:
            assert pics.counts["updated"] == 1
            assert fr._png_info(fst.with_suffix(".png"))[1] == f"{stamp_of(HOST[name])}@64"


# ---------- apply at a chosen size ----------

def test_apply_at_64_then_the_second_run_finds_the_thumbnails_current(conn):
    fst = fst_at("Applyco", "Apply Plugin")
    first = fr.apply(conn, "vendor", sections=["Effects"], scope="full", thumb_height=64)
    assert first["Effects"]["pictures"]["added"] >= 1
    size, mark = fr._png_info(fst.with_suffix(".png"))
    assert size[1] == 64 and mark == f"{stamp_of(HOST['Apply Plugin'])}@64"

    second = fr.apply(conn, "vendor", sections=["Effects"], scope="full", thumb_height=64)
    pics = second["Effects"]["pictures"]
    assert pics["added"] == 0 and pics["updated"] == 0 and pics["current"] >= 1


def test_apply_refuses_a_height_that_is_not_standard(conn):
    with pytest.raises(ValueError):
        fr.apply(conn, "vendor", thumb_height=100)


# ---------- manual links ----------

def test_fl_links_helpers_are_case_insensitive_and_cascade_with_the_plugin(conn):
    target = pid(conn, "Pro-L 2")
    db.set_fl_link(conn, "Helper Test Name", target)
    assert db.fl_links(conn)["helper test name"] == target
    db.set_fl_link(conn, "HELPER TEST NAME", None)  # same row: NOCASE
    assert "helper test name" not in db.fl_links(conn)

    conn.execute("INSERT INTO plugins (name, normalized_name, format, host_path, scanned_at) "
                 "VALUES ('Gone Soon', 'gone soon', 'VST3', 'C:\\VST\\Gone Soon.vst3', '2026-10-20')")
    gone = pid(conn, "Gone Soon")
    db.set_fl_link(conn, "Gone Soon FL", gone)
    conn.execute("DELETE FROM plugins WHERE id=?", (gone,))
    assert "gone soon fl" not in db.fl_links(conn)


def test_a_link_makes_an_unrecognized_fst_recognized_and_survives_a_new_connection(conn):
    fst_at("Linkco", "Odd FL Name (x64)")
    before = check_entry(conn, "Odd FL Name (x64)")
    assert before["plugin_id"] is None and before["linked"] is False

    target = pid(conn, "Linked Plugin")
    (e,) = fr.set_link(conn, "Odd FL Name (x64)", target)["entries"]
    conn.commit()
    assert e["plugin_id"] == target and e["plugin_name"] == "Linked Plugin"
    assert e["linked"] is True and e["vendor"] == "Linkco"

    row = fr._plugin_for_stem(conn, "Odd FL Name (x64)", fr._build_name_index(conn), fr._waves_index(conn))
    assert row["id"] == target
    with db.connect(DB_PATH) as fresh:
        assert db.fl_links(fresh)["odd fl name (x64)"] == target


def test_a_new_entry_is_matched_through_its_link(conn):
    new = INSTALLED / "Effects" / "New"
    new.mkdir(parents=True, exist_ok=True)
    (new / "Odd Import.nfo").write_text(
        "ps_name=Odd Import Name\nps_files=1\nps_file_filename_0=C:\\nowhere\\odd.vst3\nps_file_type_0=1\n",
        encoding="utf-8")
    (new / "Odd Import.fst").write_bytes(b"FST")
    try:
        def entry():
            entries = fr.propose(conn, "vendor")["Effects"]["entries"]
            return next(x for x in entries if x["key"].lower() == "odd import")

        assert entry()["matched"] is False  # nothing matches its path or its name
        db.set_fl_link(conn, "Odd Import", pid(conn, "Import Target"))
        e = entry()
        assert e["matched"] is True and e["plugin_id"] == pid(conn, "Import Target")
        assert e["destinations"] == ["Effects/Importco"]
    finally:
        db.set_fl_link(conn, "Odd Import", None)
        shutil.rmtree(new, ignore_errors=True)


def test_link_candidates_rank_the_closest_plugin_first(conn):
    got = fr.link_candidates(conn, "FabFilter Pro-Q 3 (x64)")
    assert got[0]["name"] == "Pro-Q 3" and got[0]["format"] == "VST3"
    assert all(c["format"] != "AAX" for c in got)  # AAX can't be loaded by FL
    scores = [c["score"] for c in got]
    assert scores == sorted(scores, reverse=True)
    assert all(0.35 <= s <= 1.0 for s in scores)
    assert set(got[0]) == {"id", "name", "vendor", "format", "host_path", "score", "has_picture", "ignored"}
    assert fr.link_candidates(conn, "(x64)") == []  # nothing left once the bracket and bitness are dropped


# ---------- the endpoints (app/web.py) ----------

def test_inventory_endpoint_has_the_new_fields(client):
    c, h = client
    r = c.get("/api/fl-sync/inventory", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "can_standardize" in body["counts"] and "linked" in body["counts"]
    assert all({"key", "size_name", "can_standardize", "linked"} <= set(e) for e in body["entries"])


def test_fix_pictures_endpoint(client, conn):
    c, h = client
    fst = fst_at("Acme", "Fix Me")
    assert c.post("/api/fl-sync/fix-pictures", json={"fst": [str(fst)], "size": 64}).status_code == 403
    assert c.post("/api/fl-sync/fix-pictures", json={"fst": [str(fst)], "size": 100}, headers=h).status_code == 422
    r = c.post("/api/fl-sync/fix-pictures", json={"fst": [str(fst), str(EFFECTS / "Acme" / "Ghost.fst")],
                                                  "size": 64}, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["fixed"] == 1
    assert body["skipped"][0]["reason"] == "file not found"
    assert fr._png_info(fst.with_suffix(".png"))[0][1] == 64


def test_link_endpoint_links_unlinks_and_validates(client, conn):
    c, h = client
    fst_at("Linkco", "Endpoint Link")
    assert c.post("/api/fl-sync/link", json={"name": "  ", "plugin_id": None}, headers=h).status_code == 400
    ghost = c.post("/api/fl-sync/link", json={"name": "Ghost Name", "plugin_id": 999999}, headers=h)
    assert ghost.status_code == 404

    r = c.post("/api/fl-sync/link", json={"name": "Endpoint Link", "plugin_id": pid(conn, "Linked Plugin"),
                                          "fix_picture": True, "size": 256}, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["fixed"] == 0  # Linked Plugin has no VEXT picture
    assert body["entries"][0]["linked"] is True and body["entries"][0]["plugin_id"] == pid(conn, "Linked Plugin")

    r = c.post("/api/fl-sync/link", json={"name": "Endpoint Link", "plugin_id": None}, headers=h)
    assert r.status_code == 200 and r.json()["entries"][0]["linked"] is False


def test_link_candidates_endpoint(client):
    c, _h = client
    r = c.get("/api/fl-sync/link-candidates", params={"name": "FabFilter Pro-Q 3 (x64)"})
    assert r.status_code == 200, r.text
    assert r.json()["candidates"][0]["name"] == "Pro-Q 3"


def test_apply_endpoint_validates_the_thumbnail_size(client):
    c, h = client
    r = c.post("/api/fl-sync/apply", json={"mode": "vendor", "thumb_size": 100}, headers=h)
    assert r.status_code == 422
    r = c.post("/api/fl-sync/apply", json={"mode": "vendor", "sections": ["Generators"], "thumb_size": 64},
               headers=h)
    assert r.status_code == 200, r.text
    assert "Generators" in r.json()


# ---------- Fix all pictures, Remove ----------

def test_fix_all_pictures_writes_every_fixable_one_and_leaves_current_ones(conn):
    fst = fst_at("Allco", "Odd Hundred")
    make_thumb(fst, 100)                                   # foreign, 100 px: replaced (backed up)
    unknown = fst_at("Allco", "Nobody Knows This")         # not recognized: counted, untouched
    first = fr.fix_all_pictures(conn, 128)
    assert first["written"] >= 1 and first["unknown"] >= 1
    assert first["backup"] and (Path(first["backup"]) / "Effects" / "Allco" / "Odd Hundred.png").is_file()
    assert Image.open(fst.with_suffix(".png")).size[1] == 128
    assert not unknown.with_suffix(".png").exists()
    again = fr.fix_all_pictures(conn, 128)
    assert again["written"] == 0 and again["current"] >= 1
    with pytest.raises(ValueError):
        fr.fix_all_pictures(conn, 100)


def test_remove_moves_an_unrecognized_entry_with_its_files_out_of_the_browse_tree(conn):
    fst = fst_at("Gone", "Mystery Synth")
    png = make_thumb(fst, 128)
    other = fst_at("Gone", "Mystery Twin")
    fr._write_bitmap_nfo(other.with_suffix(".nfo"), png.name)   # shares the picture: it stays
    r = fr.remove_entries(conn, [str(fst)])
    assert r["removed"] == [str(fst.resolve())] and not r["skipped"]
    moved = Path(r["folder"]) / "Effects" / "Gone"
    assert (moved / "Mystery Synth.fst").is_file() and (moved / "Mystery Synth.nfo").is_file()
    assert not fst.exists() and not fst.with_suffix(".nfo").exists()
    assert png.exists() and not (moved / png.name).exists()      # still used by Mystery Twin
    assert all(e["name"] != "Mystery Synth" for e in fr.inventory(conn)["entries"])


def test_remove_refuses_a_recognized_or_linked_plugin_and_bad_paths(conn):
    known = fst_at("Keep", "Fix Me")
    linked = fst_at("Keep", "Linked By Hand")
    db.set_fl_link(conn, "Linked By Hand", pid(conn, "Linked Plugin"))
    r = fr.remove_entries(conn, [str(known), str(linked), str(INSTALLED / "x.fst"), r"C:\Windows\notepad.exe"])
    assert not r["removed"] and r["folder"] is None and len(r["skipped"]) == 4
    assert "recognizes it as Fix Me" in r["skipped"][0]["reason"]
    assert known.exists() and linked.exists()


def test_fix_all_and_remove_endpoints(client, conn):
    c, headers = client
    r = c.post("/api/fl-sync/fix-all-pictures", json={"size": 64}, headers=headers)
    assert r.status_code == 200 and {"written", "current", "unknown", "no_picture", "failed"} <= r.json().keys()
    assert c.post("/api/fl-sync/fix-all-pictures", json={"size": 100}, headers=headers).status_code == 422
    fst = fst_at("Gone", "Endpoint Orphan")
    r = c.post("/api/fl-sync/remove", json={"fst": [str(fst)]}, headers=headers)
    assert r.status_code == 200 and r.json()["removed"] and not fst.exists()


# ---------- Ignore, one backup folder per day, Not filed -> add ----------

def test_ignore_takes_an_unrecognized_entry_out_of_the_problems_and_back(conn):
    fst = fst_at("Ign", "Totally Unknown Thing")
    e = check_entry(conn, "Totally Unknown Thing")
    assert e["plugin_id"] is None and e["issues"] and not e["ignored"]
    before = fr.inventory(conn)["counts"]
    entries = fr.set_ignored(conn, "totally unknown thing", True)
    assert entries and entries[0]["ignored"] and entries[0]["issues"] == []
    after = fr.inventory(conn)["counts"]
    assert after["unknown"] == before["unknown"] - 1 and after["ignored"] == before["ignored"] + 1
    assert fr.set_ignored(conn, "Totally Unknown Thing", False)[0]["ignored"] is False
    fst.unlink()


def test_backups_go_to_one_folder_per_day_and_keep_the_first_copy(conn):
    fst = fst_at("Daily", "Fix Me")
    make_thumb(fst, 100)                                       # foreign: backed up
    r1 = fr.fix_pictures(conn, [str(fst)], 128)
    make_thumb(fst, 90)                                        # foreign again, later the same day
    r2 = fr.fix_pictures(conn, [str(fst)], 128)
    assert r1["backup"] == r2["backup"] and r1["backup"].endswith(fr.datetime.now(fr.timezone.utc).strftime("%Y%m%d"))
    kept = Path(r1["backup"]) / "Effects" / "Daily" / "Fix Me.png"
    assert Image.open(kept).size[1] == 100                     # the first (original) copy stays


def test_add_detected_copies_fl_s_fst_into_the_vendor_folder_with_a_picture(conn):
    src_dir = INSTALLED / "Effects" / "VST3"
    src_dir.mkdir(parents=True, exist_ok=True)
    nfo = src_dir / "Apply Plugin.nfo"
    nfo.write_text("ps_name=Apply Plugin\n", encoding="utf-8")
    (src_dir / "Apply Plugin.fst").write_bytes(b"FL-FST")
    dest = EFFECTS / "Applyco" / "Apply Plugin.fst"
    for f in (dest, dest.with_suffix(".png"), dest.with_suffix(".nfo")):   # an earlier test filed it
        if f.exists():
            fr._set_hidden(f, False)
            f.unlink()
    stale = src_dir / "Ghost.nfo"
    stale.write_text("ps_name=Ghost\n", encoding="utf-8")       # no .fst beside it
    r = fr.add_detected(conn, [{"plugin_id": pid(conn, "Apply Plugin"), "nfo": str(nfo)},
                               {"plugin_id": pid(conn, "Fix Me"), "nfo": str(stale)},
                               {"plugin_id": pid(conn, "Fix Me"), "nfo": r"C:\Windows\win.ini"}], 64)
    assert len(r["added"]) == 1 and len(r["skipped"]) == 2, r["skipped"]
    assert dest.read_bytes() == b"FL-FST" and (src_dir / "Apply Plugin.fst").exists()   # copied, FL's own stays
    assert Image.open(dest.with_suffix(".png")).size[1] == 64
    assert "Plugin Manager" in r["skipped"][0]["reason"]
    # FL's name differs from VEXT's: the copied entry is linked, so it is recognized.
    other = src_dir / "Applyco Thing.nfo"
    other.write_text("ps_name=Applyco Thing\n", encoding="utf-8")
    (src_dir / "Applyco Thing.fst").write_bytes(b"FL-FST")
    r3 = fr.add_detected(conn, [{"plugin_id": pid(conn, "Import Target"), "nfo": str(other)}], 64)
    assert r3["added"][0]["plugin_id"] == pid(conn, "Import Target") and r3["added"][0]["linked"]
    again = fr.add_detected(conn, [{"plugin_id": pid(conn, "Apply Plugin"), "nfo": str(nfo)}], 64)
    assert not again["added"] and "already in" in again["skipped"][0]["reason"]


def test_ignore_and_add_detected_endpoints(client, conn):
    c, headers = client
    fst_at("Ign", "Endpoint Unknown")
    r = c.post("/api/fl-sync/ignore", json={"name": "Endpoint Unknown"}, headers=headers)
    assert r.status_code == 200 and r.json()["entries"][0]["ignored"]
    assert c.post("/api/fl-sync/ignore", json={"name": " "}, headers=headers).status_code == 400
    r = c.post("/api/fl-sync/add-detected", json={"items": [], "size": 128}, headers=headers)
    assert r.status_code == 200 and r.json() == {"added": [], "skipped": []}


# ---------- Resize: the same image, a standard size ----------

def test_resize_keeps_the_image_backs_up_a_foreign_one_and_keeps_a_vext_mark(conn):
    fst = fst_at("Resize", "Nopic Hundred")
    png = make_thumb(fst, 100)                                      # FL's own, no VEXT picture exists
    r = fr.resize_pictures(conn, [str(fst)], 64)
    assert r["resized"] == 1 and not r["skipped"] and r["backup"]
    with Image.open(png) as im:
        assert im.size == (128, 64)                                 # 200x100 -> 128x64, aspect kept
    assert (Path(r["backup"]) / "Effects" / "Resize" / png.name).is_file()
    assert r["entries"][0]["issues"] == [] and r["entries"][0]["size_name"] == "Compact"
    again = fr.resize_pictures(conn, [str(fst)], 64)
    assert again["resized"] == 0 and again["current"] == 1
    vfst = fst_at("Resize", "Fix Me")
    fr.fix_pictures(conn, [str(vfst)], 256)                         # VEXT's thumbnail, marked @256
    fr.resize_pictures(conn, [str(vfst)], 128)
    assert fr._png_info(vfst.with_suffix(".png"))[1].endswith("@128")
    assert fr.resize_pictures(conn, [str(fst_at("Resize", "No Thumb At All"))], 64)["skipped"][0]["reason"] == "no picture to resize"


def test_resize_all_and_its_endpoint(client, conn):
    r = fr.resize_all_pictures(conn, 128)
    assert r["resized"] >= 1 and r["failed"] == 0
    assert fr.resize_all_pictures(conn, 128)["resized"] == 0
    assert all(e["size"] is None or e["size"][1] == 128 for e in fr.inventory(conn)["entries"])
    c, headers = client
    r = c.post("/api/fl-sync/resize-all-pictures", json={"size": 64}, headers=headers)
    assert r.status_code == 200 and r.json()["resized"] >= 1
    assert c.post("/api/fl-sync/resize-pictures", json={"fst": [], "size": 100}, headers=headers).status_code == 422


# ---------- Link dialog: "Include ignored plugins" ----------

def test_ignored_plugins_are_link_candidates_only_when_asked(conn):
    db.add_ignored_plugin_path(conn, r"C:\VST\Hidden\Pro-Q 3 Old.vst3", "Pro-Q 3 Old")
    conn.commit()
    plain = fr.link_candidates(conn, "Pro-Q 3 Old")
    assert all(not c["ignored"] for c in plain)
    both = fr.link_candidates(conn, "Pro-Q 3 Old", include_ignored=True)
    hit = next(c for c in both if c["ignored"])
    assert hit["id"] is None and hit["format"] == "VST3" and hit["host_path"].endswith("Pro-Q 3 Old.vst3")
    assert [p["name"] for p in fr.ignored_plugins(conn, "q 3 old")] == ["Pro-Q 3 Old"]


def test_linking_an_ignored_plugin_that_is_gone_says_so(client, conn):
    c, headers = client
    r = c.post("/api/fl-sync/link", json={"name": "Whatever", "ignored_host_path": r"C:\Nope\Gone.vst3"}, headers=headers)
    assert r.status_code == 409 and "isn't there any more" in r.json()["detail"]
    r = c.get("/api/fl-sync/link-candidates", params={"name": "Pro-Q 3 Old", "include_ignored": "true"})
    assert any(x["ignored"] for x in r.json()["candidates"])
    assert c.get("/api/fl-sync/ignored-plugins", params={"q": "old"}).json()["plugins"]


def test_catalog_one_brings_an_ignored_plugin_back(conn, tmp_path):
    from app import scan_service
    from app.config import ScanRoot

    folder = tmp_path / "VST3"
    folder.mkdir()
    plugin = folder / "Brought Back.vst3"
    plugin.write_bytes(b"not really a plugin")         # a flat .vst3: read like a scan reads it
    db.add_ignored_plugin_path(conn, str(plugin), "Brought Back")
    conn.commit()
    root = ScanRoot(str(folder), str(folder), "VST3", "effect", (".vst3",))
    orig = db.scan_roots_as_objects
    db.scan_roots_as_objects = lambda _conn: [root]
    try:
        plugin_id = scan_service.catalog_one(conn, str(plugin))
    finally:
        db.scan_roots_as_objects = orig
    row = db.get_plugin(conn, plugin_id)
    assert row["host_path"].lower() == str(plugin).lower()
    assert str(plugin).lower() not in db.ignored_plugin_host_paths(conn)
