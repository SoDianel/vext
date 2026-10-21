"""Review tabs: Missing vendor, Inferred vendor, Missing type, Missing categories, Missing links,
Missing picture.

Each tab lists the plugins that lack one piece of metadata, with the Codex suggestion (if one is
waiting) beside it. Codex runs come from app/ai_batch.py. A suggestion is applied one by one or
all at once, never on its own, and goes through the same manual-edit path a hand-typed edit uses,
so rescans keep it.

Missing picture is different: its answer is up to six downloaded pictures (app/pictures.py), each
labelled "verified" or "possible" with a reason, and the owner picks which one Apply uses
(`choices`), usually in the picture picker popup. Its endpoints for the candidate files live here too.
"""

import os
import sqlite3

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app import ai_batch, db, pictures, plugin_host
from app.config import DB_PATH

router = APIRouter()

# review tab -> the ai_batch target that fills it
KINDS = {
    "vendor": "vendor",
    "inferred_vendor": "confirm_vendor",
    "type": "types",
    "categories": "categories",
    "links": "links",
    "picture": "image",
}

# A plugin's three links: its official store page and its KVR and Equipboard database pages.
LINK_FIELDS = ("store_url", "kvr_url", "equipboard_url")
# ...and the mark meaning "looked, that site has no page for it".
NONE_FLAG = {"store_url": "no_store", "kvr_url": "no_kvr", "equipboard_url": "no_equipboard"}

# review tab -> the enrichment_suggestions column holding its answer
_COLUMN = {
    "vendor": "vendor", "inferred_vendor": "vendor", "type": "plugin_type",
    "categories": "categories",
}


def _check(kind: str) -> None:
    if kind not in KINDS:
        raise HTTPException(400, "kind must be one of: " + ", ".join(KINDS))


def _is_member(kind: str, r, cat_ids: set, pics: dict | None = None) -> bool:
    """`pics`: pictures.picture_index(), read once when checking many rows for "picture"."""
    if kind == "picture":
        return r["status"] == "ok" and not pictures.has_picture(r["host_path"], pics)
    if kind == "vendor":
        return not r["vendor"]
    if kind == "inferred_vendor":
        return r["vendor_source"] == "inferred" and bool(r["vendor"])
    if kind == "type":
        return not r["plugin_type"] or r["plugin_type"] == "unknown"
    if kind == "categories":
        return r["id"] not in cat_ids
    # A link marked "none" is settled, so it isn't missing. Only never-investigated links are.
    return any(not r[f] and not r[NONE_FLAG[f]] for f in LINK_FIELDS)


def _suggestion(kind: str, sug, r=None):
    if sug is None:
        return None
    if kind == "links":
        # Only links the plugin doesn't have yet: an existing link is never replaced from here.
        found = {f: sug[f] for f in LINK_FIELDS if sug[f] and (r is None or not r[f])}
        # A links run that left a link empty looked and found none. That becomes a "none" mark on Apply.
        none = [f for f in LINK_FIELDS if sug["links_checked"] and not sug[f]
                and (r is None or (not r[f] and not r[NONE_FLAG[f]]))]
        return {"found": found, "none": none} if (found or none) else None
    value = sug[_COLUMN[kind]]
    if kind == "categories":
        return [x for x in (value or "").split(",") if x] or None
    return value or None


def _drop_if_empty(conn, plugin_id: int) -> None:
    row = conn.execute(
        """SELECT vendor, vendor_url, vendor_status, plugin_type, store_url, categories, description_es,
                  secondary_vendors, kvr_url, equipboard_url, links_checked
           FROM enrichment_suggestions WHERE plugin_id=?""",
        (plugin_id,),
    ).fetchone()
    if row is not None and all(v in (None, "", "[]", 0) for v in row):
        db.delete_enrichment_suggestion(conn, plugin_id)


def _load(conn):
    conn.row_factory = sqlite3.Row
    rows = db.all_plugins(conn)
    cat_ids = set(db.category_tags_by_plugin(conn))
    sugs = {r["plugin_id"]: r for r in conn.execute("SELECT * FROM enrichment_suggestions")}
    return rows, cat_ids, sugs


