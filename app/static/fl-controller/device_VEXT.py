# name=VEXT
"""FL Studio controller: exact preset navigation, with no translated menu commands.

Installed by VEXT beside vext_config.json. FL provides the imported controller modules.
"""

import json
import os
import sys
import time
import traceback

import channels
import midi
import mixer
import plugins
import ui

CONTROLLER_REVISION = 9
SCRIPT_BUILD = "direct-descent-logs-1"
_FRAME = object()

_job = None
_request = None
_steps = 0
_seen = set()
_heartbeat_at = 0
_last_error = ""
_log_session = str(int(time.time() * 1000000))
_log_seq = 0
_diagnostics = []


def _read(path):
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)


def _settings_path():
    # FL executes controller sources without __file__; cwd can later change with another script.
    source = globals().get("__file__")
    folders = ([os.path.dirname(os.path.abspath(source))] if source else []) + [os.getcwd()] + list(sys.path)
    for folder in folders:
        if not isinstance(folder, str):
            continue
        candidate = os.path.abspath(os.path.join(folder, "vext_config.json"))
        if os.path.isfile(candidate) and os.path.isfile(os.path.join(folder, "device_VEXT.py")):
            return candidate
    return os.path.abspath("vext_config.json")


_SETTINGS_PATH = _settings_path()


def _config():
    return _read(_SETTINGS_PATH)


def _diagnostic(stage, message, request=None, level="error"):
    global _log_seq
    _log_seq += 1
    event = {"id": _log_session + ":" + str(_log_seq), "time": time.time(),
             "stage": stage, "message": str(message)[:4000], "level": level,
             "revision": CONTROLLER_REVISION, "build": SCRIPT_BUILD}
    trace = traceback.format_exc()
    if trace.strip() != "NoneType: None":
        event["traceback"] = trace[-16000:]
    if request:
        event["request_id"] = request.get("id", "")
        event["plugin"] = (request.get("preset") or {}).get("name", "")
    _diagnostics.append(event)
    del _diagnostics[:-64]
    try:
        config = _config()
        path = os.path.join(config["directory"], "diagnostics.json")
        with open(path, "w", encoding="utf-8") as stream:
            json.dump({"secret": config["secret"], "events": _diagnostics}, stream)
        return event["id"]
    except Exception:  # noqa: BLE001 - diagnostics must not recursively fail in FL callbacks
        return None


def _reply(request, state, message, **extra):
    config = _config()
    path = os.path.join(config["directory"], "response.json")
    diagnostic_id = _diagnostic(request.get("command", "request"), message, request,
                                "warning" if state == "unknown" else "error") if state in ("error", "unknown") else None
    value = dict(id=request["id"], secret=config["secret"], state=state, message=message, revision=CONTROLLER_REVISION, build=SCRIPT_BUILD, diagnostic_id=diagnostic_id, **extra)
    # FL permits writes in this controller folder, but blocks rename/delete operations.
    # Readers tolerate an incomplete JSON document and poll again.
    with open(path, "w", encoding="utf-8") as stream:
        json.dump(value, stream)


def _report_error(stage, error):
    global _last_error
    message = stage + ": " + str(error)
    if message != _last_error:
        _last_error = message
        print("VEXT: " + message)
        _diagnostic(stage, str(error))


def _heartbeat(stamp):
    config = _config()
    path = os.path.join(config["directory"], "heartbeat.json")
    with open(path, "w", encoding="utf-8") as stream:
        json.dump({"secret": config["secret"], "time": stamp, "revision": CONTROLLER_REVISION, "build": SCRIPT_BUILD}, stream)


def _caption():
    value = ui.getFocusedNodeCaption() or ""
    return value[:-4] if value.lower().endswith(".fst") else value


def _tick():
    global _steps
    _steps += 1
    if _steps > 16000 or time.time() >= _request["expires"]:
        raise RuntimeError("Browser navigation timed out. Open FL's Plugins Browser tab and try again.")
    if ui.isInPopupMenu():
        raise RuntimeError("Close FL's open menu or dialog before loading a plugin.")


def _settle(frames=3, seconds=0.0):
    # These yields must return to FL, not just advance this generator in the same callback.
    until = time.time() + seconds
    for _ in range(frames):
        yield _FRAME
    while time.time() < until:
        yield _FRAME


def _plugins_tab():
    ui.showWindow(midi.widBrowser)
    ui.setFocused(midi.widBrowser)
    yield from _settle()
    name = ui.navigateBrowserTabs(0) or ""
    yield from _settle()
    for _ in range(32):
        if name.strip().casefold() == "plugins":
            return
        following = ui.navigateBrowserTabs(midi.FPT_Right) or ""
        yield from _settle()
        if following == name:
            break
        name = following
    raise RuntimeError("FL Browser's Plugins tab was not found. Last tab: " + name)


def _nav(direction):
    _tick()
    # Down uses FL's synchronous next-node call; Up uses the documented FPT_Up value.
    if direction == midi.FPT_Up:
        ui.navigateBrowser(midi.FPT_Up, 0)
    elif direction == midi.FPT_Down:
        ui.navigateBrowser(1, 0)
    elif direction == midi.FPT_Left:
        ui.left()
    elif direction == midi.FPT_Right:
        ui.right()
    else:
        raise RuntimeError("Unsupported Browser direction.")
    if direction in (midi.FPT_Left, midi.FPT_Right):
        yield from _settle()
    else:
        yield
    return _caption()


