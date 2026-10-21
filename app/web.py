"""Local-only web app: browse/filter the catalog, review cleanup candidates
(dedup/AAX/corrupted/ambiguous-vendor), propose + apply actions, and host
plugin previews. Binds to 127.0.0.1 only; app/security.py keeps other web
pages out (CORS, a per-launch token for every change, Host check)."""

import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app import actions, ai_agents, ai_batch, analysis_tap, applog, browser_audio, browser_record, codex_research, db, dedup, fl_reorganize, logbuf, pictures, plugin_host, registry_scan, review, security, vendor_org
from app.config import DB_PATH, FL_PLUGIN_DB_ROOT, PROJECT_ROOT, QUARANTINE_DIR
from app.config import FIXED_LOCATION_KEYS
from app import scan_service, storage  # storage: Configuration > Storage
from app.scan_service import run_scan
from app import audio_setup, host_worker  # Configuration > Audio, MIDI
from app import midi_monitor, sysmem  # the toolbar's metrics
from app import runtime_check  # Microsoft's C++ runtime: checked at start, Repair
from app import folder_access  # write access to a protected plugin folder, asked per folder
from app import playground_audio  # the Playground's Audio tab

app = FastAPI(title="VEXT")

logbuf.install()  # import-time errors too; attached again at startup, after uvicorn's dictConfig


@app.on_event("startup")
def _install_log_buffer():
    logbuf.install()
    logbuf.add("backend", "VEXT backend started")
    try:
        rt = runtime_check.status()
        if rt["ok"]:
            logbuf.add("backend", f"Visual C++ runtime {rt['version']} ({rt['path']})")
        else:
            logbuf.add("backend", f"Visual C++ runtime problem: {rt['problem']}. Plugins may crash until it's "
                                  "repaired (the warning at the top of the window has a Repair button).", "error")
    except Exception as e:  # noqa: BLE001 - a check must never stop the backend
        logbuf.add("backend", f"Couldn't check the Visual C++ runtime: {e}", "warning")

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.middleware("http")
async def _no_cache(request, call_next):
    # This app is under active local iteration and StaticFiles doesn't send
    # strong cache-busting headers on its own — WebView2 (the desktop shell,
    # see desktop/) keeps a PERSISTENT profile across app restarts, and was
    # observed serving an hours-stale index.html/app.js indefinitely despite
    # every rebuild, because nothing ever told it not to. A single-user local
    # tool has nothing to gain from caching here, so just disable it outright.
    # Exception: a response that sets its own Cache-Control keeps it (plugin pictures
    # requested with their file stamp in the URL, which can't go stale: Browse shows
    # hundreds and re-renders on every search).
    response = await call_next(request)
    response.headers.setdefault("Cache-Control", "no-store, no-cache, must-revalidate")
    return response


@app.middleware("http")
async def _require_token(request: Request, call_next):
    # Anything that can change something needs the per-launch token (see
    # app/security.py). GETs stay open: CORS keeps other sites from reading
    # them, and <img> tags can't send headers.
    if request.method not in security.SAFE_METHODS and not security.token_ok(
            request.headers.get(security.TOKEN_HEADER)):
        return JSONResponse({"detail": f"missing or wrong {security.TOKEN_HEADER} header"}, status_code=403)
    return await call_next(request)


# Added last = outermost: CORS answers preflights before the token check and
# adds its headers to a 403 too (so VEXT's own pages can read the reason).
# The desktop shell serves its pages from https://tauri.localhost (its own
# origin, see AGENTS.md "Desktop app"), so its API calls are cross-origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=security.ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", security.TOKEN_HEADER],
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=security.ALLOWED_HOSTS)


@app.on_event("startup")
def _ensure_schema_current():
    # The scanner runs db.init_db() (schema + migrations) at the start of every
    # scan, but the web app can start without ever having triggered one (e.g.
    # right after `docker compose up` if the scanner container hasn't finished
    # yet, or on a `data/plugins.db` from before a column was added) — run it
    # here too so the web app never serves against a stale schema.
    db.init_db(DB_PATH)
    ai_agents.warm()   # agent CLI checks take seconds: have them cached before the first popup
    _forget_youtube()
    _forget_input_source()


def _forget_input_source() -> None:
    """The Playground's Input source is gone (2026-10-15, owner: a later phase):
    drop its audio_input_device setting."""
    with db.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM settings WHERE key = 'audio_input_device'")


def _forget_youtube() -> None:
    """The YouTube player (yt-dlp) is gone: drop its setting and the audio it
    had fetched (a regenerable cache of up to 1 GB)."""
    with db.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM settings WHERE key = 'yt_cookies_browser'")
    cache = PROJECT_ROOT / "cache"
    for name in ("audio", "yt-dlp"):
        shutil.rmtree(cache / name, ignore_errors=True)
    try:
        cache.rmdir()  # only if nothing else lives there
    except OSError:
        pass


@app.on_event("startup")
async def _attach_browser_audio():
    browser_audio.attach(asyncio.get_running_loop())
    analysis_tap.sweep(str(plugin_host.TAP_DIR))  # rings of workers that died with the last run


def _row_to_dict(r) -> dict:
    return {k: r[k] for k in r.keys()}


app.include_router(review.router)
app.include_router(browser_audio.router)


@app.get("/")
def index():
    # Browser mode: the page learns where the API is and the token from here
    # (the desktop shell injects the same into its own webviews).
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(security.inject_bootstrap(html))


def _superseded(rows) -> list:
    """Cleanup review's dedup candidates. FL Cloud plugins must stay where they're installed."""
    return [c for c in dedup.superseded_candidates(rows) if not c["superseded"]["fl_cloud"]]


def _is_unorganized(r, scan_roots) -> bool:
    """The Organize tab's rule: a loose plugin with a known vendor (FL Cloud ones stay put)."""
    return bool(not r["fl_cloud"] and r["vendor"]
                and vendor_org.needs_vendor_folder(r["host_path"], r["format"], scan_roots))


@app.get("/api/stats")
def stats():
    with db.connect(DB_PATH) as conn:
        rows = db.all_plugins(conn)
        cat_map = db.category_tags_by_plugin(conn)
        dedup_candidates = _superseded(rows)
        pending_actions = db.list_actions(conn, applied=False)
        quarantined = db.list_quarantined(conn)
        out_of_scope_count = len(db.list_out_of_scope(conn, include_ignored=False))
        scan_roots = db.scan_roots_as_objects(conn)

    by_format = {}
    for r in rows:
        by_format[r["format"]] = by_format.get(r["format"], 0) + 1

    return {
        "total": len(rows),
        "by_format": by_format,
        "flagged": sum(1 for r in rows if r["status"] != "ok"),
        "missing_vendor": sum(1 for r in rows if not r["vendor"]),
        "ambiguous_vendor": sum(1 for r in rows if not r["vendor"] and r["vendor_note"]),
        "inferred_vendor": sum(1 for r in rows if r["vendor_source"] == "inferred"),
        "with_categories": sum(1 for r in rows if r["id"] in cat_map),
        "without_categories": sum(1 for r in rows if r["id"] not in cat_map),
        "dedup_candidates": len(dedup_candidates),
        "pending_actions": len(pending_actions),
        "quarantined": len(quarantined),
        "fl_cloud": sum(1 for r in rows if r["fl_cloud"]),
        "unorganized": sum(1 for r in rows if _is_unorganized(r, scan_roots)),
        "missing_type": sum(1 for r in rows if not r["plugin_type"] or r["plugin_type"] == "unknown"),
        "out_of_scope": out_of_scope_count,
    }


