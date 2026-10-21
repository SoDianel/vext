"""Background AI investigations, run as batches on worker threads.

Targets:
  metadata     every field of a plugin that is missing one (vendor, type, categories,
               description, store link)
  links        plugins missing the official store link, KVR page or Equipboard page
  categories   plugins with no category
  types        plugins with no effect/synth type
  vendors      vendors with no website or status
  vendor       plugins with no vendor
  confirm_vendor  plugins whose vendor was only inferred, to be confirmed or corrected
  image        plugins without a picture: VEXT reads the store / KVR pages itself, the AI
               (when needed, or no AI at all) proposes picture URLs and pages, app/pictures.py
               downloads and checks them and keeps the good ones as suggestions

Each batch runs one Codex call per item, several at a time (the AI tab's "parallel
requests" setting, 5 by default). Results are saved as pending suggestions and never
applied on their own. The owner reviews them, or uses "Update all" on a target.

The UI polls status() for a live list. Job state lives in memory, so a batch stops
if the app closes. Suggestions already saved are kept.
"""

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from app import codex_research, db

DEFAULT_CONCURRENCY = 5
MIN_CONCURRENCY = 1
MAX_CONCURRENCY = 8

TARGETS = ("metadata", "links", "categories", "types", "vendors", "vendor", "confirm_vendor", "image")

_PLUGIN_SELECT = ("SELECT p.id, p.name, p.format, p.vendor, p.host_path, p.store_url, p.kvr_url "
                  "FROM plugins p WHERE p.status = 'ok'")
_NO_SUGGESTION = "AND NOT EXISTS (SELECT 1 FROM enrichment_suggestions e WHERE e.plugin_id = p.id)"


def _no_suggestion_for(column: str) -> str:
    """Skips plugins whose pending suggestion already answers this one question."""
    return (f"AND NOT EXISTS (SELECT 1 FROM enrichment_suggestions e WHERE e.plugin_id = p.id "
            f"AND e.{column} IS NOT NULL AND e.{column} != '')")


# Each target's selection: what it is missing (_WHERE), minus what already has an answer waiting
# for review (_UNANSWERED), so a group run never repeats finished work. A run on explicit ids (a row's
# Investigate / Investigate again) skips only the first part: asking again replaces the waiting answer.
_ORDER = "ORDER BY p.normalized_name, p.format"
_WHERE = {
    "metadata": f"""{_PLUGIN_SELECT}
        AND (
              p.vendor IS NULL OR p.vendor = ''
           OR p.plugin_type IS NULL OR p.plugin_type = ''
           OR p.description_es IS NULL OR p.description_es = ''
           OR p.store_url IS NULL OR p.store_url = ''
           OR NOT EXISTS (SELECT 1 FROM plugin_categories pc WHERE pc.plugin_id = p.id)
        )""",
    "links": f"""{_PLUGIN_SELECT}
        AND ((COALESCE(p.store_url, '') = '' AND p.no_store = 0)
             OR (COALESCE(p.kvr_url, '') = '' AND p.no_kvr = 0)
             OR (COALESCE(p.equipboard_url, '') = '' AND p.no_equipboard = 0))""",
    "categories": f"""{_PLUGIN_SELECT}
        AND NOT EXISTS (SELECT 1 FROM plugin_categories pc WHERE pc.plugin_id = p.id)""",
    "types": f"""{_PLUGIN_SELECT}
        AND (p.plugin_type IS NULL OR p.plugin_type = '' OR p.plugin_type = 'unknown')""",
    "vendor": f"""{_PLUGIN_SELECT}
        AND (p.vendor IS NULL OR p.vendor = '')""",
    "confirm_vendor": f"""{_PLUGIN_SELECT}
        AND p.vendor_source = 'inferred'""",
    # Plugins without a picture: the picture is a file, so _missing_picture() filters the rows.
    "image": _PLUGIN_SELECT,
    # Vendor names come from the reference list AND from the plugins themselves: a vendor a scan
    # found may not have a reference row yet, and it still needs a website and status.
    "vendors": """SELECT n.name, n.name, 'vendor', NULL, NULL, NULL, NULL FROM (
            SELECT name FROM vendors
            UNION
            SELECT vendor FROM plugins WHERE vendor IS NOT NULL AND vendor != '' AND status = 'ok'
        ) n
        LEFT JOIN vendors v ON v.name = n.name
        WHERE (v.name IS NULL OR v.status IS NULL
               OR (COALESCE(v.url, '') = '' AND COALESCE(v.no_url, 0) = 0)
               OR (COALESCE(v.kvr_url, '') = '' AND COALESCE(v.no_kvr, 0) = 0)
               OR (COALESCE(v.equipboard_url, '') = '' AND COALESCE(v.no_equipboard, 0) = 0))""",
}
_UNANSWERED = {
    "metadata": _NO_SUGGESTION,
    "links": f"{_no_suggestion_for('store_url')} {_no_suggestion_for('kvr_url')} {_no_suggestion_for('equipboard_url')}",
    "categories": _no_suggestion_for("categories"),
    "types": _no_suggestion_for("plugin_type"),
    "vendor": _no_suggestion_for("vendor"),
    "confirm_vendor": _no_suggestion_for("vendor"),
    "image": "AND NOT EXISTS (SELECT 1 FROM picture_suggestions s WHERE s.plugin_id = p.id)",
    "vendors": "AND NOT EXISTS (SELECT 1 FROM vendor_suggestions s WHERE s.vendor = n.name)",
}
SELECTION_SQL = {
    t: f"{_WHERE[t]} {_UNANSWERED[t]} {'ORDER BY n.name' if t == 'vendors' else _ORDER}" for t in _WHERE
}
_SELECTION_ANY = {t: f"{_WHERE[t]} {'ORDER BY n.name' if t == 'vendors' else _ORDER}" for t in _WHERE}