@router.get("/api/review/vendor_sites")
def review_vendor_sites():
    """Vendors that still need a website, status or database link: researched answers waiting for
    review first, then the ones not researched yet. (Declared before /api/review/{kind} so the name
    isn't taken as a kind.)"""
    with db.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        known = {r["name"]: dict(r) for r in conn.execute("SELECT * FROM vendors")}
        counts = dict(conn.execute(
            "SELECT vendor, COUNT(*) FROM plugins WHERE vendor IS NOT NULL AND vendor != '' GROUP BY vendor").fetchall())
        pending = db.list_vendor_suggestions(conn)
        needing = ai_batch.needing_items(conn, "vendors")
        tried = db.attempts_for(conn, "vendors")
    for p in pending:
        p["plugins"] = counts.get(p["vendor"], 0)
        p["current"] = {k: (known.get(p["vendor"]) or {}).get(k) for k in (
            "url", "status", "kvr_url", "equipboard_url", "no_url", "no_kvr", "no_equipboard")}
    missing = []
    for m in needing:
        info = known.get(m["name"]) or {}
        missing.append({
            "vendor": m["name"], "plugins": counts.get(m["name"], 0),
            "url": info.get("url"), "status": info.get("status"), "status_note": info.get("status_note"),
            "kvr_url": info.get("kvr_url"), "equipboard_url": info.get("equipboard_url"),
            "no_url": info.get("no_url", 0), "no_kvr": info.get("no_kvr", 0), "no_equipboard": info.get("no_equipboard", 0),
            "investigated_at": tried.get(m["name"]),
        })
    return {"pending": pending, "missing": missing}


# Cached for a year: a candidate's file never changes under its id (a new search makes new ids).
_IMMUTABLE = {"Cache-Control": "private, max-age=31536000, immutable"}


@router.get("/api/pictures/index")
def picture_index():
    """{plugin id: picture stamp} for every plugin with a picture: lets lists that don't carry
    picture fields (the Actions cards) show thumbnails. One directory read."""
    index = pictures.picture_index()
    with db.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT id, host_path FROM plugins").fetchall()
    out = {}
    for pid, host_path in rows:
        stamp = pictures.picture_stamp(host_path, index)
        if stamp is not None:
            out[str(pid)] = stamp
    return out


@router.get("/api/pictures/suggestions/{suggestion_id}/image")
def picture_suggestion_image(suggestion_id: int, variant: str = ""):
    """A downloaded candidate (PNG), or its backdrop / thumb variant (JPEG)."""
    if variant and variant not in pictures.VARIANTS:
        raise HTTPException(400, f"unknown image variant: {variant}")
    with db.connect(DB_PATH) as conn:
        sug = pictures.get_suggestion(conn, suggestion_id)
    path = pictures.suggestion_file(sug["file"]) if sug else ""
    if not sug or not os.path.exists(path):
        raise HTTPException(404, "no such picture suggestion")
    if variant:
        return FileResponse(pictures.variant_path(path, variant), media_type="image/jpeg", headers=_IMMUTABLE)
    return FileResponse(path, media_type="image/png", headers=_IMMUTABLE)


def _picture_list() -> list:
    """Missing picture: plugins without one, each with why it can't be opened here (if it can't),
    the candidates waiting for a choice, and what the last web search came back with."""
    index = pictures.picture_index()
    with db.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = [r for r in db.all_plugins(conn) if _is_member("picture", r, set(), index)]
        quarantined = db.quarantined_host_paths(conn)
        tried = db.attempts_for(conn, "image")
        ids = {r["id"] for r in rows}
        sugs = pictures.list_suggestions(conn, ids)
        searches = pictures.last_search(conn, ids)
        pictures.prune_orphans(conn)
    names = plugin_host._shell_names_cache()
    keep = ("id", "name", "vendor", "vendor_source", "format", "host_path", "store_url", "kvr_url", "plugin_type",
            "bitness", "fl_cloud", "status")
    out = []
    for r in rows:
        d = {k: r[k] for k in keep}
        d["host_issue"] = plugin_host.host_issue(r, quarantined, names)
        d["suggestion"] = [{k: v for k, v in s.items() if k != "file"} for s in sugs.get(r["id"], [])] or None
        d["search"] = searches.get(r["id"])
        d["investigated_at"] = tried.get(str(r["id"]))
        out.append(d)
    return out


