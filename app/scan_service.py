"""The actual scan pipeline, as a plain function so both the CLI (app/main.py)
and the web app (app/web.py, "Rescan now" button) can trigger it identically."""

from pathlib import Path

from app import (
    categorizer,
    curated_catalog,
    db,
    dedup,
    enrichment,
    fl_database,
    full_scan,
    report,
    scanner,
    vendor_org,
    vendors_catalog,
)
from app.config import DATABASE_MD_PATH, DB_PATH, FL_PLUGIN_DB_ROOT


def _propose_superseded(conn) -> int:
    """Proposes (never applies) quarantine for every copy that a better format or
    a 64-bit/newer copy supersedes. The owner confirms each one in the Actions tab,
    the same as any other action. Skips FL Cloud plugins and anything that already
    has an open action."""
    rows = [r for r in db.all_plugins(conn) if not r["fl_cloud"]]
    proposed = 0
    for candidate in dedup.superseded_candidates(rows):
        plugin_id = candidate["superseded"]["id"]
        if db.active_action_for_plugin(conn, plugin_id):
            continue
        db.create_action(conn, plugin_id, "quarantine", candidate["reason"])
        proposed += 1
    return proposed


def _store_record(conn, rec: dict, fl_index: dict, fl_cloud_paths: set) -> tuple[int, bool]:
    """One scanned plugin into the catalog: FL Studio's name for it when FL knows it
    better, its FL info, the FL Cloud pin, and fresh categories. (plugin_id, whether
    FL's name replaced ours). Shared by run_scan and catalog_one."""
    fl_match = fl_database.lookup(rec["host_path"], rec["vendor"], fl_index, rec["format"])
    fl_name = fl_match["name"] if fl_match else None
    fl_type = None
    if fl_match and fl_match.get("type_code") == 1:
        fl_type = "effect"
    elif fl_match and fl_match.get("type_code") == 2:
        fl_type = "synth"
    # fl_match["name"] can be None (Waves: FL confirms the major
    # version has a working shell, but can't tell us which
    # specific bundle we're looking at — see
    # app/fl_database.py:lookup()'s WAVES branch) — a detection
    # confirmation only, never a rename source.
    name_fixed = False
    if fl_match and fl_match["name"] and fl_match["name"] != rec["name"]:
        # FL Studio actually queries the plugin for its real name; our
        # static metadata reading gets this wrong for shared
        # white-label engines (e.g. every Acustica Audio "NUW"-engine
        # effect reporting the same generic name) — see AGENTS.md.
        rec["name"] = fl_match["name"]
        rec["normalized_name"] = scanner.normalize_name(fl_match["name"])
        name_fixed = True
    plugin_id = db.upsert_plugin(conn, rec)
    db.set_fl_info(conn, plugin_id, fl_match is not None, fl_name, fl_type)
    # FL Cloud plugins stay cataloged but pinned (see fl_cloud_plugins in db.py).
    conn.execute("UPDATE plugins SET fl_cloud=? WHERE id=?",
                 (1 if rec["host_path"].lower() in fl_cloud_paths else 0, plugin_id))
    # Real bug fixed: this used to call set_plugin_categories() unconditionally,
    # even when the scanner found nothing this pass (rec["category_slugs"] == []).
    # set_plugin_categories() does DELETE-then-reinsert, so an empty result wiped
    # every category a later pipeline step (curated catalog, heuristic, or a
    # manual/API edit — including a full research pass) had ever set, on every
    # single rescan. Only write here when the scanner found something fresh AND
    # it wouldn't clobber a manual choice — moduleinfo.json is a live, authoritative
    # signal worth re-applying, an empty read is not evidence of "no categories."
    if rec.get("category_slugs") and not db.plugin_has_manual_categories(conn, plugin_id):
        db.set_plugin_categories(conn, plugin_id, rec["category_slugs"])
    return plugin_id, name_fixed


