"""Researched metadata that outlives its plugin (Database > Metadata).

A plugin's researched data lives on its `plugins` row: links (store, KVR, Equipboard and the "no page"
flags), descriptions, manual name / vendor / type edits, a manual "same plugin" link, other vendors and
manual categories. A scan deletes the row of a plugin that is no longer on disk, and an unused vendor is
pruned with its website, status and database links. Reinstalling the plugin then started from zero.

Now the scan first copies that data here (`archive_missing_plugins`, `archive_unused_vendors`,
`archive_unused_categories`), and gives it back when the plugin, vendor or category returns (`restore_plugins`,
`restore_vendors`, `restore_categories`): same path first, else the same normalized name and format. A restore
only fills what the new row lacks, except the owner's manual edits, which win as they did before. An owner-made
category a plugin used comes back with that plugin. The Metadata pane lists what is kept with no plugin behind it, plus
leftovers older than this archive (pictures of plugins that are gone, "already investigated" marks of
plugin ids that no longer exist), and clears them on request.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app import db, plugin_host

# Plain columns copied as they are. The *_source columns travel with the value they protect.
_PLUGIN_FIELDS = [
    "description", "description_es",
    "store_url", "kvr_url", "equipboard_url", "no_store", "no_kvr", "no_equipboard",
    "link_group",
]
# Manual edits: (value columns, source column). Restored over the scan's value, like before removal.
_MANUAL_FIELDS = [
    (["name"], "name_source"),
    (["vendor", "vendor_note"], "vendor_source"),
    (["plugin_type"], "plugin_type_source"),
]
_VENDOR_SKIP = {"id", "name"}
_CATEGORY_FIELDS = ["slug", "name", "description", "string_value", "source_url", "source"]
# Where the Database panes' archived rows come from: the same columns as their normal rows.
_REFERENCE_TABLES = {"vendor": "vendors", "category": "categories"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_table(conn) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS metadata_archive (
            kind            TEXT NOT NULL,      -- plugin / vendor / category
            key             TEXT NOT NULL,      -- plugin: host path (lower case); vendor: name (lower case); category: slug (lower case)
            name            TEXT NOT NULL,
            vendor          TEXT,
            format          TEXT,
            normalized_name TEXT,
            host_path       TEXT,
            data            TEXT NOT NULL,      -- JSON: the fields kept
            archived_at     TEXT NOT NULL,
            PRIMARY KEY (kind, key)
        )
        """
    )


def _columns(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _plugin_data(conn, row) -> dict:
    """What is worth keeping from one plugin row (empty dict: nothing researched)."""
    cols = row.keys()
    data = {}
    for f in _PLUGIN_FIELDS:
        if f in cols and row[f] not in (None, "", 0):
            data[f] = row[f]
    for values, source in _MANUAL_FIELDS:
        if source in cols and row[source] == "manual":
            for v in values:
                data[v] = row[v]
            data[source] = "manual"
    others = db.list_secondary_vendors(conn, row["id"])
    if others:
        data["secondary_vendors"] = others
    manual_cats = [r[0] for r in conn.execute(
        "SELECT c.slug FROM plugin_categories pc JOIN categories c ON c.id = pc.category_id "
        "WHERE pc.plugin_id=? AND pc.source='manual'", (row["id"],))]
    if manual_cats:
        data["manual_categories"] = manual_cats
    return data


def archive_missing_plugins(conn, keep_paths: set) -> int:
    """Before remove_stale_plugins: copies the researched data of every plugin about to be removed."""
    ensure_table(conn)
    keep = {p.lower() for p in keep_paths}
    kept = 0
    for row in conn.execute("SELECT * FROM plugins").fetchall():
        if row["host_path"].lower() in keep:
            continue
        data = _plugin_data(conn, row)
        if not data:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO metadata_archive "
            "(kind, key, name, vendor, format, normalized_name, host_path, data, archived_at) "
            "VALUES ('plugin', ?, ?, ?, ?, ?, ?, ?, ?)",
            (row["host_path"].lower(), row["name"], row["vendor"], row["format"], row["normalized_name"],
             row["host_path"], json.dumps(data), _now()),
        )
        kept += 1
    return kept