@app.get("/api/dashboard")
def dashboard():
    """Everything the Dashboard shows, in one call. Every count uses the same
    rule as the tab it summarizes (the review tabs, Cleanup, Organize, Actions,
    Quarantine, Playground), so a number here always matches what that tab lists."""
    with db.connect(DB_PATH) as conn:
        rows = db.all_plugins(conn)
        cat_map = db.category_tags_by_plugin(conn)
        scan_roots = db.scan_roots_as_objects(conn)
        pending_actions = len(db.list_actions(conn, applied=False))
        quarantined = db.list_quarantined(conn)
        out_of_scope_count = len(db.list_out_of_scope(conn, include_ignored=False))
        last_scan = conn.execute(
            "SELECT finished_at FROM scans WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
        fl_installed = _fl_studio_info(conn)["fl_studio_installed"]
    cat_ids = set(cat_map)

    def tally(key_of, with_size=False):
        counts: dict = {}
        for r in rows:
            key = key_of(r)
            c = counts.setdefault(key, [0, 0])
            c[0] += 1
            c[1] += r["size_bytes"] or 0
        ordered = sorted(counts.items(), key=lambda kv: (-kv[1][0], str(kv[0]).lower()))
        return [(k, n, size) if with_size else (k, n) for k, (n, size) in ordered]

    categories: dict[str, int] = {}
    for tags in cat_map.values():
        for name, _slug, _source in tags:
            categories[name] = categories.get(name, 0) + 1
    bitness = {"64": 0, "32": 0, "unknown": 0}
    for r in rows:
        bitness[str(r["bitness"]) if r["bitness"] in (32, 64) else "unknown"] += 1
    hostable = _hostable_rows(rows, {q["host_path"] for q in quarantined})

    def members(kind):
        return sum(1 for r in rows if review._is_member(kind, r, cat_ids))

    return {
        "total": len(rows),
        "total_size_bytes": sum(r["size_bytes"] or 0 for r in rows),
        "last_scan_at": last_scan[0] if last_scan else None,
        "vendors_total": len({r["vendor"] for r in rows if r["vendor"]}),
        "by_format": [{"format": k, "count": n, "size_bytes": s} for k, n, s in tally(lambda r: r["format"], True)],
        "by_type": [{"type": k, "count": n} for k, n in tally(
            lambda r: r["plugin_type"] if r["plugin_type"] and r["plugin_type"] != "unknown" else "unknown")],
        "by_category": [{"name": k, "count": n}
                        for k, n in sorted(categories.items(), key=lambda kv: (-kv[1], kv[0].lower()))],
        "by_vendor": [{"name": k, "count": n, "size_bytes": s}
                      for k, n, s in tally(lambda r: r["vendor"] or "", True) if k][:10],
        "by_bitness": bitness,
        "health": {
            "flagged": sum(1 for r in rows if r["status"] != "ok"),
            "missing_vendor": members("vendor"),
            "inferred_vendor": members("inferred_vendor"),
            "ambiguous_vendor": sum(1 for r in rows if not r["vendor"] and r["vendor_note"]),
            "missing_type": members("type"),
            "without_categories": members("categories"),
            "missing_links": members("links"),
            "dedup_candidates": len(_superseded(rows)),
            "unorganized": sum(1 for r in rows if _is_unorganized(r, scan_roots)),
        },
        "queue": {
            "pending_actions": pending_actions,
            "quarantined": len(quarantined),
            "quarantine_size_bytes": sum(q["size_bytes"] or 0 for q in quarantined),
        },
        "fl": {
            "installed": fl_installed,
            "detected": sum(1 for r in rows if r["fl_detected"]),
            "fl_cloud": sum(1 for r in rows if r["fl_cloud"]),
        },
        "playground": {
            "hostable": len(hostable),
            # Pictures across the whole catalog (owner, 2026-10-15): web pictures count too,
            # including for plugins that can't open here (FL-native, analyzers…).
            "with_image": sum(1 for r in rows if os.path.exists(plugin_host.image_path(r["host_path"]))),
            "catalog": len(rows),
        },
        "out_of_scope": out_of_scope_count,
    }


def _action_status(action_row: dict | None) -> dict | None:
    if action_row is None:
        return None
    return {
        "action_id": action_row["id"],
        "action": action_row["action"],
        "applied": bool(action_row["applied"]),
        "reason": action_row["reason"],
    }


@app.get("/api/plugins")
def list_plugins(q: str = "", vendor: list[str] = Query([]), format: list[str] = Query([]),
                  category: list[str] = Query([]), status: str = "", vendor_source: str = "",
                  plugin_type: list[str] = Query([]), fl: str = "", tag: list[str] = Query([])):
    # vendor/format/category/plugin_type are multi-select (repeated query params,
    # e.g. ?vendor=A&vendor=B) — any match within the selected set passes, same
    # "OR within a filter" semantics as a normal multi-select dropdown. status/
    # vendor_source stay single-select (not asked for, lower-cardinality anyway).
    with db.connect(DB_PATH) as conn:
        rows = db.all_plugins(conn)
        cat_map = db.category_tags_by_plugin(conn)
        action_map = db.active_actions_by_plugin(conn)
    pics = pictures.picture_index()  # one directory read for every row's picture

    q = q.lower().strip()
    out = []
    for r in rows:
        if q and q not in (r["name"] or "").lower() and q not in (r["vendor"] or "").lower():
            continue
        if vendor and (r["vendor"] or "") not in vendor:
            continue
        if format and r["format"] not in format:
            continue
        if status and r["status"] != status:
            continue
        if vendor_source and (r["vendor_source"] or "") != vendor_source:
            continue
        if plugin_type and (r["plugin_type"] or "") not in plugin_type:
            continue
        if fl == "marked" and not r["fl_detected"]:
            continue
        if fl == "unmarked" and r["fl_detected"]:
            continue
        if fl == "fl_cloud" and not r["fl_cloud"]:
            continue
        # Tag filters (Browse's tag chips): "x" keeps only plugins with tag x, "-x" drops them; all apply.
        if tag and not all(_PLUGIN_TAGS[t.lstrip("-")](r, action_map, pics) != t.startswith("-")
                           for t in tag if t.lstrip("-") in _PLUGIN_TAGS):
            continue
        tags = cat_map.get(r["id"], [])
        # "" is the frontend's sentinel for the "(No category)" filter bucket
        # (see initFilters() in app.js) — a plugin with zero tags (e.g. one
        # orphaned by a deleted category, see db.py:delete_category_reference's
        # ON DELETE CASCADE) matches it instead of matching nothing at all.
        if category:
            matches_blank = "" in category and not tags
            if not matches_blank and not any(t[0] in category for t in tags):
                continue
        d = _row_to_dict(r)
        d["categories"] = [{"name": n, "slug": sl, "source": s} for n, sl, s in tags]
        d["action_status"] = _action_status(action_map.get(r["id"]))
        # The picture's mtime: its URLs carry it, so they can be cached until it changes.
        d["image_stamp"] = pictures.picture_stamp(r["host_path"], pics)
        d["has_image"] = d["image_stamp"] is not None
        out.append(d)
    return out


# Browse's tag filters (GET /api/plugins?tag=…): name → predicate(row, action_map, picture index).
# DAW tags are prefixed by the DAW, so other DAWs can add theirs later.
_PLUGIN_TAGS = {
    "fl_cloud": lambda r, a, p: bool(r["fl_cloud"]),
    "fl_synced": lambda r, a, p: bool(r["fl_detected"]),  # in FL Studio's own plugin database
    "fl_native": lambda r, a, p: r["format"] == "FL-NATIVE",
    "picture": lambda r, a, p: pictures.picture_stamp(r["host_path"], p) is not None,
    "pending": lambda r, a, p: r["id"] in a,
}


@app.get("/api/plugins/{plugin_id}")
def get_plugin(plugin_id: int):
    with db.connect(DB_PATH) as conn:
        row = db.get_plugin(conn, plugin_id)
        if row is None:
            raise HTTPException(404, "not found")
        cat_map = db.category_tags_by_plugin(conn)
        active = db.active_action_for_plugin(conn, plugin_id)
        host_issue = plugin_host.host_issue(row, db.quarantined_host_paths(conn))
        picture = pictures.picture_info(conn, row["host_path"])
        # Pictures found online waiting for the owner's choice (the sheet offers "Choose picture…").
        picture_candidates = conn.execute("SELECT COUNT(*) FROM picture_suggestions WHERE plugin_id=?",
                                          (plugin_id,)).fetchone()[0]
    d = _row_to_dict(row)
    d["picture_candidates"] = picture_candidates
    d["categories"] = [{"name": n, "slug": sl, "source": s} for n, sl, s in cat_map.get(plugin_id, [])]
    d["action_status"] = _action_status(dict(active) if active else None)
    d["has_image"] = picture is not None
    # {source: capture | auto-capture | web, url, page_url, stamp} (see pictures.picture_info)
    d["picture"] = picture
    d["hostable"] = host_issue is None  # same rule as the Playground's plugin list
    d["host_issue"] = host_issue  # {code, reason, retry[, at]} when it can't be opened (see plugin_host.host_issue)
    # The last automatic picture capture: {ok, reason, at}, or None (never tried since the file changed).
    d["picture_capture"] = plugin_host.picture_capture_result(row["host_path"])
    d["linked"] = []
    if d.get("link_group"):
        with db.connect(DB_PATH) as conn:
            d["linked"] = [
                {"id": r["id"], "name": r["name"], "format": r["format"], "host_path": r["host_path"], "bitness": r["bitness"]}
                for r in db.all_plugins(conn) if r["link_group"] == d["link_group"] and r["id"] != plugin_id
            ]
    return d


def _hostable_plugin(plugin_id: int) -> dict:
    with db.connect(DB_PATH) as conn:
        row = db.get_plugin(conn, plugin_id)
    if row is None:
        raise HTTPException(404, "not found")
    return _row_to_dict(row)


def _host_call(fn, *args):
    try:
        return fn(*args)
    except plugin_host.HostError as e:
        raise HTTPException(409, str(e))


class HostPlayBody(BaseModel):
    source: str | None = None  # a local audio file path, or:
    which: str | None = None  # the Audio tab's 'source' (with its edits) or its 'recording' (dry)
    start_s: float = 0
    loop: list[float] | None = None  # [start_s, end_s]: loop that part
    until: list[float] | None = None  # [start_s, end_s]: play that selection once (ends at end_s)
    # which='source': its recording plays along, into the plugin (the panes' M buttons mute either)
    with_recording: bool = False
    mute_source: bool = False
    mute_recording: bool = False


class HostMixBody(BaseModel):
    mute_source: bool | None = None
    mute_recording: bool | None = None


class HostSourceBody(BaseModel):
    kind: str | None = None  # 'browser' | 'file' | None (nothing)


class HostLoopBody(BaseModel):
    start_s: float | None = None  # both None: no loop
    end_s: float | None = None


class HostRecordBody(BaseModel):
    on: bool = True


class HostSeekBody(BaseModel):
    seconds: float


class HostBypassBody(BaseModel):
    on: bool


class HostCaptureBody(BaseModel):
    target: str = "picture"  # 'picture' | 'file' | 'clipboard'


def _playback_settings() -> dict:
    """The audio settings, read fresh for every play so a change in
    Configuration applies to the next play without reopening (and is pushed
    to what plays now: settings_update -> plugin_host.apply_audio_settings)."""
    keys = ("audio_driver", "audio_output_device", "audio_sample_rate", "audio_block_size", "audio_safe_mode",
            "audio_plugin_buffer")
    with db.connect(DB_PATH) as conn:
        v = {k: db.get_setting(conn, k, _SETTINGS_DEFAULTS[k]) for k in keys}
    return {
        "driver": v["audio_driver"] if v["audio_driver"] in host_worker.DRIVERS else "wasapi",
        "device": v["audio_output_device"] or "",
        "samplerate": int(v["audio_sample_rate"] or 48000),
        "blocksize": int(v["audio_block_size"] or 512),
        "safe": v["audio_safe_mode"] == "1",
        "buffer": v["audio_plugin_buffer"] if v["audio_plugin_buffer"] in host_worker.PLUGIN_BUFFERS else "auto",
    }


def _midi_settings() -> dict:
    """{inputs: "*" | [ids] | [], channel: 0-16, block_pitchbend} for the open plugin's worker."""
    with db.connect(DB_PATH) as conn:
        raw = db.get_setting(conn, "midi_inputs", _SETTINGS_DEFAULTS["midi_inputs"])
        channel = db.get_setting(conn, "midi_channel", _SETTINGS_DEFAULTS["midi_channel"])
        bend = db.get_setting(conn, "midi_block_pitchbend", _SETTINGS_DEFAULTS["midi_block_pitchbend"])
    if raw == "*":
        inputs = "*"
    else:
        try:
            inputs = [i for i in json.loads(raw or "[]") if isinstance(i, str)]
        except ValueError:
            inputs = []
    return {"inputs": inputs, "channel": int(channel) if str(channel).isdigit() else 0,
            "block_pitchbend": bend == "1"}


def _auto_balance() -> bool:
    """Configuration > Audio's Auto Level Compensation: every plugin opens with Auto-Balance on."""
    with db.connect(DB_PATH) as conn:
        return db.get_setting(conn, "audio_auto_balance", _SETTINGS_DEFAULTS["audio_auto_balance"]) == "1"


def _memory_limit_gb() -> int:
    """Configuration's Plugin memory limit (GB) for every plugin worker."""
    with db.connect(DB_PATH) as conn:
        value = db.get_setting(conn, "plugin_memory_limit_gb", _SETTINGS_DEFAULTS["plugin_memory_limit_gb"])
    return int(value) if str(value).isdigit() else plugin_host.DEFAULT_MEMORY_LIMIT_GB


# The toolbar's MIDI indicator: watches the selected inputs while no plugin has them.
midi_watch = midi_monitor.MidiMonitor(_midi_settings, plugin_host.sessions_busy, plugin_host.open_midi_inputs)
plugin_host.configure(audio=_playback_settings, midi=_midi_settings, memory=_memory_limit_gb, auto_balance=_auto_balance,
                      midi_release=midi_watch.release, midi_tap=midi_watch.record_worker,
                      record_tap=playground_audio.on_record)


@app.on_event("startup")
def _start_midi_monitor():
    midi_watch.start()


@app.on_event("shutdown")
def _stop_midi_monitor():
    midi_watch.shutdown()


@app.get("/api/midi/activity")
def midi_activity(since: int = 0):
    """The toolbar's MIDI indicator: the messages after `since` (the seq the page
    last saw) from the selected inputs, whoever holds them (this backend's
    monitor, or the open plugin's worker): {seq, selected, owner, open, failed,
    channel, block_pitchbend, events: [{seq, t, device, source, bytes, text, flags}]}."""
    return midi_watch.activity(since)


@app.get("/api/system/memory")
def system_memory():
    """VEXT's memory (the whole process tree's private bytes, app/sysmem.py)."""
    return sysmem.usage()


@app.get("/api/system/runtime")
def system_runtime():
    """Microsoft's Visual C++ runtime as plugins will get it (app/runtime_check.py):
    {ok, problem, version, path, minimum, legacy, repair, shipped}."""
    return runtime_check.status()


@app.post("/api/system/runtime/repair")
def system_runtime_repair():
    """Installs / repairs the shipped runtimes (Windows asks for permission); waits."""
    return runtime_check.repair()


@app.exception_handler(folder_access.NeedsAccess)
async def _needs_folder_access(_request: Request, exc: folder_access.NeedsAccess):
    """A move / delete Windows refused: 403 {detail, folder}, the page offers to allow VEXT there."""
    return JSONResponse({"detail": exc.message, "folder": exc.folder}, status_code=403)


class FolderAccessBody(BaseModel):
    folder: str


@app.post("/api/system/folder-access")
def system_folder_access(body: FolderAccessBody):
    """Gives the signed-in user write access to a plugin folder (a scan root or inside one), elevated:
    Windows asks for permission. Waits. {ok, folder, message}."""
    try:
        return folder_access.grant(body.folder)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


class HostOpenBody(BaseModel):
    # The desktop shell will put the editor in its frame: it stays off the screen
    # until the frame reveals it (see plugin_host.open_plugin / reveal).
    framed: bool = False


@app.post("/api/plugins/{plugin_id}/host")
def host_open(plugin_id: int, body: HostOpenBody | None = None):
    """Opens the plugin's editor (closing any other open plugin first)."""
    p = _hostable_plugin(plugin_id)
    with db.connect(DB_PATH) as conn:
        issue = plugin_host.host_issue(p, db.quarantined_host_paths(conn))
    # Some issues don't block a retry (the cause may be fixed: a DLL installed,
    # a license activated); the others do (see plugin_host.RETRY_CODES).
    if issue and not issue["retry"]:
        raise HTTPException(409, f"Can't open this plugin: {issue['reason']}.")
    if issue and issue["code"] == "not_licensed":
        plugin_host.forget_shell_names(p["host_path"])  # licensed since? the shell is listed again
    framed = bool(body and body.framed)
    return _host_call(plugin_host.open_plugin, plugin_id, p["host_path"], p["format"], p["name"], framed)


class HostGainBody(BaseModel):
    in_db: float | None = None  # IN: -60 (silence) .. +12 dB
    out_db: float | None = None  # OUT: -60 (silence) .. +12 dB
    auto_balance: bool | None = None


@app.post("/api/plugins/{plugin_id}/host/gain")
def host_gain(plugin_id: int, body: HostGainBody):
    """The frame's IN / OUT knobs and Auto-Balance (any of them). Kept per
    plugin. The state's `gain` has them, plus what Auto-Balance applies now."""
    return _host_call(plugin_host.set_gain, plugin_id, body.in_db, body.out_db, body.auto_balance)


@app.post("/api/plugins/{plugin_id}/host/reveal")
def host_reveal(plugin_id: int):
    """Shows an editor opened with framed=true that the frame couldn't adopt:
    it stays a window of its own."""
    return _host_call(plugin_host.reveal, plugin_id)


@app.get("/api/plugins/{plugin_id}/host")
def host_state(plugin_id: int):
    return plugin_host.state(plugin_id)


@app.post("/api/plugins/{plugin_id}/host/source")
def host_source(plugin_id: int, body: HostSourceBody):
    """'browser': the in-app browser's audio plays through the plugin (stops a
    file). 'file': stops the browser's audio (play a file next). None: stops both."""
    kind = body.kind or None
    if kind not in ("browser", "file", None):
        raise HTTPException(400, "kind must be 'browser', 'file' or null")
    return _host_call(plugin_host.set_source, plugin_id, kind, _playback_settings())


@app.post("/api/plugins/{plugin_id}/host/play")
def host_play(plugin_id: int, body: HostPlayBody):
    """Plays a file through the plugin: `source` (a path), or the Audio tab's
    `which`: 'source' (its decoded copy, with its edits) or 'recording' (as
    recorded: no plugin, no gains). `loop`: [start_s, end_s]."""
    envelope, dry, source, mix = None, False, body.source, None
    try:
        if body.which == "source":
            source, envelope, _duration = playground_audio.source_for_play()
            if body.with_recording and not plugin_host.recording_sessions():
                try:
                    mix = {"path": playground_audio.recording_for_play(), "source": 0.0 if body.mute_source else 1.0,
                           "recording": 0.0 if body.mute_recording else 1.0}
                except ValueError:
                    mix = None  # no recording (any more): the file alone
        elif body.which == "recording":
            source, dry = playground_audio.recording_for_play(), True
        elif body.which is not None:
            raise HTTPException(400, "which must be 'source' or 'recording'")
    except ValueError as e:
        raise HTTPException(409, str(e))
    if not source:
        raise HTTPException(400, "give a source path or which")
    loop = body.loop if body.loop and len(body.loop) == 2 else None
    until = body.until if body.until and len(body.until) == 2 else None
    return _host_call(plugin_host.play, plugin_id, source, body.start_s, _playback_settings(), envelope, loop, dry,
                      until, mix)


@app.post("/api/plugins/{plugin_id}/host/mix")
def host_mix(plugin_id: int, body: HostMixBody):
    """Mutes the file or its recording while both play into the plugin (the M buttons)."""
    gain = lambda muted: None if muted is None else (0.0 if muted else 1.0)  # noqa: E731
    return _host_call(plugin_host.set_mix, plugin_id, gain(body.mute_source), gain(body.mute_recording))


@app.post("/api/plugins/{plugin_id}/host/loop")
def host_loop(plugin_id: int, body: HostLoopBody):
    """Loops [start_s, end_s] of the file playing; no values: plays on."""
    loop = [body.start_s, body.end_s] if body.start_s is not None and body.end_s is not None else None
    return _host_call(plugin_host.set_loop, plugin_id, loop)


@app.post("/api/plugins/{plugin_id}/host/record")
def host_record(plugin_id: int, body: HostRecordBody):
    """Arms (on) or disarms the Audio tab's recording: while its file plays
    through the plugin, what comes out is recorded (on the file's time axis)."""
    if body.on:
        try:
            _source, _edits, duration = playground_audio.source_for_play()
        except ValueError as e:
            raise HTTPException(409, str(e))
        return _host_call(plugin_host.record, plugin_id, True, playground_audio.recording_path(), duration)
    return _host_call(plugin_host.record, plugin_id, False)


# ---------- the Playground's Audio tab (app/playground_audio.py) ----------

class AudioOpenBody(BaseModel):
    path: str


class AudioEditBody(BaseModel):
    op: str  # fade_in | fade_out | normalize
    start_s: float | None = None  # the selection (none: length_s from the start / to the end; normalize: all)
    end_s: float | None = None
    length_s: float | None = None


class AudioSaveBody(BaseModel):
    path: str | None = None  # from the save dialog; none: Music\VEXT\...


def _audio_call(fn, *args):
    try:
        return fn(*args)
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.get("/api/playground/audio")
def pg_audio_state():
    """The Audio tab: the file loaded (decoding progress, info, edits) and the recording."""
    return playground_audio.state()


@app.post("/api/playground/audio/open")
def pg_audio_open(body: AudioOpenBody):
    """Decodes an audio file (any format FFmpeg reads); replaces the one loaded."""
    _stop_audio_tab_playback()
    return _audio_call(playground_audio.open_file, body.path)


@app.post("/api/playground/audio/upload")
async def pg_audio_upload(request: Request, name: str = Query("audio")):
    """The raw bytes of a file dropped on the page without the desktop app
    (a browser tab gets no path), then decoded like open."""
    target = playground_audio.upload_target(name)
    with open(target, "wb") as f:
        async for chunk in request.stream():
            f.write(chunk)
    _stop_audio_tab_playback()
    return _audio_call(playground_audio.open_file, str(target), os.path.basename(name or "audio"))


def _stop_audio_tab_playback() -> None:
    """A new file replaces the decoded copy: whatever plays from the Audio tab stops first."""
    state = playground_audio.state()
    paths = {p.lower() for p in (state.get("source_path"), (state.get("recording") or {}).get("path")) if p}
    for pid in list(plugin_host._sessions):
        st = plugin_host.state(pid)
        if st["state"] == "open" and (st["recording"] or st["mix"]
                                      or (st["playing"] and (st["source"] or "").lower() in paths)):
            try:
                plugin_host.stop(pid)
            except plugin_host.HostError:
                pass


@app.delete("/api/playground/audio")
def pg_audio_unload():
    _stop_audio_tab_playback()
    return playground_audio.unload()


@app.get("/api/playground/audio/peaks")
def pg_audio_peaks(which: str = "source", start: int = 0, end: int = 0, width: int = 1000):
    """The waveform for a view: frames [start, end) in `width` columns. Binary:
    b"VXPK", kind (0 empty, 1 samples, 2 min/max peaks), channels, count (uint32
    each), then float32 data: count x channels (samples) or count x channels x 2."""
    if which not in ("source", "recording"):
        raise HTTPException(400, "which must be 'source' or 'recording'")
    return Response(playground_audio.peaks(which, start, end, width), media_type="application/octet-stream")


# ---------- the analysis tap and the browser recording (app/analysis_tap.py, app/browser_record.py) ----------

ANALYSIS_FRAMES = 16384  # frames one stream message carries at most (a client that fell behind gets the newest)
ANALYSIS_POLL_S = 0.02


@app.websocket("/api/analysis/stream")
async def analysis_stream(ws: WebSocket, token: str = ""):
    """The Analyzer's feed of what VEXT plays: binary b"VXAN", uint32 rate, mode, flags,
    gen, uint64 end_frame (the frame after the last one sent), uint32 n, then float32
    before[n][2] and float32 after[n][2] (zeros without an input), about every 20 ms
    while something plays; and text {"status": {...}} on connect, on change and every
    second. Connected = an analyzer: the browser's pages are asked to send their sound."""
    if not security.token_ok(token):
        await ws.close(code=1008)  # before accept: the handshake is refused (HTTP 403)
        return
    await ws.accept()
    analysis_tap.client_added()

    async def watch():  # notices the client leaving even while nothing is sent to it
        try:
            while (await ws.receive())["type"] != "websocket.disconnect":
                pass
        except Exception:  # noqa: BLE001
            pass

    watcher = asyncio.ensure_future(watch())
    try:
        last, last_at, cursor = None, 0.0, (None, None, 0)  # cursor: (ring, gen, next frame)
        while not watcher.done():
            picked = analysis_tap.pick()
            reader = picked[0]
            if reader is not None:
                ring, gen, end = cursor
                if ring is not reader:  # another producer: its newest frames, not where the last one was
                    gen, end = None, 0
                got = reader.read(gen, end, ANALYSIS_FRAMES, newest=True)
                cursor = (reader, got[0], got[4])
                if len(got[6]):
                    await ws.send_bytes(analysis_tap.pack_frame(*got))
            status = analysis_tap.status(picked)
            now = time.monotonic()
            if status != last or now - last_at >= 1.0:
                last, last_at = status, now
                await ws.send_json({"status": status})
            await asyncio.sleep(ANALYSIS_POLL_S)
    except Exception:  # noqa: BLE001 - a dropped connection ends the same way
        pass
    finally:
        watcher.cancel()
        analysis_tap.client_removed()


class BrowserRecordBody(BaseModel):
    on: bool


@app.get("/api/playground/browser-record")
def pg_browser_record_state():
    """{on, seconds, frames, rate, peak (linear, since the last GET), through (the plugin
    the sound went through, else null), error, pending}."""
    return browser_record.status(take_peak=True)


@app.post("/api/playground/browser-record")
def pg_browser_record(body: BrowserRecordBody):
    """on: starts recording what the browser plays. off: stops, and opens the recording as
    the Audio tab's file (replacing the one loaded): the status plus {"audio": <Audio tab state>}.
    409 when it already records / doesn't / nothing was recorded."""
    try:
        if body.on:
            return browser_record.start()
        done = browser_record.stop()
    except browser_record.RecordError as e:
        raise HTTPException(409, str(e))
    _stop_audio_tab_playback()
    audio = _audio_call(playground_audio.open_file, done["path"], done["name"])
    return {**done["status"], "audio": audio}


@app.post("/api/playground/audio/edit")
def pg_audio_edit(body: AudioEditBody):
    """Fade in / Fade out / Normalize, non-destructive (the open plugin hears it at once)."""
    out = _audio_call(playground_audio.edit, body.op, body.start_s, body.end_s, body.length_s)
    _push_envelope()
    return out


@app.post("/api/playground/audio/undo")
def pg_audio_undo():
    out = _audio_call(playground_audio.undo)
    _push_envelope()
    return out


def _push_envelope() -> None:
    try:
        path, edits, _duration = playground_audio.source_for_play()
    except ValueError:
        return
    plugin_host.set_envelope(path, edits)


@app.delete("/api/playground/audio/recording")
def pg_audio_clear_recording():
    plugin_host.stop_dry()  # its file can't be deleted while it plays
    return _audio_call(playground_audio.clear_recording, plugin_host.recording_sessions())


@app.post("/api/playground/audio/recording/save")
def pg_audio_save_recording(body: AudioSaveBody | None = None):
    """Saves the recording as a WAV (32-bit float, the stream's rate): to the
    path chosen in the save dialog, or to Music/VEXT."""
    name = ""
    for pid in list(plugin_host._sessions):
        st = plugin_host.state(pid)
        if st["state"] == "open":
            name = st.get("plugin_name") or ""
    try:
        path = playground_audio.save_recording(body.path if body else None, name)
    except ValueError as e:
        raise HTTPException(409, str(e))
    except OSError as e:
        raise HTTPException(409, f"Couldn't save the recording: {e.strerror or e}")
    return {"ok": True, "path": path}


@app.post("/api/playground/audio/picture")
async def pg_audio_save_picture(request: Request, path: str | None = None, name: str = "Waveform"):
    """Saves a picture of the waveforms (PNG bytes in the body, made by the page):
    to `path` from the save dialog, or to Pictures/VEXT/<name> <time>.png."""
    data = await request.body()
    if not data.startswith(b"\x89PNG\r\n\x1a\n") or len(data) > 64 << 20:
        raise HTTPException(400, "not a PNG picture")
    if path:
        target = Path(path)
        if target.suffix.lower() != ".png":
            target = target.with_name(target.name + ".png")
    else:
        stamp = time.strftime("%Y-%m-%d %H.%M.%S")
        target = plugin_host.known_folder("{33E28130-4E1E-4676-835A-98395C3BC3BB}", "Pictures") / "VEXT" / (
            f"{plugin_host.safe_file_name(name)} {stamp}.png")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".part")
        tmp.write_bytes(data)
        os.replace(tmp, target)
    except OSError as e:
        raise HTTPException(409, f"Couldn't save the picture: {e.strerror or e}")
    return {"ok": True, "path": str(target)}


