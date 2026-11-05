# Database tab: every piece of metadata in rows, orphans in Vendors and Categories, no Types pane

Repo: the VEXT repository root. Backend: Python (FastAPI, sqlite3) in `app/`. Frontend: vanilla JS/CSS in
`app/static/` (no build step, no framework). Read `AGENTS.md` first (safety rules, palette: no new hex colors).
Builds on `app/metadata_archive.py` (2026-10-22), which already keeps researched data of removed plugins and
vendors in table `metadata_archive` and restores it when they return.

Words used below:
- **Installed metadata**: researched data on a plugin that is installed now (a `plugins` row).
- **Orphan metadata**: data with no installed plugin behind it: an archived plugin, an archived vendor, an
  archived category, a picture file with no plugin and no archive entry, stale "already investigated" marks.

## Owner's requests

1. Metadata pane: **all** metadata in rows (installed and orphan), with filters **No plugin** and **Images
   without plugin**, and a filter for orphan metadata in general.
2. Vendors pane: also list vendors that were investigated but have no plugin now (archived vendors).
   Categories pane: the same for categories (archived owner-added categories).
3. A checkbox at the top of the Metadata list to select all shown rows, and a button **Clear selected (N)**.
   If the selection includes installed (non-orphan) metadata, the confirm says so clearly.
4. Remove the **Plugin types** pane from Database completely (tab button, pane, its table rendering).
   Keep `/api/reference/types` and `window._refCache.types`: the plugin editor's type dropdown uses them.
5. Remove the order selector (`#db-sort-key`, `#db-sort-dir`) from Categories and Vendors. Clicking a column
   header still sorts. The Metadata pane gets its own order selector.

## A. Backend (`app/metadata_archive.py`, `app/web.py`, `tools/test_metadata_archive.py`)

### A1. Categories in the archive
- `archive_unused_categories(conn) -> int`: before `db.prune_unused_reference`, copy every category with
  `source='manual'` that no plugin uses (`id NOT IN (SELECT category_id FROM plugin_categories)`) into
  `metadata_archive` with `kind='category'`, `key=slug.lower()`, `name=name`, `data` = JSON of
  `{slug, name, description, string_value, source_url, source}`. Built-in categories are re-seeded by
  `db.seed_categories` and are not archived.
- `restore_categories(conn) -> int`: for each archived category whose slug exists again in `categories`, fill
  empty fields and delete the archive entry.
- In `_restore_into`, before `set_plugin_categories_manual`, re-create any archived category the plugin's
  `manual_categories` needs (INSERT with `source='manual'` from its archived data) and delete that category's
  archive entry. A plugin that comes back brings its owner-made categories back.
- `app/scan_service.py`: next to the existing `restore_vendors` / `archive_unused_vendors` calls (just before
  `db.prune_unused_reference(conn)`), call `restore_categories(conn)` then `archive_unused_categories(conn)`.

### A2. One listing for the Metadata pane: `listing(conn) -> dict`
Returns `{"items": [...], "counts": {status: n}}`. Each item:

| key | value |
|---|---|
| `id` | `installed:<plugin id>`, `plugin:<archive key>`, `vendor:<archive key>`, `category:<archive key>`, `picture:<file stem>`, `attempts:stale` |
| `kind` | `plugin` / `vendor` / `category` / `picture` / `attempts` |
| `status` | `installed` (a plugin row with researched data), `no_plugin` (archived plugin, vendor or category), `image_only` (picture with no plugin and no archive entry), `stale` (the marks) |
| `orphan` | `false` only for `installed` |
| `name`, `vendor`, `format`, `path` | as today (`path` = host path or picture file); null when not relevant |
| `fields` | the chips, as `_summary()` builds them today, plus `"picture"` when a picture file exists for it |
| `date` | `archived_at` for archived items, `scanned_at` for installed, null otherwise |
| `bytes` | pictures only |

Installed items: every `plugins` row for which `_plugin_data()` returns something, or which has a picture file.
`orphans(conn)` stays and returns the same shape filtered to `orphan == true` (old callers keep working).

### A3. Clearing installed metadata
`clear(conn, ids)` also accepts `installed:<plugin id>`: on that plugin set `store_url, kvr_url,
equipboard_url, description, description_es, link_group` to NULL and `no_store, no_kvr, no_equipboard` to 0,
delete its `plugin_secondary_vendors` rows and its `plugin_categories` rows with `source='manual'`, and set
`name_source`, `vendor_source`, `plugin_type_source` back to `'scan'` when they are `'manual'` (the next scan
puts the scanned values back). Its picture file is **not** deleted. It also accepts `category:<key>` (delete
the archive entry). The result dict gains `installed` and `category` counters. Unknown prefixes still raise
`ValueError`.

### A4. Endpoints in `app/web.py` (next to the existing `/api/metadata/*` block)
- `GET /api/metadata` → `metadata_archive.listing(conn)`.
- `POST /api/metadata/clear` (exists): unchanged contract, but `now` is `listing(conn)`, and the log line
  counts installed clears separately.