def _sibling(name):
    for _ in range(6000):
        current = _caption()
        if current.casefold() == name.casefold():
            return
        # Do not collapse while searching: tree rebuilds can freeze caption readback.
        following = yield from _nav(midi.FPT_Down)
        if following == current:
            yield from _settle()
            if _caption() == current:
                break
    raise RuntimeError("Preset path not found in FL Browser: " + name + ". Selection stopped at " + _caption() + ". Nothing was loaded.")


def _locate(route):
    yield from _plugins_tab()
    # Collapse only the selected branch in one bounded burst, before reading the rebuilt tree.
    # Waiting after every individual Left was the expensive part of the old reset.
    for _ in range(min(2 * len(route) + 4, 64)):
        _tick()
        ui.left()
    yield from _settle(seconds=0.12)
    for _ in range(256):
        if _caption().casefold() == route[0].casefold():
            break
        before = _caption()
        after = yield from _nav(midi.FPT_Up)
        if after == before:
            yield from _settle(seconds=0.06)
            if _caption() == before:
                break
    yield from _descend(route)


def _installed_route(route):
    return (len(route) == 5 and route[1] == "Installed"
            and route[2] in ("Effects", "Generators")
            and route[3] in ("CLAP", "Fruity", "VST", "VST3"))


def _folder_child(name, allowed, nested=()):
    # Check folder names and node types while moving forward. Do not rebuild the tree mid-search.
    allowed = {value.casefold() for value in allowed}
    nested = None if nested is None else {value.casefold() for value in nested}
    for _ in range(6000):
        _tick()
        current = _caption()
        kind = ui.getFocusedNodeFileType()
        if kind < 0:
            if current.casefold() == name.casefold():
                return
            if current.casefold() not in allowed and nested is not None and current.casefold() not in nested:
                raise RuntimeError("Browser left the expected folder level while finding " + name
                                   + ": selected " + current + ". Nothing was loaded.")
        following = yield from _nav(midi.FPT_Down)
        if following == current:
            yield from _settle(seconds=0.06)
            if _caption() == current:
                break
    raise RuntimeError("Folder " + name + " was not found at its expected Browser level. Nothing was loaded.")


def _installed_descent(route):
    yield from _sibling(route[0])
    if ui.getFocusedNodeFileType() >= 0:
        raise RuntimeError("The Plugin database root is not selected. Nothing was loaded.")
    formats = ("CLAP", "Fruity", "New", "VST", "VST3")
    levels = ((route[1], ("Effects", "Generators", "Installed"), None),
              (route[2], ("Effects", "Generators"), formats),
              (route[3], formats, ()))
    for target, allowed, nested in levels:
        ui.toggleBrowserNode(1)
        yield from _settle(seconds=0.06)
        yield from _nav(midi.FPT_Down)
        yield from _folder_child(target, allowed, nested)
    ui.toggleBrowserNode(1)
    yield from _settle(seconds=0.06)
    yield from _nav(midi.FPT_Down)
    for _ in range(6000):
        _tick()
        current = _caption()
        kind = ui.getFocusedNodeFileType()
        if kind < 0:
            raise RuntimeError("Reached another folder before finding " + route[-1] + ". Nothing was loaded.")
        if current.casefold() == route[-1].casefold():
            if kind != getattr(midi, "SBN_FST", 4):
                raise RuntimeError("The selected Browser item is not an FL preset. Nothing was loaded.")
            return
        following = yield from _nav(midi.FPT_Down)
        if following == current:
            yield from _settle(seconds=0.06)
            if _caption() == current:
                break
    raise RuntimeError("Preset not found inside " + route[2] + "/" + route[3] + ": " + route[-1])


def _descend(route):
    if _installed_route(route):
        yield from _installed_descent(route)
        return
    for index, name in enumerate(route):
        yield from _sibling(name)
        if index < len(route) - 1:
            ui.toggleBrowserNode(1)
            yield from _settle()
            # Expanding leaves the folder selected. Down enters its first child;
            # Right can toggle an already-expanded folder closed in FL.
            yield from _nav(midi.FPT_Down)


def _verify_ancestry(route):
    ui.showWindow(midi.widBrowser)
    ui.setFocused(midi.widBrowser)
    yield from _settle(seconds=0.06)
    if not ui.getFocused(midi.widBrowser):
        raise RuntimeError("FL Browser did not take keyboard focus. Close FL dialogs and plugin editors, then try again.")
    # Keep the folder check, but do not add a second collapse after Left.
    if _caption().casefold() != route[-1].casefold():
        raise RuntimeError("The Browser selection changed. Nothing was loaded.")
    for parent in reversed(route[:-1]):
        before = _caption()
        actual = before
        for _ in range(2):
            _tick()
            ui.left()
            yield from _settle(seconds=0.06)
            deadline = min(_request["expires"], time.time() + 0.25)
            while True:
                actual = _caption()
                if actual.casefold() == parent.casefold():
                    break
                if time.time() >= deadline:
                    break
                yield _FRAME
            if actual.casefold() == parent.casefold():
                break
            # Left can collapse in place first. A second Left is allowed only if it stayed there.
            if actual != before:
                break
        if actual.casefold() != parent.casefold():
            raise RuntimeError("Browser folder check failed: expected " + parent + ", selected " + actual
                               + " (from " + before + "). Nothing was loaded.")


