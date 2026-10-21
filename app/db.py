"""SQLite storage: schema, upserts, and simple queries."""

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS plugins (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    vendor          TEXT,
    vendor_source   TEXT,                         -- moduleinfo / binary / installer / inferred / default / NULL
    vendor_note     TEXT,                         -- e.g. why 'inferred', or an unresolved-ambiguity explanation
    version         TEXT,
    format          TEXT NOT NULL,
    plugin_type     TEXT,
    categories      TEXT,
    host_path       TEXT NOT NULL UNIQUE,
    size_bytes      INTEGER,
    status          TEXT NOT NULL DEFAULT 'ok',   -- ok / corrupted / needs_review
    status_reason   TEXT,
    vendor_url      TEXT,
    vendor_status   TEXT,                         -- NULL until researched / active / deprecated / down (app/vendors_catalog.py)
    vendor_status_note TEXT,                      -- why, e.g. "Acquired by Meta in 2022; ERA line sunset 2023"
    plugin_type_source TEXT NOT NULL DEFAULT 'scan',  -- scan / manual — protects a manual plugin_type edit from rescans
    fl_detected     INTEGER NOT NULL DEFAULT 0,    -- 1 if FL Studio's own plugin database also knows this plugin (app/fl_database.py)
    fl_name         TEXT,                          -- FL's resolved display name, if detected (may differ from `name` before a fix, or just confirm it)
    fl_type         TEXT,                          -- 'effect'/'synth' per FL's own ps_file_type_0, if detected — used by the Type tab
    description     TEXT,
    description_es  TEXT,                         -- short Spanish description, shown on browse-table row hover
    scanned_at      TEXT NOT NULL,
    name_source     TEXT NOT NULL DEFAULT 'scan'   -- scan / manual — protects a manual name edit from rescans
);

CREATE INDEX IF NOT EXISTS idx_plugins_normalized_name ON plugins(normalized_name);
CREATE INDEX IF NOT EXISTS idx_plugins_format ON plugins(format);
CREATE INDEX IF NOT EXISTS idx_plugins_status ON plugins(status);

CREATE TABLE IF NOT EXISTS categories (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    slug         TEXT NOT NULL UNIQUE,
    name         TEXT NOT NULL,
    string_value TEXT,             -- official VST3 subCategory string this maps to, if any
    description  TEXT NOT NULL,
    source_url   TEXT,
    source       TEXT NOT NULL DEFAULT 'builtin'   -- builtin (Steinberg taxonomy) / manual (owner-added)
);

CREATE TABLE IF NOT EXISTS vendors (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    url         TEXT,
    status      TEXT,               -- active / deprecated / down / NULL (unresearched)
    status_note TEXT,
    source      TEXT NOT NULL DEFAULT 'catalog'    -- catalog (app/vendors_catalog.py) / manual (owner-added/edited)
);

CREATE TABLE IF NOT EXISTS plugin_types (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    description TEXT,
    source      TEXT NOT NULL DEFAULT 'builtin'
);