def _wav_response(request: Request, path) -> Response:
    """A WAV the page's <audio> plays, with Range requests (206): without them the
    browser can't seek it, so playing from the selection or the playhead started at 0.
    (This Starlette's FileResponse ignores Range.)"""
    size = os.path.getsize(path)
    rng = (request.headers.get("range") or "").strip()
    start, end = 0, size - 1
    if rng.startswith("bytes=") and "," not in rng:
        a, _, b = rng[6:].partition("-")
        try:
            if a:
                start = int(a)
                end = min(int(b), size - 1) if b else size - 1
            elif b:
                start = max(0, size - int(b))
        except ValueError:
            rng = ""
        if start > end or start >= size:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
    else:
        rng = ""

    def chunks():
        # Opened for each chunk, never held between them: an <audio> leaves its request
        # pending once it has buffered enough, and an open handle would keep the file
        # from being deleted (Clear the recording: "being used by another process").
        pos, left = start, end - start + 1
        while left > 0:
            try:
                with open(path, "rb") as f:
                    f.seek(pos)
                    data = f.read(min(1 << 20, left))
            except OSError:
                break  # deleted or replaced meanwhile
            if not data:
                break
            pos += len(data)
            left -= len(data)
            yield data

    headers = {"Accept-Ranges": "bytes", "Content-Length": str(end - start + 1)}
    if rng:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return StreamingResponse(chunks(), status_code=206 if rng else 200, media_type="audio/wav", headers=headers)


@app.get("/api/playground/audio/source.wav")
def pg_audio_source_file(request: Request):
    """The loaded file with its edits, played in the page when no plugin is open."""
    try:
        path = playground_audio.heard_source_path()
    except ValueError as e:
        raise HTTPException(404, str(e))
    return _wav_response(request, path)


@app.get("/api/playground/audio/recording.wav")
def pg_audio_recording_file(request: Request):
    """The recording itself (played in the page when no plugin is open)."""
    try:
        path = playground_audio.recording_for_play()
    except ValueError as e:
        raise HTTPException(404, str(e))
    return _wav_response(request, path)


# ---------- the Playground's Signal tab (app/signal_gen.py) ----------

class SignalBody(BaseModel):
    patch: object = None  # the Signal tab's patch (signal_gen.clean_patch: any other value is a 409)


class WavetableBody(BaseModel):
    path: str  # a WAV file: imported as a user wavetable


@app.get("/api/playground/signal/tables")
def pg_signal_tables():
    """The wavetables the oscillators can read: the built-in ones, then the user's."""
    from app import signal_gen
    return {"tables": signal_gen.tables()}


@app.post("/api/playground/signal/wavetable")
def pg_signal_wavetable(body: WavetableBody):
    """Imports a WAV as a user wavetable (cache/playground/wavetables); answers the table list."""
    from app import signal_gen
    try:
        return {"tables": signal_gen.import_wavetable(body.path)}
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.post("/api/playground/signal")
def pg_signal_render(body: SignalBody):
    """Renders the patch to a WAV and opens it as the Audio tab's file, so the plugin, Record and
    the Analyzer take it like any file. Answers the Audio tab state, a preview and the measurements."""
    from app import signal_gen
    try:
        clean = signal_gen.clean_patch(body.patch)
        audio, info = signal_gen.render(clean)
        path = signal_gen.save(clean, audio, info["rate"])
    except ValueError as e:
        raise HTTPException(409, str(e))
    _stop_audio_tab_playback()
    state = _audio_call(playground_audio.open_file, str(path), f"Signal – {info['summary']}.wav")
    return {"audio": state, "preview": signal_gen.preview(audio, info["rate"]), "info": info}


@app.get("/api/playground/decoding")
def pg_decoding():
    """Configuration > Audio's "Audio decoding" line: FFmpeg built in via PyAV."""
    return playground_audio.decoding_info()


@app.post("/api/plugins/{plugin_id}/host/pause")
def host_pause(plugin_id: int):
    return _host_call(plugin_host.pause, plugin_id)


@app.post("/api/plugins/{plugin_id}/host/resume")
def host_resume(plugin_id: int):
    return _host_call(plugin_host.resume, plugin_id)


@app.post("/api/plugins/{plugin_id}/host/seek")
def host_seek(plugin_id: int, body: HostSeekBody):
    return _host_call(plugin_host.seek, plugin_id, body.seconds)


@app.post("/api/plugins/{plugin_id}/host/stop")
def host_stop(plugin_id: int):
    return _host_call(plugin_host.stop, plugin_id)


@app.post("/api/plugins/{plugin_id}/host/bypass")
def host_bypass(plugin_id: int, body: HostBypassBody):
    return _host_call(plugin_host.bypass, plugin_id, body.on)


class HostMidiBody(BaseModel):
    # Channel messages: [[status, data1, data2], ...] (note on/off, CC, pitch bend...).
    events: list[list[int]]


@app.post("/api/plugins/{plugin_id}/host/midi")
def host_midi(plugin_id: int, body: HostMidiBody):
    """MIDI from the Playground's keyboard. Answers at once (no state)."""
    if not body.events or len(body.events) > 64:
        raise HTTPException(400, "send 1 to 64 events")
    for event in body.events:
        if not 2 <= len(event) <= 3 or not 0x80 <= event[0] <= 0xEF or any(not 0 <= b <= 127 for b in event[1:]):
            raise HTTPException(400, "each event must be a MIDI channel message: [status 0x80-0xEF, data 0-127...]")
    _host_call(plugin_host.send_midi, plugin_id, body.events)
    return {"ok": True}


@app.post("/api/plugins/{plugin_id}/host/panic")
def host_panic(plugin_id: int):
    """All notes off on every channel."""
    return _host_call(plugin_host.panic, plugin_id)


class HostSoundBody(BaseModel):
    on: bool = True


@app.post("/api/plugins/{plugin_id}/host/sound")
def host_sound(plugin_id: int, body: HostSoundBody):
    """An instrument's sound on (with the current audio settings) or off."""
    return _host_call(plugin_host.set_sound, plugin_id, body.on)


@app.post("/api/plugins/{plugin_id}/host/capture")
def host_capture(plugin_id: int, body: HostCaptureBody | None = None):
    """Screenshots the editor: 'picture' sets the plugin's picture, 'file' saves
    a PNG in Pictures/VEXT, 'clipboard' copies it. No body = 'picture'."""
    target = body.target if body else "picture"
    if target not in plugin_host.CAPTURE_TARGETS:
        raise HTTPException(400, "target must be 'picture', 'file' or 'clipboard'")
    p = _hostable_plugin(plugin_id)
    path = _host_call(plugin_host.capture, plugin_id, p["host_path"], p["name"] or "", target)
    if target == "picture":
        with db.connect(DB_PATH) as conn:
            pictures.record_source(conn, p["host_path"], "capture")
    return {"ok": True, "path": path} if path else {"ok": True}


@app.delete("/api/plugins/{plugin_id}/host")
def host_close(plugin_id: int):
    plugin_host.close(plugin_id)
    return plugin_host.state(plugin_id)


# Moved to app/pictures.py with the other picture variants (thumb).
_backdrop_path = pictures.backdrop_path


@app.get("/api/plugins/{plugin_id}/image")
def plugin_image(plugin_id: int, variant: str = "", t: str = ""):
    """The plugin's saved picture (PNG). variant=backdrop: its blurred backdrop (a ~1 KB JPEG);
    variant=thumb: at most 480x300 (JPEG), for tiles and list rows (see app/pictures.py).
    With t (the picture's stamp, e.g. image_stamp from /api/plugins) the answer may be cached:
    a new picture comes with a new stamp, so the URL changes."""
    path = plugin_host.image_path(_hostable_plugin(plugin_id)["host_path"])
    if not os.path.exists(path):
        raise HTTPException(404, "no image")
    if variant and variant not in pictures.VARIANTS:
        raise HTTPException(400, f"unknown image variant: {variant}")
    headers = {"Cache-Control": "private, max-age=31536000, immutable"} if t else None
    if variant:
        return FileResponse(pictures.variant_path(path, variant), media_type="image/jpeg", headers=headers)
    return FileResponse(path, media_type="image/png", headers=headers)


@app.delete("/api/plugins/{plugin_id}/image")
def plugin_image_delete(plugin_id: int):
    pictures.delete_with_variants(plugin_host.image_path(_hostable_plugin(plugin_id)["host_path"]))
    return {"ok": True}


# ---------- Playground ----------

def _host_issues(rows, quarantined_paths: set) -> list:
    """[(row, host_issue or None)] — see plugin_host.host_issue."""
    names = plugin_host._shell_names_cache()
    return [(r, plugin_host.host_issue(r, quarantined_paths, names)) for r in rows]


def _hostable_rows(rows, quarantined_paths: set) -> list:
    """Plugins the Playground can open: a hostable format (VST3, VST2, CLAP,
    Waves), 64-bit, not flagged broken, still where it was installed, and not
    known to fail (the last attempt or check failed)."""
    return [r for r, issue in _host_issues(rows, quarantined_paths) if issue is None]


def _playground_item(r, cat_map) -> dict:
    return {
        "id": r["id"], "name": r["name"], "vendor": r["vendor"], "format": r["format"],
        "plugin_type": r["plugin_type"], "categories": [name for name, _slug, _src in cat_map.get(r["id"], [])],
        "has_image": os.path.exists(plugin_host.image_path(r["host_path"])), "bitness": r["bitness"],
    }


@app.get("/api/playground/plugins")
def playground_plugins():
    """The plugins the Playground lists: the ones that can open. `verified`:
    it opened (or passed a check) since its file last changed; false = not
    tried yet."""
    with db.connect(DB_PATH) as conn:
        rows = _hostable_rows(db.all_plugins(conn), db.quarantined_host_paths(conn))
        cat_map = db.category_tags_by_plugin(conn)
    captures = plugin_host.picture_results()
    out = [{**_playground_item(r, cat_map), "verified": plugin_host.verified(r["host_path"]),
            "picture_capture": plugin_host.picture_capture_result(r["host_path"], captures)} for r in rows]
    out.sort(key=lambda p: ((p["name"] or "").lower(), p["id"]))
    plugin_host.ensure_standby()  # the Playground is in use: a worker waits for the first plugin
    return out


@app.get("/api/playground/unavailable")
def playground_unavailable():
    """The catalog's other plugins, each with why it can't be opened:
    host_issue = {code, reason, retry[, at]} (codes: plugin_host.ISSUE_CODES;
    `retry`: opening it anyway is allowed; `at`: found by a load attempt or
    check at that time)."""
    with db.connect(DB_PATH) as conn:
        pairs = _host_issues(db.all_plugins(conn), db.quarantined_host_paths(conn))
        cat_map = db.category_tags_by_plugin(conn)
    out = [{**_playground_item(r, cat_map), "host_issue": issue} for r, issue in pairs if issue is not None]
    out.sort(key=lambda p: ((p["name"] or "").lower(), p["id"]))
    return out


class PlaygroundCheckBody(BaseModel):
    # True: only plugins never tried since their file changed; False: all of them again.
    only_unverified: bool = True
    ids: list[int] | None = None  # just these (default: every plugin the Playground lists)


@app.post("/api/playground/check")
def playground_check(body: PlaygroundCheckBody | None = None):
    """Loads every listed plugin headlessly in the background (2 at a time,
    each in its own worker, 150 s limit) and records which open; the ones
    that fail drop out of /api/playground/plugins into /unavailable. Some
    plugins show license dialogs while loading: those end as 'timeout' and
    should be opened once by hand. Poll GET for progress."""
    body = body or PlaygroundCheckBody()
    with db.connect(DB_PATH) as conn:
        rows = _hostable_rows(db.all_plugins(conn), db.quarantined_host_paths(conn))
    if body.ids:
        wanted = set(body.ids)
        rows = [r for r in rows if r["id"] in wanted]
    if body.only_unverified:
        rows = [r for r in rows if not plugin_host.verified(r["host_path"])]
    return plugin_host.check_start(rows, label=lambda r: r["name"] or os.path.basename(r["host_path"]))


@app.get("/api/playground/check")
def playground_check_status():
    """{running, total, done, ok, failed, current: [names], started_at, finished_at, cancelled}"""
    return plugin_host.check_status()


@app.delete("/api/playground/check")
def playground_check_cancel():
    return plugin_host.check_cancel()


# ---------- automatic plugin pictures ----------

