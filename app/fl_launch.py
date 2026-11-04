"""Read-only FL preset resolution and the local controller bridge."""

import csv
import hashlib
import io
import json
import os
import secrets
import shutil
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

from app import db, fl_database, logbuf, pictures
from app.config import FL_PLUGIN_DB_ROOT, PROJECT_ROOT

CONTROLLER_REVISION = 9

BRIDGE_DIR = PROJECT_ROOT / "fl-bridge"
SCRIPT = Path(__file__).parent / "static" / "fl-controller" / "device_VEXT.py"
_lock = threading.Lock()
_result = {"state": "idle"}
_process_cache = (0.0, False)
_listing_cache: tuple = (None, [])
_digest_cache: dict = {}
_listing_lock = threading.Lock()
_filed_cache: dict[str, tuple[float, dict]] = {}
_filed_lock = threading.Lock()
_FILED_TTL = 10.0
_nfo_cache: dict[Path, tuple[tuple[int, int, int], dict]] = {}
_log_lock = threading.Lock()
_logged_events: set[tuple] = set()
_log_order: deque = deque(maxlen=512)
_log_started = time.time()


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _exchange_file(name: str) -> Path:
    config = _read_json(BRIDGE_DIR / "config.json")
    return Path(config.get("directory", str(BRIDGE_DIR))) / name


def roots(conn) -> tuple[Path, Path]:
    installed = Path(db.get_setting(conn, "fl_plugin_db_root", FL_PLUGIN_DB_ROOT)).resolve()
    return installed, installed.parent


def _nfo_fields(path: Path) -> dict:
    stat = path.stat()
    stamp = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
    previous = _nfo_cache.get(path)
    if previous is not None and previous[0] == stamp:
        return previous[1]
    fields = dict(line.split("=", 1) for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines() if "=" in line)
    if len(_nfo_cache) >= 20000:
        _nfo_cache.clear()
    _nfo_cache[path] = (stamp, fields)
    return fields


def presets(conn, plugin_id: int) -> list[dict]:
    """Match every file index, never a display-name guess or shared Waves host."""
    row = db.get_plugin(conn, plugin_id)
    if row is None:
        raise ValueError("Plugin not found.")
    if row["format"] == "AAX" or row["status"] == "corrupted":
        raise ValueError("This plugin cannot be loaded in FL Studio.")
    host = Path(row["host_path"])
    if not host.exists() or str(host).lower() in {p.lower() for p in db.quarantined_host_paths(conn)}:
        raise ValueError("The plugin is missing or quarantined.")
    installed, root = roots(conn)
    target = fl_database._bundle_root(str(host)).lower()
    links = db.fl_links(conn)
    found = []
    fl_root = None
    for section in ("Effects", "Generators"):
        folder = installed / section
        for nfo in sorted(folder.rglob("*.nfo")) if folder.is_dir() else []:
            rel = nfo.relative_to(installed)
            if len(rel.parts) != 3 or rel.parts[1] not in ("VST", "VST3", "CLAP", "Fruity"):
                continue  # New is a mirror; it must not create duplicate load choices.
            try:
                fields = _nfo_fields(nfo)
            except OSError:
                continue
            linked = links.get(nfo.stem.lower())
            if linked is not None and linked != plugin_id:
                continue  # an explicit association takes precedence over old cached binary metadata
            matched = linked == plugin_id
            for key, value in fields.items():
                if not key.startswith("ps_file_filename_"):
                    continue
                # Per-product Waves identity must come from an explicit link, never a shell path.
                if row["format"] == "WAVES":
                    continue
                if "%FLPath%" in value:
                    if fl_root is None:
                        from app.config import detect_fl_studio_root
                        fl_root, _ = detect_fl_studio_root(db.get_setting(conn, "fl_studio_root", ""))
                    matched |= str(Path(value.replace("%FLPath%", fl_root)).parent).lower() == str(host).lower()
                else:
                    matched |= fl_database._bundle_root(value).lower() == target
            if matched:
                # Resolve and validate only matching presets, not every unrelated FL entry.
                fst = nfo.with_suffix(".fst").resolve()
                if not fst.is_relative_to(root) or not fst.is_file():
                    continue
                path = fst.relative_to(root)
                found.append({"key": path.as_posix(), "name": fst.stem, "path": str(fst),
                              "root": str(root), "route": [root.name, *path.parts[:-1], fst.stem],
                              "kind": section, "format": rel.parts[1]})
    # A manual link may point at a filed preset when the original Installed entry is unavailable.
    if not found:
        for section in ("Effects", "Generators"):
            for fst in sorted((root / section).rglob("*.fst")):
                if links.get(fst.stem.lower()) != plugin_id:
                    continue
                resolved = fst.resolve()
                if not resolved.is_relative_to(root):
                    continue
                path = resolved.relative_to(root)
                found.append({"key": path.as_posix(), "name": fst.stem, "path": str(resolved),
                              "root": str(root), "route": [root.name, *path.parts[:-1], fst.stem],
                              "kind": section, "format": row["format"]})
    return found


