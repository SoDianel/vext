"""Tests for Database > Metadata (app/metadata_archive.py): researched data kept when a plugin or vendor
goes away, given back when it returns, listed when nothing owns it, and cleared on request. Everything runs
in a temporary VEXT_ROOT with its own database and pictures; never the real ones.

Run:  .venv-dev\\Scripts\\python.exe -m pytest tools/test_metadata_archive.py -q
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
WORK = Path(tempfile.mkdtemp(prefix="vext-metadata-test-"))
os.environ["VEXT_ROOT"] = str(WORK / "app")            # set before app.config is imported

import pytest

from app import db, metadata_archive, plugin_host
from app.config import PROJECT_ROOT

assert Path(PROJECT_ROOT).resolve() == (WORK / "app").resolve(), "app.config was imported before VEXT_ROOT was set"

PATH = r"C:\Program Files\Common Files\VST3\Fake Delay.vst3"
MOVED = r"C:\Program Files\Common Files\VST3\Fake Vendor\Fake Delay.vst3"


@pytest.fixture()
def conn(tmp_path):
    # Pictures live in one shared folder: start every test without the last test's files.
    shutil.rmtree(plugin_host.IMAGES_DIR, ignore_errors=True)
    path = str(tmp_path / "plugins.db")
    db.init_db(path)
    with db.connect(path) as c:
        metadata_archive.ensure_table(c)
        yield c


def add_plugin(c, host_path, name="Fake Delay", fmt="VST3", vendor="Fake Vendor"):
    c.execute(
        "INSERT INTO plugins (name, normalized_name, vendor, format, host_path, scanned_at) VALUES (?, ?, ?, ?, ?, 'now')",
        (name, name.lower(), vendor, fmt, host_path))
    return c.execute("SELECT id FROM plugins WHERE host_path=?", (host_path,)).fetchone()[0]


def research(c, pid):
    c.execute("UPDATE plugins SET store_url='https://store.example/delay', kvr_url='https://kvr.example/delay', "
              "no_equipboard=1, description='A fake delay.', name='Fake Delay Pro', name_source='manual' WHERE id=?", (pid,))


def picture(host_path) -> Path:
    p = Path(plugin_host.image_path(host_path))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x89PNG fake")
    return p


def uninstall(c, keep=("C:\\other.vst3",)):
    """What a scan does when the plugin is gone: keep, then remove."""
    metadata_archive.archive_missing_plugins(c, set(keep))
    db.remove_stale_plugins(c, set(keep))


def test_reinstall_same_path_gets_everything_back(conn):
    research(conn, add_plugin(conn, PATH))
    uninstall(conn)
    assert conn.execute("SELECT COUNT(*) FROM plugins").fetchone()[0] == 0
    items = metadata_archive.orphans(conn)["items"]
    assert [i["kind"] for i in items] == ["plugin"]
    assert {"store link", "KVR link", "description", "name edit", "checked: no page"} <= set(items[0]["fields"])

    pid = add_plugin(conn, PATH)              # scanned again: a fresh row, nothing researched
    assert metadata_archive.restore_plugins(conn) == 1
    row = conn.execute("SELECT * FROM plugins WHERE id=?", (pid,)).fetchone()
    assert row["store_url"] == "https://store.example/delay" and row["kvr_url"] == "https://kvr.example/delay"
    assert row["no_equipboard"] == 1 and row["description"] == "A fake delay."
    assert row["name"] == "Fake Delay Pro" and row["name_source"] == "manual"
    assert metadata_archive.orphans(conn)["items"] == []


def test_moved_plugin_matches_by_name_and_format(conn):
    research(conn, add_plugin(conn, PATH))
    uninstall(conn, keep=(MOVED,))
    pid = add_plugin(conn, MOVED)
    assert metadata_archive.restore_plugins(conn) == 1
    assert conn.execute("SELECT store_url FROM plugins WHERE id=?", (pid,)).fetchone()[0] == "https://store.example/delay"


def test_restore_never_overwrites_new_data(conn):
    research(conn, add_plugin(conn, PATH))
    uninstall(conn)
    pid = add_plugin(conn, PATH)
    conn.execute("UPDATE plugins SET store_url='https://new.example' WHERE id=?", (pid,))
    metadata_archive.restore_plugins(conn)
    row = conn.execute("SELECT store_url, kvr_url FROM plugins WHERE id=?", (pid,)).fetchone()
    assert row["store_url"] == "https://new.example" and row["kvr_url"] == "https://kvr.example/delay"


def test_ambiguous_match_stays_archived(conn):
    research(conn, add_plugin(conn, PATH))
    uninstall(conn, keep=("C:\\a\\Fake Delay.vst3", "C:\\b\\Fake Delay.vst3"))
    add_plugin(conn, "C:\\a\\Fake Delay.vst3")
    add_plugin(conn, "C:\\b\\Fake Delay.vst3")
    assert metadata_archive.restore_plugins(conn) == 0
    assert len(metadata_archive.orphans(conn)["items"]) == 1


def test_plugin_without_research_is_not_kept(conn):
    add_plugin(conn, PATH)
    uninstall(conn)
    assert metadata_archive.orphans(conn)["items"] == []


def test_vendor_links_survive_pruning(conn):
    conn.execute("INSERT INTO vendors (name, url, status, source, kvr_url) VALUES ('Gone Audio', 'https://gone.example', 'down', 'catalog', 'https://kvr.example/gone')")
    conn.execute("INSERT INTO vendors (name, url, status, source) VALUES ('Plain Catalog', 'https://plain.example', 'active', 'catalog')")
    assert metadata_archive.archive_unused_vendors(conn) == 1   # the plain catalog vendor comes back from the catalog anyway
    db.prune_unused_reference(conn)
    assert [i["name"] for i in metadata_archive.orphans(conn)["items"]] == ["Gone Audio"]
    conn.execute("INSERT INTO vendors (name, url, status, source) VALUES ('Gone Audio', 'https://gone.example', 'down', 'catalog')")
    assert metadata_archive.restore_vendors(conn) == 1
    assert conn.execute("SELECT kvr_url FROM vendors WHERE name='Gone Audio'").fetchone()[0] == "https://kvr.example/gone"


def test_clear_takes_the_picture_with_it(conn):
    research(conn, add_plugin(conn, PATH))
    pic = picture(PATH)
    uninstall(conn)
    item = metadata_archive.orphans(conn)["items"][0]
    assert "picture" in item["fields"]
    done = metadata_archive.clear(conn, [item["id"]])
    assert done["plugin"] == 1 and done["picture"] == 1 and not pic.exists()
    assert metadata_archive.orphans(conn)["items"] == []


def test_leftovers_listed_and_cleared(conn):
    stray = picture(r"C:\long gone\Old.dll")
    keep_pid = add_plugin(conn, PATH)
    own = picture(PATH)
    conn.execute("INSERT INTO investigation_attempts (target, item_key, attempted_at) VALUES ('links', '99999', 'now')")
    conn.execute("INSERT INTO investigation_attempts (target, item_key, attempted_at) VALUES ('links', ?, 'now')", (str(keep_pid),))
    items = {i["kind"]: i for i in metadata_archive.orphans(conn)["items"]}
    assert set(items) == {"picture", "attempts"}
    metadata_archive.clear(conn, [items["picture"]["id"], items["attempts"]["id"]])
    assert not stray.exists() and own.exists()          # an installed plugin's picture is never touched
    assert conn.execute("SELECT item_key FROM investigation_attempts").fetchall()[0][0] == str(keep_pid)


def test_unknown_item_is_refused(conn):
    with pytest.raises(ValueError):
        metadata_archive.clear(conn, ["nonsense:1"])


def test_owner_category_archived_when_unused_built_in_is_not(conn):
    db.seed_categories(conn)
    conn.execute("INSERT INTO categories (slug, name, description, source) VALUES ('my-cat', 'My Cat', 'Mine.', 'manual')")
    assert metadata_archive.archive_unused_categories(conn) == 1     # the seeded built-ins are not kept
    db.prune_unused_reference(conn)
    assert conn.execute("SELECT COUNT(*) FROM categories WHERE source='manual'").fetchone()[0] == 0
    assert [i["id"] for i in metadata_archive.orphans(conn)["items"]] == ["category:my-cat"]

    conn.execute("INSERT INTO categories (slug, name, description, source) VALUES ('my-cat', 'My Cat', '', 'manual')")
    assert metadata_archive.restore_categories(conn) == 1
    assert conn.execute("SELECT description FROM categories WHERE slug='my-cat'").fetchone()[0] == "Mine."
    assert metadata_archive.orphans(conn)["items"] == []


def test_category_in_use_is_not_archived(conn):
    pid = add_plugin(conn, PATH)
    conn.execute("INSERT INTO categories (slug, name, description, source) VALUES ('in-use', 'In Use', 'x', 'manual')")
    db.set_plugin_categories_manual(conn, pid, ["in-use"])
    assert metadata_archive.archive_unused_categories(conn) == 0


def test_returning_plugin_brings_its_owner_category_back(conn):
    pid = add_plugin(conn, PATH)
    conn.execute("INSERT INTO categories (slug, name, description, source) VALUES ('my-cat', 'My Cat', 'Mine.', 'manual')")
    db.set_plugin_categories_manual(conn, pid, ["my-cat"])
    uninstall(conn)                                     # the plugin's links go with it
    metadata_archive.archive_unused_categories(conn)    # what a scan does after the removal
    db.prune_unused_reference(conn)
    assert db.category_id_by_slug(conn, "my-cat") is None
    assert {i["id"] for i in metadata_archive.orphans(conn)["items"]} == {f"plugin:{PATH.lower()}", "category:my-cat"}

    pid = add_plugin(conn, PATH)
    assert metadata_archive.restore_plugins(conn) == 1
    assert conn.execute("SELECT source FROM categories WHERE slug='my-cat'").fetchone()[0] == "manual"
    assert [r[0] for r in conn.execute(
        "SELECT c.slug FROM plugin_categories pc JOIN categories c ON c.id = pc.category_id WHERE pc.plugin_id=?",
        (pid,))] == ["my-cat"]
    assert metadata_archive.orphans(conn)["items"] == []


def test_listing_statuses(conn):
    gone = r"C:\VST\Gone.vst3"
    research(conn, add_plugin(conn, gone, name="Gone"))
    research(conn, add_plugin(conn, PATH))
    own = picture(PATH)                                  # an installed plugin's picture
    add_plugin(conn, MOVED, name="Plain")                # installed, nothing researched: not listed
    stray = picture(r"C:\long gone\Old.dll")             # a picture nothing owns
    conn.execute("INSERT INTO investigation_attempts (target, item_key, attempted_at) VALUES ('links', '99999', 'now')")
    uninstall(conn, keep=(PATH, MOVED))

    result = metadata_archive.listing(conn)
    items = {i["id"]: i for i in result["items"]}
    pid = conn.execute("SELECT id FROM plugins WHERE host_path=?", (PATH,)).fetchone()[0]
    assert items[f"installed:{pid}"]["status"] == "installed" and items[f"installed:{pid}"]["orphan"] is False
    assert "picture" in items[f"installed:{pid}"]["fields"]
    assert items[f"plugin:{gone.lower()}"]["status"] == "no_plugin" and items[f"plugin:{gone.lower()}"]["orphan"]
    assert items[f"picture:{stray.stem}"]["status"] == "image_only" and items[f"picture:{stray.stem}"]["bytes"] > 0
    assert f"picture:{own.stem}" not in items
    assert [i["status"] for i in result["items"] if i["status"] == "stale"] == ["stale"]
    assert result["counts"] == {"installed": 1, "no_plugin": 1, "image_only": 1, "stale": 1}
    assert len(items) == 4


def test_clear_installed_resets_research_but_keeps_picture(conn):
    pid = add_plugin(conn, PATH)
    research(conn, pid)
    conn.execute("UPDATE plugins SET vendor_source='manual', plugin_type_source='manual' WHERE id=?", (pid,))
    db.set_secondary_vendors(conn, pid, ["Other Co"])
    conn.execute("INSERT INTO categories (slug, name, description, source) VALUES ('my-cat', 'My Cat', '', 'manual')")
    db.set_plugin_categories_manual(conn, pid, ["my-cat"])
    pic = picture(PATH)

    done = metadata_archive.clear(conn, [f"installed:{pid}"])
    assert done["installed"] == 1 and pic.exists() and done["picture"] == 0
    row = conn.execute("SELECT * FROM plugins WHERE id=?", (pid,)).fetchone()
    assert row["store_url"] is None and row["kvr_url"] is None and row["description"] is None
    assert row["no_equipboard"] == 0
    assert row["name_source"] == "scan" and row["vendor_source"] == "scan" and row["plugin_type_source"] == "scan"
    assert row["name"] == "Fake Delay Pro"               # the value stays until the next scan puts the scanned one back
    assert db.list_secondary_vendors(conn, pid) == [] and not db.plugin_has_manual_categories(conn, pid)


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from app import security, web
    db.init_db(web.DB_PATH)
    return TestClient(web.app, base_url="http://127.0.0.1:8000", headers={security.TOKEN_HEADER: security.TOKEN})


def test_include_orphans_on_both_reference_endpoints(client):
    from app import web
    with db.connect(web.DB_PATH) as c:
        metadata_archive.ensure_table(c)
        c.execute("INSERT INTO vendors (name, url, status, source) VALUES ('Live Audio', 'https://live.example', 'active', 'manual')")
        c.execute("INSERT INTO plugins (name, normalized_name, vendor, format, host_path, scanned_at) "
                  "VALUES ('Live', 'live', 'Live Audio', 'VST3', 'C:\\Live.vst3', 'now')")
        c.execute("INSERT INTO vendors (name, url, status, source, kvr_url) VALUES ('Gone Audio', 'https://gone.example', 'down', 'catalog', 'https://kvr.example/gone')")
        c.execute("INSERT INTO categories (slug, name, description, source) VALUES ('my-cat', 'My Cat', 'Mine.', 'manual')")
        metadata_archive.archive_unused_vendors(c)
        metadata_archive.archive_unused_categories(c)
        db.prune_unused_reference(c)

    # Without the flag the answer is the plain list: no orphan keys, no archived rows.
    plain = client.get("/api/reference/vendors").json()
    assert all("orphan" not in r for r in plain) and "Gone Audio" not in [r["name"] for r in plain]
    rows = client.get("/api/reference/vendors?include_orphans=1").json()
    live = [r for r in rows if "archive_id" not in r]
    gone = [r for r in rows if r.get("archive_id") == "vendor:gone audio"]
    assert all(r["orphan"] is False for r in live) and "Live Audio" in [r["name"] for r in live]
    assert len(gone) == 1 and gone[0]["orphan"] is True and gone[0]["id"] is None
    assert gone[0]["name"] == "Gone Audio" and gone[0]["kvr_url"] == "https://kvr.example/gone"
    assert set(gone[0]) == set(live[0]) | {"archive_id"}

    cats = client.get("/api/categories?include_orphans=1").json()
    gone_cat = [r for r in cats if r.get("archive_id") == "category:my-cat"]
    assert len(gone_cat) == 1 and gone_cat[0]["orphan"] is True and gone_cat[0]["slug"] == "my-cat"
    assert gone_cat[0]["description"] == "Mine."
    assert not any(r["slug"] == "my-cat" for r in client.get("/api/categories").json())

    assert any(i["id"] == "category:my-cat" for i in client.get("/api/metadata").json()["items"])
    cleared = client.post("/api/metadata/clear", json={"ids": ["category:my-cat"]}).json()
    assert cleared["cleared"]["category"] == 1
    assert not any(i["id"] == "category:my-cat" for i in cleared["now"]["items"])