def _missing_picture(items: list[dict]) -> list[dict]:
    from app import pictures
    index = pictures.picture_index()
    return [i for i in items if not pictures.has_picture(i["host_path"], index)]


def needing_items(conn, target: str, include_answered: bool = False) -> list[dict]:
    """Everything this target is still missing, whether or not it was tried before. Items with an
    answer waiting for review are left out unless `include_answered`."""
    if target not in TARGETS:
        raise ValueError(f"Unknown target: {target}")
    sql = (_SELECTION_ANY if include_answered else SELECTION_SQL)[target]
    rows = conn.execute(sql).fetchall()
    items = [
        {"id": r[0], "name": r[1], "format": r[2], "vendor": r[3], "host_path": r[4], "store_url": r[5],
         "kvr_url": r[6]}
        for r in rows
    ]
    return _missing_picture(items) if target == "image" else items


def _worth_retrying_picture(item: dict, search: dict | None, with_ai: bool) -> bool:
    """A plugin whose last picture search kept nothing is tried again by a group run when something
    changed that could help: a store or KVR page it didn't read yet, or an AI now that the last
    search ran without one."""
    if not search or search.get("found"):
        return False
    read = set(search.get("pages") or [])
    if any(u and u not in read for u in (item.get("store_url"), item.get("kvr_url"))):
        return True
    return with_ai and not search.get("ai_used")


def selected_items(conn, target: str, ids: list | None = None, with_ai: bool = True) -> list[dict]:
    """What this target still needs and hasn't been tried yet. With `ids`, exactly those items (plugin
    ids, or vendor names for the "vendors" target), tried before or not, answered or not."""
    if ids is not None:
        wanted = {str(i) for i in ids}
        return [i for i in needing_items(conn, target, include_answered=True) if str(i["id"]) in wanted]
    tried = db.attempts_for(conn, target)
    items = needing_items(conn, target)
    if target == "image":
        from app import pictures
        searches = pictures.last_search(conn)
        return [i for i in items if str(i["id"]) not in tried
                or _worth_retrying_picture(i, searches.get(i["id"]), with_ai)]
    return [i for i in items if str(i["id"]) not in tried]


def clamp_concurrency(value) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = DEFAULT_CONCURRENCY
    return max(MIN_CONCURRENCY, min(MAX_CONCURRENCY, n))


class _Batch:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.stop_requested = threading.Event()
        self.target: str | None = None
        self.items: dict = {}
        self.order: list = []
        self.concurrency = DEFAULT_CONCURRENCY
        self.started_at: str | None = None
        self.agent: str | None = None
        self.ask_ai: bool | None = None
        self.finished_at: str | None = None


_batch = _Batch()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _set(key, **changes) -> None:
    with _batch.lock:
        _batch.items[key].update(changes)


def _summary_for_plugin(result: dict) -> dict:
    return {
        "vendor": result.get("vendor"),
        "secondary_vendors": result.get("secondary_vendors") or [],
        "plugin_type": result.get("plugin_type"),
        "categories": result.get("categories") or [],
        "store_url": result.get("store_url"),
        "kvr_url": result.get("kvr_url"),
        "equipboard_url": result.get("equipboard_url"),
        "confidence": result.get("confidence"),
        "notes": result.get("notes"),
    }