def _load(request):
    preset = request["preset"]
    route = preset["route"]
    if not (3 <= len(route) <= 32) or any(not isinstance(p, str) or not p or p in (".", "..") for p in route):
        raise RuntimeError("Invalid preset route.")
    if not os.path.isfile(preset["path"]) or not preset["path"].lower().endswith(".fst"):
        raise RuntimeError("The FL preset is missing. Refresh it in VEXT.")
    track = mixer.trackNumber()
    channel = channels.selectedChannel()
    count = channels.channelCount()
    if preset["kind"] == "Effects" and all(plugins.isValid(track, slot) for slot in range(10)):
        raise RuntimeError("The selected mixer insert has no empty effect slot.")
    yield from _locate(route)
    if not _installed_route(route):
        # Filed/manual presets still use the conservative ancestry check.
        yield from _verify_ancestry(route)
        yield from _descend(route)
    # Installed paths have been checked level-by-level; do not leave the found preset.
    if _caption().casefold() != route[-1].casefold() or ui.getFocusedNodeFileType() < 0:
        raise RuntimeError("FL did not select a preset file. Nothing was loaded.")
    if mixer.trackNumber() != track or channels.selectedChannel() != channel:
        raise RuntimeError("The selected FL destination changed. Nothing was loaded.")
    _tick()
    if not ui.getFocused(midi.widBrowser):
        raise RuntimeError("FL Browser lost keyboard focus. Nothing was loaded.")
    # Record the point of no return before asking FL to open it; never automatically retry.
    _reply(request, "activating", "FL is opening the preset. Do not repeat the request.")
    ui.enter()
    for _ in range(5):
        yield
    if preset["kind"] == "Generators":
        if channels.channelCount() > count:
            destination = "a new Channel Rack channel"
        else:
            destination = "Channel Rack channel " + str(channel + 1)
    else:
        destination = "mixer insert " + str(track)
    # Browser activation has no load-success return value; do not claim verified audio/plugin state.
    _reply(request, "sent", "Opened “" + preset["name"] + "” for " + destination + ". Check FL for any plugin dialog.")


def OnInit():
    print("VEXT controller: " + SCRIPT_BUILD)
    _diagnostic("startup", "Controller ready", level="info")


def OnSysEx(event):
    global _job, _request, _steps
    data = bytes(event.sysex)
    if not data.startswith(b"\xf0\x7dVEXT\x01"):
        return
    event.handled = True
    if len(data) != 40 or data[-1] != 0xF7:
        return
    try:
        request_id = data[7:-1].decode("ascii")
        config = _config()
        request = _read(os.path.join(config["directory"], "request.json"))
        if (request.get("id") != request_id or request.get("secret") != config["secret"]
                or time.time() >= request.get("expires", 0) or request_id in _seen):
            return
        previous = os.path.join(config["directory"], "response.json")
        if os.path.isfile(previous) and _read(previous).get("id") == request_id:
            return
        if _job is not None:
            return
        if request.get("revision") != CONTROLLER_REVISION:
            _reply(request, "error", "VEXT and its FL controller versions differ. Update the controller and restart FL.")
            return
        _seen.add(request_id)
        if len(_seen) > 128:
            _seen.clear()
            _seen.add(request_id)
        if request.get("command") == "ping":
            _reply(request, "ready", "FL Studio is listening to VEXT.")
        elif request.get("command") == "load":
            _request, _steps = request, 0
            _job = _load(request)
    except Exception as exc:  # noqa: BLE001 - FL callbacks must never escape into the host
        _report_error("MIDI request", exc)


def OnIdle():
    global _job, _heartbeat_at
    now = time.time()
    if now - _heartbeat_at >= 1:
        _heartbeat_at = now
        try:
            _heartbeat(now)
        except (OSError, ValueError, KeyError, SystemError) as exc:
            _report_error("Controller heartbeat", exc)
    if _job is None:
        return
    try:
        for _ in range(24):
            if next(_job) is _FRAME:
                break
    except StopIteration:
        _job = None
    except Exception as exc:  # noqa: BLE001 - a plugin/FL API failure must end this job safely
        _job = None
        try:
            _reply(_request, "error", str(exc))
        except Exception as reply_error:  # noqa: BLE001 - FL file APIs can also raise SystemError
            _report_error("Request reply", reply_error)


def OnDeInit():
    global _job
    try:
        _heartbeat(0)
        if _job is not None:
            _reply(_request, "unknown", "The FL controller stopped. Check FL before loading again.")
    except (OSError, ValueError, KeyError, SystemError) as exc:
        _report_error("Controller shutdown", exc)
    finally:
        _job = None