class PicturesCaptureBody(BaseModel):
    ids: list[int] | None = None  # just these plugins (default: every plugin the Playground lists)
    # True: only plugins without a picture, skipping ones whose last automatic capture failed
    # (unless their ids are given). False: every one of them again, replacing their pictures.
    only_missing: bool = True


def _picture_saved(row) -> None:
    with db.connect(DB_PATH) as conn:
        pictures.record_source(conn, row["host_path"], "auto-capture")


class PictureLink(BaseModel):
    url: str
    page_url: str | None = None   # the page it was on (an image chosen in the in-app search)


@app.post("/api/plugins/{plugin_id}/picture/link")
def plugin_picture_link(plugin_id: int, body: PictureLink):
    """The owner pastes an image link: VEXT downloads it safely and adds it to the plugin's
    waiting pictures (the picker then shows it; Apply sets it)."""
    from app import pictures

    with db.connect(DB_PATH) as conn:
        if db.get_plugin(conn, plugin_id) is None:
            raise HTTPException(404, "plugin not found")
        try:
            return pictures.add_link_candidate(conn, plugin_id, body.url, page_url=body.page_url,
                                               note="From the image search" if body.page_url else "Your link")
        except pictures.FetchRefused as e:
            raise HTTPException(400, f"Couldn't use that image: {e}") from None


class PictureCrop(BaseModel):
    x: float
    y: float
    width: float
    height: float


@app.post("/api/pictures/suggestions/{suggestion_id}/crop")
def picture_suggestion_crop(suggestion_id: int, body: PictureCrop):
    """Crops a waiting picture (in its own pixels) into a new candidate beside it: {id, width, height}."""
    from app import pictures

    with db.connect(DB_PATH) as conn:
        try:
            return pictures.crop_suggestion(conn, suggestion_id, (body.x, body.y, body.width, body.height))
        except ValueError as e:
            raise HTTPException(400, str(e)) from None


@app.post("/api/plugins/{plugin_id}/picture/crop")
def plugin_picture_crop(plugin_id: int, body: PictureCrop):
    """Crops the plugin's own picture in place: {stamp, width, height}."""
    from app import pictures

    host_path = _hostable_plugin(plugin_id)["host_path"]
    with db.connect(DB_PATH) as conn:
        try:
            return pictures.crop_picture(conn, host_path, (body.x, body.y, body.width, body.height))
        except ValueError as e:
            raise HTTPException(400, str(e)) from None


@app.get("/api/pictures/search")
def pictures_search(q: str, offset: int = 0, count: int = 5):
    """The in-app image search (no AI): {query, offset, results, more}. Thumbnails come through
    /api/pictures/search/thumb; choosing one posts it to /api/plugins/{id}/picture/link."""
    from app import pictures

    try:
        return pictures.image_search(q, offset, count)
    except pictures.FetchRefused as e:
        raise HTTPException(502, f"The image search failed: {e}") from None


@app.get("/api/pictures/search/thumb")
def pictures_search_thumb(u: str):
    from app import pictures

    try:
        data = pictures.search_thumbnail(u)
    except pictures.FetchRefused as e:
        raise HTTPException(404, str(e)) from None
    return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


@app.post("/api/pictures/capture")
def pictures_capture(body: PicturesCaptureBody | None = None):
    """Starts capturing plugin pictures in the background (or returns the job
    that runs): each plugin opens off screen in its own worker, its editor is
    saved once it has drawn itself, then it closes. One at a time. Poll GET."""
    body = body or PicturesCaptureBody()
    with db.connect(DB_PATH) as conn:
        rows = _hostable_rows(db.all_plugins(conn), db.quarantined_host_paths(conn))
    if body.ids:
        wanted = set(body.ids)
        rows = [r for r in rows if r["id"] in wanted]
    if body.only_missing:
        index = pictures.picture_index()
        results = plugin_host.picture_results()
        rows = [r for r in rows if not pictures.has_picture(r["host_path"], index)]
        if not body.ids:  # asked for by id: tried again even if it failed before
            rows = [r for r in rows if (plugin_host.picture_capture_result(r["host_path"], results) or {"ok": True})["ok"]]
    rows.sort(key=lambda r: ((r["name"] or "").lower(), r["id"]))
    return plugin_host.pictures_start(rows, label=lambda r: r["name"] or os.path.basename(r["host_path"]),
                                      on_saved=_picture_saved)


@app.get("/api/pictures/capture")
def pictures_capture_status():
    """{running, total, done, ok, failed, current, started_at, finished_at, cancelled,
    results: [{id, name, ok, reason}]} (the current or last job, newest last)."""
    return plugin_host.pictures_status()


@app.delete("/api/pictures/capture")
def pictures_capture_cancel():
    return plugin_host.pictures_cancel()


@app.get("/api/audio/devices")
def audio_devices():
    """Output devices for Configuration > Audio. Store a device's `id` in the
    audio_output_device setting ('' = the one marked `default`)."""
    try:
        devices = plugin_host.audio_devices()
    except Exception as e:  # noqa: BLE001 - PortAudio failing to start
        raise HTTPException(409, f"Couldn't list the audio devices: {e}")
    keys = ("id", "name", "hostapi", "default", "channels", "default_samplerate")
    return [{k: d[k] for k in keys} for d in devices]


@app.get("/api/audio/config")
def audio_config(driver: str = "", device: str | None = None, refresh: bool = False):
    """Configuration > Audio for one driver type (default: the saved one):
    {driver, drivers: [{id, label, available}], outputs, inputs, device (the
    id the setting resolves to), device_rate, rates: {rates, native}, error,
    settings: {...the audio settings...}, block_sizes, sample_rates, safe_extra_blocks}.
    `device`: the output to report rates for (default: the saved one). ASIO
    is listed by a short-lived process (cached a minute; refresh=true redoes it)."""
    settings = _playback_settings()
    driver = driver if driver in host_worker.DRIVERS else settings["driver"]
    wanted = settings["device"] if device is None else device
    out = audio_setup.describe(driver, wanted, refresh=refresh)
    out.update(settings=settings, block_sizes=list(host_worker.BLOCK_SIZES),
               sample_rates=list(host_worker.SAMPLE_RATES), safe_extra_blocks=host_worker.SAFE_EXTRA_BLOCKS)
    return out


@app.post("/api/audio/test")
def audio_test():
    """Plays the test tone (440 Hz, 0.6 s, amplitude 0.03) through the saved
    audio settings: {ok, via: 'plugin'|'test', audio: {driver, device_name,
    rate, block, safe, buffer_ms, latency_ms, xruns}, notes: [fallbacks]} or
    {ok: false, error}. While the open plugin plays, the tone joins its sound."""
    return audio_setup.test(_playback_settings())


@app.get("/api/midi/devices")
def midi_devices():
    """MIDI inputs connected now ([{id, name}]) and, while a plugin is open,
    which of them its worker opened: {inputs, open, failed: [{id, error}] | null}."""
    from app.hosting import midi

    try:
        inputs = [{"id": d["id"], "name": d["name"]} for d in midi.list_inputs()]
    except OSError as e:
        raise HTTPException(409, f"Couldn't list the MIDI inputs: {e}")
    opened = plugin_host.open_midi_inputs()
    return {"inputs": inputs, "open": opened["open"] if opened else None,
            "failed": opened["failed"] if opened else None, "settings": _midi_settings()}


class FlCloudBody(BaseModel):
    enabled: bool = True


@app.post("/api/plugins/{plugin_id}/fl-cloud")
def set_fl_cloud(plugin_id: int, body: FlCloudBody):
    """Marks/unmarks a plugin as an FL Cloud plugin: it stays cataloged but is
    pinned to its install path — excluded from dedup, vendor-folder organize,
    and quarantine so FL Cloud authorization keeps working. Blocked while an
    action is pending/unresolved on it."""
    with db.connect(DB_PATH) as conn:
        if db.get_plugin(conn, plugin_id) is None:
            raise HTTPException(404, "not found")
        if body.enabled and db.active_action_for_plugin(conn, plugin_id) is not None:
            raise HTTPException(409, "This plugin has a pending or unresolved action (check Actions/Quarantine) — resolve it first.")
        db.set_fl_cloud(conn, plugin_id, body.enabled)
    return {"ok": True}


@app.get("/api/fl-cloud")
def fl_cloud_list():
    with db.connect(DB_PATH) as conn:
        rows = [r for r in db.all_plugins(conn) if r["fl_cloud"]]
    return [_row_to_dict(r) for r in rows]


@app.post("/api/plugins/{plugin_id}/ignore")
def ignore_plugin(plugin_id: int):
    """Removes this plugin from the active catalog and remembers its
    host_path so future rescans skip it entirely — not cataloged, not FL
    cross-referenced, not dedup-grouped (see app/scan_service.py). Blocked
    while an action is pending or the plugin is sitting in quarantine: the
    plugin row deleting out from under those would either orphan a
    physically-quarantined file (no way to restore/permanently-delete it
    anymore) or just silently drop a pending proposal — resolve that first."""
    with db.connect(DB_PATH) as conn:
        row = db.get_plugin(conn, plugin_id)
        if row is None:
            raise HTTPException(404, "not found")
        active = db.active_action_for_plugin(conn, plugin_id)
        if active is not None:
            raise HTTPException(409, "This plugin has a pending or unresolved action (check Actions/Quarantine) — resolve it before ignoring.")
        db.add_ignored_plugin_path(conn, row["host_path"], row["name"])
        conn.execute("DELETE FROM plugins WHERE id=?", (plugin_id,))
    return {"ok": True}


def _row_to_suggestion_dict(row) -> dict:
    d = _row_to_dict(row)
    d["categories"] = [s for s in (d.get("categories") or "").split(",") if s]
    d["sources"] = [s for s in (d.get("sources") or "").split(",") if s]
    return d


@app.get("/api/plugins/{plugin_id}/research")
def get_plugin_research(plugin_id: int):
    """The currently-pending research suggestion for this plugin, if any
    (nothing has been researched yet, or a previous one was already applied/
    discarded) — null, not a 404, since "no suggestion yet" is normal."""
    with db.connect(DB_PATH) as conn:
        row = db.get_enrichment_suggestion(conn, plugin_id)
    return _row_to_suggestion_dict(row) if row else None


@app.post("/api/plugins/{plugin_id}/research")
def run_plugin_research(plugin_id: int, agent: str = "codex"):
    """Investigates this plugin via the `codex` CLI (vendor site, KVR Audio,
    etc. — see app/codex_research.py) and stores the result as a *pending
    suggestion*, never applied automatically — the owner reviews/edits it in
    the UI and explicitly applies or discards (same propose-then-apply
    safety model as every other action here). Synchronous: the request just
    waits for codex to finish (confirmed by hand this reliably completes
    well under the timeout)."""
    with db.connect(DB_PATH) as conn:
        row = db.get_plugin(conn, plugin_id)
        if row is None:
            raise HTTPException(404, "not found")
        cfg = _agent_config(conn, agent)
        template = db.get_setting(conn, "codex_prompt", "") or None
    try:
        codex_research.begin_batch()  # a Stop pressed earlier must not block a single run
        result = codex_research.run_research(
            row["name"], row["format"], row["vendor"], row["host_path"], cfg,
            template=template, store_url=row["store_url"],
        )
    except codex_research.ResearchError as exc:
        raise HTTPException(502, str(exc))
    with db.connect(DB_PATH) as conn:
        db.upsert_enrichment_suggestion(conn, plugin_id, result)
        saved = db.get_enrichment_suggestion(conn, plugin_id)
    return _row_to_suggestion_dict(saved)


@app.delete("/api/plugins/{plugin_id}/research")
def discard_plugin_research(plugin_id: int):
    with db.connect(DB_PATH) as conn:
        db.delete_enrichment_suggestion(conn, plugin_id)
    return {"ok": True}


class ResearchApply(BaseModel):
    vendor: str | None = None
    vendor_url: str | None = None
    vendor_status: str | None = None
    vendor_status_note: str | None = None
    plugin_type: str | None = None
    store_url: str | None = None
    secondary_vendors: list[str] | None = None
    categories: list[str] = []
    description_es: str | None = None


@app.post("/api/plugins/{plugin_id}/research/apply")
def apply_plugin_research(plugin_id: int, body: ResearchApply):
    """Applies the (possibly owner-edited, re-submitted from the review form
    — not necessarily identical to what codex originally proposed) fields
    through the exact same manual-edit path a hand-typed correction would
    use (db.py:update_plugin_manual / set_plugin_categories_manual), so
    everything already protected from being overwritten by a future rescan
    (vendor_source/plugin_type_source/name_source='manual') stays protected
    here too — this isn't a separate write path, just a pre-filled one."""
    if body.vendor_status not in (None, "active", "deprecated", "down"):
        raise HTTPException(400, "vendor_status must be one of: active, deprecated, down")
    with db.connect(DB_PATH) as conn:
        if db.get_plugin(conn, plugin_id) is None:
            raise HTTPException(404, "not found")
        fields = {
            k: v for k, v in {
                "vendor": body.vendor, "plugin_type": body.plugin_type,
                "description_es": body.description_es, "store_url": _clean_store_url(body.store_url),
            }.items() if v is not None
        }
        if fields:
            try:
                db.update_plugin_manual(conn, plugin_id, fields)
            except ValueError as e:  # a plugin type that isn't one of the fixed ones
                raise HTTPException(400, str(e)) from None
        if body.categories:
            db.set_plugin_categories_manual(conn, plugin_id, body.categories)
        vendor_name = body.vendor
        if vendor_name and (body.vendor_url or body.vendor_status or body.vendor_status_note):
            # The vendor is one entity shared by all its plugins: research on one
            # plugin updates the vendor row, and every plugin of that vendor follows.
            existing = db.get_vendor_info(conn, vendor_name) or {}
            db.upsert_vendor_reference(
                conn, vendor_name,
                body.vendor_url or existing.get("url"),
                body.vendor_status or existing.get("status"),
                body.vendor_status_note or existing.get("status_note"),
            )
        if body.secondary_vendors is not None:
            try:
                db.set_secondary_vendors(conn, plugin_id, body.secondary_vendors, source="research")
            except ValueError as exc:
                raise HTTPException(400, str(exc))
        db.sync_plugin_vendor_info(conn)
        db.delete_enrichment_suggestion(conn, plugin_id)
    return {"ok": True}


@app.get("/api/ignored-plugins")
def ignored_plugins_list():
    with db.connect(DB_PATH) as conn:
        rows = db.list_ignored_plugin_paths(conn)
    return [_row_to_dict(r) for r in rows]


class IgnoredPluginRestore(BaseModel):
    host_path: str


@app.post("/api/ignored-plugins/unignore")
def ignored_plugins_unignore(body: IgnoredPluginRestore):
    """Un-ignoring doesn't bring the plugin back into the catalog by itself
    — it was fully removed when ignored — the next rescan re-discovers and
    re-catalogs it normally, same as any newly-installed plugin."""
    with db.connect(DB_PATH) as conn:
        db.remove_ignored_plugin_path(conn, body.host_path)
    return {"ok": True}


class LinkBody(BaseModel):
    other_id: int


@app.post("/api/plugins/{plugin_id}/link")
def link_plugin(plugin_id: int, body: LinkBody):
    """Manual "these are the same plugin" link, for twins the automatic name+vendor
    matcher can't pair (see app/dedup.py). Persisted in plugins.link_group, which
    rescans never touch. Only affects grouping — no file is moved or deleted."""
    if plugin_id == body.other_id:
        raise HTTPException(400, "cannot link a plugin to itself")
    with db.connect(DB_PATH) as conn:
        if db.get_plugin(conn, plugin_id) is None or db.get_plugin(conn, body.other_id) is None:
            raise HTTPException(404, "plugin not found")
        gid = db.link_plugins(conn, plugin_id, body.other_id)
    return {"ok": True, "link_group": gid}


@app.delete("/api/plugins/{plugin_id}/link")
def unlink_plugin(plugin_id: int):
    with db.connect(DB_PATH) as conn:
        if db.get_plugin(conn, plugin_id) is None:
            raise HTTPException(404, "plugin not found")
        db.unlink_plugin(conn, plugin_id)
    return {"ok": True}


class PluginEdit(BaseModel):
    name: str | None = None
    vendor: str | None = None
    plugin_type: str | None = None
    description_es: str | None = None
    store_url: str | None = None
    kvr_url: str | None = None
    equipboard_url: str | None = None
    no_store: bool | None = None       # "looked, there is no official store page"
    no_kvr: bool | None = None
    no_equipboard: bool | None = None