def _restore_into(conn, plugin, data: dict) -> None:
    sets, args = [], []
    cols = plugin.keys()
    for f in _PLUGIN_FIELDS:
        if f in data and f in cols and plugin[f] in (None, "", 0):
            sets.append(f"{f}=?")
            args.append(data[f])
    for values, source in _MANUAL_FIELDS:
        if data.get(source) == "manual" and source in cols:
            for v in values:
                sets.append(f"{v}=?")
                args.append(data.get(v))
            sets.append(f"{source}='manual'")
    if sets:
        conn.execute(f"UPDATE plugins SET {', '.join(sets)} WHERE id=?", (*args, plugin["id"]))
    if data.get("secondary_vendors") and not db.list_secondary_vendors(conn, plugin["id"]):
        db.set_secondary_vendors(conn, plugin["id"], data["secondary_vendors"])
    if data.get("manual_categories") and not db.plugin_has_manual_categories(conn, plugin["id"]):
        # Before the link: set_plugin_categories drops a slug that does not exist yet.
        for slug in data["manual_categories"]:
            _recreate_category(conn, slug)
        db.set_plugin_categories_manual(conn, plugin["id"], data["manual_categories"])


def restore_plugins(conn) -> int:
    """After a scan's upserts: gives archived data back to plugins that are here again."""
    ensure_table(conn)
    entries = conn.execute(
        "SELECT * FROM metadata_archive WHERE kind='plugin' ORDER BY archived_at DESC").fetchall()
    if not entries:
        return 0
    by_path = {r["host_path"].lower(): r for r in conn.execute("SELECT * FROM plugins").fetchall()}
    restored = 0
    taken = set()
    for e in entries:
        plugin = by_path.get(e["key"])
        if plugin is None:
            same = conn.execute(
                "SELECT * FROM plugins WHERE normalized_name=? AND format=?",
                (e["normalized_name"], e["format"])).fetchall()
            if len(same) != 1:
                continue  # gone, or ambiguous: keep it archived
            plugin = same[0]
        if plugin["id"] in taken:
            continue
        _restore_into(conn, plugin, json.loads(e["data"]))
        conn.execute("DELETE FROM metadata_archive WHERE kind='plugin' AND key=?", (e["key"],))
        taken.add(plugin["id"])
        restored += 1
    return restored


def _vendor_data(row) -> dict:
    return {k: row[k] for k in row.keys() if k not in _VENDOR_SKIP and row[k] not in (None, "", 0)
            and not (k == "source" and row[k] == "catalog")}


def archive_unused_vendors(conn) -> int:
    """Before prune_unused_reference: keeps what was researched about vendors no plugin uses now."""
    ensure_table(conn)
    kept = 0
    for row in conn.execute(
            "SELECT * FROM vendors WHERE name NOT IN (SELECT vendor FROM plugins WHERE vendor IS NOT NULL)").fetchall():
        data = _vendor_data(row)
        # Catalog vendors come back with their website and status from app/vendors_catalog.py at every
        # scan: keep only what research or the owner added on top.
        if not ({"kvr_url", "equipboard_url", "no_url", "no_kvr", "no_equipboard"} & data.keys()
                or data.get("source") == "manual"):
            continue
        conn.execute(
            "INSERT OR REPLACE INTO metadata_archive "
            "(kind, key, name, vendor, format, normalized_name, host_path, data, archived_at) "
            "VALUES ('vendor', ?, ?, NULL, NULL, NULL, NULL, ?, ?)",
            (row["name"].lower(), row["name"], json.dumps(data), _now()),
        )
        kept += 1
    return kept