- `GET /api/reference/vendors?include_orphans=1` and `GET /api/categories?include_orphans=1`: when the flag
  is set, append archived vendors / categories as extra rows with the same keys the normal rows have (from the
  archived JSON; missing keys null) plus `"orphan": true` and `"archive_id": "vendor:<key>"` /
  `"category:<key>"`. Normal rows get `"orphan": false`. Without the flag the answer is exactly as today.

### A5. Tests (`tools/test_metadata_archive.py`, keep the 9 existing tests passing)
Add: category archive and restore (manual kept, built-in not); a returning plugin re-creates its archived
manual category; `listing()` statuses (installed / no_plugin / image_only / stale); clearing `installed:<id>`
resets the fields and the sources but keeps the picture; `include_orphans` on both endpoints (use FastAPI
`TestClient(web.app, base_url="http://127.0.0.1:8000", headers={security.TOKEN_HEADER: security.TOKEN})`
and call `db.init_db(DB_PATH)` first; set `VEXT_ROOT` to a temp dir before importing `app`).
Run: `.venv-dev\Scripts\python.exe -m pytest tools\test_metadata_archive.py -q`.

## B. Database panel (`app/static/index.html` Database section, `app/static/app.js` reference code)

- **Types pane gone**: remove `<button data-ref-pane="types">`, the `.ref-pane[data-ref-pane="types"]` block and
  the `ref-types-tbody` rendering. Remove `types` from `REF_KINDS_LIST`, `REF_NOUNS`, `REF_SORTS`,
  `REF_SOURCES`, `REF_SEARCH_FIELDS`, `refSort`, `refData`. `loadReference()` still fetches
  `/api/reference/types` and keeps `window._refCache.types`.
- **Order selector gone** for Categories and Vendors: remove `.browse-sort` (`#db-sort-key`, `#db-sort-dir`)
  from `#db-sticky` and their listeners; `syncRefControls` no longer fills them. Header clicks still sort
  (keep `setRefSort` and the arrows). `REF_SORTS` can go if nothing else uses it.
- **Orphans in the lists**: `loadReference()` fetches `/api/categories?include_orphans=1` and
  `/api/reference/vendors?include_orphans=1` for the panes; `window._refCache.categories` / `.vendors` must
  contain **only** rows with `orphan === false` (the plugin editor must never offer an archived one).
- New filter `<select id="db-f-orphan">` in `#db-sticky .filters` (for both panes): `All` (value ""),
  `With plugins` (`with`), `No plugin` (`orphan`). Applied in `refRows()`.
- An orphan row shows a `<span class="badge">No plugin</span>` after its name, its data read-only, and one
  action button **Clear** (`class="danger" data-clear-archive="<archive_id>"`) instead of Edit / Assign /
  Delete. Clear asks `confirmDialog("Clear what VEXT kept about <name>? If a plugin with it comes back, it starts
  without this data.")`, then `POST /api/metadata/clear {ids: [archive_id]}`, toast, `loadReference()`.
- The count line (`#db-count`) counts orphan rows too ("12 of 210 vendors").

## C. Metadata pane (`app/static/js/database-metadata.js`, `app/static/css/database-metadata.css`)

Rewrite the pane as one table of `GET /api/metadata` items (keep the `V.dbMetadata.load(el)` entry point and
the IIFE style; reuse `css/config-storage.css` classes where they fit).

- **Toolbar**: search box (name, vendor, path, chips), filter `<select>`: `All metadata` (""), `Installed`
  (`installed`), `Orphan (no plugin)` (any `orphan`), `No plugin` (`no_plugin`), `Images without plugin`
  (`image_only`), `Other leftovers` (`stale`); order `<select>` (Name, Vendor, Status, Date) and an
  ascending/descending button (same look as the Scan tab's `.browse-sort`).
- **Count line**: "N items · M orphan" (of the filtered rows: "N of T items").
- **Table**: columns: [checkbox] · Name (+ vendor · format under it, path small) · Kept (chips) · Status
  (badge: Installed / No plugin / Image only / Leftover) · Date (or size for pictures) · [Clear button].
  Header checkbox selects or clears **all rows currently shown** (indeterminate when some are).
- **Action bar** (sticky bottom, like Storage): **Clear selected (N)**, disabled at 0.
- **Confirm**: always `V.confirmDialog`. If any selected item has `orphan === false`, the text starts with a
  warning: "N of these belong to installed plugins: their links, descriptions and your edits are erased (the
  next scan restores scanned names, vendors and types). Their pictures stay." then "Clear N items?".
  Orphan-only selection: "Clear N items for good? If a plugin comes back, it starts without this data."
- After clearing: use the answer's `now` to redraw, drop cleared ids from the selection, toast "Cleared N items."
- Palette tokens only (`var(--fg-2)`, `var(--bg-subtle)`, `var(--danger)`, …). Keyboard: checkboxes and buttons
  are real `<input>`/`<button>`s with labels.

## Done when

- `tools\test_metadata_archive.py` passes, and every other `tools\test_*.py` still passes (one file per process).
- In a scratch backend (`VEXT_ROOT` = a temp folder) the Database tab shows Categories, Vendors, Metadata only;
  no order selector in Categories/Vendors; the orphan filter works in all three panes.
- No file outside the ones named in each part is changed. Nothing is committed.