def catalog_one(conn, host_path: str) -> int:
    """One plugin back into the catalog without a full rescan (an ignored plugin
    FL Studio Sync links): it stops being ignored, the scan root holding it reads
    it the way a scan does, and it is stored the same way. Raises ValueError when
    the file is gone or isn't a plugin VEXT can read."""
    target = Path(host_path)
    if not target.exists():
        raise ValueError(f"{host_path} isn't there any more")
    roots = []
    for root in db.scan_roots_as_objects(conn):
        try:
            target.relative_to(root.host_path)
        except ValueError:
            continue
        roots.append(root)
    rec = None
    for root in sorted(roots, key=lambda r: -len(r.host_path)):   # the most specific root first
        rec = scanner._process_entry(target, root)
        if rec:
            break
    if rec is None:
        raise ValueError(f"VEXT can't read {target.name} as a plugin in any of its scan folders")
    db.remove_ignored_plugin_path(conn, host_path)
    fl_root = db.get_setting(conn, "fl_plugin_db_root", FL_PLUGIN_DB_ROOT)
    plugin_id, _ = _store_record(conn, rec, fl_database.build_index(fl_root), db.fl_cloud_host_paths(conn))
    db.sync_plugin_vendor_info(conn)
    conn.commit()
    return plugin_id