def _research_one(db_path: str, target: str, item: dict, cfg, template: str | None) -> None:
    key = item["id"]
    if _batch.stop_requested.is_set():
        _set(key, state="skipped")
        return
    _set(key, state="running")
    try:
        if target == "vendors":
            result = codex_research.run_vendor_research(item["name"], None, None, cfg)
            with db.connect(db_path) as conn:
                db.upsert_vendor_suggestion(conn, item["name"], result)
                db.record_attempt(conn, target, key)
            _set(key, state="done", summary={"url": result.get("url"), "status": result.get("status"),
                                             "confidence": result.get("confidence")})
            return
        if target == "image":
            from app import pictures
            try:
                summary = pictures.investigate(db_path, item, cfg, _batch.ask_ai)
            except (OSError, sqlite3.Error) as exc:
                _set(key, state="failed", error=f"The pictures couldn't be stored: {exc}")
                return
            _set(key, state="done", summary=summary)
            return
        if target in codex_research._FOCUSED:
            result = codex_research.run_focused(
                target, item["name"], item["format"], item["vendor"], item["host_path"], item["store_url"], cfg)
        else:
            result = codex_research.run_research(
                item["name"], item["format"], item["vendor"], item["host_path"],
                cfg=cfg, template=template if target == "metadata" else None,
                store_url=item["store_url"], focus=codex_research.FOCUS.get(target),
            )
    except codex_research.ResearchCancelled:
        _set(key, state="skipped")
        return
    except codex_research.ResearchError as exc:
        _set(key, state="failed", error=str(exc))
        return
    if target == "links":
        result["links_checked"] = 1   # an empty link in this answer means "looked, none found"
    with db.connect(db_path) as conn:
        db.upsert_enrichment_suggestion(conn, key, result)
        db.record_attempt(conn, target, key)
    _set(key, state="done", summary=_summary_for_plugin(result))


def _run(db_path: str, target: str, items: list[dict], cfg, template: str | None, concurrency: int) -> None:
    try:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            for item in items:
                pool.submit(_research_one, db_path, target, item, cfg, template)
    finally:
        with _batch.lock:
            _batch.running = False
            _batch.finished_at = _now()


def start(db_path: str, target: str, cfg, template: str | None, concurrency, limit: int | None = None,
          ids: list | None = None, ask_ai: bool | None = None) -> int:
    """Starts a batch for one target. Returns how many items were queued. `cfg` may be None for
    "image" only (no AI: VEXT reads the store and KVR pages itself); `ask_ai` is passed to
    pictures.investigate. Raises RuntimeError if a batch is already running or nothing is missing."""
    if cfg is None and target != "image":
        raise ValueError("This investigation needs an AI agent.")
    with _batch.lock:
        if _batch.running:
            raise RuntimeError("A batch is already running.")
    with db.connect(db_path) as conn:
        items = selected_items(conn, target, ids, with_ai=cfg is not None)
    if limit:
        items = items[:limit]
    if not items:
        raise RuntimeError("Nothing is missing for this investigation.")
    concurrency = clamp_concurrency(concurrency)

    def key_of(i):
        return i["id"]

    with _batch.lock:
        _batch.running = True
        _batch.target = target
        _batch.stop_requested.clear()
        codex_research.begin_batch()
        _batch.concurrency = concurrency
        _batch.started_at = _now()
        _batch.agent = cfg.agent if cfg is not None else None
        _batch.ask_ai = ask_ai
        _batch.finished_at = None
        _batch.items = {
            key_of(i): {"id": key_of(i), "name": i["name"], "format": i["format"], "state": "queued",
                        "error": None, "summary": None}
            for i in items
        }
        _batch.order = [key_of(i) for i in items]
    threading.Thread(
        target=_run, args=(db_path, target, items, cfg, template, concurrency), daemon=True,
    ).start()
    return len(items)


def stop() -> None:
    """Stops the batch: items not yet started are skipped. Ones already running finish."""
    _batch.stop_requested.set()
    codex_research.cancel_running()


def status() -> dict:
    with _batch.lock:
        items = [dict(_batch.items[k]) for k in _batch.order]
        counts = {"queued": 0, "running": 0, "done": 0, "failed": 0, "skipped": 0}
        for it in items:
            counts[it["state"]] = counts.get(it["state"], 0) + 1
        return {
            "running": _batch.running,
            "target": _batch.target,
            "agent": _batch.agent,
            "stop_requested": _batch.stop_requested.is_set(),
            "concurrency": _batch.concurrency,
            "total": len(items),
            "counts": counts,
            "started_at": _batch.started_at,
            "finished_at": _batch.finished_at,
            "items": items,
        }