def restore_vendors(conn) -> int:
    """Fills a vendor row that exists again with what was archived for it (only what it lacks)."""
    ensure_table(conn)
    cols = _columns(conn, "vendors")
    restored = 0
    for e in conn.execute("SELECT * FROM metadata_archive WHERE kind='vendor'").fetchall():
        row = conn.execute("SELECT * FROM vendors WHERE lower(name)=?", (e["key"],)).fetchone()
        if row is None:
            continue
        data = json.loads(e["data"])
        sets, args = [], []
        for k, v in data.items():
            if k in cols and (row[k] in (None, "", 0) or (k == "source" and v == "manual")):
                sets.append(f"{k}=?")
                args.append(v)
        if sets:
            conn.execute(f"UPDATE vendors SET {', '.join(sets)} WHERE id=?", (*args, row["id"]))
        conn.execute("DELETE FROM metadata_archive WHERE kind='vendor' AND key=?", (e["key"],))
        restored += 1
    return restored


def archive_unused_categories(conn) -> int:
    """Before prune_unused_reference: keeps owner-added categories no plugin uses now. Built-in ones come back
    from app/categories.py at every start (db.seed_categories), so they are not kept."""
    ensure_table(conn)
    kept = 0
    for row in conn.execute(
            "SELECT * FROM categories WHERE source='manual' AND id NOT IN (SELECT category_id FROM plugin_categories)"
    ).fetchall():
        data = {k: row[k] for k in _CATEGORY_FIELDS}
        conn.execute(
            "INSERT OR REPLACE INTO metadata_archive "
            "(kind, key, name, vendor, format, normalized_name, host_path, data, archived_at) "
            "VALUES ('category', ?, ?, NULL, NULL, NULL, NULL, ?, ?)",
            (row["slug"].lower(), row["name"], json.dumps(data), _now()),
        )
        kept += 1
    return kept


def restore_categories(conn) -> int:
    """Fills a category that exists again (same slug) with what was archived for it (only what it lacks)."""
    ensure_table(conn)
    restored = 0
    for e in conn.execute("SELECT * FROM metadata_archive WHERE kind='category'").fetchall():
        row = conn.execute("SELECT * FROM categories WHERE lower(slug)=?", (e["key"],)).fetchone()
        if row is None:
            continue
        data = json.loads(e["data"])
        sets, args = [], []
        for k in ("name", "string_value", "description", "source_url"):
            if row[k] in (None, "") and data.get(k) not in (None, ""):
                sets.append(f"{k}=?")
                args.append(data[k])
        if sets:
            conn.execute(f"UPDATE categories SET {', '.join(sets)} WHERE id=?", (*args, row["id"]))
        conn.execute("DELETE FROM metadata_archive WHERE kind='category' AND key=?", (e["key"],))
        restored += 1
    return restored


def _recreate_category(conn, slug: str) -> None:
    """A returning plugin's owner-made category: put back from the archive when it is not in the table."""
    if db.category_id_by_slug(conn, slug) is not None:
        return
    e = conn.execute("SELECT data FROM metadata_archive WHERE kind='category' AND key=?", (slug.lower(),)).fetchone()
    if e is None:
        return
    data = json.loads(e["data"])
    conn.execute(
        "INSERT INTO categories (slug, name, string_value, description, source_url, source) "
        "VALUES (?, ?, ?, ?, ?, 'manual')",
        (slug, data.get("name") or slug, data.get("string_value"), data.get("description") or "",
         data.get("source_url")),
    )
    conn.execute("DELETE FROM metadata_archive WHERE kind='category' AND key=?", (slug.lower(),))


# ---------- the Metadata pane ----------

def _picture_files() -> dict:
    d = Path(plugin_host.IMAGES_DIR)
    if not d.is_dir():
        return {}
    return {p.stem: p for p in d.glob("*.png")}


def _picture_key(host_path: str) -> str:
    return Path(plugin_host.image_path(host_path)).stem


def _summary(data: dict) -> list[str]:
    labels = {
        "store_url": "store link", "kvr_url": "KVR link", "equipboard_url": "Equipboard link",
        "url": "website", "status": "status", "description": "description", "description_es": "Spanish description",
        "name_source": "name edit", "vendor_source": "vendor edit", "plugin_type_source": "type edit",
        "link_group": "linked plugins", "secondary_vendors": "other vendors", "manual_categories": "categories",
    }
    out = [labels[k] for k in labels if k in data]
    if any(k in data for k in ("no_store", "no_kvr", "no_equipboard", "no_url")):
        out.append("checked: no page")
    return out