def run_scan(log=print, include_full_scan: bool = False) -> dict:
    db.init_db(DB_PATH)
    with db.connect(DB_PATH) as conn:
        db.seed_categories(conn)
        db.seed_vendors(conn)
        db.seed_plugin_types(conn)

        scan_id = db.start_scan(conn)
        # Plugin ids only grow (AUTOINCREMENT never reuses one), so whatever this scan adds gets
        # an id above this: that's the "new" count (the UI shows them first, see app.js).
        max_id_before = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM sqlite_sequence WHERE name='plugins'").fetchone()[0]

        reconciled = db.reconcile_missing_quarantine(conn)
        if reconciled:
            log(f"Reconciled {reconciled} quarantine entrie(s) whose file was removed outside the app "
                f"(see the log file for detail on which)")

        oos_reconciled = db.reconcile_missing_out_of_scope(conn)
        if oos_reconciled:
            log(f"Removed {oos_reconciled} out-of-scope entrie(s) whose file was deleted directly, outside the app")

        fl_root = db.get_setting(conn, "fl_plugin_db_root", FL_PLUGIN_DB_ROOT)
        fl_index = fl_database.build_index(fl_root)
        log(f"FL Studio plugin database: {len(fl_index['by_path'])} entries for name cross-reference"
            if fl_index["by_path"] else "FL Studio plugin database not mounted/found — skipping name cross-reference")

        db.seed_scan_roots(conn)
        all_roots = db.scan_roots_as_objects(conn)
        custom_count = sum(1 for r in db.list_scan_roots(conn, enabled_only=True) if r["source"] != "builtin")
        if custom_count:
            log(f"Owner-added/edited scan paths (see the Configuration tab): {custom_count}")

        ignored_paths = db.ignored_plugin_host_paths(conn)
        ignored_count = 0
        fl_cloud_paths = db.fl_cloud_host_paths(conn)

        found = 0
        flagged = 0
        fl_name_fixed = 0
        per_root = []
        seen_host_paths = set()
        for root in all_roots:
            records = scanner.scan_root(root)
            for rec in records:
                if rec["host_path"].lower() in ignored_paths:
                    # Owner explicitly ignored this exact plugin (Browse
                    # tab's "Ignore" button) — skip it entirely: not
                    # upserted, not FL-cross-referenced, not counted. Not
                    # adding it to seen_host_paths also means
                    # remove_stale_plugins() below cleans up its catalog row
                    # if one still exists from before it was ignored.
                    ignored_count += 1
                    continue
                _plugin_id, name_fixed = _store_record(conn, rec, fl_index, fl_cloud_paths)
                fl_name_fixed += name_fixed
                seen_host_paths.add(rec["host_path"])
                found += 1
                if rec["status"] != "ok":
                    flagged += 1
            per_root.append({"format": root.format, "path": root.host_path, "count": len(records)})
            log(f"[{root.format}] {root.host_path}: {len(records)} plugin(s)")
        if fl_name_fixed:
            log(f"FL Studio cross-reference: corrected {fl_name_fixed} plugin name(s) our own metadata got wrong")
        if ignored_count:
            log(f"Skipped {ignored_count} plugin(s) the owner marked ignored (Configuration tab's \"Ignored plugins\")")

        # Plugins currently sitting in unresolved quarantine aren't "seen" here
        # (their file is in data/quarantine/, not its original location) but
        # aren't stale either — protect them from removal (see db.py docstring).
        protected = db.quarantined_host_paths(conn)
        removed = db.remove_stale_plugins(conn, seen_host_paths | protected)
        db.sync_plugin_vendor_info(conn)
        if removed:
            log(f"Removed {removed} plugin(s) no longer found on disk "
                f"(cascades to clean up any of their pending/quarantined actions too)")

        canonicalized = 0
        for row in db.all_plugins(conn):
            if row["vendor_source"] == "manual":
                continue
            canon = vendors_catalog.canonicalize(row["vendor"])
            if canon and canon != row["vendor"]:
                db.set_vendor_name(conn, row["id"], canon)
                canonicalized += 1
        log(f"Vendor name canonicalization (app/vendors_catalog.py): cleaned up {canonicalized} plugin(s) "
            f"(casing/legal-suffix duplicates like 'Soundspot' vs 'SoundSpot')")

        vendor_filled = 0  # vendor names now come from AI research (AI tab), not an installer archive

        enrich_result = enrichment.apply(conn)
        log(f"Research-backed overrides (app/enrichment.py): filled vendor for {enrich_result['filled']}, "
            f"flagged {enrich_result['ambiguous']} as ambiguous (needs manual verification)")

        # General parent-folder vendor fallback — catches e.g. MeldaProduction's
        # 39 plugins (Vendor\Category\Plugin.vst3, no moduleinfo.json vendor field)
        # without a per-name NAME_OVERRIDES entry. Must run every scan: an earlier
        # one-off manual fix for exactly this case used vendor_source='inferred',
        # which isn't protected from being reset on the next rescan the way
        # 'manual' is — see AGENTS.md.
        known_vendor_names = {v["name"] for v in db.list_vendors_reference(conn)}
        known_vendor_names.update(row["vendor"] for row in db.all_plugins(conn) if row["vendor"])
        path_vendor_filled = 0
        for row in db.all_plugins(conn):
            if row["vendor"]:
                continue
            vendor = vendor_org.vendor_from_path(row["host_path"], row["format"], known_vendor_names, all_roots)
            if vendor:
                db.fill_vendor_override(conn, row["id"], vendor, f"Installed inside its own {vendor} vendor subfolder (parent-folder fallback)")
                path_vendor_filled += 1
        if path_vendor_filled:
            log(f"Parent-folder vendor fallback: filled vendor for {path_vendor_filled} plugin(s)")

        # Vendor website/status live once per vendor in the `vendors` table (seeded
        # from app/vendors_catalog.py, plus anything added in Reference data). Each
        # plugin just mirrors its vendor's row, so every plugin from one vendor agrees.
        db.sync_plugin_vendor_info(conn)
        log('Vendor website/status: synced from the vendor list for every plugin')

        curated_tagged = 0
        for row in db.all_plugins(conn):
            hit = curated_catalog.lookup(row["format"], row["name"])
            if hit is None:
                continue
            slugs, desc_es = hit
            db.set_description_es(conn, row["id"], desc_es)
            if not db.plugin_has_categories(conn, row["id"]) and not db.plugin_has_manual_categories(conn, row["id"]):
                db.set_plugin_categories(conn, row["id"], slugs, source="curated")
            curated_tagged += 1
        log(f"Curated catalog (app/curated_catalog.py — Waves + FL-Native): tagged {curated_tagged} plugin(s)")

        # Many "uncategorized" rows are just the VST2/CLAP sibling of an already-tagged
        # VST3 copy of the exact same (vendor-verified) plugin — copy tags across siblings
        # before falling back to a name-keyword guess.
        all_rows = db.all_plugins(conn)
        by_name = dedup.group_by_name(all_rows)
        slug_map = db.category_slugs_by_plugin(conn)
        propagated = 0
        for row in all_rows:
            if row["id"] in slug_map or db.plugin_has_manual_categories(conn, row["id"]):
                continue
            for sib in dedup.siblings_of(row, by_name):
                if sib["id"] in slug_map:
                    db.set_plugin_categories(conn, row["id"], slug_map[sib["id"]], source="propagated")
                    propagated += 1
                    break
        log(f"Category propagation across format-siblings: tagged {propagated} plugin(s)")

        heuristic_tagged = 0
        for row in db.all_plugins(conn):
            if db.plugin_has_categories(conn, row["id"]):
                continue
            slugs = categorizer.categorize_name(row["name"])
            if slugs:
                db.set_plugin_categories(conn, row["id"], slugs, source="heuristic")
                heuristic_tagged += 1
        log(f"Category heuristic (app/categorizer.py): tagged {heuristic_tagged} plugin(s) with no moduleinfo.json/curated entry")

        pruned = db.prune_now_cataloged_out_of_scope(conn)
        if pruned:
            log(f"Out of scope: removed {pruned} entrie(s) that are now properly cataloged "
                f"(e.g. after \"Add path to scope\")")

        # Type from categories: FL's own type_code only covers plugins FL's database
        # knows about, leaving ~435 as 'unknown'. Once a plugin has categories, an
        # Instrument* tag means synth and anything else means effect — same rule as
        # scanner._derive_type, but applied after curated/propagated/heuristic tagging.
        # Never touches plugin_type_source='manual' (see set_plugin_type_derived).
        slug_map = db.category_slugs_by_plugin(conn)
        type_derived = 0
        for row in db.all_plugins(conn):
            if row["plugin_type"] not in (None, "", "unknown") or row["id"] not in slug_map:
                continue
            slugs = slug_map[row["id"]]
            db.set_plugin_type_derived(conn, row["id"], "synth" if any(s.startswith("instrument") for s in slugs) else "effect")
            type_derived += 1
        log(f"Type derived from categories: set {type_derived} plugin(s) previously 'unknown'")

        out_of_scope_found = 0
        if include_full_scan:
            log("\nFull C:\\ scan requested — this is much slower than the targeted scan above, "
                "walking the whole drive through the same Docker bind-mount bottleneck (see AGENTS.md).")
            known_paths = db.known_plugin_host_paths(conn)
            known_root_paths = {r.host_path.lower() for r in all_roots}
            ignored_folder_paths = {row["host_path"].lower() for row in db.list_ignored_scan_folders(conn)}
            for item in full_scan.scan_full_c(known_root_paths, ignored_folder_paths):
                if item["host_path"].lower() in known_paths:
                    continue  # already properly cataloged — not "out of scope"
                db.upsert_out_of_scope(conn, item["host_path"], item["name"], item["format_guess"], item["size_bytes"])
                out_of_scope_found += 1
            log(f"Full C:\\ scan: found {out_of_scope_found} plugin-like file(s) outside the known scan directories")

        db.finish_scan(conn, scan_id, roots_count=len(all_roots), found_count=found, flagged_count=flagged)
        rows = db.all_plugins(conn)
        cat_map = db.category_tags_by_plugin(conn)
        still_missing_vendor = sum(1 for r in rows if not r["vendor"])
        new_plugins = sum(1 for r in rows if r["id"] > max_id_before)
        db.prune_unused_reference(conn)
        proposed_quarantine = 0
        if db.get_setting(conn, "auto_propose_superseded", "1") == "1":
            proposed_quarantine = _propose_superseded(conn)
        if proposed_quarantine:
            log(f"Proposed quarantine for {proposed_quarantine} older or lower-ranked copy(ies). Review them in the Actions tab.")

    report.write_database_md(rows, cat_map, DATABASE_MD_PATH)

    summary = {
        "found": found,
        "new": new_plugins,
        "flagged": flagged,
        "vendor_filled_installer": vendor_filled,
        "vendor_filled_research": enrich_result["filled"],
        "vendor_ambiguous": enrich_result["ambiguous"],
        "category_heuristic_tagged": heuristic_tagged,
        "still_missing_vendor": still_missing_vendor,
        "per_root": per_root,
    }
    log(f"\nScanned {found} plugin(s), {new_plugins} new, {flagged} flagged for review.")
    log(f"Still missing vendor: {still_missing_vendor}")
    log(f"DB: {DB_PATH}")
    log(f"Report: {DATABASE_MD_PATH}")
    return summary
