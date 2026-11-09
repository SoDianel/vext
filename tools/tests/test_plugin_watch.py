"""Tests for the plugin folder watcher's decision (app/plugin_watch.py), Browse's one row per
plugin (app/dedup.py unify_groups, app/web.py ?unify=1), the AAX / Pro Tools rule
(app/scan_service.py apply_aax_rule) and the opt-in obsolete-format suggestions.

Everything runs in a temporary VEXT_ROOT with fake plugin folders. Never the real plugin
folders or the real VEXT data.

Run:  .venv-dev\\Scripts\\python.exe -m pytest tools/tests/test_plugin_watch.py -q
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
WORK = tempfile.mkdtemp(prefix="vext-watch-test-")
os.environ["VEXT_ROOT"] = WORK  # set before app.config is imported

import pytest

from app import db, dedup, plugin_watch, scan_service, scanner
from app.config import DB_PATH, PROJECT_ROOT, ScanRoot

assert Path(PROJECT_ROOT).resolve() == Path(WORK).resolve(), "app.config was imported before VEXT_ROOT was set"

VST3 = Path(WORK) / "plugins" / "VST3"

# name, vendor, format, host_path, bitness
PLUGINS = [
    ("Equalizer Random", "Fake Vendor Name", "VST3", r"C:\Common Files\VST3\Equalizer Random.vst3", 64),
    ("Equalizer Random", "Fake Vendor Name", "CLAP", r"C:\Common Files\CLAP\Equalizer Random.clap", 64),
    ("Equalizer Random", "Fake Vendor Name", "AAX", r"C:\Common Files\Avid\Audio\Plug-Ins\Equalizer Random.aaxplugin", 64),
    # The classic VstPlugins folder ("VST") and Common Files\VST2 ("VST2"): the same format twice.
    ("EqualizerRandom_x64", None, "VST2", r"C:\Program Files\VstPlugins\EqualizerRandom_x64.dll", 64),
    ("Fake Vendor Name Equalizer Random", "Fake Vendor Name Ltd", "VST2",
     r"C:\Program Files\Common Files\VST2\Equalizer Random.dll", 64),
    ("Equalizer Random", "Fake Vendor Name", "VST3", r"C:\Program Files (x86)\Common Files\VST3\Equalizer Random.vst3", 32),
    # Same name, another vendor: a different plugin.
    ("Equalizer Random", "Other Company", "VST3", r"C:\Common Files\VST3\Other\Equalizer Random.vst3", 64),
]


@pytest.fixture(scope="module")
def conn():
    db.init_db(DB_PATH)
    with db.connect(DB_PATH) as c:
        for name, vendor, fmt, host, bits in PLUGINS:
            c.execute(
                "INSERT INTO plugins (name, normalized_name, vendor, format, host_path, bitness, status, scanned_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'ok', '2026-11-07T00:00:00+00:00')",
                (name, scanner.normalize_name(name), vendor, fmt, host, bits),
            )
        c.commit()
        yield c


def ids(conn, vendor=None):
    rows = conn.execute("SELECT id, vendor FROM plugins").fetchall()
    return {i for i, v in rows if vendor is None or v == vendor}


# ---------- names and groups ----------

@pytest.mark.parametrize("name", ["Equalizer Random (x64)", "EqualizerRandom_x64.dll", "Equalizer Random VST2",
                                  "Equalizer Random [AAX]", "Equalizer Random.vst3", "Equalizer_Random-CLAP"])
def test_format_and_bitness_tags_are_stripped_from_names(name):
    assert scanner.normalize_name(name) == "equalizerrandom"


def test_every_copy_of_one_plugin_is_one_group_and_another_vendor_is_not(conn):
    rows = db.all_plugins(conn)
    other = next(r["id"] for r in rows if r["vendor"] == "Other Company")
    bare = next(r["id"] for r in rows if not r["vendor"])
    groups = dedup.unify_groups(rows)
    named = {r["id"] for r in rows if r["id"] not in (other, bare)}
    assert len({groups[i] for i in named}) == 1
    assert groups[other] not in {groups[i] for i in named}
    # Two vendors share the name: the copy without a vendor isn't guessed into either.
    assert groups[bare] not in {groups[i] for i in named} | {groups[other]}

    # One vendor for the name: the copy without a vendor joins it.
    alone = dedup.unify_groups([r for r in rows if r["id"] != other])
    assert len({alone[i] for i in named | {bare}}) == 1


def test_vst_and_vst2_folders_get_their_own_labels(conn):
    labels = {r["host_path"]: dedup.format_label(r) for r in db.all_plugins(conn)}
    assert labels[r"C:\Program Files\VstPlugins\EqualizerRandom_x64.dll"] == "VST"
    assert labels[r"C:\Program Files\Common Files\VST2\Equalizer Random.dll"] == "VST2"


def test_proposals_still_need_a_vendor_on_both_sides_and_never_replace_aax(conn):
    rows = db.all_plugins(conn)
    superseded = {c["superseded"]["host_path"]: c["keeper"]["format"] for c in dedup.superseded_candidates(rows)}
    assert r"C:\Program Files\VstPlugins\EqualizerRandom_x64.dll" not in superseded  # no vendor: display only
    assert superseded[r"C:\Program Files\Common Files\VST2\Equalizer Random.dll"] == "CLAP"  # vendor prefix stripped
    assert not any("aaxplugin" in p for p in superseded)
    assert superseded[r"C:\Program Files (x86)\Common Files\VST3\Equalizer Random.vst3"] == "CLAP"


def test_browse_lists_one_row_with_every_copy(conn):
    from fastapi.testclient import TestClient

    from app import web
    conn.execute("DELETE FROM plugins WHERE vendor='Other Company'")  # the VST copy's vendor is unambiguous now
    conn.commit()
    rows = TestClient(web.app, base_url="http://127.0.0.1").get("/api/plugins", params={"q": "equalizer", "unify": "1"}).json()
    assert len(rows) == 1
    ours = rows[0]
    assert ours["format"] == "CLAP"  # the best copy is the row
    labels = [c["label"] + (" x86" if c["bitness"] == 32 else "") for c in ours["copies"]]
    assert labels == ["VST", "VST2", "VST3", "VST3 x86", "AAX", "CLAP"]

    vst2_only = TestClient(web.app, base_url="http://127.0.0.1").get("/api/plugins", params={"q": "equalizer", "format": "VST2", "unify": "1"}).json()
    assert len(vst2_only) == 1 and len(vst2_only[0]["copies"]) == 6


# ---------- AAX and Pro Tools ----------

def test_aax_is_flagged_only_without_pro_tools(conn):
    aax = conn.execute("SELECT id FROM plugins WHERE format='AAX'").fetchone()[0]

    def state():
        return tuple(conn.execute("SELECT status, status_reason FROM plugins WHERE id=?", (aax,)).fetchone())

    assert scan_service.apply_aax_rule(conn, pro_tools=False) == 1
    assert state() == ("needs_review", scan_service.AAX_REVIEW_REASON)
    assert scan_service.apply_aax_rule(conn, pro_tools=False) == 0  # idempotent
    assert scan_service.apply_aax_rule(conn, pro_tools=True) == 1
    assert state() == ("ok", None)

    conn.execute("UPDATE plugins SET status='corrupted', status_reason='PE parse failed' WHERE id=?", (aax,))
    scan_service.apply_aax_rule(conn, pro_tools=False)
    assert state() == ("corrupted", "PE parse failed; " + scan_service.AAX_REVIEW_REASON)
    scan_service.apply_aax_rule(conn, pro_tools=True)
    assert state() == ("corrupted", "PE parse failed")


def test_pro_tools_folder_needs_its_exe(tmp_path):
    from app.config import detect_pro_tools_root, pro_tools_installed
    assert not pro_tools_installed(str(tmp_path))
    (tmp_path / "ProTools.exe").write_bytes(b"MZ")
    assert pro_tools_installed(str(tmp_path))
    assert detect_pro_tools_root(str(tmp_path)) == (str(tmp_path), "manual")


# ---------- obsolete-format suggestions are opt-in ----------

def test_duplicates_are_listed_only_when_suggestions_are_on(conn):
    from fastapi.testclient import TestClient

    from app import web
    client = TestClient(web.app, base_url="http://127.0.0.1")
    db.set_setting(conn, scan_service.SUGGEST_OBSOLETE_KEY, "0")
    conn.commit()
    assert client.get("/api/dedup").json() == []
    db.set_setting(conn, scan_service.SUGGEST_OBSOLETE_KEY, "1")
    conn.commit()
    assert len(client.get("/api/dedup").json()) >= 2
    db.set_setting(conn, scan_service.SUGGEST_OBSOLETE_KEY, "0")
    conn.commit()


# ---------- the watcher's decision ----------

def _root():
    return ScanRoot(container_path=str(VST3), host_path=str(VST3), format="VST3", default_type="unknown",
                    extensions=(".vst3",), recurse_vendor_dirs=True)


def test_listing_sees_plugins_only_and_needs_scan_follows_the_catalog():
    (VST3 / "Acme").mkdir(parents=True)
    (VST3 / "Acme" / "One.vst3").mkdir()
    (VST3 / "readme.txt").write_text("not a plugin")
    (VST3 / "WaveShell1-VST3 15.5_x64.vst3").mkdir()  # Waves' shared host: never a catalog entry
    root = _root()
    before = plugin_watch._listing([root])
    key = str(VST3).lower()
    one = str(VST3 / "Acme" / "One.vst3").lower()
    assert set(before[key]) == {one}

    # An install: a plugin the catalog doesn't know.
    (VST3 / "Two.vst3").mkdir()
    after = plugin_watch._listing([root])
    two = str(VST3 / "Two.vst3").lower()
    assert plugin_watch.needs_scan(before, after, known={one}, ignored=set(), quarantined=set())
    # Already cataloged (VEXT's own move or restore) or ignored: no scan.
    assert not plugin_watch.needs_scan(before, after, known={one, two}, ignored=set(), quarantined=set())
    assert not plugin_watch.needs_scan(before, after, known={one}, ignored={two}, quarantined=set())

    # An uninstall of a cataloged plugin, versus VEXT's own quarantine move.
    gone = {key: {}}
    assert plugin_watch.needs_scan(before, gone, known={one}, ignored=set(), quarantined=set())
    assert not plugin_watch.needs_scan(before, gone, known={one}, ignored=set(), quarantined={one})
    assert not plugin_watch.needs_scan(before, gone, known=set(), ignored=set(), quarantined=set())

    # An update: same path, new modification time.
    updated = {key: {one: before[key][one] + 1}}
    assert plugin_watch.needs_scan(before, updated, known={one}, ignored=set(), quarantined=set())

    # A scan folder only in one listing (just added in Configuration) isn't an install.
    assert not plugin_watch.needs_scan({}, after, known=set(), ignored=set(), quarantined=set())


def test_scan_root_still_reads_the_same_entries_as_the_listing():
    root = _root()
    listed = {p.lower() for p, _ in scanner.plugin_entries(root)}
    scanned = {r["host_path"].lower() for r in scanner.scan_root(root)}
    assert listed == scanned