def _item(id, kind, status, name, vendor=None, format=None, path=None, fields=(), date=None, size=None,
          data=None) -> dict:
    """One row of the listing. Only an installed plugin is not an orphan."""
    return {"id": id, "kind": kind, "status": status, "orphan": status != "installed", "name": name,
            "vendor": vendor, "format": format, "path": path, "fields": list(fields), "date": date,
            "bytes": size, "data": data or {}}


def listing(conn) -> dict:
    """Every row of Database > Metadata: installed plugins with researched data (or a picture), what is kept
    with no installed plugin behind it (no_plugin), pictures nothing owns (image_only), and stale marks."""
    ensure_table(conn)
    pictures = _picture_files()
    plugin_keys = {_picture_key(r[0]) for r in conn.execute("SELECT host_path FROM plugins")}
    items = []
    claimed = set()  # picture keys shown with an archived plugin
    for row in conn.execute("SELECT * FROM plugins").fetchall():
        data = _plugin_data(conn, row)
        pic = pictures.get(_picture_key(row["host_path"]))
        if not data and pic is None:
            continue
        items.append(_item(
            id=f"installed:{row['id']}", kind="plugin", status="installed", name=row["name"], vendor=row["vendor"],
            format=row["format"], path=row["host_path"], fields=_summary(data) + (["picture"] if pic else []),
            date=row["scanned_at"], data=data))
    for e in conn.execute("SELECT * FROM metadata_archive ORDER BY kind, name COLLATE NOCASE").fetchall():
        data = json.loads(e["data"])
        fields = _summary(data)
        if e["kind"] == "plugin":
            k = _picture_key(e["host_path"])
            if k in pictures and k not in plugin_keys:
                claimed.add(k)
                fields.append("picture")
        items.append(_item(
            id=f"{e['kind']}:{e['key']}", kind=e["kind"], status="no_plugin", name=e["name"], vendor=e["vendor"],
            format=e["format"], path=e["host_path"], fields=fields, date=e["archived_at"], data=data))
    for key, p in sorted(pictures.items()):
        if key in plugin_keys or key in claimed:
            continue
        items.append(_item(id=f"picture:{key}", kind="picture", status="image_only", name=p.name, path=str(p),
                           fields=["picture"], size=p.stat().st_size))
    ids = {str(r[0]) for r in conn.execute("SELECT id FROM plugins")}
    vendors = {r[0].lower() for r in conn.execute("SELECT name FROM vendors")}
    stale = [(r["target"], r["item_key"]) for r in conn.execute("SELECT target, item_key FROM investigation_attempts")
             if (r["item_key"].lower() not in vendors if r["target"] == "vendors" else r["item_key"] not in ids)] \
        if "investigation_attempts" in _tables(conn) else []
    if stale:
        items.append(_item(id="attempts:stale", kind="attempts", status="stale",
                           name=f"{len(stale)} \"already investigated\" marks",
                           fields=["for plugins or vendors that are gone"]))
    counts = {}
    for i in items:
        counts[i["status"]] = counts.get(i["status"], 0) + 1
    return {"items": items, "counts": counts}


def orphans(conn) -> dict:
    """Everything VEXT keeps with no installed plugin (or used vendor) behind it: the listing without the
    installed rows."""
    items = [i for i in listing(conn)["items"] if i["orphan"]]
    counts = {}
    for i in items:
        counts[i["status"]] = counts.get(i["status"], 0) + 1
    return {"items": items, "counts": counts}