@router.get("/api/pictures/candidates/{plugin_id}")
def picture_candidates(plugin_id: int):
    """One plugin's picture candidates for the picker popup (the review row and the plugin sheet
    open it): {id, name, vendor, has_picture, suggestion: [...], search}."""
    with db.connect(DB_PATH) as conn:
        row = conn.execute("SELECT id, name, vendor, format, host_path FROM plugins WHERE id=?", (plugin_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "not found")
        sugs = pictures.list_suggestions(conn, {plugin_id}).get(plugin_id, [])
        search = pictures.last_search(conn, {plugin_id}).get(plugin_id)
    return {"id": row[0], "name": row[1], "vendor": row[2], "format": row[3],
            "has_picture": pictures.has_picture(row[4]),
            "suggestion": [{k: v for k, v in s.items() if k != "file"} for s in sugs], "search": search}


@router.get("/api/review/{kind}")
def review_list(kind: str):
    _check(kind)
    if kind == "picture":
        return _picture_list()
    with db.connect(DB_PATH) as conn:
        rows, cat_ids, sugs = _load(conn)
        tried = db.attempts_for(conn, KINDS[kind])
    out = []
    for r in rows:
        if not _is_member(kind, r, cat_ids):
            continue
        d = {k: r[k] for k in r.keys()}
        sug = sugs.get(r["id"])
        d["suggestion"] = _suggestion(kind, sug, r)
        d["investigated_at"] = tried.get(str(r["id"]))
        d["suggestion_sources"] = [x for x in ((sug["sources"] if sug else "") or "").split(",") if x]
        d["suggestion_notes"] = sug["notes"] if sug else None
        d["suggestion_confidence"] = sug["confidence"] if sug else None
        out.append(d)
    return out


@router.get("/api/review-summary")
def review_summary():
    """Per tab: how many plugins are listed and how many have a suggestion waiting."""
    pics = pictures.picture_index()
    with db.connect(DB_PATH) as conn:
        rows, cat_ids, sugs = _load(conn)
        sites = len(ai_batch.needing_items(conn, "vendors"))
        site_pending = conn.execute("SELECT COUNT(*) FROM vendor_suggestions").fetchone()[0]
        pic_waiting = {r[0] for r in conn.execute("SELECT DISTINCT plugin_id FROM picture_suggestions")}
    out = {}
    for kind in KINDS:
        members = [r for r in rows if _is_member(kind, r, cat_ids, pics)]
        if kind == "picture":
            pending = sum(1 for r in members if r["id"] in pic_waiting)
        else:
            pending = sum(1 for r in members if _suggestion(kind, sugs.get(r["id"]), r))
        out[kind] = {"total": len(members), "pending": pending}
    out["vendor_sites"] = {"total": sites, "pending": site_pending}
    return out


class ReviewIds(BaseModel):
    ids: list[int] | None = None  # None = every listed plugin that has a suggestion waiting
    # Missing picture only: plugin id -> the candidate (suggestion id) to apply. A plugin without
    # a choice gets its first candidate.
    choices: dict[int, int] | None = None


def _targets(conn, kind: str, ids):
    rows, cat_ids, sugs = _load(conn)
    wanted = None if ids is None else set(ids)
    out = []
    for r in rows:
        if (wanted is not None and r["id"] not in wanted) or not _is_member(kind, r, cat_ids):
            continue
        sug = sugs.get(r["id"])
        value = _suggestion(kind, sug, r)
        if value:
            out.append((r, sug, value))
    return out


def _apply_pictures(body: ReviewIds) -> dict:
    applied, skipped = 0, []
    choices = body.choices or {}
    with db.connect(DB_PATH) as conn:
        waiting = pictures.list_suggestions(conn, None if body.ids is None else set(body.ids))
        for pid, cands in waiting.items():
            chosen = choices.get(pid)
            sug = next((c for c in cands if c["id"] == chosen), None) if chosen is not None else cands[0]
            if sug is None:
                skipped.append({"id": pid, "reason": "the chosen picture isn't one of its candidates"})
                continue
            try:
                # The owner's own link may replace an existing picture; other candidates never do.
                pictures.apply_suggestion(conn, sug["id"], replace=sug.get("origin") == "link")
            except (ValueError, FileExistsError, OSError) as exc:
                skipped.append({"id": pid, "reason": str(exc)})
                continue
            applied += 1
    return {"ok": True, "applied": applied, "skipped": skipped}


@router.post("/api/review/{kind}/apply")
def review_apply(kind: str, body: ReviewIds):
    _check(kind)
    if kind == "picture":
        return _apply_pictures(body)
    applied = 0
    with db.connect(DB_PATH) as conn:
        for r, sug, value in _targets(conn, kind, body.ids):
            pid = r["id"]
            if kind in ("vendor", "inferred_vendor"):
                db.update_plugin_manual(conn, pid, {"vendor": value})
                # A vendor new to the reference list gets its website from the same answer;
                # an existing vendor's record is left alone.
                if sug["vendor_url"] and not db.get_vendor_info(conn, value):
                    db.upsert_vendor_reference(conn, value, sug["vendor_url"], sug["vendor_status"],
                                               sug["vendor_status_note"], source="manual")
                conn.execute("UPDATE enrichment_suggestions SET vendor=NULL WHERE plugin_id=?", (pid,))
            elif kind == "links":
                changes = dict(value["found"])
                changes.update({NONE_FLAG[f]: True for f in value["none"]})
                db.update_plugin_manual(conn, pid, changes)
                for field in LINK_FIELDS:
                    conn.execute(f"UPDATE enrichment_suggestions SET {field}=NULL WHERE plugin_id=?", (pid,))
                conn.execute("UPDATE enrichment_suggestions SET links_checked=0 WHERE plugin_id=?", (pid,))
            elif kind == "type":
                if not db.is_plugin_type(conn, value):  # types are fixed: effect, synth
                    continue
                db.update_plugin_manual(conn, pid, {"plugin_type": value})
                conn.execute("UPDATE enrichment_suggestions SET plugin_type=NULL WHERE plugin_id=?", (pid,))
            elif kind == "categories":
                db.set_plugin_categories_manual(conn, pid, value)
                conn.execute("UPDATE enrichment_suggestions SET categories='' WHERE plugin_id=?", (pid,))
            _drop_if_empty(conn, pid)
            applied += 1
        db.sync_plugin_vendor_info(conn)
    return {"ok": True, "applied": applied}


@router.post("/api/review/{kind}/discard")
def review_discard(kind: str, body: ReviewIds):
    """Drops the waiting answer for this question. The plugin itself is untouched."""
    _check(kind)
    if kind == "picture":
        with db.connect(DB_PATH) as conn:
            waiting = pictures.list_suggestions(conn, None if body.ids is None else set(body.ids))
            for pid in waiting:
                pictures.discard_suggestions(conn, pid)
        return {"ok": True, "discarded": len(waiting)}
    columns = list(LINK_FIELDS) if kind == "links" else [_COLUMN[kind]]
    discarded = 0
    with db.connect(DB_PATH) as conn:
        for r, _sug, _value in _targets(conn, kind, body.ids):
            for column in columns:
                empty = "''" if column == "categories" else "NULL"
                conn.execute(f"UPDATE enrichment_suggestions SET {column}={empty} WHERE plugin_id=?", (r["id"],))
            if kind == "links":
                conn.execute("UPDATE enrichment_suggestions SET links_checked=0 WHERE plugin_id=?", (r["id"],))
            _drop_if_empty(conn, r["id"])
            discarded += 1
    return {"ok": True, "discarded": discarded}


@router.post("/api/review/inferred_vendor/confirm")
def review_confirm_inferred(body: ReviewIds):
    """Accepts the inferred vendor as it is. No AI: it just marks the vendor confirmed so rescans keep it."""
    confirmed = 0
    with db.connect(DB_PATH) as conn:
        rows, cat_ids, _sugs = _load(conn)
        wanted = None if body.ids is None else set(body.ids)
        for r in rows:
            if not _is_member("inferred_vendor", r, cat_ids):
                continue
            if wanted is not None and r["id"] not in wanted:
                continue
            db.update_plugin_manual(conn, r["id"], {"vendor": r["vendor"]})
            confirmed += 1
        db.sync_plugin_vendor_info(conn)
    return {"ok": True, "confirmed": confirmed}