@app.patch("/api/plugins/{plugin_id}")
def edit_plugin(plugin_id: int, body: PluginEdit):
    """Manual correction. Marks the touched fields as 'manual' so future
    rescans don't silently overwrite them — see db.py:update_plugin_manual."""
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(400, "no fields to update")
    if "name" in fields and not (fields["name"] or "").strip():
        raise HTTPException(400, "name can't be empty")
    for link in ("store_url", "kvr_url", "equipboard_url"):
        if link in fields:
            fields[link] = _clean_store_url(fields[link])
    with db.connect(DB_PATH) as conn:
        if db.get_plugin(conn, plugin_id) is None:
            raise HTTPException(404, "plugin not found")
        try:
            db.update_plugin_manual(conn, plugin_id, fields)
        except ValueError as e:  # a plugin type that isn't one of the fixed ones
            raise HTTPException(400, str(e)) from None
        # Vendor URL and status belong to the vendor, edited under Reference data.
        # Here we only refresh this plugin's cached copy in case its vendor changed.
        db.sync_plugin_vendor_info(conn)
    return {"ok": True}


def _clean_store_url(value: str | None) -> str | None:
    """A plugin's store link: empty clears it, otherwise it must be an http(s) URL."""
    value = (value or "").strip()
    if not value:
        return None
    if not value.lower().startswith(("http://", "https://")):
        raise HTTPException(400, "The store link must start with http:// or https://")
    return value


class CategoriesEdit(BaseModel):
    slugs: list[str]


@app.put("/api/plugins/{plugin_id}/categories")
def edit_plugin_categories(plugin_id: int, body: CategoriesEdit):
    with db.connect(DB_PATH) as conn:
        if db.get_plugin(conn, plugin_id) is None:
            raise HTTPException(404, "plugin not found")
        db.set_plugin_categories_manual(conn, plugin_id, body.slugs)
    return {"ok": True}


@app.get("/api/categories")
def list_categories():
    with db.connect(DB_PATH) as conn:
        conn.row_factory = None
        rows = conn.execute(
            "SELECT slug, name, string_value, description, source_url, id, source FROM categories ORDER BY name"
        ).fetchall()
    return [{"slug": r[0], "name": r[1], "string_value": r[2], "description": r[3], "source_url": r[4],
             "id": r[5], "source": r[6]} for r in rows]


class CategoryCreate(BaseModel):
    name: str
    description: str
    slug: str | None = None


@app.post("/api/categories")
def create_category(body: CategoryCreate):
    import re as _re
    slug = body.slug or _re.sub(r"[^a-z0-9]+", "-", body.name.lower()).strip("-")
    if not slug:
        raise HTTPException(400, "could not derive a slug from that name")
    with db.connect(DB_PATH) as conn:
        db.add_category_reference(conn, slug, body.name, body.description)
    return {"ok": True, "slug": slug}


@app.delete("/api/categories/{slug}")
def delete_category(slug: str):
    """Deleting a category is never blocked by plugins tagged with it — the
    join row is cascade-deleted (see db.py:delete_category_reference), the
    plugin itself is untouched otherwise. Returns how many plugins lost the
    tag so the UI can say so."""
    with db.connect(DB_PATH) as conn:
        row = conn.execute("SELECT 1 FROM categories WHERE slug=?", (slug,)).fetchone()
        if row is None:
            raise HTTPException(404, "category not found")
        affected = db.count_plugins_by_category(conn, slug)
        db.delete_category_reference(conn, slug)
    return {"ok": True, "affected": affected}


class VendorCreate(BaseModel):
    name: str
    url: str | None = None
    status: str | None = None
    status_note: str | None = None
    kvr_url: str | None = None          # None keeps what is stored, "" clears it
    equipboard_url: str | None = None
    no_url: bool | None = None          # None keeps the mark; True = "looked, this site has no page"
    no_kvr: bool | None = None
    no_equipboard: bool | None = None


@app.get("/api/reference/vendors")
def list_reference_vendors():
    with db.connect(DB_PATH) as conn:
        rows = db.list_vendors_reference(conn)
    return [_row_to_dict(r) for r in rows]


@app.post("/api/reference/vendors")
def create_reference_vendor(body: VendorCreate):
    if body.status not in (None, "active", "deprecated", "down"):
        raise HTTPException(400, "status must be one of: active, deprecated, down")
    with db.connect(DB_PATH) as conn:
        db.upsert_vendor_reference(
            conn, body.name, _clean_store_url(body.url), body.status, body.status_note,
            kvr_url=None if body.kvr_url is None else (_clean_store_url(body.kvr_url) or ""),
            equipboard_url=None if body.equipboard_url is None else (_clean_store_url(body.equipboard_url) or ""),
            no_url=body.no_url, no_kvr=body.no_kvr, no_equipboard=body.no_equipboard,
        )
        db.sync_plugin_vendor_info(conn)
    return {"ok": True}


@app.delete("/api/reference/vendors/{name}")
def delete_reference_vendor(name: str):
    """Deleting a vendor is never blocked by plugins carrying its name — see
    db.py:delete_vendor_reference (`plugins.vendor` is a plain text column,
    not a foreign key). Affected plugins keep their vendor text as-is, just
    orphaned from the reference list; returns the count so the UI can point
    the owner at "Assign to plugin(s)" for a different vendor afterward."""
    with db.connect(DB_PATH) as conn:
        if db.get_vendor_info(conn, name) is None:
            raise HTTPException(404, "vendor not found")
        affected = db.count_plugins_by_vendor(conn, name)
        db.delete_vendor_reference(conn, name)
        db.sync_plugin_vendor_info(conn)
    return {"ok": True, "affected": affected}


class VendorRename(BaseModel):
    new_name: str


@app.post("/api/reference/vendors/{name}/rename")
def rename_reference_vendor(name: str, body: VendorRename):
    """Changes a vendor's name; every plugin from it follows (db.rename_vendor_reference).
    A name another vendor already has is refused: moving plugins between two vendors is
    "Assign", not a rename."""
    new = body.new_name.strip()
    if not new:
        raise HTTPException(400, "the vendor needs a name")
    with db.connect(DB_PATH) as conn:
        if db.get_vendor_info(conn, name) is None:
            raise HTTPException(404, "vendor not found")
        if new != name:
            clash = conn.execute(
                "SELECT name FROM vendors WHERE name=? COLLATE NOCASE AND name != ?", (new, name)
            ).fetchone()
            if clash:
                raise HTTPException(409, f"there is already a vendor called \"{clash[0]}\". "
                                         "Use Assign on one of them to move its plugins instead")
            affected = db.rename_vendor_reference(conn, name, new)
        else:
            affected = 0
        db.sync_plugin_vendor_info(conn)
    return {"ok": True, "affected": affected}


class VendorAssignRequest(BaseModel):
    plugin_ids: list[int]


@app.post("/api/reference/vendors/{name}/assign")
def assign_vendor_to_plugins(name: str, body: VendorAssignRequest):
    """Sets `vendor` (as a manual edit, so future rescans don't overwrite it —
    see db.py:update_plugin_manual) on every listed plugin, and copies this
    vendor's current url/status/status_note onto each one immediately rather
    than waiting for the next rescan's "Vendor website/status" fill step to
    pick it up. The point: research a vendor's site/status once in the
    Reference data tab, then apply it to every plugin from that vendor in one
    action instead of editing each plugin individually."""
    with db.connect(DB_PATH) as conn:
        info = db.get_vendor_info(conn, name)
        if info is None:
            raise HTTPException(404, "vendor not found in reference list — add it first")
        assigned = 0
        for plugin_id in body.plugin_ids:
            if db.get_plugin(conn, plugin_id) is None:
                continue
            db.update_plugin_manual(conn, plugin_id, {"vendor": name})
            assigned += 1
        db.sync_plugin_vendor_info(conn)
    return {"ok": True, "assigned": assigned}


@app.get("/api/reference/types")
def list_reference_types():
    """The plugin types: fixed (effect, synth), so there is no endpoint to add or edit one."""
    with db.connect(DB_PATH) as conn:
        rows = db.list_plugin_types_reference(conn)
    return [_row_to_dict(r) for r in rows]


@app.get("/api/types")
def list_types():
    with db.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT plugin_type, COUNT(*) FROM plugins WHERE plugin_type IS NOT NULL AND plugin_type != '' GROUP BY plugin_type ORDER BY plugin_type"
        ).fetchall()
    return [{"type": r[0], "count": r[1]} for r in rows]


@app.get("/api/vendors")
def list_vendors():
    """Drives the Browse tab's vendor filter dropdown — built from the
    plugins' own `vendor` text, not the `vendors` reference table, so a
    plugin whose vendor was cleared (deleted reference, see
    db.py:delete_vendor_reference) naturally falls out of the named list and
    into the blank bucket below instead of lingering as a stale option."""
    with db.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT vendor, COUNT(*) FROM plugins WHERE vendor IS NOT NULL AND vendor != '' GROUP BY vendor ORDER BY vendor"
        ).fetchall()
        missing = conn.execute("SELECT COUNT(*) FROM plugins WHERE vendor IS NULL OR vendor = ''").fetchone()[0]
    out = [{"vendor": r[0], "count": r[1]} for r in rows]
    if missing:
        out.insert(0, {"vendor": "", "count": missing})
    return out


@app.get("/api/dedup")
def dedup_candidates():
    with db.connect(DB_PATH) as conn:
        rows = db.all_plugins(conn)
        action_map = db.active_actions_by_plugin(conn)
    out = []
    for c in _superseded(rows):
        superseded = _row_to_dict(c["superseded"])
        superseded["action_status"] = _action_status(action_map.get(superseded["id"]))
        out.append({
            "superseded": superseded,
            "keeper": _row_to_dict(c["keeper"]),
            "reason": c["reason"],
        })
    return out


@app.get("/api/unorganized")
def unorganized():
    with db.connect(DB_PATH) as conn:
        rows = db.all_plugins(conn)
        action_map = db.active_actions_by_plugin(conn)
        scan_roots = db.scan_roots_as_objects(conn)
    out = []
    for r in rows:
        if not _is_unorganized(r, scan_roots):
            continue
        d = _row_to_dict(r)
        d["action_status"] = _action_status(action_map.get(r["id"]))
        out.append(d)
    return out


@app.get("/api/missing-type")
def missing_type():
    """Plugins with no known effect/synth type, alongside FL Studio's own
    suggestion (fl_type, from ps_file_type_0 — see app/fl_database.py) when
    it detected one. Applying a suggestion is a plain metadata PATCH (same
    endpoint the plugin-edit form already uses), not a propose/apply file
    action — nothing on disk is touched, so there's no risk requiring that
    two-step dance (see AGENTS.md safety rules, which are specifically about
    file moves/deletes)."""
    with db.connect(DB_PATH) as conn:
        rows = db.all_plugins(conn)
    return [_row_to_dict(r) for r in rows if not r["plugin_type"] or r["plugin_type"] == "unknown"]


@app.get("/api/flagged")
def flagged():
    with db.connect(DB_PATH) as conn:
        rows = db.all_plugins(conn)
        action_map = db.active_actions_by_plugin(conn)
    out = []
    for r in rows:
        if r["status"] == "ok":
            continue
        d = _row_to_dict(r)
        d["action_status"] = _action_status(action_map.get(r["id"]))
        out.append(d)
    return out


@app.get("/api/ambiguous")
def ambiguous():
    with db.connect(DB_PATH) as conn:
        rows = db.all_plugins(conn)
    return [_row_to_dict(r) for r in rows if not r["vendor"] and r["vendor_note"]]


@app.post("/api/rescan")
def rescan(full_scan: bool = False):
    logs = []
    summary = run_scan(log=lambda msg: logs.append(str(msg)), include_full_scan=full_scan)
    summary["log"] = logs
    return summary


@app.get("/api/out-of-scope")
def out_of_scope(include_ignored: bool = False):
    """Plugin-like files found outside the known scan directories (only
    populated after a "full C: scan" rescan — see app/full_scan.py). Never
    enriched, never part of dedup/cleanup; the only actions are "open in
    Explorer to fix it yourself" or "ignore"."""
    with db.connect(DB_PATH) as conn:
        rows = db.list_out_of_scope(conn, include_ignored=include_ignored)
    return [_row_to_dict(r) for r in rows]


@app.post("/api/out-of-scope/{item_id}/ignore")
def out_of_scope_ignore(item_id: int):
    with db.connect(DB_PATH) as conn:
        db.set_out_of_scope_ignored(conn, item_id, True)
    return {"ok": True}


@app.post("/api/out-of-scope/{item_id}/unignore")
def out_of_scope_unignore(item_id: int):
    with db.connect(DB_PATH) as conn:
        db.set_out_of_scope_ignored(conn, item_id, False)
    return {"ok": True}