def select_preset(conn, plugin_id: int | None, key: str) -> dict:
    if plugin_id is None:
        return preset_by_key(conn, key)
    choices = presets(conn, plugin_id)
    selected = [p for p in choices if p["key"] == key] if key else choices
    if not selected:
        raise ValueError("No matching FL preset. Scan it in FL's Plugin Manager or link its preset in FL Studio Sync.")
    if len(selected) != 1:
        raise ValueError("Choose the FL preset and format to load.")
    return selected[0]


def setup_info(conn) -> dict:
    installed, _ = roots(conn)
    # .../<user data>/Presets/Plugin database/Installed; never guess a user profile.
    user_data = installed.parent.parent.parent
    destination = user_data / "Settings" / "Hardware" / "VEXT"
    config = _read_json(BRIDGE_DIR / "config.json")
    script_path = destination / "device_VEXT.py"
    return {"script_path": str(script_path),
            "installed": (destination / "device_VEXT.py").is_file()
            and _read_json(destination / "vext_config.json") == config and bool(config),
            "update_available": script_path.is_file() and script_path.read_bytes() != SCRIPT.read_bytes(),
            "port": db.get_setting(conn, "fl_launch_port", "Vext"), "result": status()}


def install(conn, expected_path: str) -> dict:
    info = setup_info(conn)
    destination = Path(info["script_path"])
    if str(destination) != expected_path:
        raise ValueError("The FL data folder changed. Review the script destination again.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        shutil.copy2(destination, destination.with_name("device_VEXT.py.bak"))
    previous_config = destination.parent / "vext_config.json"
    if previous_config.is_file():
        shutil.copy2(previous_config, previous_config.with_suffix(".json.bak"))
    config = _read_json(BRIDGE_DIR / "config.json")
    config = {"directory": str(destination.parent.resolve()),
              "secret": config.get("secret") or secrets.token_hex(32)}
    _write_json(BRIDGE_DIR / "config.json", config)
    shutil.copyfile(SCRIPT, destination)
    _write_json(destination.parent / "vext_config.json", config)
    return setup_info(conn)


def _log_once(key: tuple, text: str, level: str = "error") -> None:
    with _log_lock:
        if key in _logged_events:
            return
        if len(_log_order) == _log_order.maxlen:
            _logged_events.discard(_log_order[0])
        _log_order.append(key)
        _logged_events.add(key)
        secret = _read_json(BRIDGE_DIR / "config.json").get("secret")
        if isinstance(secret, str) and secret:
            text = text.replace(secret, "[redacted]")
        logbuf.add("FL controller", text, level)


def collect_logs() -> None:
    config = _read_json(BRIDGE_DIR / "config.json")
    secret = config.get("secret")
    if not isinstance(secret, str) or not secret:
        return
    diagnostics = _read_json(_exchange_file("diagnostics.json"))
    if diagnostics.get("secret") != secret:
        return
    events = diagnostics.get("events")
    if not isinstance(events, list):
        return
    for event in events[-64:]:
        if not isinstance(event, dict):
            continue
        event_id, stamp = event.get("id"), event.get("time")
        if not isinstance(event_id, str) or not event_id or len(event_id) > 128:
            continue
        if not isinstance(stamp, (int, float)) or not _log_started <= stamp <= time.time() + 5:
            continue
        level = event.get("level", "error")
        if level not in ("info", "warning", "error"):
            continue
        stage = str(event.get("stage", "script"))[:100]
        message = str(event.get("message", ""))[:4000]
        context = ""
        if event.get("plugin"):
            context += " | Plugin: " + str(event["plugin"])[:500]
        if event.get("build"):
            context += " | Build: " + str(event["build"])[:100]
        if event.get("request_id"):
            context += " | Request: " + str(event["request_id"])[:128]
        text = stage + ": " + message + context
        if event.get("traceback"):
            text += "\n" + str(event["traceback"])[-16000:]
        _log_once(("diagnostic", event_id), text, level)


def _log_request_failure(result: dict) -> None:
    if result.get("state") not in ("error", "unknown") or result.get("diagnostic_id"):
        return
    message = str(result.get("message", "FL request failed."))
    if result.get("plugin"):
        message += " | Plugin: " + str(result["plugin"])
    _log_once(("request", result.get("id"), result.get("state")), message)


def status() -> dict:
    collect_logs()
    result = dict(_result)
    if result.get("state") in ("pending", "activating"):
        reply = _read_json(_exchange_file("response.json"))
        if reply.get("id") == result.get("id") and reply.get("secret") == _read_json(BRIDGE_DIR / "config.json").get("secret"):
            result.update({k: v for k, v in reply.items() if k != "secret"})
            if reply.get("revision") != CONTROLLER_REVISION:
                result.update(state="error", message="FL is running an older VEXT controller. Reload the script or restart FL Studio.")
        if result.get("state") in ("pending", "activating") and time.time() > result["expires"]:
            result.update(state="unknown", message="FL did not confirm the request. Check the controller setup before trying again.")
        _result.update(result)
        collect_logs()
    _log_request_failure(result)
    return result


def fl_running() -> bool:
    """Inspect process names only; cache the Windows query between UI polls."""
    global _process_cache
    now = time.monotonic()
    if now - _process_cache[0] < 3:
        return _process_cache[1]
    running = False
    if os.name == "nt":
        try:
            output = subprocess.run(
                ["tasklist.exe", "/FO", "CSV", "/NH"], check=False, capture_output=True,
                text=True, timeout=3, creationflags=subprocess.CREATE_NO_WINDOW,
            )
            running = any(row and row[0].casefold() in {"fl.exe", "fl64.exe", "fl (scaled).exe", "fl64 (scaled).exe"}
                          for row in csv.reader(io.StringIO(output.stdout)))
        except (OSError, subprocess.TimeoutExpired):
            pass
    _process_cache = (now, running)
    return running


def connection(conn) -> dict:
    info = setup_info(conn)
    running = fl_running()
    heartbeat = _read_json(_exchange_file("heartbeat.json"))
    config = _read_json(BRIDGE_DIR / "config.json")
    stamp = heartbeat.get("time", 0)
    fresh = isinstance(stamp, (int, float)) and 0 <= time.time() - stamp < 6
    connected = bool(running and info["installed"] and not info["update_available"]
                     and config.get("secret") and heartbeat.get("secret") == config["secret"] and fresh
                     and heartbeat.get("revision") == CONTROLLER_REVISION)
    message = "Connected to FL Studio" if connected else (
        "FL Studio running, controller disconnected" if running else "FL Studio closed")
    if running and not connected:
        if not info["installed"]:
            message = "Install the VEXT controller in FL Studio Sync."
        elif info["update_available"]:
            message = "Update the VEXT controller, then reload it in FL Studio."
        elif heartbeat.get("revision") != CONTROLLER_REVISION:
            message = "FL is running an older VEXT controller. Reload the script or restart FL Studio."
        elif not fresh:
            message = "Reload the VEXT controller in FL Studio MIDI settings; no heartbeat received."
        else:
            message = "VEXT controller settings do not match. Reinstall it in FL Studio Sync."
    return {"running": running, "connected": connected, "message": message}


def send(conn, preset: dict | None = None) -> dict:
    global _result
    from app.hosting.midi_output import send_sysex

    with _lock:
        if status().get("state") in ("pending", "activating"):
            raise ValueError("Wait for the current FL request to finish.")
        info = setup_info(conn)
        if not info["installed"]:
            raise ValueError("Install the VEXT controller in FL Studio Sync first.")
        if info["update_available"]:
            raise ValueError("Update the VEXT controller in FL Studio Sync, then reload it in FL.")
        if preset is not None and not connection(conn)["connected"]:
            raise ValueError("Connect the VEXT controller in FL Studio before loading a plugin.")
        config = _read_json(BRIDGE_DIR / "config.json")
        request_id = secrets.token_hex(16)
        request = {"id": request_id, "secret": config["secret"], "expires": time.time() + 25,
                   "command": "load" if preset else "ping", "preset": preset, "revision": CONTROLLER_REVISION}
        _write_json(_exchange_file("request.json"), request)
        _result = {"id": request_id, "state": "pending", "expires": request["expires"],
                   "plugin": (preset or {}).get("name", ""),
                   "message": "Waiting for FL Studioâ€¦"}
        try:
            send_sysex(info["port"], b"\xf0\x7dVEXT\x01" + request_id.encode("ascii") + b"\xf7")
        except (OSError, ValueError) as exc:
            _result.update(state="error", message=str(exc))
            _log_request_failure(_result)
            raise ValueError(str(exc)) from exc
        return status()


def _fl_studio_installed(conn) -> bool:
    # Same rule as web.py's _fl_studio_info (which we can't import: it imports this module's routes).
    from app.config import detect_fl_studio_root

    detected, _ = detect_fl_studio_root(db.get_setting(conn, "fl_studio_root", ""))
    return os.path.isdir(os.path.join(detected, "Plugins"))


def _configured(conn) -> tuple[bool, str]:
    if not _fl_studio_installed(conn):
        return False, "Install FL Studio, or set its folder in Configuration > DAWs > FL Studio."
    info = setup_info(conn)
    if info["update_available"]:
        return False, "Update the VEXT controller in Configuration > DAWs > FL Studio."
    if not info["installed"]:
        return False, "Install the VEXT controller in Configuration > DAWs > FL Studio."
    return True, ""


def preset_by_key(conn, key: str) -> dict:
    """A launcher key names one real FST under FL's Installed plugin database."""
    _, root = roots(conn)
    relative = Path(key)
    parts = relative.parts
    if (not key or relative.is_absolute() or ".." in parts
            or relative.suffix.lower() != ".fst"):
        raise ValueError("Invalid FL preset key.")
    if parts[0] != "Installed":
        raise ValueError("Choose a plugin preset from FL's Installed plugin database.")
    if len(parts) != 4 or parts[1] not in ("Effects", "Generators") or parts[2] not in ("VST", "VST3", "CLAP", "Fruity"):
        raise ValueError("Choose a plugin preset from FL's format folders.")
    kind, fmt = parts[1:3]
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("The FL preset is missing or outside the plugin database.")
    # Revalidate known unhealthy binaries even when the launcher addresses their FST directly.
    row = _preset_row(conn, path)
    if row is not None:
        blocked = {p.lower() for p in db.quarantined_host_paths(conn)}
        if row["format"] == "AAX" or row["status"] == "corrupted" or row["host_path"].lower() in blocked:
            raise ValueError("This plugin is corrupted or quarantined.")
    return {"key": relative.as_posix(), "name": path.stem, "path": str(path), "root": str(root),
            "route": [root.name, *parts[:-1], path.stem], "kind": kind, "format": fmt}


def _preset_row(conn, fst: Path, rows=None, links=None):
    rows = list(db.all_plugins(conn)) if rows is None else rows
    links = db.fl_links(conn) if links is None else links
    linked = links.get(fst.stem.lower())
    if linked is not None:
        return next((row for row in rows if row["id"] == linked), None)
    try:
        fields = _nfo_fields(fst.with_suffix(".nfo"))
    except OSError:
        fields = {}
    targets = {fl_database._bundle_root(value).lower() for key, value in fields.items()
               if key.startswith("ps_file_filename_") and "%FLPath%" not in value}
    matches = [row for row in rows if row["format"] != "WAVES"
               and fl_database._bundle_root(row["host_path"]).lower() in targets]
    if len(matches) == 1:
        return matches[0]
    installed, root = roots(conn)
    relative = fst.relative_to(root) if fst.is_relative_to(root) else None
    if relative is not None and relative.parts[0] in ("Effects", "Generators"):
        for fmt in ("VST", "VST3", "CLAP", "Fruity"):
            candidate = (installed / relative.parts[0] / fmt / fst.name).resolve()
            try:
                if not candidate.is_relative_to(root) or not candidate.is_file() or _fst_digest(candidate) != _fst_digest(fst):
                    continue
                row = _preset_row(conn, candidate, rows, links)
                if row is not None:
                    return row
            except OSError:
                continue
    return None


def preset_image(conn, key: str) -> Path | None:
    preset = preset_by_key(conn, key)
    root = Path(preset["root"])
    fst = Path(preset["path"])
    try:
        fields = _nfo_fields(fst.with_suffix(".nfo"))
    except OSError:
        fields = {}
    bitmap = next((value for name, value in fields.items() if name.lower() == "bitmap"), fst.with_suffix(".png").name)
    image = (fst.parent / bitmap).resolve()
    if image.is_relative_to(root) and image.suffix.lower() == ".png" and image.is_file():
        return image
    # No own picture: the filed copy with the same section and name may supply one.
    filed = _filed_pictures(root).get((preset["kind"], fst.stem.casefold()))
    if filed is not None and filed.is_relative_to(root) and filed.suffix.lower() == ".png" and filed.is_file():
        return filed
    return None


def _filed_pictures(root: Path) -> dict:
    """Pictures of the filed copies under Effects and Generators, by (section, stem); never Installed."""
    key = str(root)
    with _filed_lock:
        cached = _filed_cache.get(key)
        if cached is not None and time.monotonic() - cached[0] < _FILED_TTL:
            return cached[1]
        found: dict = {}
        ambiguous: set = set()
        for section in ("Effects", "Generators"):
            folder = root / section
            if not folder.is_dir():
                continue
            base = folder.resolve()
            for fst in folder.rglob("*.fst"):
                try:
                    bitmap = next((value for name, value in _nfo_fields(fst.with_suffix(".nfo")).items()
                                   if name.lower() == "bitmap"), "")
                except OSError:
                    bitmap = ""
                try:
                    picture = (fst.parent / bitmap if bitmap else fst.with_suffix(".png")).resolve()
                    if picture.suffix.lower() != ".png" or not picture.is_relative_to(base) or not picture.is_file():
                        continue
                except (OSError, ValueError):
                    continue
                slot = (section, fst.stem.casefold())
                if found.setdefault(slot, picture) != picture:
                    ambiguous.add(slot)  # two different pictures: never guess which one is right
        result = {slot: picture for slot, picture in found.items() if slot not in ambiguous}
        _filed_cache[key] = (time.monotonic(), result)
        return result


def _file_stamp(path: Path) -> tuple:
    try:
        stat = path.stat()
        return (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
    except OSError:
        return ()


def _fst_digest(path: Path) -> bytes:
    stamp = _file_stamp(path)
    cached = _digest_cache.get(path)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    digest = hashlib.sha256(path.read_bytes()).digest()
    if len(_digest_cache) > 20000:
        _digest_cache.clear()
    _digest_cache[path] = (stamp, digest)
    return digest


def launchable(conn) -> list[dict]:
    """Lists FL's Installed plugin database only; the catalog only supplies verified enrichment."""
    global _listing_cache
    installed, root = roots(conn)
    rows, links = list(db.all_plugins(conn)), db.fl_links(conn)
    blocked = {p.lower() for p in db.quarantined_host_paths(conn)}
    cat_map, pics = db.category_tags_by_plugin(conn), pictures.picture_index()
    filed = _filed_pictures(root)
    files = []
    for section in ("Effects", "Generators"):
        for fmt in ("VST", "VST3", "CLAP", "Fruity"):
            folder = installed / section / fmt
            if folder.is_dir():
                files.extend(sorted(folder.glob("*.fst")))
    signature = (str(root), tuple(tuple(row) for row in rows), tuple(sorted(links.items())),
                 repr(cat_map), tuple(sorted(blocked)), tuple(sorted(pics.items())),
                 tuple((str(fst), str(fst.resolve()), _file_stamp(fst), _file_stamp(fst.with_suffix(".nfo")),
                        _file_stamp(fst.with_suffix(".png"))) for fst in files),
                 tuple(sorted((slot, str(picture), _file_stamp(picture)) for slot, picture in filed.items())))
    with _listing_lock:
        if signature == _listing_cache[0]:
            return _listing_cache[1]
    out = _build_listing(conn, root, files, rows, links, blocked, cat_map, pics, filed)
    with _listing_lock:
        _listing_cache = (signature, out)
    return out


def _build_listing(conn, root, files, rows, links, blocked, cat_map, pics, filed):
    out, seen = [], set()
    for fst in files:
        try:
            resolved = fst.resolve()
            if not resolved.is_relative_to(root) or not resolved.is_file():
                continue
            relative = fst.relative_to(root)
            kind = relative.parts[1]
            # Never list the same Installed preset twice.
            identity = (kind, fst.stem.casefold(), _fst_digest(resolved))
            if identity in seen:
                continue
            row = _preset_row(conn, resolved, rows, links)
            if row is not None and (row["format"] == "AAX" or row["status"] == "corrupted" or row["host_path"].lower() in blocked):
                continue
            seen.add(identity)
            fields = _nfo_fields(fst.with_suffix(".nfo")) if fst.with_suffix(".nfo").is_file() else {}
            image = fst.with_suffix(".png").is_file() or any(name.lower() == "bitmap" for name in fields)
            categories = [(kind, "fl-" + kind.lower())]
            if row is not None:
                categories.extend((name, slug) for name, slug, _ in cat_map.get(row["id"], []))
            out.append({"id": row["id"] if row is not None else None,
                        "key": relative.as_posix(), "name": fst.stem,
                        "vendor": (row["vendor"] if row is not None else fields.get("ps_vendor", "")) or "Unassigned",
                        "plugin_type": "Effect" if kind == "Effects" else "Instrument",
                        "format": relative.parts[2],
                        "has_image": bool(row is not None and pictures.picture_stamp(row["host_path"], pics) is not None),
                        "has_preset_image": image, "has_filed_image": (kind, fst.stem.casefold()) in filed,
                        "categories": categories})
        except OSError:
            continue
    return out


def menu(conn, include_plugins: bool = False) -> dict:
    """Categories and vendors that have at least one launchable plugin, for the bubble's rings."""
    categories: dict[str, dict] = {}
    vendors: dict[str, dict] = {}
    plugins = launchable(conn)
    for plugin in plugins:
        for name, slug in plugin["categories"]:
            entry = categories.setdefault(slug.casefold(), {"slug": slug, "name": name, "count": 0})
            entry["count"] += 1
        if plugin["vendor"]:
            entry = vendors.setdefault(plugin["vendor"].casefold(), {"name": plugin["vendor"], "count": 0})
            entry["count"] += 1
    configured, reason = _configured(conn)

    def by_name(entry):
        return entry["name"].casefold()

    result = {"configured": configured, "reason": reason,
              "categories": sorted(categories.values(), key=by_name),
              "vendors": sorted(vendors.values(), key=by_name)}
    if include_plugins:
        result["plugins"] = plugins
    return result


def plugins_for(conn, kind: str, value: str) -> dict:
    """The launchable plugins in one category (slug) or from one vendor (name), by name."""
    if kind not in ("category", "vendor"):
        raise ValueError("kind must be category or vendor.")
    wanted = value.strip().casefold()
    title = value.strip()
    matches = []
    if not wanted:  # an empty vendor is no group: never list the plugins without one
        return {"title": title, "plugins": matches}
    for plugin in launchable(conn):
        if kind == "category":
            names = [name for name, slug in plugin["categories"] if slug.casefold() == wanted]
            if not names:
                continue
            title = names[0]
        else:
            if plugin["vendor"].casefold() != wanted:
                continue
            title = plugin["vendor"]
        matches.append({key: plugin[key] for key in ("id", "key", "name", "vendor", "plugin_type", "format", "has_image", "has_preset_image")})
    matches.sort(key=lambda p: p["name"].casefold())
    return {"title": title, "plugins": matches}