def orphan_rows(conn, kind: str) -> list[dict]:
    """Archived vendors or categories in the shape of their normal reference rows, for the Database panes'
    include_orphans lists. Keys the archive does not keep are null; `orphan` and `archive_id` mark them."""
    ensure_table(conn)
    columns = _columns(conn, _REFERENCE_TABLES[kind])
    out = []
    for e in conn.execute("SELECT * FROM metadata_archive WHERE kind=? ORDER BY name COLLATE NOCASE",
                          (kind,)).fetchall():
        data = json.loads(e["data"])
        row = {c: data.get(c) for c in columns}
        row.update(id=None, name=e["name"], orphan=True, archive_id=f"{kind}:{e['key']}")
        out.append(row)
    return out


def _tables(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _clear_installed(conn, plugin_id: str) -> bool:
    """Puts an installed plugin back to what a scan alone gives it: its links, descriptions, the owner's manual
    edits, categories and other vendors go. Its picture stays (the scan keeps using it)."""
    if conn.execute("SELECT 1 FROM plugins WHERE id=?", (plugin_id,)).fetchone() is None:
        return False
    conn.execute(
        "UPDATE plugins SET store_url=NULL, kvr_url=NULL, equipboard_url=NULL, description=NULL, "
        "description_es=NULL, link_group=NULL, no_store=0, no_kvr=0, no_equipboard=0, "
        "name_source=CASE WHEN name_source='manual' THEN 'scan' ELSE name_source END, "
        "vendor_source=CASE WHEN vendor_source='manual' THEN 'scan' ELSE vendor_source END, "
        "plugin_type_source=CASE WHEN plugin_type_source='manual' THEN 'scan' ELSE plugin_type_source END "
        "WHERE id=?", (plugin_id,))
    conn.execute("DELETE FROM plugin_secondary_vendors WHERE plugin_id=?", (plugin_id,))
    conn.execute("DELETE FROM plugin_categories WHERE plugin_id=? AND source='manual'", (plugin_id,))
    # Research starts over: without this, the AI batches would skip it as "already investigated".
    if "investigation_attempts" in _tables(conn):
        conn.execute("DELETE FROM investigation_attempts WHERE target != 'vendors' AND item_key=?", (str(plugin_id),))
    return True


def clear(conn, ids: list[str]) -> dict:
    """Clears the listed items. An archived plugin takes its leftover picture with it."""
    ensure_table(conn)
    pictures = _picture_files()
    plugin_keys = {_picture_key(r[0]) for r in conn.execute("SELECT host_path FROM plugins")}
    done = {"plugin": 0, "vendor": 0, "category": 0, "installed": 0, "picture": 0, "attempts": 0}
    for item in ids:
        kind, _, key = item.partition(":")
        if kind == "installed":
            if _clear_installed(conn, key):
                done["installed"] += 1
        elif kind in ("plugin", "vendor", "category"):
            row = conn.execute("SELECT host_path FROM metadata_archive WHERE kind=? AND key=?", (kind, key)).fetchone()
            if row is None:
                continue
            conn.execute("DELETE FROM metadata_archive WHERE kind=? AND key=?", (kind, key))
            done[kind] += 1
            if kind == "plugin" and row[0]:
                k = _picture_key(row[0])
                if k in pictures and k not in plugin_keys:
                    _remove_picture(conn, pictures[k], k)
                    done["picture"] += 1
        elif kind == "picture":
            if key in pictures and key not in plugin_keys:
                _remove_picture(conn, pictures[key], key)
                done["picture"] += 1
        elif kind == "attempts":
            ids_now = {str(r[0]) for r in conn.execute("SELECT id FROM plugins")}
            vendors = {r[0].lower() for r in conn.execute("SELECT name FROM vendors")}
            for r in conn.execute("SELECT target, item_key FROM investigation_attempts").fetchall():
                gone = r[1].lower() not in vendors if r[0] == "vendors" else r[1] not in ids_now
                if gone:
                    conn.execute("DELETE FROM investigation_attempts WHERE target=? AND item_key=?", (r[0], r[1]))
                    done["attempts"] += 1
        else:
            raise ValueError(f"unknown item: {item}")
    return done


def _remove_picture(conn, path: Path, key: str) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    conn.execute("DELETE FROM picture_meta WHERE key=?", (key,)) if "picture_meta" in _tables(conn) else None