@app.post("/api/out-of-scope/{item_id}/delete")
def out_of_scope_delete(item_id: int):
    """Permanent, no quarantine step — see actions.permanent_delete_out_of_scope.
    Requested primarily as an ".aax removal tool" but not restricted to AAX."""
    with db.connect(DB_PATH) as conn:
        cur = conn.execute("SELECT host_path FROM out_of_scope_plugins WHERE id=?", (item_id,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(404, "not found")
        try:
            actions.permanent_delete_out_of_scope(row[0])
        except actions.ActionError as exc:
            folder_access.raise_if_denied(exc, row[0])
            raise HTTPException(409, str(exc))
        except OSError as exc:
            folder_access.raise_if_denied(exc, row[0])
            raise HTTPException(409, f"Filesystem error, nothing was changed: {exc}")
        conn.execute("DELETE FROM out_of_scope_plugins WHERE id=?", (item_id,))
    return {"ok": True}


_VALID_SCAN_FORMATS = {"VST3", "VST2", "CLAP", "AAX", "WAVES", "FL-NATIVE"}

# Matches app/config.py's builtin ROOTS per format — used when a new scan root
# is added without explicit extensions (the Configuration tab's "+ Add scan
# path" form never asks for them; only the more detailed per-root edit form
# does). Real bug found and fixed: ScanRootAdd.extensions defaults to [], and
# add_scan_root() stored that empty list as-is, so a root added this way
# matched zero files regardless of what was actually sitting in the folder —
# confirmed real: two owner-added "Common Files\VST2" roots sat enabled for
# days matching 0 plugins each, silently missing two real, non-trivial .dll
# files the whole time (see "FL Studio plugin count mismatch" below).
_DEFAULT_EXTENSIONS_BY_FORMAT = {
    "VST3": (".vst3",),
    "VST2": (".dll",),
    "CLAP": (".clap",),
    "AAX": (".aaxplugin",),
    "WAVES": (".bundle",),
    "FL-NATIVE": (),  # folder-per-plugin, no extension — matches config.py's FL-NATIVE roots
}


class ScanRootAdd(BaseModel):
    host_path: str
    format: str
    default_type: str = "unknown"
    extensions: list[str] = []
    recurse_vendor_dirs: bool = True


class ScanRootUpdate(BaseModel):
    host_path: str | None = None
    format: str | None = None
    default_type: str | None = None
    extensions: list[str] | None = None
    recurse_vendor_dirs: bool | None = None


def _quarantine_dir(conn) -> str:
    return db.get_setting(conn, "quarantine_dir", QUARANTINE_DIR)


def _clean_host_path(host_path: str) -> str:
    host_path = host_path.strip().rstrip("\\")
    if not host_path or host_path[1:3] != ":\\":
        raise HTTPException(400, r"host_path must be an absolute Windows path, e.g. C:\Program Files\Steinberg\VstPlugins")
    return host_path


@app.get("/api/scan-roots")
def scan_roots_list():
    """Every scan root — built-in (app/config.py:ROOTS, seeded into the
    database) and owner-added/edited alike. See AGENTS.md's Configuration
    section for the full source='builtin'/'custom'/'manual' + enabled model."""
    with db.connect(DB_PATH) as conn:
        db.seed_scan_roots(conn)  # standard locations appear on first visit, not only after a scan
        rows = db.list_scan_roots(conn)
    out = []
    for r in rows:
        d = _row_to_dict(r)
        d["exists"] = os.path.isdir(d["host_path"])
        d["fixed"] = d.get("location_key") in FIXED_LOCATION_KEYS
        out.append(d)
    return out


@app.post("/api/scan-roots/{root_id}/reset")
def scan_roots_reset(root_id: int):
    """Puts a standard location back to its detected path (see app/config.py)."""
    with db.connect(DB_PATH) as conn:
        try:
            db.reset_scan_root(conn, root_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "The detected path is already used by another scan path.")
    return {"ok": True}


@app.post("/api/scan-roots")
def scan_roots_add(body: ScanRootAdd):
    if body.format not in _VALID_SCAN_FORMATS:
        raise HTTPException(400, f"format must be one of: {', '.join(sorted(_VALID_SCAN_FORMATS))}")
    host_path = _clean_host_path(body.host_path)
    extensions = tuple(body.extensions) if body.extensions else _DEFAULT_EXTENSIONS_BY_FORMAT[body.format]
    with db.connect(DB_PATH) as conn:
        try:
            root_id = db.add_scan_root(conn, host_path, body.format, body.default_type,
                                        extensions, body.recurse_vendor_dirs)
        except sqlite3.IntegrityError:
            raise HTTPException(409, "That path is already a scan root.")
    return {"ok": True, "id": root_id}


@app.patch("/api/scan-roots/{root_id}")
def scan_roots_update(root_id: int, body: ScanRootUpdate):
    fields = body.model_dump(exclude_unset=True)
    if "format" in fields and fields["format"] not in _VALID_SCAN_FORMATS:
        raise HTTPException(400, f"format must be one of: {', '.join(sorted(_VALID_SCAN_FORMATS))}")
    if "host_path" in fields:
        fields["host_path"] = _clean_host_path(fields["host_path"])
    with db.connect(DB_PATH) as conn:
        try:
            db.update_scan_root(conn, root_id, **fields)
        except sqlite3.IntegrityError:
            raise HTTPException(409, "That path is already a scan root.")
    return {"ok": True}


@app.post("/api/scan-roots/{root_id}/enable")
def scan_roots_enable(root_id: int):
    with db.connect(DB_PATH) as conn:
        db.set_scan_root_enabled(conn, root_id, True)
    return {"ok": True}


@app.post("/api/scan-roots/{root_id}/disable")
def scan_roots_disable(root_id: int):
    """Temporarily excludes this root from scanning without losing its row
    (settings, source) — reversible via /enable. See DELETE below for
    removing one outright instead."""
    with db.connect(DB_PATH) as conn:
        db.set_scan_root_enabled(conn, root_id, False)
    return {"ok": True}


@app.delete("/api/scan-roots/{root_id}")
def scan_roots_delete(root_id: int):
    """Deletes any scan root — built-in included. A built-in (or edited
    built-in) row is also tombstoned (removed_scan_root_paths) so it can't
    silently reappear on the next rescan's reseed; see
    /api/removed-scan-roots to undo that. A 'custom' row just needs nothing
    else — no reseed ever re-adds it."""
    with db.connect(DB_PATH) as conn:
        row = conn.execute("SELECT id, location_key FROM scan_roots WHERE id=?", (root_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "not found")
        if row[1] in FIXED_LOCATION_KEYS:
            raise HTTPException(409, "This format folder is required and can't be removed. You can change its path instead.")
        db.delete_scan_root(conn, root_id)
    return {"ok": True}


@app.get("/api/removed-scan-roots")
def removed_scan_roots_list():
    """Built-in scan paths the owner has deleted outright (see DELETE
    /api/scan-roots/{id}) — kept here so a delete is undoable."""
    with db.connect(DB_PATH) as conn:
        rows = db.list_removed_scan_root_paths(conn)
    return [_row_to_dict(r) for r in rows]


class RestoreScanRoot(BaseModel):
    host_path: str


@app.post("/api/removed-scan-roots/restore")
def removed_scan_roots_restore(body: RestoreScanRoot):
    with db.connect(DB_PATH) as conn:
        db.restore_scan_root_path(conn, body.host_path)
        db.seed_scan_roots(conn)  # bring it back immediately rather than waiting for the next rescan
    return {"ok": True}


@app.get("/api/ignored-folders")
def ignored_folders_list():
    """Folder-level ignore rules — a whole subtree excluded from the full C:
    scan, either added directly (below) or via the Out of scope tab's
    "Consolidate" feature. As opposed to one-off per-file ignores
    (out_of_scope_plugins.ignored)."""
    with db.connect(DB_PATH) as conn:
        rows = db.list_ignored_scan_folders(conn)
    return [_row_to_dict(r) for r in rows]


class IgnoredFolderAdd(BaseModel):
    host_path: str


@app.post("/api/ignored-folders")
def ignored_folders_add(body: IgnoredFolderAdd):
    host_path = _clean_host_path(body.host_path)
    with db.connect(DB_PATH) as conn:
        db.add_ignored_scan_folder(conn, host_path)
    return {"ok": True}


@app.delete("/api/ignored-folders/{folder_id}")
def ignored_folders_remove(folder_id: int):
    with db.connect(DB_PATH) as conn:
        db.remove_ignored_scan_folder(conn, folder_id)
    return {"ok": True}


@app.get("/api/out-of-scope/consolidate-candidates")
def out_of_scope_consolidate_candidates():
    with db.connect(DB_PATH) as conn:
        return db.consolidate_candidates(conn)


class ConsolidateRequest(BaseModel):
    parent: str


@app.post("/api/out-of-scope/consolidate")
def out_of_scope_consolidate(body: ConsolidateRequest):
    with db.connect(DB_PATH) as conn:
        count = db.consolidate_ignored_folder(conn, body.parent)
    return {"ok": True, "consolidated": count}


class PromptBody(BaseModel):
    value: str


def _agent_config(conn, agent: str):
    """The owner's model/effort choice for this agent. 400 if the agent doesn't exist."""
    try:
        return ai_agents.config_for(conn, agent)
    except ai_agents.ResearchError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/ai")
def ai_status():
    """The research prompt shown in the AI tab. (The agents have their own endpoint below.)"""
    with db.connect(DB_PATH) as conn:
        saved_prompt = db.get_setting(conn, "codex_prompt", "") or ""
    return {
        "npm_available": ai_agents.npm_available(),
        "prompt": saved_prompt or codex_research.DEFAULT_PROMPT,
        "default_prompt": codex_research.DEFAULT_PROMPT,
        "customized": bool(saved_prompt),
        "required_placeholders": list(codex_research.REQUIRED_PLACEHOLDERS),
    }


def _describe_agent(agent_id: str, conn) -> dict:
    agent = ai_agents.get(agent_id)
    status = ai_agents.status_for(agent_id)
    usable = status["installed"] and agent.supported
    catalog = ai_agents.models_for(agent_id) if usable else {"models": [], "source": "", "note": ""}
    cfg = ai_agents.config_for(conn, agent_id)
    ids = [m["id"] for m in catalog["models"]]
    # A saved choice the agent no longer lists (a retired model) falls back to the agent's default.
    model = cfg.model if cfg.model in ids else (agent.default_model if agent.default_model in ids else (ids[0] if ids else cfg.model))
    entry = next((m for m in catalog["models"] if m["id"] == model), None)
    effort = cfg.effort if entry and cfg.effort in entry["efforts"] else (entry["default_effort"] if entry else cfg.effort)
    billed = status.get("billing_note")
    return {
        "id": agent_id, "label": agent.label, "binary": agent.binary, "blurb": agent.blurb,
        **status,
        # Ready = can investigate now: installed, signed in, usable by VEXT, and (if it bills per
        # use) allowed by the owner.
        "ready": bool(usable and status["signed_in"] and (not billed or cfg.allow_billed)),
        "supported": agent.supported, "unsupported_reason": agent.unsupported_reason,
        "issue_url": getattr(agent, "issue_url", ""),
        "recommended": agent.recommended,
        "install_url": agent.install_url, "install_hint": agent.install_hint,
        "npm_installable": bool(agent.npm_package),
        "allow_billed": cfg.allow_billed,
        "model": model, "effort": effort,
        "models": catalog["models"], "models_source": catalog["source"], "models_note": catalog["note"],
        "signin_command": agent.signin_command(),
    }


@app.get("/api/ai/agents")
def ai_agents_list(refresh: bool = False):
    """Every AI agent with its install/sign-in state, model list (from the vendor's own catalog),
    and the model and effort chosen for it. `ready` agents can be used for an investigation."""
    if refresh:
        ai_agents.forget()
    ids = list(ai_agents.AGENTS)
    # Each agent's checks run its own CLI, so do them side by side. One connection per thread.
    def describe(agent_id):
        with db.connect(DB_PATH) as conn:
            return _describe_agent(agent_id, conn)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=len(ids)) as pool:
        agents = list(pool.map(describe, ids))
    return {"agents": agents, "npm_available": ai_agents.npm_available()}


class AgentChoice(BaseModel):
    model: str
    effort: str | None = None


@app.put("/api/ai/agents/{agent_id}/settings")
def ai_agent_settings(agent_id: str, body: AgentChoice):
    """Saves the model and effort for one agent. Only values the agent itself lists are accepted."""
    if agent_id not in ai_agents.AGENTS:
        raise HTTPException(404, "unknown agent")
    agent = ai_agents.get(agent_id)
    try:
        ai_agents.validate_choice(agent_id, body.model, body.effort)
    except ai_agents.ResearchError as exc:
        raise HTTPException(400, str(exc))
    with db.connect(DB_PATH) as conn:
        db.set_setting(conn, agent.model_key, body.model)
        db.set_setting(conn, agent.effort_key, body.effort or "")
    return {"ok": True}


class AgentConsent(BaseModel):
    allow: bool


@app.put("/api/ai/agents/{agent_id}/consent")
def ai_agent_consent(agent_id: str, body: AgentConsent):
    """The owner allows (or stops allowing) an agent whose sign-in bills per use (Gemini CLI on an
    API key). Without it, VEXT never runs that agent."""
    if agent_id not in ai_agents.AGENTS:
        raise HTTPException(404, "unknown agent")
    agent = ai_agents.get(agent_id)
    with db.connect(DB_PATH) as conn:
        db.set_setting(conn, agent.consent_key, "1" if body.allow else "0")
    ai_agents.forget(agent_id)
    return {"ok": True, "allow": body.allow}


@app.post("/api/ai/agents/{agent_id}/install")
def ai_agent_install(agent_id: str):
    """Installs the agent's CLI with npm. Only runs from an explicit click in the AI tab."""
    if agent_id not in ai_agents.AGENTS:
        raise HTTPException(404, "unknown agent")
    agent = ai_agents.get(agent_id)
    try:
        output = agent.install()
    except ai_agents.ResearchError as exc:
        raise HTTPException(502, str(exc))
    ai_agents.forget(agent_id)
    return {"ok": True, "output": output, "version": agent.version()}


@app.post("/api/ai/agents/{agent_id}/signin")
def ai_agent_signin(agent_id: str):
    """Opens a terminal running the agent's sign-in. It's a browser flow, so it happens in that
    window, not inside VEXT."""
    if agent_id not in ai_agents.AGENTS:
        raise HTTPException(404, "unknown agent")
    agent = ai_agents.get(agent_id)
    if not agent.installed():
        raise HTTPException(409, f"Install {agent.label} first.")
    # The one console window VEXT opens on purpose: the tool's own sign-in, which the owner drives.
    subprocess.Popen(f'start "VEXT: {agent.label} sign-in" cmd /k {agent.signin_command()}', shell=True)
    ai_agents.forget(agent_id)
    return {"ok": True}


@app.put("/api/ai/prompt")
def ai_prompt_save(body: PromptBody):
    try:
        codex_research.validate_prompt(body.value)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    with db.connect(DB_PATH) as conn:
        db.set_setting(conn, "codex_prompt", body.value.strip())
    return {"ok": True}


@app.post("/api/ai/prompt/reset")
def ai_prompt_reset():
    with db.connect(DB_PATH) as conn:
        db.set_setting(conn, "codex_prompt", "")
    return {"ok": True}


# ---------- vendor websites: apply every waiting result ----------

class ApplyAllRequest(BaseModel):
    target: str


@app.post("/api/investigations/apply-all")
def investigations_apply_all(body: ApplyAllRequest):
    """Applies every researched vendor website/status/link waiting for review. (The plugin tabs
    have their own apply under /api/review.)"""
    if body.target != "vendors":
        raise HTTPException(400, "Only vendors are applied from here. Use the review tabs for plugins.")
    applied = 0
    with db.connect(DB_PATH) as conn:
        for row in db.list_vendor_suggestions(conn):
            db.apply_vendor_suggestion(conn, row["vendor"])
            applied += 1
        db.sync_plugin_vendor_info(conn)
    return {"ok": True, "applied": applied}


@app.delete("/api/vendor-suggestions/{vendor}")
def vendor_suggestion_discard(vendor: str):
    with db.connect(DB_PATH) as conn:
        db.delete_vendor_suggestion(conn, vendor)
    return {"ok": True}


@app.post("/api/vendor-suggestions/{vendor}/apply")
def vendor_suggestion_apply(vendor: str):
    with db.connect(DB_PATH) as conn:
        try:
            db.apply_vendor_suggestion(conn, vendor)
        except ValueError as exc:
            raise HTTPException(404, str(exc))
    return {"ok": True}


# ---------- multivendor: a plugin's other vendors (main vendor stays the organize key) ----------

class SecondaryVendors(BaseModel):
    vendors: list[str]


@app.get("/api/plugins/{plugin_id}/vendors")
def plugin_vendors(plugin_id: int):
    with db.connect(DB_PATH) as conn:
        row = db.get_plugin(conn, plugin_id)
        if row is None:
            raise HTTPException(404, "not found")
        return {
            "main": row["vendor"],
            "others": db.list_secondary_vendors(conn, plugin_id),
            "max": db.MAX_SECONDARY_VENDORS,
        }


@app.put("/api/plugins/{plugin_id}/vendors")
def plugin_vendors_save(plugin_id: int, body: SecondaryVendors):
    with db.connect(DB_PATH) as conn:
        if db.get_plugin(conn, plugin_id) is None:
            raise HTTPException(404, "not found")
        try:
            stored = db.set_secondary_vendors(conn, plugin_id, body.vendors, source="manual")
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    return {"ok": True, "others": stored}


class BatchStart(BaseModel):
    target: str = "metadata"
    limit: int | None = None
    ids: list[int | str] | None = None  # only these plugin ids (vendor names for "vendors")
    # Which AI agent runs it (see /api/ai/agents). For "image" it may be "" (no AI): VEXT then only
    # reads the plugin's store and KVR pages for pictures.
    agent: str = "codex"
    # "image" only: None = ask the AI when the pages VEXT read gave no usable picture; True = ask
    # it anyway (a row's Find online again); False = never.
    ask_ai: bool | None = None


@app.post("/api/ai/batch/start")
def ai_batch_start(body: BatchStart | None = None):
    """Runs one investigation (metadata, store links, categories, types, vendors or pictures) over
    every item it's missing, several AI calls at a time. Results wait for review."""
    body = body or BatchStart()
    agent_id = body.agent or ""
    target = body.target
    if not agent_id and target != "image":
        raise HTTPException(400, "Pick an AI agent for this investigation.")
    if agent_id:
        if agent_id not in ai_agents.AGENTS:
            raise HTTPException(400, "unknown agent")
        agent = ai_agents.get(agent_id)
        if not agent.supported:
            raise HTTPException(409, f"{agent.label} can't be used by VEXT yet: {agent.unsupported_reason}")
        if not agent.installed():
            raise HTTPException(409, f"Install {agent.label} first (Configuration > AI).")
    with db.connect(DB_PATH) as conn:
        cfg = _agent_config(conn, agent_id) if agent_id else None
        template = db.get_setting(conn, "codex_prompt", "") or None
        concurrency = db.get_setting(conn, "ai_concurrency", str(ai_batch.DEFAULT_CONCURRENCY))
    try:
        queued = ai_batch.start(DB_PATH, target, cfg, template, concurrency,
                                limit=body.limit, ids=body.ids, ask_ai=body.ask_ai)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(409, str(exc))
    return {"ok": True, "queued": queued, "target": target, "agent": agent_id or None}


@app.post("/api/ai/batch/stop")
def ai_batch_stop():
    ai_batch.stop()
    return {"ok": True}


@app.get("/api/ai/batch")
def ai_batch_status():
    return ai_batch.status()


_SETTINGS_DEFAULTS = {
    "fl_plugin_db_root": FL_PLUGIN_DB_ROOT,
    "quarantine_dir": QUARANTINE_DIR,
    # Empty = auto-detect (registry, then the standard folder). Set = the user's own install folder.
    "fl_studio_root": "",
    # 1 = after each scan, propose quarantine for copies a better format/bitness supersedes.
    # Proposals only: nothing moves until confirmed in the Actions tab.
    "auto_propose_superseded": "1",
    # How many AI research calls run at once during a batch (1-8).
    "ai_concurrency": "5",
    # Animations, the 3D Scan button and chart motion (js/fx.js). auto = on unless the OS
    # asks for reduced motion (3D only with hardware WebGL).
    "visual_effects": "auto",
    # Plugin preview playback (the Playground), Configuration > Audio (js/audio-settings.js,
    # app/audio_setup.py). Driver type: host_worker.DRIVERS. Device: an `id` from
    # /api/audio/config ("name, host API"); empty = the driver type's default device.
    "audio_driver": "wasapi",
    "audio_output_device": "",
    "audio_sample_rate": "48000",
    "audio_block_size": "512",
    # 1 = Safe Mode: two more buffers queued ahead of the device (host_worker.SAFE_EXTRA_BLOCKS).
    "audio_safe_mode": "0",
    # How far ahead of the sound a plugin runs (host_worker.PLUGIN_BUFFERS, ms): "auto" starts at
    # 60 ms and grows only when a plugin needs it. Heavy spectral plugins are gapless with it.
    "audio_plugin_buffer": "auto",
    # Configuration > Audio, Automatic. 1 = on. Fade and Normalize: the Playground's new files and
    # browser recordings get them once decoded (js/playground.js applyAutoEdits; fades were always
    # on for recordings before, hence on by default). Balance: Auto Level Compensation, every plugin
    # opens with Auto-Balance on (plugin_host._after_load).
    "audio_auto_fade": "1",
    "audio_auto_normalize": "0",
    "audio_auto_balance": "0",
    # MIDI keyboards for the open plugin: "*" = every connected input, "" = none,
    # else a JSON list of input ids (app/hosting/midi.py list_inputs). Channel: 0 = all, 1-16.
    # Off by default (owner, 2026-10-15): an open input is held, so a DAW couldn't use it.
    "midi_inputs": "",
    "midi_channel": "0",
    # 1 = Bypass pitch bend: the keyboards' pitch-bend messages (status 0xEn) never reach the plugin.
    "midi_block_pitchbend": "0",
    # Plugin memory limit (GB): the most memory one plugin's worker may commit (a Windows job
    # object, plugin_host._Job). Protects the PC from runaway plugins; big sample libraries
    # (Kontakt, Vienna) may need more. Applies to the next plugin opened.
    "plugin_memory_limit_gb": str(plugin_host.DEFAULT_MEMORY_LIMIT_GB),
}

# Settings that only take one of a few values.
_SETTING_CHOICES = {
    "visual_effects": ("auto", "on", "off"),
    "audio_driver": tuple(host_worker.DRIVERS),
    "audio_sample_rate": tuple(str(r) for r in host_worker.SAMPLE_RATES),
    "audio_block_size": tuple(str(b) for b in host_worker.BLOCK_SIZES),
    "audio_safe_mode": ("0", "1"),
    "audio_plugin_buffer": tuple(host_worker.PLUGIN_BUFFERS),
    "midi_channel": tuple(str(c) for c in range(17)),
    "midi_block_pitchbend": ("0", "1"),
    "audio_auto_fade": ("0", "1"),
    "audio_auto_normalize": ("0", "1"),
    "audio_auto_balance": ("0", "1"),
    "plugin_memory_limit_gb": tuple(str(g) for g in plugin_host.MEMORY_LIMITS_GB),
}
# Settings where empty is a real choice (auto-detect / system default / off).
_SETTINGS_EMPTY_OK = {"fl_studio_root", "audio_output_device", "midi_inputs"}
# Changing these restarts what the open plugin plays / its MIDI inputs at once.
_AUDIO_KEYS = {"audio_driver", "audio_output_device", "audio_sample_rate", "audio_block_size", "audio_safe_mode",
               "audio_plugin_buffer"}
_MIDI_KEYS = {"midi_inputs", "midi_channel", "midi_block_pitchbend"}


def _fl_studio_info(conn) -> dict:
    from app.config import detect_fl_studio_root

    detected, how = detect_fl_studio_root()
    # "Installed" = the folder FL Studio will actually use has its Plugins folder.
    effective = db.get_setting(conn, "fl_studio_root", "") or detected
    return {
        "fl_studio_detected": detected,
        "fl_studio_detected_from": how,
        "fl_studio_installed": os.path.isdir(os.path.join(effective, "Plugins")),
    }


@app.get("/api/settings")
def settings_list():
    with db.connect(DB_PATH) as conn:
        values = {key: db.get_setting(conn, key, default) for key, default in _SETTINGS_DEFAULTS.items()}
        values.update(_fl_studio_info(conn))
    return values


class SettingUpdate(BaseModel):
    value: str


@app.put("/api/settings/{key}")
def settings_update(key: str, body: SettingUpdate):
    if key not in _SETTINGS_DEFAULTS:
        raise HTTPException(404, "unknown setting")
    value = body.value.strip()
    if not value and key not in _SETTINGS_EMPTY_OK:
        raise HTTPException(400, "value can't be empty")
    if key in _SETTING_CHOICES and value not in _SETTING_CHOICES[key]:
        choices = ", ".join(c or "(empty)" for c in _SETTING_CHOICES[key])
        raise HTTPException(400, f"{key} must be one of: {choices}")
    if key == "audio_output_device" and value:
        try:
            known = audio_setup.device_known(value)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(409, f"Couldn't list the audio devices: {e}")
        if not known:
            raise HTTPException(400, "That output device isn't connected. Pick one from the list.")
    if key == "midi_inputs" and value not in ("", "*"):
        try:
            ids = json.loads(value)
        except ValueError:
            ids = None
        if not isinstance(ids, list) or not all(isinstance(i, str) and 0 < len(i) <= 200 for i in ids) or len(ids) > 64:
            raise HTTPException(400, 'midi_inputs must be "*", "" or a JSON list of MIDI input names')
        value = json.dumps(ids)
    if key == "fl_studio_root" and value and not os.path.isdir(value):
        raise HTTPException(400, "That folder doesn't exist. Point it at the FL Studio install folder (the one containing Plugins\\).")
    with db.connect(DB_PATH) as conn:
        if key == "quarantine_dir" and db.list_quarantined(conn):
            raise HTTPException(409, "Can't change the quarantine directory while quarantine isn't empty — "
                                      "restore or permanently delete everything in the Quarantine tab first.")
        previous = db.get_setting(conn, key, _SETTINGS_DEFAULTS[key])
        db.set_setting(conn, key, value)
    if key == "plugin_memory_limit_gb" and int(value) > int(previous if str(previous).isdigit() else 0):
        plugin_host.forget_memory_failures()  # what needed more may open now
    # Applied to the open plugin right away (in the background: restarting its sound takes a moment).
    if key in _AUDIO_KEYS:
        threading.Thread(target=plugin_host.apply_audio_settings, daemon=True).start()
    elif key in _MIDI_KEYS:
        threading.Thread(target=plugin_host.apply_midi_settings, daemon=True).start()
        midi_watch.poke()
    return {"ok": True}


class ActionCreate(BaseModel):
    plugin_id: int
    action: str  # 'quarantine' | 'reorganize'
    reason: str


@app.post("/api/actions")
def propose_action(body: ActionCreate):
    if body.action not in ("quarantine", "reorganize"):
        raise HTTPException(400, "action must be 'quarantine' or 'reorganize'")
    with db.connect(DB_PATH) as conn:
        target = db.get_plugin(conn, body.plugin_id)
        if target is None:
            raise HTTPException(404, "plugin not found")
        if target["fl_cloud"]:
            raise HTTPException(409, "This is an FL Cloud plugin — it must stay at its original install path "
                                     "for FL Cloud authorization, so it can't be moved or quarantined. "
                                     "Unmark it in the FL Cloud tab first if you really want that.")
        existing = db.active_action_for_plugin(conn, body.plugin_id)
        if existing is not None:
            state = "already applied (check the Quarantine tab)" if existing["applied"] else "already pending"
            raise HTTPException(409, f"This plugin has a '{existing['action']}' action {state} — resolve that first.")
        action_id = db.create_action(conn, body.plugin_id, body.action, body.reason)
    return {"id": action_id, "applied": False}


@app.get("/api/actions")
def list_actions(applied: bool | None = None):
    with db.connect(DB_PATH) as conn:
        rows = db.list_actions(conn, applied=applied)
    out = []
    for r in rows:
        d = _row_to_dict(r)
        if not d["applied"]:
            # Reconcile against reality now, not just at the next rescan — if the
            # owner deleted/moved the file outside the app, "Apply" would just
            # fail forever with a confusing error. Surface it instead.
            src = actions.host_to_container(d["host_path"])
            d["source_missing"] = src is None or not src.exists()
        else:
            d["source_missing"] = False
        out.append(d)
    return out


@app.delete("/api/actions/{action_id}")
def reject_action(action_id: int):
    with db.connect(DB_PATH) as conn:
        db.delete_action(conn, action_id)
    return {"ok": True}


@app.post("/api/actions/{action_id}/apply")
def apply_action(action_id: int):
    with db.connect(DB_PATH) as conn:
        row = db.get_action(conn, action_id)
        if row is None:
            raise HTTPException(404, "action not found")
        if row["applied"]:
            raise HTTPException(400, "already applied")
        if row["host_path"].lower() in db.fl_cloud_host_paths(conn):
            raise HTTPException(409, "This plugin is marked FL Cloud and must stay at its install path — reject this action.")

        try:
            if row["action"] == "quarantine":
                new_path = actions.apply_quarantine(row["host_path"], _quarantine_dir(conn))
                db.mark_action_applied(conn, action_id, new_host_path=None)
                # Row stays — this is now live state (shows up in the Quarantine
                # tab), not history.
            elif row["action"] == "reorganize":
                new_path = actions.apply_reorganize(row["host_path"], row["vendor"])
                db.mark_action_applied(conn, action_id, new_host_path=new_path)
                # One-shot and done — nothing left to track, so drop the row
                # instead of keeping it as a "resolved" history entry.
                applog.log_event(f"Reorganized '{row['plugin_name']}': {row['host_path']} -> {new_path}")
                db.drop_action(conn, action_id)
            else:
                raise HTTPException(400, f"unknown action type {row['action']}")
        except actions.ActionError as exc:
            folder_access.raise_if_denied(exc, row["host_path"])
            raise HTTPException(409, str(exc))
        except OSError as exc:
            # Any filesystem error we didn't anticipate — surface it cleanly
            # instead of a bare 500, and never leave the action marked applied.
            folder_access.raise_if_denied(exc, row["host_path"])
            raise HTTPException(409, f"Filesystem error, nothing was changed: {exc}")

    return {"ok": True, "new_path": new_path}


# ---------- Quarantine tab: list / restore / permanently delete ----------

@app.get("/api/quarantine")
def quarantine_list():
    with db.connect(DB_PATH) as conn:
        rows = db.list_quarantined(conn)
    return [_row_to_dict(r) for r in rows]


@app.post("/api/quarantine/{action_id}/restore")
def quarantine_restore(action_id: int):
    with db.connect(DB_PATH) as conn:
        row = db.get_action(conn, action_id)
        if row is None or row["action"] != "quarantine" or not row["applied"] or row["resolution"] is not None:
            raise HTTPException(404, "not an active quarantine entry")
        try:
            actions.restore_from_quarantine(row["host_path"], _quarantine_dir(conn))
        except actions.ActionError as exc:
            folder_access.raise_if_denied(exc, row["host_path"])
            raise HTTPException(409, str(exc))
        except OSError as exc:
            folder_access.raise_if_denied(exc, row["host_path"])
            raise HTTPException(409, f"Filesystem error, nothing was changed: {exc}")
        applog.log_event(f"Restored '{row['plugin_name']}' from quarantine to {row['host_path']}.")
        db.drop_action(conn, action_id)
    return {"ok": True}


@app.post("/api/quarantine/{action_id}/delete-forever")
def quarantine_delete_forever(action_id: int):
    with db.connect(DB_PATH) as conn:
        row = db.get_action(conn, action_id)
        if row is None or row["action"] != "quarantine" or not row["applied"] or row["resolution"] is not None:
            raise HTTPException(404, "not an active quarantine entry")
        if not actions.quarantine_target(row["host_path"], _quarantine_dir(conn)).exists():
            # Already gone — the owner deleted it directly, outside the app.
            # Nothing to delete; just drop the now-meaningless registry entry
            # instead of surfacing an error for something already accomplished.
            applog.log_event(
                f"Delete-forever requested for '{row['plugin_name']}' (host path: {row['host_path']}) "
                f"but it was already missing from quarantine (deleted outside the app) — removed its "
                f"registry entry instead of erroring."
            )
            conn.execute("DELETE FROM plugins WHERE id=?", (row["plugin_id"],))
            return {"ok": True, "already_gone": True}
        try:
            actions.permanent_delete_from_quarantine(row["host_path"], _quarantine_dir(conn))
        except actions.ActionError as exc:
            raise HTTPException(409, str(exc))
        except OSError as exc:
            raise HTTPException(409, f"Filesystem error, nothing was changed: {exc}")
        applog.log_event(f"Permanently deleted '{row['plugin_name']}' (host path: {row['host_path']}) from quarantine.")
        conn.execute("DELETE FROM plugins WHERE id=?", (row["plugin_id"],))
    registry_hits = []
    try:
        registry_hits = registry_scan.find_uninstall_entries(row["vendor"], row["plugin_name"])
    except OSError:
        pass  # Best-effort hint only — the delete itself already succeeded.
    return {"ok": True, "registry_hits": registry_hits}


# ---------- Registry cleanup: stale "installed program" traces ----------
# See app/registry_scan.py for the full rationale (owner's reported case:
# deleted a VST3 through this app, but the next installer run still detected
# VST2/AAX/VST3 as "already installed"). Offered two ways from the UI: as a
# hint right after a Quarantine "Delete forever" (registry_hits above), and
# as a standalone search in Configuration for a plugin that was already
# deleted before this feature existed.

@app.get("/api/registry-scan")
def registry_scan_search(vendor: str = "", name: str = ""):
    vendor = vendor.strip()
    name = name.strip()
    if not vendor and not name:
        raise HTTPException(400, "provide at least a vendor or a name to search for")
    return registry_scan.find_uninstall_entries(vendor or None, name or None)


class RegistryDeleteRequest(BaseModel):
    hive: str
    key_path: str


@app.post("/api/registry-scan/delete")
def registry_scan_delete(body: RegistryDeleteRequest):
    try:
        registry_scan.delete_uninstall_entry(body.hive, body.key_path)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except PermissionError as exc:
        raise HTTPException(409, str(exc))
    except OSError as exc:
        raise HTTPException(409, f"Registry error, nothing was changed: {exc}")
    applog.log_event(f"Deleted registry uninstall entry {body.hive}\\{body.key_path}.")
    return {"ok": True}


# ---------- FL Studio browse-tree reorganize ----------
# See app/fl_reorganize.py for the full rationale: reorganizes FL Studio's
# own "Plugin database\Effects\"/"Generators\" browse tree directly via file
# operations (not FL's own Plugin Manager UI, which turned out to not
# actually clear its "New\" staging folders — see AGENTS.md).

def _entry_to_dict(e: dict) -> dict:
    return {
        "key": e["key"],
        "ps_name": e["ps_name"],
        "plugin_id": e["plugin_id"],
        "matched": e["matched"],
        "reason": e["reason"],
    }


_VALID_FL_SYNC_MODES = {"vendor", "category"}
_VALID_FL_SYNC_SCOPES = {"new", "full"}


@app.get("/api/about")
def about():
    """Name, version, company and creator for the sidebar and the About window."""
    from app import config

    return {"name": config.APP_NAME, "version": config.app_version(), "company": config.APP_COMPANY,
            "creator": config.APP_CREATOR}


@app.get("/api/logs")
def get_logs(after: int = 0):
    """Recent backend activity for the Logs tab (app/logbuf.py): every Python
    `logging` call (source "backend", which includes uvicorn's own request/
    startup logs) plus every host worker's stderr (source "worker:<file>" —
    previously discarded, see plugin_host._spawn_worker). `after`: only lines
    with a higher seq than this (0 the first call) — poll with the previous
    response's `latest` to tail it live without re-sending what's already shown."""
    lines = logbuf.recent(after)
    return {"lines": lines, "latest": lines[-1]["seq"] if lines else after,
            "last_error": logbuf.status()["last_error"]}


@app.get("/api/logs/status")
def get_logs_status():
    """{latest, last_error}: cheap enough for the sidebar to poll for its error badge."""
    return logbuf.status()


@app.delete("/api/logs")
def clear_logs():
    logbuf.clear()
    return {"ok": True}


@app.get("/api/logs/text")
def get_logs_text():
    """The whole buffer as plain text (the browser fallback for Export)."""
    return Response(logbuf.as_text(), media_type="text/plain; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="vext-log.txt"'})


class LogExportBody(BaseModel):
    path: str


@app.post("/api/logs/export")
def export_logs(body: LogExportBody):
    """Writes the buffer to `path`, a .txt the user picked in the save dialog."""
    path = Path(body.path)
    if path.suffix.lower() != ".txt":
        raise HTTPException(400, "the log can only be exported as a .txt file")
    try:
        path.write_text(logbuf.as_text(), encoding="utf-8")
    except OSError as e:
        raise HTTPException(400, f"couldn't write {path}: {e}") from e
    return {"ok": True, "path": str(path)}


@app.post("/api/fl-sync/open-database")
def fl_sync_open_database():
    """Opens FL Studio's "Plugin database" folder in Windows Explorer (the FL
    Studio Sync button): the browse tree's root, parent of the `Installed`
    folder the fl_plugin_db_root setting names (fl_reorganize._plugin_db_root)."""
    with db.connect(DB_PATH) as conn:
        path = str(fl_reorganize._plugin_db_root(conn))
    if not os.path.isdir(path):
        raise HTTPException(404, f"FL Studio's plugin database folder isn't there: {path} "
                                 "(set it in Configuration, DAWs)")
    os.startfile(path)  # Explorer opens the folder; no console involved
    return {"ok": True, "path": path}


@app.get("/api/fl-sync/options")
def fl_sync_options():
    """Vendor/category names for the root-bound combobox — see
    app/fl_reorganize.py:list_options()."""
    with db.connect(DB_PATH) as conn:
        return fl_reorganize.list_options(conn)


@app.get("/api/fl-sync/preview")
def fl_sync_preview(mode: str = "vendor", scope: str = "new"):
    if mode not in _VALID_FL_SYNC_MODES:
        raise HTTPException(400, "mode must be 'vendor' or 'category'")
    if scope not in _VALID_FL_SYNC_SCOPES:
        raise HTTPException(400, "scope must be 'new' or 'full'")
    with db.connect(DB_PATH) as conn:
        plan = fl_reorganize.propose(conn, mode, scope=scope)
    out = {}
    for section, p in plan.items():
        entries = p["entries"]
        root_bound = [e for e in entries if not e["destinations"]]
        out[section] = {
            "total": len(entries),
            "new_count": sum(1 for e in entries if e["from_new"]) + len(p["consume_only"]),
            "filed_count": len(entries) - len(root_bound),
            "root_count": len(root_bound),
            "renamed_count": sum(1 for e in entries if e["matched"] and e["renamed_from"]),
            "picture_count": sum(1 for e in entries if e["picture"]),
            "root_bound": [_entry_to_dict(e) for e in root_bound],
        }
    return out


class FlSyncApply(BaseModel):
    mode: str
    overrides: dict[str, dict[str, str]] = {}  # {"Effects": {"<key>": "<vendor-or-category name>"}, ...}
    sections: list[str] = ["Effects", "Generators"]
    dangerous_wipe: bool = False
    save_backup: bool = True
    scope: str = "new"                # "new": only Installed/<section>/New; "full": every plugin FL detected
    pictures: bool = True             # VEXT's pictures as FL thumbnails (hidden .png + .nfo)
    replace_thumbnails: bool = False  # also replace thumbnails VEXT didn't make (backed up first)
    thumb_size: int = 256             # the one global thumbnail height: 256 (Large), 128 (Medium), 64 (Compact)


class FlSyncFixPictures(BaseModel):
    fst: list[str] = []   # paths of browse-tree .fst files (Effects / Generators)
    size: int = 256       # 256 / 128 / 64


class FlSyncLink(BaseModel):
    name: str             # the .fst's stem: FL's name for the plugin
    plugin_id: int | None = None   # None removes the link
    ignored_host_path: str | None = None   # instead of plugin_id: a plugin ignored in VEXT, brought back first
    fix_picture: bool = False      # also fix the pictures of every .fst with this name
    size: int = 256


def _check_thumb_size(size: int) -> None:
    if size not in fl_reorganize.THUMB_SIZES:
        raise HTTPException(422, "thumbnail size must be 256 (Large), 128 (Medium) or 64 (Compact)")


@app.post("/api/fl-sync/apply")
def fl_sync_apply(body: FlSyncApply):
    if body.mode not in _VALID_FL_SYNC_MODES:
        raise HTTPException(400, "mode must be 'vendor' or 'category'")
    if body.scope not in _VALID_FL_SYNC_SCOPES:
        raise HTTPException(400, "scope must be 'new' or 'full'")
    _check_thumb_size(body.thumb_size)
    with db.connect(DB_PATH) as conn:
        try:
            summary = fl_reorganize.apply(
                conn, body.mode, body.overrides, body.sections, body.dangerous_wipe, body.save_backup,
                body.scope, body.pictures, body.replace_thumbnails, thumb_height=body.thumb_size,
            )
        except OSError as exc:
            raise HTTPException(409, f"Filesystem error: {exc}")
    for section, s in summary.items():
        pics = s["pictures"]
        applog.log_event(
            f"FL Studio sync ({section}, {'complete' if body.scope == 'full' else 'new only'}): filed "
            f"{s['applied']}, {s['unmatched']} unmatched (root), {s['moved']} moved, "
            f"{s['renamed']} name(s) synced from FL. Backup: {s['backup'] or 'n/a'}."
            + (f" Thumbnails: {pics['added']} added, {pics['updated']} updated, {pics['kept']} kept, "
               f"{pics['missing']} without a VEXT picture." if pics else "")
        )
    return summary


@app.get("/api/fl-sync/inventory")
def fl_sync_inventory():
    """The browse-tree check (app/fl_reorganize.py:inventory): every .fst with
    its .nfo/.png validation, orphans, and VEXT vs FL Studio's database."""
    with db.connect(DB_PATH) as conn:
        return fl_reorganize.inventory(conn)


@app.post("/api/fl-sync/repair")
def fl_sync_repair():
    """Writes missing .nfo Bitmap lines and sets the hidden attribute (fl_reorganize.repair)."""
    with db.connect(DB_PATH) as conn:
        try:
            result = fl_reorganize.repair(conn)
        except OSError as exc:
            raise HTTPException(409, f"Filesystem error: {exc}")
    applog.log_event(f"FL Studio browse tree repaired: {result['nfo_written']} .nfo written, "
                     f"{result['hidden']} file(s) hidden.")
    return result


@app.get("/api/fl-sync/thumbnail")
def fl_sync_thumbnail(path: str):
    """A browse-tree picture for the check list. Only .png files inside FL's
    Plugin database folder are served."""
    with db.connect(DB_PATH) as conn:
        root = fl_reorganize._plugin_db_root(conn).resolve()
    target = Path(path).resolve()
    if target.suffix.lower() != ".png" or not target.is_relative_to(root) or not target.is_file():
        raise HTTPException(404, "no such picture in FL Studio's plugin database")
    return FileResponse(str(target), media_type="image/png", headers={"Cache-Control": "no-cache"})


@app.post("/api/fl-sync/open-plugin-manager")
def fl_sync_open_plugin_manager():
    """Starts FL Studio's own Plugin Manager, where "Find installed plugins"
    scans for the plugins FL doesn't know yet."""
    exe = fl_reorganize.plugin_manager_path()
    if not exe:
        raise HTTPException(404, "FL Studio's Plugin Manager wasn't found (PluginManager.exe under "
                                 "System, Tools, Plugin Manager in FL Studio's folder)")
    os.startfile(exe)  # its own window; no console involved
    return {"ok": True, "path": exe}


@app.get("/api/fl-sync/link-candidates")
def fl_sync_link_candidates(name: str = "", include_ignored: bool = False):
    """VEXT plugins that might be the one an FL plugin name means, best first
    (app/fl_reorganize.py:link_candidates); with include_ignored, the plugins
    ignored in VEXT too."""
    with db.connect(DB_PATH) as conn:
        return {"candidates": fl_reorganize.link_candidates(conn, name, include_ignored=include_ignored)}


@app.get("/api/fl-sync/ignored-plugins")
def fl_sync_ignored_plugins(q: str = ""):
    """The plugins ignored in VEXT, for the link dialog's search with "Include
    ignored plugins" (fl_reorganize.ignored_plugins)."""
    with db.connect(DB_PATH) as conn:
        return {"plugins": fl_reorganize.ignored_plugins(conn, q)[:30]}


@app.post("/api/fl-sync/fix-pictures")
def fl_sync_fix_pictures(body: FlSyncFixPictures):
    """Gives the picked browse-tree .fst files VEXT's picture at the chosen size
    (fl_reorganize.fix_pictures). A thumbnail VEXT didn't make is backed up first."""
    _check_thumb_size(body.size)
    with db.connect(DB_PATH) as conn:
        try:
            result = fl_reorganize.fix_pictures(conn, body.fst, body.size)
        except OSError as exc:
            raise HTTPException(409, f"Filesystem error: {exc}")
    applog.log_event(f"FL Studio thumbnails fixed: {result['fixed']} at {body.size} px, "
                     f"{len(result['skipped'])} skipped." + (f" Backup: {result['backup']}." if result["backup"] else ""))
    return result


class FlSyncSize(BaseModel):
    size: int = 256       # 256 / 128 / 64


@app.post("/api/fl-sync/fix-all-pictures")
def fl_sync_fix_all_pictures(body: FlSyncSize):
    """Every browse-tree .fst VEXT recognizes and has a picture for gets it at the
    chosen size (fl_reorganize.fix_all_pictures); current ones are left alone."""
    _check_thumb_size(body.size)
    with db.connect(DB_PATH) as conn:
        try:
            result = fl_reorganize.fix_all_pictures(conn, body.size)
        except OSError as exc:
            raise HTTPException(409, f"Filesystem error: {exc}")
    applog.log_event(f"FL Studio thumbnails, fix all at {body.size} px: {result['written']} written, "
                     f"{result['current']} already current, {result['failed']} failed."
                     + (f" Backup: {result['backup']}." if result["backup"] else ""))
    return result


class FlSyncIgnore(BaseModel):
    name: str             # an FL plugin name (.fst stem) VEXT doesn't recognize
    ignored: bool = True


@app.post("/api/fl-sync/ignore")
def fl_sync_ignore(body: FlSyncIgnore):
    """Ignores an unrecognized FL plugin name in the check, or stops ignoring it."""
    if not body.name.strip():
        raise HTTPException(400, "name is empty")
    with db.connect(DB_PATH) as conn:
        entries = fl_reorganize.set_ignored(conn, body.name.strip(), body.ignored)
    applog.log_event(f"FL Studio plugin \"{body.name}\" {'ignored' if body.ignored else 'no longer ignored'} in FL Studio Sync's check.")
    return {"entries": entries}


class FlSyncAddDetected(BaseModel):
    items: list[dict] = []   # [{"plugin_id", "nfo"}]: Not filed rows (FL's own .nfo in Installed)
    size: int = 256
    pictures: bool = True


@app.post("/api/fl-sync/add-detected")
def fl_sync_add_detected(body: FlSyncAddDetected):
    """Copies FL's own .fst of plugins FL detected but the browse tree doesn't show into the
    browse tree (vendor folder), with VEXT's picture (fl_reorganize.add_detected)."""
    _check_thumb_size(body.size)
    with db.connect(DB_PATH) as conn:
        result = fl_reorganize.add_detected(conn, body.items, body.size, body.pictures)
    for e in result["added"]:
        applog.log_event(f"FL Studio browser entry added from FL's plugin database: {e['fst']}")
    return result


# ---------- Configuration > Storage (app/storage.py) ----------

def _fl_root_for_storage(conn):
    """FL Studio's Plugin database folder (VEXT's FL backups live there), or None."""
    root = fl_reorganize._plugin_db_root(conn)
    return root if root.is_dir() else None


@app.get("/api/storage")
def storage_measure():
    """What can go to free space (caches, backups), each measured."""
    with db.connect(DB_PATH) as conn:
        return storage.measure(_fl_root_for_storage(conn))


class StorageClear(BaseModel):
    keys: list[str] = []


@app.post("/api/storage/clear")
def storage_clear(body: StorageClear):
    """Clears the picked items (storage.clear); then answers the new sizes too."""
    with db.connect(DB_PATH) as conn:
        root = _fl_root_for_storage(conn)
        try:
            result = storage.clear(body.keys, root)
        except ValueError as e:
            raise HTTPException(400, str(e))
        applog.log_event("Storage cleared: " + ", ".join(
            f"{r['key']} {r['freed'] / 1048576:.1f} MB ({r['files']} files, {r['left']} in use)" for r in result["results"]))
        return {**result, "now": storage.measure(root)}


@app.post("/api/fl-sync/resize-pictures")
def fl_sync_resize_pictures(body: FlSyncFixPictures):
    """Changes only the size of the picked .fst files' thumbnails (the same image)
    to the chosen size (fl_reorganize.resize_pictures)."""
    _check_thumb_size(body.size)
    with db.connect(DB_PATH) as conn:
        result = fl_reorganize.resize_pictures(conn, body.fst, body.size)
    applog.log_event(f"FL Studio thumbnails resized to {body.size} px: {result['resized']}, "
                     f"{len(result['skipped'])} skipped." + (f" Backup: {result['backup']}." if result["backup"] else ""))
    return result


@app.post("/api/fl-sync/resize-all-pictures")
def fl_sync_resize_all_pictures(body: FlSyncSize):
    """Every thumbnail in the browse tree at the chosen size, the same images
    (fl_reorganize.resize_all_pictures)."""
    _check_thumb_size(body.size)
    with db.connect(DB_PATH) as conn:
        result = fl_reorganize.resize_all_pictures(conn, body.size)
    applog.log_event(f"FL Studio thumbnails, resize all to {body.size} px: {result['resized']} resized, "
                     f"{result['current']} already that size, {result['failed']} failed."
                     + (f" Backup: {result['backup']}." if result["backup"] else ""))
    return result


class FlSyncRemove(BaseModel):
    fst: list[str] = []   # browse-tree .fst files VEXT doesn't recognize


@app.post("/api/fl-sync/remove")
def fl_sync_remove(body: FlSyncRemove):
    """Moves unrecognized .fst entries (with their .nfo and picture) out of FL's
    browser into Plugin database/Removed.vext-<time>/ (fl_reorganize.remove_entries)."""
    with db.connect(DB_PATH) as conn:
        result = fl_reorganize.remove_entries(conn, body.fst)
    for path in result["removed"]:
        applog.log_event(f"FL Studio browser entry removed (not recognized by VEXT): {path} -> {result['folder']}")
    return result


@app.post("/api/fl-sync/link")
def fl_sync_link(body: FlSyncLink):
    """Links an FL plugin name (an .fst stem) to a VEXT plugin, or unlinks it
    (plugin_id null). Optionally fixes the pictures of every .fst with that name
    (fl_reorganize.set_link)."""
    if not body.name.strip():
        raise HTTPException(400, "the FL plugin name is empty")
    _check_thumb_size(body.size)
    with db.connect(DB_PATH) as conn:
        if body.ignored_host_path:
            # An ignored plugin isn't in the catalog: it comes back (un-ignored, read again) first.
            try:
                body.plugin_id = scan_service.catalog_one(conn, body.ignored_host_path)
            except ValueError as e:
                raise HTTPException(409, str(e))
            applog.log_event(f"Ignored plugin brought back into the catalog to link it in FL Studio Sync: "
                             f"{body.ignored_host_path}")
        if body.plugin_id is not None and db.get_plugin(conn, body.plugin_id) is None:
            raise HTTPException(404, f"no VEXT plugin with id {body.plugin_id}")
        try:
            result = fl_reorganize.set_link(conn, body.name, body.plugin_id, body.fix_picture, body.size)
        except OSError as exc:
            raise HTTPException(409, f"Filesystem error: {exc}")
    action = "unlinked" if body.plugin_id is None else f"linked to VEXT plugin {body.plugin_id}"
    applog.log_event(f"FL Studio plugin \"{body.name.strip()}\" {action}."
                     + (f" Thumbnails fixed: {result['fixed']}." if body.fix_picture else ""))
    return result


@app.post("/api/sync")
def sync_state():
    """Lightweight reconcile, distinct from a full "Rescan now": just checks
    that quarantine's and out-of-scope's registries still match what's
    actually on disk (catches the owner deleting a tracked file directly,
    e.g. in Explorer) and refreshes derived counts — no plugin-directory
    scanning involved."""
    with db.connect(DB_PATH) as conn:
        removed = db.reconcile_missing_quarantine(conn)
        removed_oos = db.reconcile_missing_out_of_scope(conn)
    return {"removed_quarantine": removed, "removed_out_of_scope": removed_oos}