CREATE TABLE IF NOT EXISTS plugin_categories (
    plugin_id   INTEGER NOT NULL,
    category_id INTEGER NOT NULL,
    source      TEXT NOT NULL DEFAULT 'moduleinfo',  -- moduleinfo / heuristic
    PRIMARY KEY (plugin_id, category_id),
    FOREIGN KEY (plugin_id) REFERENCES plugins(id) ON DELETE CASCADE,
    FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS scan_roots (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    host_path           TEXT NOT NULL UNIQUE,
    format              TEXT NOT NULL,          -- VST3 / VST2 / CLAP / AAX / WAVES / FL-NATIVE
    default_type        TEXT NOT NULL DEFAULT 'unknown',
    extensions          TEXT NOT NULL DEFAULT '',   -- comma-joined, e.g. ".vst3" ("" for folder-per-plugin formats)
    recurse_vendor_dirs INTEGER NOT NULL DEFAULT 1,
    source              TEXT NOT NULL DEFAULT 'custom',  -- builtin (seeded from app/config.py:ROOTS) / custom (owner-added) / manual (owner-edited a builtin one)
    enabled             INTEGER NOT NULL DEFAULT 1,      -- owner can disable/remove any root without it reappearing on the next reseed
    added_at            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ignored_scan_folders (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    host_path  TEXT NOT NULL UNIQUE,
    added_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS removed_scan_root_paths (
    host_path  TEXT PRIMARY KEY,   -- tombstone: a builtin/manual scan_roots row the owner deleted
    removed_at TEXT NOT NULL       -- outright, not just disabled — stops seed_scan_roots() resurrecting it
);

CREATE TABLE IF NOT EXISTS ignored_plugin_paths (
    host_path  TEXT PRIMARY KEY,   -- a plugin the owner chose to ignore from the main catalog (Browse
                                    -- tab "Ignore" button) — excluded from every future rescan entirely:
                                    -- not cataloged, not FL-cross-referenced, not dedup-grouped. Distinct
                                    -- from out_of_scope_plugins.ignored (files never in the catalog at
                                    -- all) and ignored_scan_folders (a whole subtree, out-of-scope only).
    name       TEXT NOT NULL,      -- kept for display in the Configuration tab even after the plugin
                                    -- row itself is gone
    added_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fl_cloud_plugins (
    host_path  TEXT PRIMARY KEY,   -- a plugin the owner marked as an FL Cloud plugin (modal button / FL Cloud tab).
                                    -- FL Cloud authorizes against the exact install path, so these stay cataloged
                                    -- but are never proposed for dedup, vendor-folder reorganize, or quarantine,
                                    -- and the file is never moved. Survives rescans (keyed on host_path, not id).
    name       TEXT NOT NULL,
    added_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS enrichment_suggestions (
    plugin_id           INTEGER PRIMARY KEY, -- one pending suggestion per plugin; re-researching replaces it
    vendor               TEXT,
    vendor_url           TEXT,
    vendor_status        TEXT,
    vendor_status_note   TEXT,
    plugin_type          TEXT,
    categories           TEXT,                -- comma-joined slugs, owner's taxonomy
    description_es       TEXT,
    confidence           TEXT,                -- high / medium / low, the researcher's own self-assessment
    sources              TEXT,                -- comma-joined URLs actually consulted
    notes                TEXT,                -- anything worth flagging for human review
    created_at           TEXT NOT NULL,
    FOREIGN KEY (plugin_id) REFERENCES plugins(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS out_of_scope_plugins (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    host_path    TEXT NOT NULL UNIQUE,
    name         TEXT NOT NULL,
    format_guess TEXT,               -- extension-based only (VST3/CLAP/AAXPLUGIN/"VST2?") — never enriched
    size_bytes   INTEGER,
    found_at     TEXT NOT NULL,
    ignored      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS scans (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    roots_count INTEGER,
    found_count INTEGER,
    flagged_count INTEGER,
    notes       TEXT
);

CREATE TABLE IF NOT EXISTS action_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    plugin_id   INTEGER,
    action      TEXT NOT NULL,     -- quarantine / reorganize
    reason      TEXT,
    dry_run     INTEGER NOT NULL DEFAULT 1,
    applied     INTEGER NOT NULL DEFAULT 0,
    resolution  TEXT,              -- NULL (unresolved/still in quarantine) / restored / deleted_forever
    FOREIGN KEY (plugin_id) REFERENCES plugins(id) ON DELETE CASCADE
);
"""


@contextmanager
def connect(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# Columns added to `plugins` after its first release, for existing databases that
# predate them. SQLite has no "ADD COLUMN IF NOT EXISTS", so _migrate() diffs
# PRAGMA table_info against this list instead of requiring `rm data/plugins.db`
# on every schema change (which happened more than once early on).
_PLUGIN_COLUMNS_ADDED_LATER = [
    ("vendor_status_note", "TEXT"),
    ("plugin_type_source", "TEXT NOT NULL DEFAULT 'scan'"),
    ("fl_detected", "INTEGER NOT NULL DEFAULT 0"),
    ("fl_name", "TEXT"),
    ("fl_type", "TEXT"),
    ("bitness", "INTEGER"),        # 32 / 64, read from the PE header; NULL when unknown (Waves/FL-native/unparsed)
    ("link_group", "TEXT"),        # manual "same plugin" link, survives rescans (upsert never touches it)
    ("fl_cloud", "INTEGER NOT NULL DEFAULT 0"),  # 1 = FL Cloud plugin: pinned in place, excluded from dedup/organize/actions
    ("name_source", "TEXT NOT NULL DEFAULT 'scan'"),  # scan / manual — protects a manual name edit from rescans
    ("store_url", "TEXT"),  # this plugin's own store/product page (not the vendor's site)
    ("kvr_url", "TEXT"),        # this plugin's page in the KVR Audio database
    ("equipboard_url", "TEXT"),  # this plugin's page in the Equipboard database
    # 1 = looked for it and that site has no page for this plugin. Different from a link that was
    # never looked for (NULL url, flag 0): the first is settled, the second still needs investigating.
    ("no_store", "INTEGER NOT NULL DEFAULT 0"),
    ("no_kvr", "INTEGER NOT NULL DEFAULT 0"),
    ("no_equipboard", "INTEGER NOT NULL DEFAULT 0"),
]
_ACTION_LOG_COLUMNS_ADDED_LATER = [
    ("resolution", "TEXT"),
]
# location_key ties a row to one standard location (see app/config.py:
# detect_locations), detected_path is what detection found this run, and label
# is the name shown in the Configuration tab. Custom paths leave location_key NULL.
_SCAN_ROOTS_COLUMNS_ADDED_LATER = [
    ("location_key", "TEXT"),
    ("detected_path", "TEXT"),
    ("label", "TEXT"),
]
_CATEGORIES_COLUMNS_ADDED_LATER = [
    ("source", "TEXT NOT NULL DEFAULT 'builtin'"),
]


def _add_missing_columns(conn, table: str, columns: list[tuple[str, str]]) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, coltype in columns:
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {coltype}")


def _ensure_action_log_cascade(conn) -> None:
    """The original action_log FK had no ON DELETE behavior (default: NO ACTION),
    which meant remove_stale_plugins()/a full rescan would hard-fail with 'FOREIGN
    KEY constraint failed' the moment any action existed (confirmed). Existing
    databases need the table rebuilt to pick up ON DELETE CASCADE — SQLite can't
    ALTER a constraint in place. Safe rebuild per SQLite's own recommended recipe."""
    fk = conn.execute("PRAGMA foreign_key_list(action_log)").fetchall()
    if fk and fk[0][6] == "CASCADE":  # column 6 = on_delete
        return

    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("ALTER TABLE action_log RENAME TO action_log_old")
    conn.execute("""
        CREATE TABLE action_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT NOT NULL,
            plugin_id   INTEGER,
            action      TEXT NOT NULL,
            reason      TEXT,
            dry_run     INTEGER NOT NULL DEFAULT 1,
            applied     INTEGER NOT NULL DEFAULT 0,
            resolution  TEXT,
            FOREIGN KEY (plugin_id) REFERENCES plugins(id) ON DELETE CASCADE
        )
    """)
    old_cols = {row[1] for row in conn.execute("PRAGMA table_info(action_log_old)")}
    common = [c for c in ("id", "ts", "plugin_id", "action", "reason", "dry_run", "applied", "resolution") if c in old_cols]
    conn.execute(f"INSERT INTO action_log ({','.join(common)}) SELECT {','.join(common)} FROM action_log_old")
    conn.execute("DROP TABLE action_log_old")
    conn.execute("PRAGMA foreign_keys = ON")


def _migrate_custom_scan_roots(conn) -> None:
    """One-time: custom_scan_roots (host_path/format only) became scan_roots
    (holds both owner-added and built-in roots, plus per-root scan settings —
    see AGENTS.md's Configuration section). Existing rows carry over as
    source='custom', which is exactly what they always were; extensions
    inferred the same way app/custom_roots.py used to at scan time (that
    module is now retired — see app/db.py:scan_roots_as_objects)."""
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "custom_scan_roots" not in tables:
        return
    extensions_by_format = {"VST3": ".vst3", "VST2": ".dll", "CLAP": ".clap", "AAX": ".aaxplugin"}
    for host_path, fmt, added_at in conn.execute("SELECT host_path, format, added_at FROM custom_scan_roots"):
        conn.execute(
            """
            INSERT INTO scan_roots (host_path, format, default_type, extensions, recurse_vendor_dirs, source, enabled, added_at)
            VALUES (?, ?, 'unknown', ?, 1, 'custom', 1, ?)
            ON CONFLICT(host_path) DO NOTHING
            """,
            (host_path, fmt, extensions_by_format.get(fmt, ""), added_at),
        )
    conn.execute("DROP TABLE custom_scan_roots")


def _migrate(conn) -> None:
    _add_missing_columns(conn, "plugins", _PLUGIN_COLUMNS_ADDED_LATER)
    _ensure_investigation_tables(conn)
    _ensure_picture_tables(conn)
    _ensure_fl_links_table(conn)
    _ensure_fl_ignored_table(conn)
    _add_missing_columns(conn, "enrichment_suggestions", [("secondary_vendors", "TEXT")])
    _add_missing_columns(conn, "action_log", _ACTION_LOG_COLUMNS_ADDED_LATER)
    _add_missing_columns(conn, "categories", _CATEGORIES_COLUMNS_ADDED_LATER)
    _add_missing_columns(conn, "scan_roots", _SCAN_ROOTS_COLUMNS_ADDED_LATER)
    _add_missing_columns(conn, "enrichment_suggestions", [("store_url", "TEXT")])
    _add_missing_columns(conn, "enrichment_suggestions", [("kvr_url", "TEXT"), ("equipboard_url", "TEXT")])
    _add_missing_columns(conn, "vendors", [("kvr_url", "TEXT"), ("equipboard_url", "TEXT"),
                                           ("no_url", "INTEGER NOT NULL DEFAULT 0"),
                                           ("no_kvr", "INTEGER NOT NULL DEFAULT 0"),
                                           ("no_equipboard", "INTEGER NOT NULL DEFAULT 0")])
    # 1 = a links investigation produced this row, so a link left empty in it means "none found"
    _add_missing_columns(conn, "enrichment_suggestions", [("links_checked", "INTEGER NOT NULL DEFAULT 0")])
    _add_missing_columns(conn, "vendor_suggestions", [("kvr_url", "TEXT"), ("equipboard_url", "TEXT")])
    # Standard location keys were renamed when the fixed format folders were
    # reorganized (the classic VstPlugins folder is "vst_*" now; "vst2_*" is
    # Common Files\VST2). Carry the existing rows over to their new keys.
    for old_key, new_key in (("vst2_x64", "vst_x64"), ("vst2_x86", "vst_x32")):
        conn.execute("UPDATE scan_roots SET location_key=? WHERE location_key=?", (new_key, old_key))
    _ensure_action_log_cascade(conn)
    _migrate_custom_scan_roots(conn)


def init_db(db_path: str) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def seed_categories(conn) -> None:
    from app.categories import CATEGORIES, SOURCE_URL

    for slug, name, string_value, description in CATEGORIES:
        conn.execute(
            """
            INSERT INTO categories (slug, name, string_value, description, source_url)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(slug) DO UPDATE SET
                name=excluded.name, string_value=excluded.string_value,
                description=excluded.description, source_url=excluded.source_url
            """,
            (slug, name, string_value, description, SOURCE_URL),
        )


def seed_vendors(conn) -> None:
    """Loads app/vendors_catalog.py's static VENDORS dict into the `vendors` table
    as the default data — but never clobbers a vendor the owner has since edited
    or added manually (source='manual') through the UI. The vendors table, not
    the static module, is what the scan pipeline actually reads from now, so a
    manual add/edit takes effect on every future scan without a code change."""
    from app.vendors_catalog import VENDORS

    for name, (url, status, note) in VENDORS.items():
        conn.execute(
            """
            INSERT INTO vendors (name, url, status, status_note, source)
            VALUES (?, ?, ?, ?, 'catalog')
            ON CONFLICT(name) DO UPDATE SET
                url=excluded.url, status=excluded.status, status_note=excluded.status_note
            WHERE vendors.source != 'manual'
            """,
            (name, url, status, note),
        )


def seed_plugin_types(conn) -> None:
    defaults = [
        ("effect", "An audio effect processor (inserted on a track/mixer channel)."),
        ("synth", "An instrument that generates sound (loaded on an instrument track)."),
        ("unknown", "Type not yet determined from the plugin's own metadata."),
    ]
    for name, description in defaults:
        conn.execute(
            "INSERT INTO plugin_types (name, description, source) VALUES (?, ?, 'builtin') "
            "ON CONFLICT(name) DO NOTHING",
            (name, description),
        )


def get_vendor_info(conn, name: str):
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM vendors WHERE name=?", (name,)).fetchone()
    return dict(row) if row else None


def list_vendors_reference(conn):
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM vendors ORDER BY name").fetchall()


def upsert_vendor_reference(conn, name: str, url: str | None, status: str | None,
                             status_note: str | None, source: str = "manual",
                             kvr_url: str | None = None, equipboard_url: str | None = None,
                             no_url: bool | None = None, no_kvr: bool | None = None,
                             no_equipboard: bool | None = None) -> None:
    """kvr_url / equipboard_url: None leaves what is stored, "" clears it, anything else replaces it.
    no_*: None leaves the mark, True/False sets it. A mark and its link exclude each other: giving a
    link clears the mark, and marking "none" empties the link."""
    if kvr_url and no_kvr is None:
        no_kvr = False
    if equipboard_url and no_equipboard is None:
        no_equipboard = False
    if url and no_url is None:
        no_url = False
    if no_kvr and not kvr_url:
        kvr_url = ""
    if no_equipboard and not equipboard_url:
        equipboard_url = ""
    if no_url:
        url = None
    def flag(v):
        return None if v is None else (1 if v else 0)
    conn.execute(
        """
        INSERT INTO vendors (name, url, status, status_note, source, kvr_url, equipboard_url,
                             no_url, no_kvr, no_equipboard)
        VALUES (?, ?, ?, ?, ?, NULLIF(?, ''), NULLIF(?, ''), COALESCE(?, 0), COALESCE(?, 0), COALESCE(?, 0))
        ON CONFLICT(name) DO UPDATE SET
            url=excluded.url, status=excluded.status, status_note=excluded.status_note, source=excluded.source,
            kvr_url=CASE WHEN ? IS NULL THEN vendors.kvr_url ELSE NULLIF(?, '') END,
            equipboard_url=CASE WHEN ? IS NULL THEN vendors.equipboard_url ELSE NULLIF(?, '') END,
            no_url=CASE WHEN ? IS NULL THEN vendors.no_url ELSE ? END,
            no_kvr=CASE WHEN ? IS NULL THEN vendors.no_kvr ELSE ? END,
            no_equipboard=CASE WHEN ? IS NULL THEN vendors.no_equipboard ELSE ? END
        """,
        (name, url, status, status_note, source, kvr_url, equipboard_url,
         flag(no_url), flag(no_kvr), flag(no_equipboard),
         kvr_url, kvr_url, equipboard_url, equipboard_url,
         flag(no_url), flag(no_url), flag(no_kvr), flag(no_kvr), flag(no_equipboard), flag(no_equipboard)),
    )


def count_plugins_by_vendor(conn, name: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM plugins WHERE vendor=?", (name,)).fetchone()[0]


def delete_vendor_reference(conn, name: str) -> None:
    """Removes this vendor from the reference list AND clears it off every
    plugin that carried it — `vendor`, `vendor_source`, `vendor_url`,
    `vendor_status`, `vendor_status_note` all reset to NULL, same blank state
    as a plugin whose vendor was never resolved. `vendor_source` is reset
    (not left 'manual') specifically so a future rescan is free to
    re-resolve it from moduleinfo/binary/installer data rather than treating
    the now-gone vendor as a still-protected manual correction.

    Real bug found and fixed: the first version of this only deleted the
    `vendors` row and left `plugins.vendor` untouched (reasoning: it's a
    plain text column, not a foreign key, so nothing *required* clearing it).
    That left affected plugins showing a vendor name with nothing backing it
    — not counted by the "Missing vendor" stat, not filterable via the "(No
    vendor)" bucket, invisible as something needing attention. The owner's
    actual expectation was explicit: a deleted vendor should make its
    plugins real orphans, the same "Missing vendor" state as a plugin that
    was never resolved, not a vendor name floating with no reference behind
    it. `plugin_categories` already behaves this way via `ON DELETE CASCADE`
    (see delete_category_reference) — this makes vendor deletion consistent
    with it instead of a special case."""
    conn.execute(
        "UPDATE plugins SET vendor=NULL, vendor_source=NULL, vendor_url=NULL, "
        "vendor_status=NULL, vendor_status_note=NULL WHERE vendor=?",
        (name,),
    )
    conn.execute("DELETE FROM vendors WHERE name=?", (name,))


def rename_vendor_reference(conn, old: str, new: str) -> int:
    """Renames a vendor everywhere it is used: its row in `vendors` (same id, so its place in
    the scan order stays) and the vendor text on every plugin that carried it. Those plugins are
    marked vendor_source='manual', like any hand-set vendor, so a rescan doesn't put the old name
    back. Returns how many plugins followed."""
    conn.execute("UPDATE vendors SET name=?, source='manual' WHERE name=?", (new, old))
    return conn.execute(
        "UPDATE plugins SET vendor=?, vendor_source='manual' WHERE vendor=?", (new, old)
    ).rowcount


def list_plugin_types_reference(conn):
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM plugin_types ORDER BY name").fetchall()


# Plugin types are fixed (owner, 2026-10-15): the builtin 'effect' and 'synth' rows of
# plugin_types. They can't be added or edited, and a plugin has exactly one of them
# (or none yet). update_plugin_manual rejects any other value.
def is_plugin_type(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM plugin_types WHERE name=?", (name,)).fetchone() is not None


def list_categories_reference(conn):
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM categories ORDER BY name").fetchall()


def add_category_reference(conn, slug: str, name: str, description: str) -> None:
    conn.execute(
        """
        INSERT INTO categories (slug, name, description, source) VALUES (?, ?, ?, 'manual')
        ON CONFLICT(slug) DO UPDATE SET name=excluded.name, description=excluded.description
        """,
        (slug, name, description),
    )


def count_plugins_by_category(conn, slug: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM plugin_categories pc JOIN categories c ON c.id = pc.category_id WHERE c.slug=?",
        (slug,),
    ).fetchone()
    return row[0] if row else 0


def delete_category_reference(conn, slug: str) -> None:
    """Removes this category outright. `plugin_categories.category_id` has
    `ON DELETE CASCADE` (and `connect()` always turns PRAGMA foreign_keys on),
    so any plugin tagged with it is simply un-tagged — never a blocker, no
    orphaned join rows left behind. The plugin itself isn't touched otherwise;
    the owner can tag it with a different category afterward via the normal
    edit form."""
    conn.execute("DELETE FROM categories WHERE slug=?", (slug,))


def update_plugin_manual(conn, plugin_id: int, fields: dict) -> None:
    """Manual edits from the plugin-detail editor. Setting `vendor` marks
    vendor_source='manual' so the scan pipeline's vendor-resolution passes
    (installer cross-reference, enrichment, canonicalization, vendor-info
    lookup) skip this plugin on future rescans instead of overwriting the
    correction — see app/scan_service.py. Setting `name` similarly marks
    name_source='manual', protected in upsert_plugin()'s own ON CONFLICT
    (not in app/scan_service.py's Python loop — the FL Studio cross-reference
    rename happens before upsert_plugin runs, so it's the SQL CASE there,
    not a Python-level skip, that actually makes a manual rename stick)."""
    # Vendor URL/status/note are NOT per-plugin: they live once on the vendor row
    # (see sync_plugin_vendor_info). A plugin only links to its vendor by name.
    allowed = {"name", "vendor", "plugin_type", "description_es", "store_url", "kvr_url", "equipboard_url",
               "no_store", "no_kvr", "no_equipboard"}
    vendor_fields = {"vendor"}
    if fields.get("plugin_type") and not is_plugin_type(conn, fields["plugin_type"]):
        raise ValueError(f"'{fields['plugin_type']}' isn't a plugin type (the types are fixed: effect, synth)")
    sets, params = [], []
    for key, value in fields.items():
        if key not in allowed:
            continue
        sets.append(f"{key}=?")
        params.append((1 if value else 0) if key.startswith("no_") else value)
    # A link and its "none" mark exclude each other: setting a URL clears the mark, and marking
    # "none" empties the URL (unless a URL is given in the same edit, which then wins).
    for url_field, flag in (("store_url", "no_store"), ("kvr_url", "no_kvr"), ("equipboard_url", "no_equipboard")):
        if fields.get(url_field):
            if flag not in fields:
                sets.append(f"{flag}=0")
        elif fields.get(flag):
            if url_field not in fields:
                sets.append(f"{url_field}=NULL")
    if vendor_fields & fields.keys():
        sets.append("vendor_source='manual'")
    if "plugin_type" in fields:
        sets.append("plugin_type_source='manual'")
    if "name" in fields:
        from app.scanner import normalize_name
        sets.append("normalized_name=?")
        params.append(normalize_name(fields["name"]))
        sets.append("name_source='manual'")
    if not sets:
        return
    params.append(plugin_id)
    conn.execute(f"UPDATE plugins SET {', '.join(sets)} WHERE id=?", params)


def set_plugin_categories_manual(conn, plugin_id: int, slugs: list[str]) -> None:
    set_plugin_categories(conn, plugin_id, slugs, source="manual")


def plugin_has_manual_categories(conn, plugin_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM plugin_categories WHERE plugin_id=? AND source='manual' LIMIT 1", (plugin_id,)
    ).fetchone()
    return row is not None


def get_or_create_custom_category(conn, slug: str) -> int:
    """For non-standard subcategory strings a vendor made up (slug like 'custom:Foo|Bar')."""
    row = conn.execute("SELECT id FROM categories WHERE slug=?", (slug,)).fetchone()
    if row:
        return row[0]
    label = slug.split(":", 1)[-1]
    cur = conn.execute(
        "INSERT INTO categories (slug, name, string_value, description, source_url) VALUES (?, ?, NULL, ?, NULL)",
        (slug, label, f"Non-standard sub-category reported by a plugin's own metadata: {label!r}. Not part of the official VST3 taxonomy."),
    )
    return cur.lastrowid


def category_id_by_slug(conn, slug: str) -> int | None:
    row = conn.execute("SELECT id FROM categories WHERE slug=?", (slug,)).fetchone()
    return row[0] if row else None


def set_plugin_categories(conn, plugin_id: int, slugs: list[str], source: str = "moduleinfo") -> None:
    conn.execute("DELETE FROM plugin_categories WHERE plugin_id=?", (plugin_id,))
    for slug in slugs:
        cat_id = category_id_by_slug(conn, slug)
        if cat_id is None and slug.startswith("custom:"):
            cat_id = get_or_create_custom_category(conn, slug)
        if cat_id is not None:
            conn.execute(
                "INSERT OR IGNORE INTO plugin_categories (plugin_id, category_id, source) VALUES (?, ?, ?)",
                (plugin_id, cat_id, source),
            )


def set_description_es(conn, plugin_id: int, text: str) -> None:
    conn.execute("UPDATE plugins SET description_es=? WHERE id=?", (text, plugin_id))


def set_vendor_name(conn, plugin_id: int, canonical_vendor: str) -> None:
    conn.execute("UPDATE plugins SET vendor=? WHERE id=?", (canonical_vendor, plugin_id))


def sync_plugin_vendor_info(conn) -> None:
    """Keeps each plugin's cached vendor_url/vendor_status/vendor_status_note equal
    to its vendor's single row in `vendors`. That row is the only place these
    values are edited, so every plugin from one vendor always agrees: vendors are
    N:1 to plugins. Call after any vendor change and after each scan."""
    conn.execute(
        """
        UPDATE plugins SET
            vendor_url=(SELECT url FROM vendors WHERE vendors.name=plugins.vendor),
            vendor_status=(SELECT status FROM vendors WHERE vendors.name=plugins.vendor),
            vendor_status_note=(SELECT status_note FROM vendors WHERE vendors.name=plugins.vendor)
        """
    )


def plugin_has_categories(conn, plugin_id: int) -> bool:
    row = conn.execute("SELECT 1 FROM plugin_categories WHERE plugin_id=? LIMIT 1", (plugin_id,)).fetchone()
    return row is not None


def start_scan(conn) -> int:
    cur = conn.execute(
        "INSERT INTO scans (started_at) VALUES (?)",
        (datetime.now(timezone.utc).isoformat(),),
    )
    return cur.lastrowid


def finish_scan(conn, scan_id: int, roots_count: int, found_count: int, flagged_count: int, notes: str = "") -> None:
    conn.execute(
        "UPDATE scans SET finished_at=?, roots_count=?, found_count=?, flagged_count=?, notes=? WHERE id=?",
        (datetime.now(timezone.utc).isoformat(), roots_count, found_count, flagged_count, notes, scan_id),
    )


def prune_unused_reference(conn) -> dict:
    """Reference data only exists while something uses it. Vendors, plugin types
    and categories that no plugin references are erased here, and the next scan
    recreates them when a plugin needs them again. Runs at the end of every scan."""
    return {
        "vendors": conn.execute(
            "DELETE FROM vendors WHERE name NOT IN (SELECT vendor FROM plugins WHERE vendor IS NOT NULL)"
        ).rowcount,
        "plugin_types": conn.execute(
            "DELETE FROM plugin_types WHERE name NOT IN (SELECT plugin_type FROM plugins WHERE plugin_type IS NOT NULL)"
        ).rowcount,
        "categories": conn.execute(
            "DELETE FROM categories WHERE id NOT IN (SELECT category_id FROM plugin_categories)"
        ).rowcount,
    }


def remove_stale_plugins(conn, seen_host_paths: set) -> int:
    """Scanner is the source of truth for what's currently on disk — but unlike a
    blanket 'DELETE FROM plugins then reinsert everything', only remove rows for
    plugins that genuinely aren't there anymore. A full wipe would reset every
    plugin's id (AUTOINCREMENT never reuses one), silently orphaning every row in
    action_log that references one — and since action_log.plugin_id is a real FK,
    that wipe would outright fail once any action exists (confirmed: 'FOREIGN KEY
    constraint failed'). upsert_plugin's ON CONFLICT(host_path) already preserves
    the id for anything still present; this only deletes what's actually gone,
    which correctly (and intentionally) cascades to remove any action_log rows
    for a plugin the owner deleted/moved outside the app — see AGENTS.md."""
    if not seen_host_paths:
        conn.execute("DELETE FROM plugins")
        return 0
    placeholders = ",".join("?" for _ in seen_host_paths)
    cur = conn.execute(f"DELETE FROM plugins WHERE host_path NOT IN ({placeholders})", tuple(seen_host_paths))
    return cur.rowcount


def upsert_plugin(conn, record: dict) -> int:
    # vendor*/plugin_type are protected on UPDATE when the existing row was
    # manually edited (vendor_source/plugin_type_source='manual') — otherwise a
    # rescan would silently overwrite a manual correction right at the first
    # write of the pipeline, before any of the later "skip manual rows" checks
    # in app/scan_service.py even run. "plugins.<col>" in the CASE refers to the
    # pre-conflict (existing) row's value; this is standard SQLite upsert syntax.
    cur = conn.execute(
        """
        INSERT INTO plugins
            (name, normalized_name, vendor, vendor_source, version, format, plugin_type, plugin_type_source, categories,
             host_path, size_bytes, status, status_reason, scanned_at, bitness)
        VALUES (:name, :normalized_name, :vendor, :vendor_source, :version, :format, :plugin_type, 'scan', :categories,
                :host_path, :size_bytes, :status, :status_reason, :scanned_at, :bitness)
        ON CONFLICT(host_path) DO UPDATE SET
            name = CASE WHEN plugins.name_source='manual' THEN plugins.name ELSE excluded.name END,
            normalized_name = CASE WHEN plugins.name_source='manual' THEN plugins.normalized_name ELSE excluded.normalized_name END,
            vendor = CASE WHEN plugins.vendor_source='manual' THEN plugins.vendor ELSE excluded.vendor END,
            vendor_source = CASE WHEN plugins.vendor_source='manual' THEN plugins.vendor_source ELSE excluded.vendor_source END,
            -- Reset on every non-manual rescan rather than leaving a stale note from a
            -- previous run's enrichment pass — a real bug this fixed: after the FL Studio
            -- name cross-reference (app/fl_database.py) corrected a plugin's name,
            -- app/enrichment.py's old name-keyed lookup stopped matching and never re-set
            -- vendor, but the *previous* run's vendor_note lingered here untouched, showing
            -- a confident-sounding note next to an empty vendor. Later steps in the same
            -- scan (app/enrichment.py) re-populate this within the same run when they apply.
            vendor_note = CASE WHEN plugins.vendor_source='manual' THEN plugins.vendor_note ELSE NULL END,
            version=excluded.version, format=excluded.format,
            plugin_type = CASE WHEN plugins.plugin_type_source='manual' THEN plugins.plugin_type ELSE excluded.plugin_type END,
            categories=excluded.categories, size_bytes=excluded.size_bytes, status=excluded.status,
            status_reason=excluded.status_reason, scanned_at=excluded.scanned_at,
            bitness=excluded.bitness
        RETURNING id
        """,
        record,
    )
    return cur.fetchone()[0]


def link_plugins(conn, plugin_id: int, other_id: int) -> str:
    """Manually declares two rows the same plugin (e.g. renamed/rebranded twins the
    name+vendor matcher can't pair). Joins whichever link_group either already has,
    otherwise mints a new one. Returns the group id."""
    import uuid
    rows = conn.execute("SELECT id, link_group FROM plugins WHERE id IN (?, ?)", (plugin_id, other_id)).fetchall()
    existing = next((r[1] for r in rows if r[1]), None)
    gid = existing or uuid.uuid4().hex[:12]
    conn.execute("UPDATE plugins SET link_group=? WHERE id IN (?, ?)", (gid, plugin_id, other_id))
    if existing:  # merge the other side's old group too
        for r in rows:
            if r[1] and r[1] != gid:
                conn.execute("UPDATE plugins SET link_group=? WHERE link_group=?", (gid, r[1]))
    return gid


def unlink_plugin(conn, plugin_id: int) -> None:
    row = conn.execute("SELECT link_group FROM plugins WHERE id=?", (plugin_id,)).fetchone()
    conn.execute("UPDATE plugins SET link_group=NULL WHERE id=?", (plugin_id,))
    if row and row[0]:  # a group of one is meaningless — dissolve it
        left = conn.execute("SELECT id FROM plugins WHERE link_group=?", (row[0],)).fetchall()
        if len(left) == 1:
            conn.execute("UPDATE plugins SET link_group=NULL WHERE id=?", (left[0][0],))


def set_fl_info(conn, plugin_id: int, detected: bool, fl_name: str | None, fl_type: str | None = None) -> None:
    """Records whether FL Studio's own plugin database also knows this plugin,
    its resolved name there, and its effect/synth type (app/fl_database.py) —
    kept even when it matches ours, so the UI can show a "confirmed by FL
    Studio" signal, not just a correction flag."""
    conn.execute(
        "UPDATE plugins SET fl_detected=?, fl_name=?, fl_type=? WHERE id=?",
        (1 if detected else 0, fl_name, fl_type, plugin_id),
    )


def fl_cloud_host_paths(conn) -> set:
    """Lowercased host_paths the owner marked as FL Cloud plugins."""
    return {row[0].lower() for row in conn.execute("SELECT host_path FROM fl_cloud_plugins")}


def set_fl_cloud(conn, plugin_id: int, on: bool) -> bool:
    """Marks/unmarks a plugin as FL Cloud. Persists by host_path so the flag
    survives rescans. Returns False if the plugin doesn't exist."""
    row = conn.execute("SELECT host_path, name FROM plugins WHERE id=?", (plugin_id,)).fetchone()
    if row is None:
        return False
    if on:
        conn.execute(
            "INSERT INTO fl_cloud_plugins (host_path, name, added_at) VALUES (?, ?, ?) "
            "ON CONFLICT(host_path) DO UPDATE SET name=excluded.name",
            (row[0], row[1], datetime.now(timezone.utc).isoformat()),
        )
    else:
        conn.execute("DELETE FROM fl_cloud_plugins WHERE host_path=?", (row[0],))
    conn.execute("UPDATE plugins SET fl_cloud=? WHERE id=?", (1 if on else 0, plugin_id))
    return True


def fl_links(conn) -> dict[str, int]:
    """Every FL plugin-name link: lowercased .fst stem -> VEXT plugin_id."""
    return {name.lower(): plugin_id for name, plugin_id in conn.execute("SELECT name, plugin_id FROM fl_links")}


def set_fl_link(conn, name: str, plugin_id: int | None) -> None:
    """Links an FL plugin name to a VEXT plugin, replacing any earlier link for that name.
    plugin_id None removes the link."""
    if plugin_id is None:
        conn.execute("DELETE FROM fl_links WHERE name=?", (name,))
        return
    conn.execute(
        "INSERT INTO fl_links (name, plugin_id, created_at) VALUES (?, ?, ?) "
        "ON CONFLICT(name) DO UPDATE SET plugin_id=excluded.plugin_id",
        (name, plugin_id, datetime.now(UTC).isoformat()),
    )


def fl_ignored(conn) -> set[str]:
    """FL plugin names (lowercased .fst stems) the owner chose to ignore in FL Studio Sync's check."""
    return {name.lower() for (name,) in conn.execute("SELECT name FROM fl_ignored")}


def set_fl_ignored(conn, name: str, ignored: bool) -> None:
    """Ignores an FL plugin name VEXT doesn't recognize (the check stops flagging it), or stops ignoring it."""
    if ignored:
        conn.execute("INSERT OR IGNORE INTO fl_ignored (name, created_at) VALUES (?, ?)",
                     (name, datetime.now(UTC).isoformat()))
    else:
        conn.execute("DELETE FROM fl_ignored WHERE name=?", (name,))


def set_plugin_type_manual(conn, plugin_id: int, plugin_type: str) -> None:
    conn.execute(
        "UPDATE plugins SET plugin_type=?, plugin_type_source='manual' WHERE id=?",
        (plugin_type, plugin_id),
    )


def seed_scan_roots(conn) -> None:
    """Keeps the built-in scan roots (app/config.py:ROOTS) in sync in the
    database — same pattern as seed_vendors: never overwrites a row the
    owner has since edited via the Configuration tab (source becomes
    'manual' on edit, see update_scan_root), and never touches `enabled` (so
    disabling a built-in root sticks across reseeds/rescans instead of
    silently coming back). A built-in root the owner outright *deleted* is
    skipped entirely — see removed_scan_root_paths/delete_scan_root — so
    deleting one is truly permanent, not just disabled-and-hidden. The scan
    pipeline reads scan roots from here now, not directly from config.ROOTS
    — see app/scan_service.py."""
    from app.config import detect_locations

    fl_override = get_setting(conn, "fl_studio_root", "") or ""
    removed = {row[0] for row in conn.execute("SELECT host_path FROM removed_scan_root_paths")}
    now = datetime.now(timezone.utc).isoformat()
    locations = detect_locations(fl_override)
    # A row without a key that already sits at a standard path (created before location_key
    # existed, or added by hand) is adopted as that location, so it isn't duplicated as a
    # separate custom path.
    for loc in locations:
        conn.execute(
            "UPDATE scan_roots SET location_key=?, label=?, "
            "source=CASE WHEN source='custom' THEN 'builtin' ELSE source END "
            "WHERE location_key IS NULL AND host_path=?",
            (loc.key, loc.label, loc.host_path),
        )
    for loc in locations:
        row = conn.execute(
            "SELECT id, host_path, source FROM scan_roots WHERE location_key=?", (loc.key,)
        ).fetchone()
        if row is None:
            if loc.host_path in removed:
                continue
            try:
                conn.execute(
                    """
                    INSERT INTO scan_roots (host_path, format, default_type, extensions, recurse_vendor_dirs,
                                            source, enabled, added_at, location_key, detected_path, label)
                    VALUES (?, ?, ?, ?, ?, 'builtin', 1, ?, ?, ?, ?)
                    """,
                    (loc.host_path, loc.format, loc.default_type, ",".join(loc.extensions),
                     1 if loc.recurse_vendor_dirs else 0, now, loc.key, loc.host_path, loc.label),
                )
            except sqlite3.IntegrityError:
                # The same folder is already a custom or edited row. Leave that
                # one alone; the owner's own entry wins over detection.
                continue
            continue

        root_id, current_path, source = row
        # Always refresh what detection found, so Reset can return to it.
        conn.execute(
            "UPDATE scan_roots SET detected_path=?, label=? WHERE id=?",
            (loc.host_path, loc.label, root_id),
        )
        # Built-in rows follow detection (e.g. FL Studio updated to a new
        # version). Edited rows keep the owner's path.
        if source == "builtin":
            try:
                conn.execute(
                    """
                    UPDATE scan_roots SET host_path=?, format=?, default_type=?, extensions=?,
                                          recurse_vendor_dirs=?
                    WHERE id=?
                    """,
                    (loc.host_path, loc.format, loc.default_type, ",".join(loc.extensions),
                     1 if loc.recurse_vendor_dirs else 0, root_id),
                )
            except sqlite3.IntegrityError:
                pass

    # Standard rows whose location no longer exists (e.g. a retired format folder).
    # Built-in ones go away; ones the owner edited become plain custom paths,
    # so their path is never lost.
    current_keys = {loc.key for loc in locations}
    for root_id, key, source in conn.execute(
        "SELECT id, location_key, source FROM scan_roots WHERE location_key IS NOT NULL"
    ).fetchall():
        if key in current_keys:
            continue
        if source == "builtin":
            conn.execute("DELETE FROM scan_roots WHERE id=?", (root_id,))
        else:
            conn.execute("UPDATE scan_roots SET location_key=NULL WHERE id=?", (root_id,))


def reset_scan_root(conn, root_id: int) -> None:
    """Puts a standard location back to what detection found, and marks it
    built-in again so future detection updates apply to it."""
    row = conn.execute(
        "SELECT detected_path, host_path FROM scan_roots WHERE id=? AND location_key IS NOT NULL",
        (root_id,),
    ).fetchone()
    if row is None or not row[0]:
        raise ValueError("Only standard locations can be reset to their detected path.")
    detected = row[0]
    conn.execute("DELETE FROM removed_scan_root_paths WHERE host_path=?", (detected,))
    conn.execute("UPDATE scan_roots SET host_path=?, source='builtin' WHERE id=?", (detected, root_id))


def list_scan_roots(conn, enabled_only: bool = False):
    """Raw rows for the Configuration UI (id/host_path/format/.../source/enabled)."""
    conn.row_factory = sqlite3.Row
    sql = "SELECT * FROM scan_roots"
    if enabled_only:
        sql += " WHERE enabled=1"
    sql += " ORDER BY source, host_path"
    return conn.execute(sql).fetchall()


def scan_roots_as_objects(conn) -> list:
    """Every enabled scan root (built-in + custom + manually-edited alike) as
    real ScanRoot instances, ready for app/scanner.py:scan_root — the single
    source of truth the scan pipeline actually walks now."""
    from app.config import ScanRoot

    out = []
    for row in list_scan_roots(conn, enabled_only=True):
        ext = tuple(e for e in (row["extensions"] or "").split(",") if e)
        out.append(ScanRoot(
            container_path=row["host_path"],
            host_path=row["host_path"],
            format=row["format"],
            default_type=row["default_type"],
            extensions=ext,
            recurse_vendor_dirs=bool(row["recurse_vendor_dirs"]),
        ))
    return out


def add_scan_root(conn, host_path: str, format: str, default_type: str = "unknown",
                   extensions: tuple = (), recurse_vendor_dirs: bool = True) -> int:
    cur = conn.execute(
        """
        INSERT INTO scan_roots (host_path, format, default_type, extensions, recurse_vendor_dirs, source, enabled, added_at)
        VALUES (?, ?, ?, ?, ?, 'custom', 1, ?)
        """,
        (host_path, format, default_type, ",".join(extensions), 1 if recurse_vendor_dirs else 0,
         datetime.now(timezone.utc).isoformat()),
    )
    return cur.lastrowid


def update_scan_root(conn, root_id: int, **fields) -> None:
    """fields: any of host_path/format/default_type/extensions(list|tuple)/
    recurse_vendor_dirs(bool). Editing a builtin-sourced row marks it
    'manual' so the next reseed (see seed_scan_roots) never overwrites the
    owner's own edit."""
    if "extensions" in fields and isinstance(fields["extensions"], (list, tuple)):
        fields["extensions"] = ",".join(fields["extensions"])
    if "recurse_vendor_dirs" in fields:
        fields["recurse_vendor_dirs"] = 1 if fields["recurse_vendor_dirs"] else 0
    if not fields:
        return
    row = conn.execute("SELECT source FROM scan_roots WHERE id=?", (root_id,)).fetchone()
    if row is None:
        return
    set_clause = ", ".join(f"{k}=?" for k in fields)
    if row[0] == "builtin":
        set_clause += ", source='manual'"
    conn.execute(f"UPDATE scan_roots SET {set_clause} WHERE id=?", (*fields.values(), root_id))


def set_scan_root_enabled(conn, root_id: int, enabled: bool) -> None:
    conn.execute("UPDATE scan_roots SET enabled=? WHERE id=?", (1 if enabled else 0, root_id))


def delete_scan_root(conn, root_id: int) -> None:
    """Hard delete — works for any scan root now, built-in included. A
    'custom' row just goes (nothing re-seeds it). A 'builtin'/'manual' row
    also gets tombstoned in removed_scan_root_paths so seed_scan_roots()
    never resurrects it — see list_removed_scan_root_paths/
    restore_scan_root_path for the undo path."""
    row = conn.execute("SELECT source, host_path FROM scan_roots WHERE id=?", (root_id,)).fetchone()
    if row is None:
        return
    source, host_path = row
    conn.execute("DELETE FROM scan_roots WHERE id=?", (root_id,))
    if source in ("builtin", "manual"):
        conn.execute(
            "INSERT INTO removed_scan_root_paths (host_path, removed_at) VALUES (?, ?) "
            "ON CONFLICT(host_path) DO NOTHING",
            (host_path, datetime.now(timezone.utc).isoformat()),
        )


def list_removed_scan_root_paths(conn):
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM removed_scan_root_paths ORDER BY host_path").fetchall()


def restore_scan_root_path(conn, host_path: str) -> None:
    """Undoes delete_scan_root() for a built-in path — clears the tombstone
    so it reappears on the next seed_scan_roots() call (next rescan, or
    immediately if the caller reseeds right away)."""
    conn.execute("DELETE FROM removed_scan_root_paths WHERE host_path=?", (host_path,))


def list_ignored_scan_folders(conn):
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM ignored_scan_folders ORDER BY host_path").fetchall()


def add_ignored_scan_folder(conn, host_path: str) -> None:
    conn.execute(
        "INSERT INTO ignored_scan_folders (host_path, added_at) VALUES (?, ?) ON CONFLICT(host_path) DO NOTHING",
        (host_path, datetime.now(timezone.utc).isoformat()),
    )


def list_ignored_plugin_paths(conn):
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM ignored_plugin_paths ORDER BY name").fetchall()


def ignored_plugin_host_paths(conn) -> set:
    """Lowercased set for the scan pipeline to check each found file against
    — see app/scan_service.py. A plain Python set, not a SQL filter, since
    the scanner already has each record's host_path in hand by the time
    this matters."""
    return {row[0].lower() for row in conn.execute("SELECT host_path FROM ignored_plugin_paths")}


def add_ignored_plugin_path(conn, host_path: str, name: str) -> None:
    conn.execute(
        "INSERT INTO ignored_plugin_paths (host_path, name, added_at) VALUES (?, ?, ?) "
        "ON CONFLICT(host_path) DO UPDATE SET name=excluded.name",
        (host_path, name, datetime.now(timezone.utc).isoformat()),
    )


def remove_ignored_plugin_path(conn, host_path: str) -> None:
    conn.execute("DELETE FROM ignored_plugin_paths WHERE host_path=?", (host_path,))


def get_enrichment_suggestion(conn, plugin_id: int):
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM enrichment_suggestions WHERE plugin_id=?", (plugin_id,)).fetchone()


def upsert_enrichment_suggestion(conn, plugin_id: int, data: dict) -> None:
    """data: the parsed JSON from app/codex_research.py — keys matching the
    enrichment_suggestions columns (vendor/vendor_url/vendor_status/
    vendor_status_note/plugin_type/description_es/confidence/notes as plain
    values; categories/sources as lists, joined here). A field the new result leaves empty keeps
    what an earlier (focused) run already found, so one question's answer never erases another's."""
    conn.execute(
        """
        INSERT INTO enrichment_suggestions
            (plugin_id, vendor, vendor_url, vendor_status, vendor_status_note, plugin_type,
             categories, description_es, confidence, sources, notes, created_at, store_url,
             kvr_url, equipboard_url, links_checked)
        VALUES (:plugin_id, :vendor, :vendor_url, :vendor_status, :vendor_status_note, :plugin_type,
                :categories, :description_es, :confidence, :sources, :notes, :created_at, :store_url,
                :kvr_url, :equipboard_url, :links_checked)
        ON CONFLICT(plugin_id) DO UPDATE SET
            vendor=COALESCE(NULLIF(excluded.vendor, ''), enrichment_suggestions.vendor),
            vendor_url=COALESCE(NULLIF(excluded.vendor_url, ''), enrichment_suggestions.vendor_url),
            vendor_status=COALESCE(NULLIF(excluded.vendor_status, ''), enrichment_suggestions.vendor_status),
            vendor_status_note=COALESCE(NULLIF(excluded.vendor_status_note, ''), enrichment_suggestions.vendor_status_note),
            plugin_type=COALESCE(NULLIF(excluded.plugin_type, ''), enrichment_suggestions.plugin_type),
            categories=COALESCE(NULLIF(excluded.categories, ''), enrichment_suggestions.categories),
            description_es=COALESCE(NULLIF(excluded.description_es, ''), enrichment_suggestions.description_es),
            store_url=COALESCE(NULLIF(excluded.store_url, ''), enrichment_suggestions.store_url),
            kvr_url=COALESCE(NULLIF(excluded.kvr_url, ''), enrichment_suggestions.kvr_url),
            equipboard_url=COALESCE(NULLIF(excluded.equipboard_url, ''), enrichment_suggestions.equipboard_url),
            links_checked=MAX(excluded.links_checked, enrichment_suggestions.links_checked),
            confidence=excluded.confidence, sources=excluded.sources, notes=excluded.notes,
            created_at=excluded.created_at
        """,
        {
            "plugin_id": plugin_id,
            "vendor": data.get("vendor"),
            "store_url": data.get("store_url"),
            "kvr_url": data.get("kvr_url"),
            "equipboard_url": data.get("equipboard_url"),
            "links_checked": 1 if data.get("links_checked") else 0,
            "vendor_url": data.get("vendor_url"),
            "vendor_status": data.get("vendor_status"),
            "vendor_status_note": data.get("vendor_status_note"),
            "plugin_type": data.get("plugin_type"),
            "categories": ",".join(data.get("categories") or []),
            "description_es": data.get("description_es"),
            "confidence": data.get("confidence"),
            "sources": ",".join(data.get("sources") or []),
            "notes": data.get("notes"),
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    _set_suggestion_secondary_vendors(conn, plugin_id, data.get("secondary_vendors"))


def _set_suggestion_secondary_vendors(conn, plugin_id: int, names) -> None:
    import json
    conn.execute(
        "UPDATE enrichment_suggestions SET secondary_vendors=? WHERE plugin_id=?",
        (json.dumps(list(names or [])), plugin_id),
    )


def delete_enrichment_suggestion(conn, plugin_id: int) -> None:
    conn.execute("DELETE FROM enrichment_suggestions WHERE plugin_id=?", (plugin_id,))


def remove_ignored_scan_folder(conn, folder_id: int) -> None:
    conn.execute("DELETE FROM ignored_scan_folders WHERE id=?", (folder_id,))


def consolidate_candidates(conn, min_count: int = 2):
    """Groups currently-ignored out-of-scope items by a shared ancestor
    folder, for any ancestor with >= min_count ignored items under it —
    candidates for turning into a single ignored_scan_folders rule instead
    of N individual per-file ignores (see the Configuration tab's
    "Consolidate" button). Returns [{"parent": str, "count": int, "names":
    [str, ...]}, ...].

    Real bug found and fixed: this used to only match the *exact* immediate
    parent folder, missing the common case where each item ships in its own
    subfolder — e.g. Spectral Plugins' OCS-45/Pancz/Spacer each sit in their
    own "<Product>\\Lib\\" (2 extra levels above the immediate parent before
    anything is shared), while Waves' WaveShell1-VST3 13.0/17.1 only need 1
    extra level (differing version subfolders). There's no single fixed
    depth that covers every install layout, so this now climbs every
    ancestor level of each item's parent folder and groups by whichever
    ancestor enough items actually agree on — floored at 3 path segments
    (e.g. "C:\\Program Files\\X") so it never suggests something as broad as
    bare "C:\\Program Files", which nearly everything shares and would be a
    useless (and risky) suggestion."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT host_path, name FROM out_of_scope_plugins WHERE ignored=1").fetchall()

    ancestor_items: dict[str, list[dict]] = {}
    for row in rows:
        parts = row["host_path"].rsplit("\\", 1)[0].split("\\")
        for depth in range(len(parts), 2, -1):  # floor: keep at least "C:\X\Y"
            ancestor = "\\".join(parts[:depth])
            ancestor_items.setdefault(ancestor, []).append({"host_path": row["host_path"], "name": row["name"]})

    qualifying = {a: items for a, items in ancestor_items.items() if len(items) >= min_count}
    result = []
    seen_item_sets: list[frozenset] = []
    for ancestor in sorted(qualifying, key=len, reverse=True):  # deepest/most-specific first
        items = qualifying[ancestor]
        item_paths = frozenset(i["host_path"] for i in items)
        if item_paths in seen_item_sets:
            continue  # a deeper ancestor already covers this exact same set of items
        seen_item_sets.append(item_paths)
        result.append({"parent": ancestor, "count": len(items), "names": [i["name"] for i in items]})
    result.sort(key=lambda c: c["parent"])
    return result


def consolidate_ignored_folder(conn, parent_path: str) -> int:
    """Turns every currently-ignored out-of-scope item under parent_path into
    one ignored_scan_folders rule, then drops their now-redundant individual
    rows. Unlike per-item ignores (which only ever cover what already
    existed at ignore time), the folder rule also keeps any *new* file added
    to that folder later from ever showing up as out-of-scope — see
    app/full_scan.py. Returns how many individual rows were consolidated."""
    add_ignored_scan_folder(conn, parent_path)
    conn.row_factory = sqlite3.Row
    prefix = parent_path.lower() + "\\"
    to_delete = [
        row["id"] for row in conn.execute("SELECT id, host_path FROM out_of_scope_plugins WHERE ignored=1")
        if row["host_path"].lower().startswith(prefix)
    ]
    for item_id in to_delete:
        conn.execute("DELETE FROM out_of_scope_plugins WHERE id=?", (item_id,))
    return len(to_delete)


def get_setting(conn, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row and row[0] is not None else default


def set_setting(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def set_plugin_type_derived(conn, plugin_id: int, plugin_type: str) -> None:
    """Sets plugin_type without marking it manual, so a rescan can re-derive it."""
    conn.execute(
        "UPDATE plugins SET plugin_type=? WHERE id=? AND plugin_type_source != 'manual'",
        (plugin_type, plugin_id),
    )


def upsert_out_of_scope(conn, host_path: str, name: str, format_guess: str, size_bytes: int) -> None:
    conn.execute(
        """
        INSERT INTO out_of_scope_plugins (host_path, name, format_guess, size_bytes, found_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(host_path) DO UPDATE SET
            name=excluded.name, format_guess=excluded.format_guess, size_bytes=excluded.size_bytes
        """,
        (host_path, name, format_guess, size_bytes, datetime.now(timezone.utc).isoformat()),
    )


def list_out_of_scope(conn, include_ignored: bool = False):
    conn.row_factory = sqlite3.Row
    sql = "SELECT * FROM out_of_scope_plugins"
    if not include_ignored:
        sql += " WHERE ignored=0"
    sql += " ORDER BY name"
    return conn.execute(sql).fetchall()


def set_out_of_scope_ignored(conn, item_id: int, ignored: bool) -> None:
    conn.execute("UPDATE out_of_scope_plugins SET ignored=? WHERE id=?", (1 if ignored else 0, item_id))


def known_plugin_host_paths(conn) -> set:
    return {row[0].lower() for row in conn.execute("SELECT host_path FROM plugins")}


def prune_now_cataloged_out_of_scope(conn) -> int:
    """Removes out_of_scope_plugins rows whose host_path is now a real,
    properly-cataloged plugin — e.g. after the owner uses "Add path to scope"
    (app/custom_roots.py) and rescans. Runs on every rescan, not just a full
    C: scan, since a plugin can become cataloged other ways too (moved by
    hand into a known root, etc.)."""
    cur = conn.execute("""
        DELETE FROM out_of_scope_plugins
        WHERE EXISTS (
            SELECT 1 FROM plugins p
            WHERE lower(p.host_path) = lower(out_of_scope_plugins.host_path)
        )
    """)
    return cur.rowcount


def reconcile_missing_out_of_scope(conn) -> int:
    """Drops out_of_scope_plugins rows whose file is no longer on disk — the
    owner deleted it directly, outside the app (there's no quarantine step
    for these, so a direct delete is the only way one ever disappears). Same
    reasoning as reconcile_missing_quarantine: nothing was ever tracked in
    the `plugins` table for these, so there's no plugin row to touch, just a
    stale listing that would otherwise sit there forever offering "Delete
    Forever" on a file that's already gone. Runs on every rescan and on
    demand via the Sync button, matching quarantine's reconcile."""
    import os

    from app.applog import log_event

    conn.row_factory = sqlite3.Row
    removed = 0
    for row in conn.execute("SELECT id, host_path FROM out_of_scope_plugins"):
        if not os.path.exists(row["host_path"]):
            log_event(
                f"Out-of-scope entry '{row['host_path']}' was no longer found on disk — "
                f"it was likely deleted directly, outside the app. Removed its tracking entry "
                f"(out_of_scope_plugins id={row['id']})."
            )
            conn.execute("DELETE FROM out_of_scope_plugins WHERE id=?", (row["id"],))
            removed += 1
    return removed


def fill_vendor_from_installer(conn, plugin_id: int, vendor: str) -> None:
    conn.execute(
        "UPDATE plugins SET vendor=?, vendor_source='installer' WHERE id=? AND (vendor IS NULL OR vendor='')",
        (vendor, plugin_id),
    )


def fill_vendor_override(conn, plugin_id: int, vendor: str, note: str) -> None:
    """For research-backed identifications the scanner/installer-index couldn't make on
    their own (see app/enrichment.py). Never overwrites a higher-confidence value."""
    conn.execute(
        "UPDATE plugins SET vendor=?, vendor_source='inferred', vendor_note=? WHERE id=? AND (vendor IS NULL OR vendor='')",
        (vendor, note, plugin_id),
    )


def mark_vendor_ambiguous(conn, plugin_id: int, note: str) -> None:
    """Leaves vendor blank on purpose — research found 2+ plausible candidates and
    couldn't tell which is actually installed without opening the plugin. Recording
    a guess here would risk the same false-grouping bug as the DeEsser case."""
    conn.execute(
        "UPDATE plugins SET vendor_note=? WHERE id=? AND (vendor IS NULL OR vendor='')",
        (note, plugin_id),
    )


def all_plugins(conn) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT * FROM plugins ORDER BY normalized_name, format"
    ).fetchall()


def get_plugin(conn, plugin_id: int):
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM plugins WHERE id=?", (plugin_id,)).fetchone()


def create_action(conn, plugin_id: int, action: str, reason: str) -> int:
    cur = conn.execute(
        "INSERT INTO action_log (ts, plugin_id, action, reason, dry_run, applied) VALUES (?, ?, ?, ?, 1, 0)",
        (datetime.now(timezone.utc).isoformat(), plugin_id, action, reason),
    )
    return cur.lastrowid


def list_actions(conn, applied: bool | None = None):
    conn.row_factory = sqlite3.Row
    sql = """
        SELECT a.*, p.name AS plugin_name, p.host_path, p.format, p.vendor
        FROM action_log a JOIN plugins p ON p.id = a.plugin_id
    """
    params = ()
    if applied is not None:
        sql += " WHERE a.applied=?"
        params = (1 if applied else 0,)
    sql += " ORDER BY a.ts DESC"
    return conn.execute(sql, params).fetchall()


def get_action(conn, action_id: int):
    conn.row_factory = sqlite3.Row
    return conn.execute(
        """
        SELECT a.*, p.name AS plugin_name, p.host_path, p.format, p.vendor
        FROM action_log a JOIN plugins p ON p.id = a.plugin_id
        WHERE a.id=?
        """,
        (action_id,),
    ).fetchone()


def delete_action(conn, action_id: int) -> None:
    conn.execute("DELETE FROM action_log WHERE id=? AND applied=0", (action_id,))


def mark_action_applied(conn, action_id: int, new_host_path: str | None) -> None:
    conn.execute("UPDATE action_log SET applied=1, dry_run=0 WHERE id=?", (action_id,))
    if new_host_path:
        plugin_id = conn.execute(
            "SELECT plugin_id FROM action_log WHERE id=?", (action_id,)
        ).fetchone()[0]
        # Real bug found and fixed: reorganize can replace an already-organized
        # copy sitting at the destination (see app/actions.py:_replace_existing)
        # — that copy was its own separate catalog row (host_path is UNIQUE,
        # different path = different plugin row), and its file just got
        # overwritten out from under it. Without this, the UPDATE below hit
        # "UNIQUE constraint failed: plugins.host_path" against that still-
        # present stale row; the whole apply transaction then rolled back
        # silently (action_log's applied=1 above included) while the file move
        # itself — a real filesystem operation, not part of the SQL
        # transaction — had already happened and couldn't un-happen. Net
        # result: the file was genuinely moved, but the action stayed stuck
        # "not applied" forever, and GET /api/actions' reconcile check flagged
        # it "source not found" since the original path was now empty.
        # Dropping the stale row first (cascades to its plugin_categories/
        # action_log/etc. via ON DELETE CASCADE) makes the destination path
        # free again before this plugin claims it.
        conn.execute("DELETE FROM plugins WHERE host_path=? AND id!=?", (new_host_path, plugin_id))
        conn.execute("UPDATE plugins SET host_path=? WHERE id=?", (new_host_path, plugin_id))


def active_action_for_plugin(conn, plugin_id: int):
    """The one action (if any) that's either still pending approval, or applied
    but unresolved (e.g. sitting in quarantine awaiting restore/permanent-delete).
    Used to block duplicate proposals and to show status in the browse table."""
    conn.row_factory = sqlite3.Row
    return conn.execute(
        """
        SELECT * FROM action_log
        WHERE plugin_id=? AND (applied=0 OR resolution IS NULL)
        ORDER BY ts DESC LIMIT 1
        """,
        (plugin_id,),
    ).fetchone()


def active_actions_by_plugin(conn) -> dict[int, dict]:
    """Bulk version of active_action_for_plugin, for the plugin-list endpoint."""
    conn.row_factory = sqlite3.Row
    result = {}
    for row in conn.execute(
        "SELECT * FROM action_log WHERE applied=0 OR resolution IS NULL ORDER BY ts DESC"
    ):
        result.setdefault(row["plugin_id"], dict(row))  # first hit per plugin = most recent (ts DESC)
    return result


def quarantined_host_paths(conn) -> set:
    """Original host_paths of everything still unresolved in quarantine. These
    plugin rows must survive remove_stale_plugins()'s "not seen on disk this
    scan" check even though their file genuinely isn't at that path anymore (we
    moved it) — otherwise a rescan would cascade-delete the action_log row and
    orphan the physically-quarantined file from the Quarantine tab."""
    conn.row_factory = sqlite3.Row
    return {
        row["host_path"]
        for row in conn.execute(
            """
            SELECT p.host_path FROM action_log a JOIN plugins p ON p.id = a.plugin_id
            WHERE a.action='quarantine' AND a.applied=1 AND a.resolution IS NULL
            """
        )
    }


def list_quarantined(conn):
    conn.row_factory = sqlite3.Row
    return conn.execute(
        """
        SELECT a.*, p.name AS plugin_name, p.host_path, p.vendor, p.format, p.size_bytes
        FROM action_log a JOIN plugins p ON p.id = a.plugin_id
        WHERE a.action='quarantine' AND a.applied=1 AND a.resolution IS NULL
        ORDER BY a.ts DESC
        """
    ).fetchall()


def drop_action(conn, action_id: int) -> None:
    """Removes an action_log row outright once it's reached a terminal state
    (restored, reorganize applied) — action_log is working state, not history
    (see AGENTS.md): nothing browses resolved actions anymore, so nothing is
    lost by not keeping the row around."""
    conn.execute("DELETE FROM action_log WHERE id=?", (action_id,))


def reconcile_missing_quarantine(conn) -> int:
    """Detects quarantine entries whose file is no longer at data/quarantine/...
    (the owner deleted it directly, outside the app) and drops the plugin's
    registry entirely (cascades to its action_log row) — there's nothing left
    to restore or permanently delete, so keeping the entry around would just
    show a broken pair of buttons for a file that isn't there. Returns the
    count removed; full detail (which plugin, which path) goes to the plain
    log file only (app/applog.py) — callers surface just the count so the UI
    can show one generic toast instead of a history list."""
    from app.actions import quarantine_target
    from app.applog import log_event
    from app.config import QUARANTINE_DIR

    quarantine_dir = get_setting(conn, "quarantine_dir", QUARANTINE_DIR)
    removed = 0
    for row in list_quarantined(conn):
        if not quarantine_target(row["host_path"], quarantine_dir).exists():
            log_event(
                f"Quarantine entry for '{row['plugin_name']}' (host path: {row['host_path']}) "
                f"was no longer found on disk — it was likely deleted directly, outside the app. "
                f"Removed its tracking entry (action_log id={row['id']}, plugin_id={row['plugin_id']})."
            )
            conn.execute("DELETE FROM plugins WHERE id=?", (row["plugin_id"],))
            removed += 1
    return removed


def category_tags_by_plugin(conn) -> dict[int, list[tuple[str, str, str]]]:
    """plugin_id -> [(category name, slug, source), ...]"""
    conn.row_factory = sqlite3.Row
    result: dict[int, list[tuple[str, str, str]]] = {}
    for row in conn.execute(
        """
        SELECT pc.plugin_id, c.name, c.slug, pc.source
        FROM plugin_categories pc JOIN categories c ON c.id = pc.category_id
        ORDER BY pc.plugin_id, c.name
        """
    ):
        result.setdefault(row["plugin_id"], []).append((row["name"], row["slug"], row["source"]))
    return result


def category_slugs_by_plugin(conn) -> dict[int, list[str]]:
    """plugin_id -> [category slug, ...] — for logic that needs to re-apply the same
    tags elsewhere (e.g. propagating to a format-sibling), as opposed to display."""
    conn.row_factory = sqlite3.Row
    result: dict[int, list[str]] = {}
    for row in conn.execute(
        """
        SELECT pc.plugin_id, c.slug
        FROM plugin_categories pc JOIN categories c ON c.id = pc.category_id
        """
    ):
        result.setdefault(row["plugin_id"], []).append(row["slug"])
    return result


# ---------- multivendor + vendor investigation ----------

MAX_SECONDARY_VENDORS = 5


def _ensure_investigation_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS plugin_secondary_vendors (
            plugin_id INTEGER NOT NULL,
            vendor    TEXT NOT NULL,
            position  INTEGER NOT NULL DEFAULT 0,
            source    TEXT NOT NULL DEFAULT 'manual',   -- manual / research
            PRIMARY KEY (plugin_id, vendor),
            FOREIGN KEY (plugin_id) REFERENCES plugins(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS investigation_attempts (
            target       TEXT NOT NULL,      -- ai_batch target (links, categories, vendors, ...)
            item_key     TEXT NOT NULL,      -- plugin id, or vendor name for "vendors"
            attempted_at TEXT NOT NULL,
            PRIMARY KEY (target, item_key)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS vendor_suggestions (
            vendor      TEXT PRIMARY KEY,
            url         TEXT,
            status      TEXT,
            status_note TEXT,
            sources     TEXT,
            confidence  TEXT,
            notes       TEXT,
            created_at  TEXT NOT NULL
        )
        """
    )


def _ensure_picture_tables(conn) -> None:
    """Pictures found on the web (app/pictures.py): candidates waiting for the owner's choice,
    what each plugin's last search came back with, and where a saved picture came from."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS picture_suggestions (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            plugin_id  INTEGER NOT NULL,
            n          INTEGER NOT NULL,          -- rank, 1 = the AI's first choice
            file       TEXT NOT NULL,             -- PNG in cache/pictures/suggestions
            image_url  TEXT NOT NULL,             -- where it was downloaded from (after redirects)
            page_url   TEXT,                      -- the page it was found on
            kind       TEXT,                      -- interface / product / other (the AI's word)
            note       TEXT,
            width      INTEGER,
            height     INTEGER,
            bytes      INTEGER,
            agent      TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (plugin_id) REFERENCES plugins(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS picture_searches (
            plugin_id  INTEGER PRIMARY KEY,
            at         TEXT NOT NULL,
            found      INTEGER NOT NULL DEFAULT 0,   -- candidates kept
            confidence TEXT,
            notes      TEXT,
            rejected   TEXT,                         -- JSON [{url, reason}]: proposed but not kept
            sources    TEXT,                         -- JSON list of pages consulted
            agent      TEXT,
            FOREIGN KEY (plugin_id) REFERENCES plugins(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS picture_meta (
            key      TEXT PRIMARY KEY,   -- the picture file's name without .png (sha1 of the host path)
            source   TEXT NOT NULL,      -- web / auto-capture (no row = a capture)
            url      TEXT,
            page_url TEXT,
            mtime    REAL,               -- the file's mtime when recorded: a newer file replaced it since
            set_at   TEXT NOT NULL
        )
        """
    )
    # Later: the tier the candidate was proposed with (verified / possible) and where it came from
    # (ai / page: read by VEXT itself); which pages the last search read, and the agent that answered.
    _add_missing_columns(conn, "picture_suggestions", [("tier", "TEXT"), ("origin", "TEXT")])
    _add_missing_columns(conn, "picture_searches", [("pages", "TEXT"), ("ai_used", "TEXT")])


def _ensure_fl_ignored_table(conn) -> None:
    """FL Studio plugin names (.fst stems) VEXT doesn't recognize and the owner chose to ignore in the
    browse-tree check (app/fl_reorganize.py): not counted as problems or as unrecognized any more."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fl_ignored (
            name       TEXT PRIMARY KEY COLLATE NOCASE,
            created_at TEXT NOT NULL
        )
        """
    )


def _ensure_fl_links_table(conn) -> None:
    """Manual links from an FL Studio plugin name (an .fst stem in the browse tree) to a VEXT plugin,
    for the ones FL shows under a name VEXT can't match (app/fl_reorganize.py). Keyed case-insensitively."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fl_links (
            name       TEXT PRIMARY KEY COLLATE NOCASE,
            plugin_id  INTEGER NOT NULL REFERENCES plugins(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL
        )
        """
    )


def list_secondary_vendors(conn, plugin_id: int) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT vendor FROM plugin_secondary_vendors WHERE plugin_id=? ORDER BY position, vendor",
        (plugin_id,),
    ).fetchall()]


def set_secondary_vendors(conn, plugin_id: int, names, source: str = "manual") -> list[str]:
    """Replaces this plugin's other vendors. The main vendor (plugins.vendor) is never
    repeated here, duplicates are dropped case-insensitively, and the list is capped at
    MAX_SECONDARY_VENDORS. Returns the stored list."""
    main = (conn.execute("SELECT vendor FROM plugins WHERE id=?", (plugin_id,)).fetchone() or [None])[0]
    cleaned, seen = [], set()
    for raw in names or []:
        name = (raw or "").strip()
        key = name.lower()
        if not name or key in seen or (main and key == main.lower()):
            continue
        seen.add(key)
        cleaned.append(name)
    if len(cleaned) > MAX_SECONDARY_VENDORS:
        raise ValueError(f"A plugin can have at most {MAX_SECONDARY_VENDORS} other vendors.")
    conn.execute("DELETE FROM plugin_secondary_vendors WHERE plugin_id=?", (plugin_id,))
    for position, name in enumerate(cleaned):
        conn.execute(
            "INSERT INTO plugin_secondary_vendors (plugin_id, vendor, position, source) VALUES (?, ?, ?, ?)",
            (plugin_id, name, position, source),
        )
    return cleaned


def upsert_vendor_suggestion(conn, vendor: str, data: dict) -> None:
    import json
    conn.execute(
        """
        INSERT INTO vendor_suggestions (vendor, url, status, status_note, sources, confidence, notes, created_at,
                                        kvr_url, equipboard_url)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(vendor) DO UPDATE SET
            url=excluded.url, status=excluded.status, status_note=excluded.status_note,
            sources=excluded.sources, confidence=excluded.confidence, notes=excluded.notes,
            created_at=excluded.created_at, kvr_url=excluded.kvr_url, equipboard_url=excluded.equipboard_url
        """,
        (vendor, data.get("url"), data.get("status"), data.get("status_note"),
         json.dumps(data.get("sources") or []), data.get("confidence"), data.get("notes"),
         datetime.now(timezone.utc).isoformat(), data.get("kvr_url"), data.get("equipboard_url")),
    )


def list_vendor_suggestions(conn) -> list[dict]:
    import json
    conn.row_factory = sqlite3.Row
    rows = []
    for r in conn.execute("SELECT * FROM vendor_suggestions ORDER BY vendor").fetchall():
        d = dict(r)
        d["sources"] = json.loads(d["sources"] or "[]")
        rows.append(d)
    return rows


def delete_vendor_suggestion(conn, vendor: str) -> None:
    conn.execute("DELETE FROM vendor_suggestions WHERE vendor=?", (vendor,))


def apply_vendor_suggestion(conn, vendor: str) -> None:
    """Applies a researched vendor website/status/links to the vendor row (one entity for all its
    plugins), then refreshes every plugin's cached copy. A field the research left empty keeps
    what the vendor already has."""
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM vendor_suggestions WHERE vendor=?", (vendor,)).fetchone()
    if row is None:
        raise ValueError("No pending research for this vendor.")
    existing = get_vendor_info(conn, vendor) or {}

    def settled(found, field, flag):
        """A link the research found is set. One it looked for and didn't find becomes a "none" mark,
        unless the vendor already has that link or mark."""
        if found:
            return found, False
        if existing.get(field) or existing.get(flag):
            return existing.get(field), None
        return None, True

    url, no_url = settled(row["url"], "url", "no_url")
    kvr, no_kvr = settled(row["kvr_url"], "kvr_url", "no_kvr")
    eqb, no_eqb = settled(row["equipboard_url"], "equipboard_url", "no_equipboard")
    upsert_vendor_reference(
        conn, vendor, url, row["status"] or existing.get("status"),
        row["status_note"] or existing.get("status_note"), source="manual",
        kvr_url=kvr or None, equipboard_url=eqb or None,
        no_url=no_url, no_kvr=no_kvr, no_equipboard=no_eqb,
    )
    delete_vendor_suggestion(conn, vendor)
    sync_plugin_vendor_info(conn)


# ---------- which items an investigation already tried ----------
# A chunk run skips items it has already investigated, so an obscure plugin with no KVR or
# Equipboard page isn't asked about again and again. Investigating one explicitly still works.

def record_attempt(conn, target: str, item_key) -> None:
    conn.execute(
        "INSERT INTO investigation_attempts (target, item_key, attempted_at) VALUES (?, ?, ?) "
        "ON CONFLICT(target, item_key) DO UPDATE SET attempted_at=excluded.attempted_at",
        (target, str(item_key), datetime.now(timezone.utc).isoformat()),
    )


def attempts_for(conn, target: str) -> dict:
    """item_key -> attempted_at"""
    return {r[0]: r[1] for r in conn.execute(
        "SELECT item_key, attempted_at FROM investigation_attempts WHERE target=?", (target,))}
