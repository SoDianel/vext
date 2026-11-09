"""FL launch tests use only temporary catalogs, preset trees, and a simulated FL API.

Run this file in its own process, as required by AGENTS.md.
"""

import json
import os
import sys
import tempfile
import time
import types
from pathlib import Path

WORK = Path(tempfile.mkdtemp(prefix="vext-fl-launch-"))
os.environ["VEXT_ROOT"] = str(WORK)
os.environ["LOCALAPPDATA"] = str(WORK / "local")
os.environ["APPDATA"] = str(WORK / "roaming")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pytest

from app import db
from app import fl_launch as launch
from app.config import PROJECT_ROOT

assert Path(PROJECT_ROOT).resolve() == WORK.resolve()


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    database = tmp_path / "plugins.db"
    db.init_db(database)
    monkeypatch.setattr(launch, "BRIDGE_DIR", tmp_path / "bridge")
    monkeypatch.setattr(launch, "_result", {"state": "idle"})
    with db.connect(database) as conn:
        installed = tmp_path / "fl" / "Presets" / "Plugin database" / "Installed"
        db.set_setting(conn, "fl_plugin_db_root", str(installed))
        host = tmp_path / "Plugins" / "Instrument.vst3"
        host.parent.mkdir()
        host.write_bytes(b"fixture")
        cur = conn.execute("INSERT INTO plugins (name, normalized_name, host_path, format, scanned_at) VALUES (?, ?, ?, ?, ?)",
                           ("Catalog display name", "catalog", str(host), "VST3", "2026-11-02"))
        conn.commit()
        yield conn, cur.lastrowid, installed, host


def preset(installed, host, name="Exact preset", section="Generators", fmt="VST3", index=0):
    fst = installed / section / fmt / (name + ".fst")
    fst.parent.mkdir(parents=True, exist_ok=True)
    fst.write_bytes(b"fixture fst")
    fst.with_suffix(".nfo").write_text(
        f"ps_name={name}\nps_files={index + 1}\nps_file_filename_{index}={host}\n", encoding="utf-8")
    return fst


def test_exact_binary_match_uses_actual_preset_name_and_every_file_index(catalog):
    conn, pid, installed, host = catalog
    fst = preset(installed, host, "Synté", index=1)
    choices = launch.presets(conn, pid)
    assert len(choices) == 1
    assert choices[0]["name"] == "Synté"
    assert choices[0]["route"] == ["Plugin database", "Installed", "Generators", "VST3", "Synté"]
    assert choices[0]["path"] == str(fst.resolve())


def test_same_name_other_format_and_new_mirror_do_not_match(catalog):
    conn, pid, installed, host = catalog
    preset(installed, host)
    preset(installed, host.with_suffix(".dll"), fmt="VST")
    preset(installed, host, fmt="New")
    assert [p["format"] for p in launch.presets(conn, pid)] == ["VST3"]


def test_multiple_presets_require_an_explicit_key(catalog):
    conn, pid, installed, host = catalog
    preset(installed, host, "A")
    preset(installed, host, "B")
    with pytest.raises(ValueError, match="Choose"):
        launch.select_preset(conn, pid, "")
    assert launch.select_preset(conn, pid, "Installed/Generators/VST3/B.fst")["name"] == "B"
    with pytest.raises(ValueError, match="No matching"):
        launch.select_preset(conn, pid, "../../other.fst")


@pytest.mark.parametrize("state", ["corrupted", "missing", "quarantined", "AAX"])
def test_unavailable_plugins_are_rejected(catalog, state):
    conn, pid, installed, host = catalog
    preset(installed, host)
    if state == "corrupted":
        conn.execute("UPDATE plugins SET status='corrupted' WHERE id=?", (pid,))
    elif state == "missing":
        host.unlink()
    elif state == "quarantined":
        monkey = pytest.MonkeyPatch()
        monkey.setattr(db, "quarantined_host_paths", lambda conn: [str(host)])
    else:
        conn.execute("UPDATE plugins SET format='AAX' WHERE id=?", (pid,))
    try:
        with pytest.raises(ValueError):
            launch.presets(conn, pid)
    finally:
        if state == "quarantined":
            monkey.undo()


def test_missing_fst_and_same_display_name_are_not_enough(catalog):
    conn, pid, installed, host = catalog
    fst = preset(installed, host)
    fst.unlink()
    preset(installed, host.with_name("Other.vst3"), "Catalog display name")
    assert launch.presets(conn, pid) == []


def test_waves_needs_per_product_manual_link(catalog):
    conn, pid, installed, host = catalog
    conn.execute("UPDATE plugins SET format='WAVES' WHERE id=?", (pid,))
    preset(installed, host.with_name("WaveShell.vst3"), "Specific product Stereo", section="Effects")
    assert launch.presets(conn, pid) == []
    db.set_fl_link(conn, "Specific product Stereo", pid)
    assert launch.presets(conn, pid)[0]["name"] == "Specific product Stereo"


def test_manual_association_overrides_old_binary_metadata(catalog):
    conn, pid, installed, host = catalog
    preset(installed, host)
    other = conn.execute("INSERT INTO plugins (name, normalized_name, host_path, format, scanned_at) VALUES (?, ?, ?, ?, ?)",
                         ("Other", "other", str(host.with_name("Other.vst3")), "VST3", "2026-11-02")).lastrowid
    db.set_fl_link(conn, "Exact preset", other)
    assert launch.presets(conn, pid) == []


def test_install_path_must_match_reviewed_destination_and_script_is_backed_up(catalog):
    conn, _, _, _ = catalog
    info = launch.setup_info(conn)
    with pytest.raises(ValueError, match="changed"):
        launch.install(conn, str(WORK / "wrong.py"))
    assert launch.install(conn, info["script_path"])["installed"]
    target = Path(info["script_path"])
    target.write_text("old controller", encoding="utf-8")
    launch.install(conn, info["script_path"])
    assert target.with_name("device_VEXT.py.bak").read_text() == "old controller"
    assert target.read_bytes() == launch.SCRIPT.read_bytes()


def test_pending_requests_are_not_repeated_and_replies_must_match_secret(catalog, monkeypatch):
    from app.hosting import midi_output
    conn, _, _, _ = catalog
    launch.install(conn, launch.setup_info(conn)["script_path"])
    calls = []
    monkeypatch.setattr(midi_output, "send_sysex", lambda *args: calls.append(args))
    result = launch.send(conn)
    assert calls[0][0] == "VEXT"
    assert len(calls[0][1]) == 40
    with pytest.raises(ValueError, match="Wait"):
        launch.send(conn)
    reply = launch._exchange_file("response.json")
    launch._write_json(reply, {"id": result["id"], "secret": "wrong", "state": "ready", "revision": launch.CONTROLLER_REVISION})
    assert launch.status()["state"] == "pending"
    config = launch._read_json(launch.BRIDGE_DIR / "config.json")
    launch._write_json(reply, {"id": result["id"], "secret": config["secret"], "state": "ready", "revision": launch.CONTROLLER_REVISION})
    assert launch.status()["state"] == "ready"
    assert "secret" not in launch.status()


class Browser:
    """Visible tree with independent collapse state; permits duplicate names in different folders."""

    def __init__(self):
        self.nodes = [("Current project", -1), ("Plugin database", -1), ("Installed", 1),
                      ("Effects", 2), ("VST3", 3), ("Exact preset", 4),
                      ("Generators", 2), ("VST", 6), ("Exact preset", 7),
                      ("VST3", 6), ("Exact preset", 9)]
        self.expanded = set(range(11))
        self.at = 5
        self.loaded = []
        self.tab = 0
        self.tab_switches = []

    def visible(self):
        def visible(index):
            parent = self.nodes[index][1]
            return parent == -1 or (parent in self.expanded and visible(parent))
        return [i for i in range(len(self.nodes)) if visible(i)]

    def toggleBrowserNode(self, value):
        if value: self.expanded.add(self.at)
        else: self.expanded.discard(self.at)

    def getFocusedNodeCaption(self):
        return self.nodes[self.at][0] + (".fst" if self.at in (5, 8, 10) else "")

    def move(self, direction, shift=0):
        if direction == 2:
            parent = self.nodes[self.at][1]
            if parent != -1: self.at = parent
        elif direction == 3:
            if shift and self.at in (5, 8, 10): self.loaded.append(self.at)
            elif self.at in self.expanded:
                children = [i for i, (_, p) in enumerate(self.nodes) if p == self.at]
                if children: self.at = children[0]
        else:
            nodes = self.visible()
            self.at = nodes[max(0, min(len(nodes) - 1, nodes.index(self.at) + (-1 if direction == 0 else 1)))]
        return self.getFocusedNodeCaption()

    def navigateBrowserTabs(self, direction):
        self.tab = min(2, self.tab + 1) if direction == 43 else max(0, self.tab - 1) if direction == 42 else 0
        self.tab_switches.append(self.tab)
        return ("All", "PLUGINS", "Library")[self.tab]

    def navigateBrowser(self, direction, shift=0):
        assert direction in (40, 1), "Previous node uses FPT_Up (40); next node uses 1"
        return self.move(0 if direction == 40 else 1, shift)

    def left(self):
        self.expanded.discard(self.at)
        self.move(2)

    def right(self):
        # FL can toggle an expanded folder closed; it does not reliably select its child.
        if self.at in self.expanded:
            self.expanded.discard(self.at)
        else:
            self.expanded.add(self.at)

    def enter(self):
        self.loaded.append(self.at)


@pytest.fixture
def controller(tmp_path, monkeypatch):
    browser = Browser()
    for name, module in {
        "ui": types.SimpleNamespace(**{key: getattr(browser, key) for key in ("navigateBrowserTabs", "navigateBrowser", "toggleBrowserNode", "getFocusedNodeCaption", "left", "right", "enter")},
                                    showWindow=lambda *_: None, setFocused=lambda *_: None, getFocused=lambda *_: True, isInPopupMenu=lambda: False,
                                    getFocusedNodeFileType=lambda: 4 if browser.at in (5, 8, 10) else -100),
        "midi": types.SimpleNamespace(FPT_Up=40, FPT_Down=41, FPT_Left=42, FPT_Right=43, widBrowser=4),
        "channels": types.SimpleNamespace(selectedChannel=lambda: 0, channelCount=lambda: 1),
        "mixer": types.SimpleNamespace(trackNumber=lambda: 2),
        "plugins": types.SimpleNamespace(isValid=lambda *_: False),
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    module = types.ModuleType("vext_controller_test")
    module.__file__ = str(launch.SCRIPT)
    exec(compile(launch.SCRIPT.read_text(encoding="utf-8"), str(launch.SCRIPT), "exec"), module.__dict__)  # noqa: S102 - fake FL runtime; avoid writing distributable pyc files
    clock = [time.time()]
    module.time = types.SimpleNamespace(time=lambda: clock[0])
    module._test_advance_clock = lambda: clock.__setitem__(0, clock[0] + 0.02)
    config = {"directory": str(tmp_path), "secret": "secret"}
    monkeypatch.setattr(module, "_config", lambda: config)
    fst = tmp_path / "Exact preset.fst"
    fst.write_bytes(b"fixture")
    request = {"id": "a" * 32, "secret": "secret", "expires": time.time() + 30, "command": "load", "revision": launch.CONTROLLER_REVISION,
               "preset": {"name": "Exact preset", "path": str(fst), "kind": "Generators",
                          "route": ["Plugin database", "Installed", "Generators", "VST3", "Exact preset"]}}
    (tmp_path / "request.json").write_text(json.dumps(request))
    return module, browser, request, tmp_path


def run_controller(module, request):
    event = types.SimpleNamespace(sysex=b"\xf0\x7dVEXT\x01" + request["id"].encode() + b"\xf7", handled=False)
    module.OnSysEx(event)
    for _ in range(1000):
        if module._job is None: break
        module._test_advance_clock()
        module.OnIdle()
        if hasattr(module.ui, "_test_frame"):
            module.ui._test_frame()
    assert event.handled


def test_controller_selects_exact_format_and_does_not_repeat(controller):
    module, browser, request, folder = controller
    run_controller(module, request)
    assert browser.loaded == [10]
    assert json.loads((folder / "response.json").read_text())["state"] == "sent"
    run_controller(module, request)
    assert browser.loaded == [10]


def test_controller_rejects_wrong_ancestry_before_opening(controller):
    module, browser, request, folder = controller
    request["preset"]["route"][2] = "Effects"
    # The desired VST folder is absent under Effects; the same named generator must not load.
    request["preset"]["route"][3] = "VST"
    (folder / "request.json").write_text(json.dumps(request))
    run_controller(module, request)
    assert browser.loaded == []
    assert json.loads((folder / "response.json").read_text())["state"] == "error"


@pytest.mark.parametrize("change", ["expired", "wrong_secret"])
def test_controller_ignores_stale_or_untrusted_requests(controller, change):
    module, browser, request, folder = controller
    if change == "expired": request["expires"] = time.time() - 1
    else: request["secret"] = "wrong"
    (folder / "request.json").write_text(json.dumps(request))
    run_controller(module, request)
    assert browser.loaded == []
    assert not (folder / "response.json").exists()


def test_no_free_effect_slot_never_activates(controller, monkeypatch):
    module, browser, request, folder = controller
    monkeypatch.setattr(module.plugins, "isValid", lambda *_: True)
    request["preset"]["kind"] = "Effects"
    (folder / "request.json").write_text(json.dumps(request))
    run_controller(module, request)
    assert browser.loaded == []
    assert "no empty" in json.loads((folder / "response.json").read_text())["message"]


def test_response_timeout_is_uncertain_and_activation_keeps_polling(catalog, monkeypatch):
    conn, _, _, _ = catalog
    launch.install(conn, launch.setup_info(conn)["script_path"])
    config = launch._read_json(launch.BRIDGE_DIR / "config.json")
    monkeypatch.setattr(launch, "_result", {"id": "request", "state": "pending", "expires": time.time() + 10})
    launch._write_json(launch._exchange_file("response.json"), {"id": "request", "secret": config["secret"], "state": "activating", "revision": launch.CONTROLLER_REVISION})
    assert launch.status()["state"] == "activating"
    launch._result["expires"] = time.time() - 1
    assert launch.status()["state"] == "unknown"
    launch._result.update(state="activating", expires=time.time() + 10)
    launch._write_json(launch._exchange_file("response.json"), {"id": "request", "secret": config["secret"], "state": "sent", "revision": launch.CONTROLLER_REVISION})
    assert launch.status()["state"] == "sent"
    monkeypatch.setattr(launch, "_result", {"id": "other", "state": "pending", "expires": time.time() - 1})
    assert launch.status()["state"] == "unknown"


def test_api_requires_launch_token_and_install_confirmation(catalog, monkeypatch):
    from fastapi.testclient import TestClient

    from app import fl_launch_api, security, web
    conn, pid, installed, host = catalog
    database = conn.execute("PRAGMA database_list").fetchone()[2]
    monkeypatch.setattr(fl_launch_api, "DB_PATH", database)
    monkeypatch.setattr(web, "DB_PATH", database)
    preset(installed, host)
    client = TestClient(web.app, base_url="http://127.0.0.1")
    body = {"plugin_id": pid, "key": "Installed/Generators/VST3/Exact preset.fst"}
    assert client.post("/api/fl-launch/drag-resolve", json=body).status_code == 403
    headers = {security.TOKEN_HEADER: security.TOKEN}
    assert client.post("/api/fl-launch/drag-resolve", json=body, headers=headers).status_code == 200
    body["key"] = "../../arbitrary.fst"
    assert client.post("/api/fl-launch/drag-resolve", json=body, headers=headers).status_code == 409
    path = launch.setup_info(conn)["script_path"]
    assert client.post("/api/fl-launch/install", json={"path": path}, headers=headers).status_code == 400
    assert not Path(path).exists()
    assert client.post("/api/fl-launch/install", json={"path": path, "confirmed": True}, headers=headers).status_code == 200
    assert "secret" not in client.get("/api/fl-launch/status").json()
    released = []
    monkeypatch.setattr(web.midi_watch, "release", lambda: released.append(True))
    monkeypatch.setattr(web.midi_watch, "poke", lambda: None)
    monkeypatch.setattr(web.plugin_host, "apply_midi_settings", lambda: None)
    assert client.put("/api/settings/fl_launch_port", json={"value": "Other port"}, headers=headers).status_code == 200
    assert released == [True]


def test_all_input_selection_never_opens_fl_command_port(catalog, monkeypatch):
    import threading

    from app import config
    from app.hosting import midi as midi_input
    conn, _, _, _ = catalog
    monkeypatch.setattr(config, "DB_PATH", conn.execute("PRAGMA database_list").fetchone()[2])
    monkeypatch.setattr(midi_input, "list_inputs", lambda: [{"name": "Vext", "id": "Vext", "index": 1}])
    inputs = object.__new__(midi_input.MidiInputs)
    inputs.close = lambda: None
    inputs._lock = threading.Lock()
    inputs._handles = {}
    inputs._open_ids = []
    assert inputs.open("*")["open"] == []
    assert inputs.open(["Vext"])["open"] == []


def test_winmm_output_uses_original_device_index_and_releases_buffers(monkeypatch):
    from app.hosting import midi_output
    calls = []

    class WinMM:
        def midiOutOpen(self, handle, index, *_):
            calls.append(("open", index))
            handle._obj.value = 10
            return 0

        def midiOutPrepareHeader(self, *_):
            calls.append("prepare")
            return 0

        def midiOutLongMsg(self, handle, pointer, size):
            pointer._obj.flags = 1
            calls.append("send")
            return 0

        def midiOutReset(self, *_): calls.append("reset")
        def midiOutUnprepareHeader(self, *_): calls.append("unprepare")
        def midiOutClose(self, *_): calls.append("close")

    monkeypatch.setattr(midi_output, "_outputs", lambda: [(3, "Vext")])
    monkeypatch.setattr(midi_output, "_api", WinMM)
    midi_output.send_sysex("Vext", b"\xf0\x7dVEXT\xf7")
    assert calls == [("open", 3), "prepare", "send", "reset", "unprepare", "close"]


def test_connection_requires_running_fl_fresh_authenticated_heartbeat(catalog, monkeypatch):
    conn, _, _, _ = catalog
    launch.install(conn, launch.setup_info(conn)["script_path"])
    monkeypatch.setattr(launch, "fl_running", lambda: True)
    config = launch._read_json(launch.BRIDGE_DIR / "config.json")
    heartbeat = launch._exchange_file("heartbeat.json")
    assert not launch.connection(conn)["connected"]
    launch._write_json(heartbeat, {"secret": "wrong", "time": time.time(), "revision": launch.CONTROLLER_REVISION})
    assert not launch.connection(conn)["connected"]
    launch._write_json(heartbeat, {"secret": config["secret"], "time": time.time() - 10})
    assert not launch.connection(conn)["connected"]
    launch._write_json(heartbeat, {"secret": config["secret"], "time": time.time(), "revision": launch.CONTROLLER_REVISION})
    assert launch.connection(conn)["connected"]
    monkeypatch.setattr(launch, "fl_running", lambda: False)
    assert not launch.connection(conn)["connected"]


def test_load_is_blocked_when_controller_disconnected(catalog, monkeypatch):
    conn, _, _, _ = catalog
    launch.install(conn, launch.setup_info(conn)["script_path"])
    monkeypatch.setattr(launch, "fl_running", lambda: True)
    with pytest.raises(ValueError, match="Connect the VEXT controller"):
        launch.send(conn, {"kind": "Generators"})
    assert not (launch._exchange_file("request.json")).exists()


def test_fl_runtime_without_file_keeps_settings_path_when_cwd_changes(tmp_path, monkeypatch):
    controller_dir = tmp_path / "Hardware" / "VEXT"
    controller_dir.mkdir(parents=True)
    (controller_dir / "device_VEXT.py").write_bytes(launch.SCRIPT.read_bytes())
    bridge = controller_dir
    config = {"directory": str(bridge), "secret": "fixture secret"}
    (controller_dir / "vext_config.json").write_text(json.dumps(config))
    for name in ("channels", "midi", "mixer", "plugins", "ui"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.chdir(controller_dir)
    module = types.ModuleType("fl_exec_without_file")
    exec(compile(launch.SCRIPT.read_text(encoding="utf-8"), "<FL controller>", "exec"), module.__dict__)  # noqa: S102 - reproduce FL's embedded source execution
    assert "__file__" not in module.__dict__
    monkeypatch.chdir(tmp_path)
    real_open = open
    def sandbox_open(path, mode="r", **kwargs):
        if any(flag in mode for flag in "wax+") and Path(path).parent != controller_dir:
            raise SystemError("FileIO returned NULL without setting an exception")
        return real_open(path, mode, **kwargs)
    module.open = sandbox_open
    def forbidden(*_args):
        raise SystemError("operation returned NULL without setting an exception")
    module.os = types.SimpleNamespace(path=os.path, replace=forbidden, remove=forbidden)
    module.OnIdle()
    heartbeat = json.loads((bridge / "heartbeat.json").read_text())
    assert heartbeat["secret"] == config["secret"]
    assert abs(time.time() - heartbeat["time"]) < 2
    request = {"id": "b" * 32, "secret": config["secret"], "expires": time.time() + 20, "command": "ping", "revision": launch.CONTROLLER_REVISION}
    (bridge / "request.json").write_text(json.dumps(request))
    event = types.SimpleNamespace(sysex=b"\xf0\x7dVEXT\x01" + request["id"].encode() + b"\xf7", handled=False)
    module.OnSysEx(event)
    assert json.loads((bridge / "response.json").read_text())["state"] == "ready"
    module.OnDeInit()
    assert json.loads((bridge / "heartbeat.json").read_text())["time"] == 0


def test_controller_system_errors_are_reported_once_and_never_escape(controller, monkeypatch):
    module, _, _, _ = controller
    output = []
    monkeypatch.setattr(module, "print", output.append, raising=False)
    def denied(_stamp):
        raise SystemError("FileIO returned NULL without setting an exception")
    monkeypatch.setattr(module, "_heartbeat", denied)
    for _ in range(3):
        module._heartbeat_at = 0
        module.OnIdle()
    assert len(output) == 1
    module.OnDeInit()
    assert module._job is None


def test_exchange_files_stay_inside_confirmed_controller_folder(catalog):
    conn, _, _, _ = catalog
    info = launch.install(conn, launch.setup_info(conn)["script_path"])
    target = Path(info["script_path"]).parent
    assert launch._exchange_file("request.json").parent == target
    assert launch._exchange_file("response.json").parent == target
    assert launch._exchange_file("heartbeat.json").parent == target


def test_expanded_browser_folders_are_not_toggled_closed_during_descent(controller, monkeypatch):
    module, browser, request, folder = controller
    browser.at = 1
    browser.expanded = {1, 2, 3, 4, 6, 7, 9}
    def unexpected_right():
        raise AssertionError("Right must not follow explicit folder expansion")
    monkeypatch.setattr(module.ui, "right", unexpected_right)
    calls = []
    locate = module._locate
    def count_locate(route):
        calls.append(route)
        yield from locate(route)
    monkeypatch.setattr(module, "_locate", count_locate)
    run_controller(module, request)
    assert browser.loaded == [10]
    assert len(calls) == 1, "A verified root must not trigger a second full Browser reset"
    assert json.loads((folder / "response.json").read_text())["state"] == "sent"


def test_preset_metadata_cache_avoids_rereads_and_observes_changes(catalog, monkeypatch):
    conn, pid, installed, host = catalog
    fst = preset(installed, host)
    original = Path.read_text
    reads = []
    def counted_read(path, *args, **kwargs):
        if path.suffix == ".nfo":
            reads.append(path)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", counted_read)
    assert launch.presets(conn, pid)
    assert launch.presets(conn, pid)
    assert len(reads) == 1
    fst.with_suffix(".nfo").write_text("ps_file_filename_0=changed-binary-path.vst3\n")
    assert launch.presets(conn, pid) == []
    assert len(reads) == 2


def test_tab_switches_and_tree_rebuilds_wait_for_real_host_callbacks(controller, monkeypatch):
    module, browser, request, folder = controller
    pending = []
    frame_count = 0
    def queue(operation):
        pending.append([2, operation])
    def advance_frame():
        nonlocal frame_count
        frame_count += 1
        for entry in list(pending):
            entry[0] -= 1
            if entry[0] == 0:
                pending.remove(entry)
                entry[1]()
    def deferred_collapse(value):
        index = browser.at
        queue(lambda: browser.expanded.add(index) if value else browser.expanded.discard(index))
    def deferred_left():
        queue(browser.left)
    navigate = browser.navigateBrowser
    def guarded_navigation(direction, shift=0):
        # FL drops moves while the UI thread has not rebuilt the tree yet.
        return browser.getFocusedNodeCaption() if pending else navigate(direction, shift)
    monkeypatch.setattr(module.ui, "toggleBrowserNode", deferred_collapse)
    monkeypatch.setattr(module.ui, "left", deferred_left)
    monkeypatch.setattr(module.ui, "navigateBrowser", guarded_navigation)
    monkeypatch.setattr(module.ui, "_test_frame", advance_frame, raising=False)
    run_controller(module, request)
    assert browser.tab == 1
    assert browser.tab_switches[:2] == [0, 1]
    assert frame_count > 6
    assert browser.loaded == [10]
    assert json.loads((folder / "response.json").read_text())["state"] == "sent"


def test_missing_plugins_tab_stops_without_opening_a_preset(controller, monkeypatch):
    module, browser, request, folder = controller
    monkeypatch.setattr(module.ui, "navigateBrowserTabs", lambda _direction: "Library")
    run_controller(module, request)
    assert not browser.loaded
    assert "Plugins tab was not found" in json.loads((folder / "response.json").read_text())["message"]


def test_old_runtime_heartbeat_cannot_enable_loading(catalog, monkeypatch):
    conn, _, _, _ = catalog
    launch.install(conn, launch.setup_info(conn)["script_path"])
    monkeypatch.setattr(launch, "fl_running", lambda: True)
    config = launch._read_json(launch.BRIDGE_DIR / "config.json")
    launch._write_json(launch._exchange_file("heartbeat.json"), {"secret": config["secret"], "time": time.time()})
    result = launch.connection(conn)
    assert not result["connected"]
    assert "older VEXT controller" in result["message"]


def test_controller_rejects_an_incompatible_request_revision(controller):
    module, browser, request, folder = controller
    assert module.CONTROLLER_REVISION == launch.CONTROLLER_REVISION
    request["revision"] -= 1
    (folder / "request.json").write_text(json.dumps(request))
    run_controller(module, request)
    assert not browser.loaded
    assert "versions differ" in json.loads((folder / "response.json").read_text())["message"]


def test_reset_batches_lefts_and_never_explicitly_collapses_each_folder(controller, monkeypatch):
    module, browser, request, folder = controller
    sent_at = []
    def left():
        sent_at.append(module.time.time())
        browser.left()
    original_toggle = browser.toggleBrowserNode
    def expand_only(value):
        assert value == 1, "Do not collapse nodes during caption searches"
        original_toggle(value)
    monkeypatch.setattr(module.ui, "left", left)
    monkeypatch.setattr(module.ui, "toggleBrowserNode", expand_only)
    run_controller(module, request)
    reset_count = 2 * len(request["preset"]["route"]) + 4
    assert len(set(sent_at[:reset_count])) == 1, "Reset must issue one burst before settling"
    assert browser.loaded == [10]
    assert json.loads((folder / "response.json").read_text())["state"] == "sent"


def test_ancestry_supports_left_collapsing_in_place_before_parent(controller, monkeypatch):
    module, browser, request, folder = controller
    def left():
        if browser.at not in (5, 8, 10) and browser.at in browser.expanded:
            browser.expanded.discard(browser.at)
        else:
            browser.move(2)
    monkeypatch.setattr(module.ui, "left", left)
    run_controller(module, request)
    assert browser.loaded == [10]
    assert json.loads((folder / "response.json").read_text())["state"] == "sent"


def test_installed_preset_does_not_send_left_after_finding_leaf(controller, monkeypatch):
    module, browser, request, folder = controller
    def left():
        if browser.at == 10:
            browser.at = 3
        else:
            browser.left()
    monkeypatch.setattr(module.ui, "left", left)
    run_controller(module, request)
    assert browser.loaded == [10]
    assert json.loads((folder / "response.json").read_text())["state"] == "sent"


def test_installed_preset_loads_without_generic_left_at_leaf(controller, monkeypatch):
    module, browser, request, folder = controller
    def captured_left():
        # Initial reset works, but the plugin captures generic keys once the target is selected.
        if browser.at != 10:
            browser.left()
    monkeypatch.setattr(module.ui, "left", captured_left)
    run_controller(module, request)
    assert browser.loaded == [10]
    assert json.loads((folder / "response.json").read_text())["state"] == "sent"


def test_browser_focus_failure_blocks_activation(controller, monkeypatch):
    module, browser, request, folder = controller
    monkeypatch.setattr(module.ui, "getFocused", lambda *_: False)
    run_controller(module, request)
    assert not browser.loaded
    message = json.loads((folder / "response.json").read_text())["message"]
    assert "lost keyboard focus" in message


def test_installed_leaf_search_stops_at_next_folder_despite_duplicate_name(controller):
    module, browser, request, folder = controller
    browser.nodes[5] = ("Different preset", 4)
    request["preset"]["kind"] = "Effects"
    request["preset"]["route"][2] = "Effects"
    (folder / "request.json").write_text(json.dumps(request))
    run_controller(module, request)
    assert not browser.loaded
    reply = json.loads((folder / "response.json").read_text())
    assert "Reached another folder" in reply["message"]


def test_matching_caption_on_non_fst_file_cannot_load(controller, monkeypatch):
    module, browser, request, folder = controller
    monkeypatch.setattr(module.ui, "getFocusedNodeFileType", lambda: (7 if browser.at == 10 else 4) if browser.at in (5, 8, 10) else -100)
    run_controller(module, request)
    assert not browser.loaded
    assert "not an FL preset" in json.loads((folder / "response.json").read_text())["message"]


def test_controller_failure_reaches_vext_log_with_traceback_and_no_duplicate(controller, monkeypatch):
    from app import logbuf
    module, _, request, folder = controller
    monkeypatch.setattr(launch, "BRIDGE_DIR", folder / "backend")
    launch._write_json(launch.BRIDGE_DIR / "config.json", {"directory": str(folder), "secret": "secret"})
    monkeypatch.setattr(launch, "_result", {"id": request["id"], "state": "pending", "expires": request["expires"]})
    logbuf.clear()
    request["preset"]["route"][2:4] = ["Effects", "VST"]
    (folder / "request.json").write_text(json.dumps(request))
    run_controller(module, request)
    result = launch.status()
    assert result["state"] == "error"
    launch.status()
    errors = [line for line in logbuf.recent() if line["source"] == "FL controller"]
    assert len(errors) == 1
    assert errors[0]["level"] == "error"
    assert "Exact preset" in errors[0]["text"]
    assert module.SCRIPT_BUILD in errors[0]["text"]
    assert "Traceback" in errors[0]["text"] and "RuntimeError" in errors[0]["text"]
    assert logbuf.status()["last_error"] == errors[0]["seq"]


def test_diagnostics_require_auth_are_redacted_and_stay_cleared(catalog):
    from app import logbuf
    conn, _, _, _ = catalog
    launch.install(conn, launch.setup_info(conn)["script_path"])
    config = launch._read_json(launch.BRIDGE_DIR / "config.json")
    event = {"id": str(time.time_ns()), "time": time.time(), "stage": "heartbeat",
             "message": "Failure " + config["secret"], "traceback": "Traceback " + config["secret"]}
    path = launch._exchange_file("diagnostics.json")
    logbuf.clear()
    launch._write_json(path, {"secret": "wrong", "events": [event]})
    launch.collect_logs()
    assert not [line for line in logbuf.recent() if line["source"] == "FL controller"]
    launch._write_json(path, {"secret": config["secret"], "events": [event]})
    launch.collect_logs()
    launch.collect_logs()
    lines = [line for line in logbuf.recent() if line["source"] == "FL controller"]
    assert len(lines) == 1 and "[redacted]" in lines[0]["text"]
    assert config["secret"] not in logbuf.as_text()
    logbuf.clear()
    launch.collect_logs()
    assert not logbuf.recent()


def test_timeout_is_logged_once(catalog, monkeypatch):
    from app import logbuf
    logbuf.clear()
    monkeypatch.setattr(launch, "_result", {"id": str(time.time_ns()), "state": "pending",
                                           "expires": time.time() - 1, "plugin": "Test synth"})
    assert launch.status()["state"] == "unknown"
    launch.status()
    lines = [line for line in logbuf.recent() if line["source"] == "FL controller"]
    assert len(lines) == 1 and "Test synth" in lines[0]["text"]
    assert lines[0]["level"] == "error"


def test_logs_api_collects_controller_errors_and_badge_before_opening_fl_setup(catalog):
    from fastapi.testclient import TestClient

    from app import logbuf, security, web
    conn, _, _, _ = catalog
    launch.install(conn, launch.setup_info(conn)["script_path"])
    config = launch._read_json(launch.BRIDGE_DIR / "config.json")
    event = {"id": str(time.time_ns()), "time": time.time(), "stage": "load", "message": "Script error fixture"}
    launch._write_json(launch._exchange_file("diagnostics.json"), {"secret": config["secret"], "events": [event]})
    logbuf.clear()
    client = TestClient(web.app, base_url="http://127.0.0.1")
    assert client.get("/api/logs/status").json()["last_error"] > 0
    lines = client.get("/api/logs").json()["lines"]
    assert any(line["source"] == "FL controller" and "Script error fixture" in line["text"] for line in lines)
    assert "Script error fixture" in client.get("/api/logs/text").text
    assert client.delete("/api/logs", headers={security.TOKEN_HEADER: security.TOKEN}).status_code == 200
    assert client.get("/api/logs").json()["lines"] == []


def test_controller_callback_failure_is_forwarded_to_diagnostics_without_traceback_spam(controller, monkeypatch):
    module, _, _, folder = controller
    def denied(_stamp):
        raise SystemError("FileIO fixture failure")
    monkeypatch.setattr(module, "_heartbeat", denied)
    for _ in range(3):
        module._heartbeat_at = 0
        module.OnIdle()
    diagnostics = json.loads((folder / "diagnostics.json").read_text())
    assert len(diagnostics["events"]) == 1
    assert "SystemError" in diagnostics["events"][0]["traceback"]


def add_plugin(conn, host, name, vendor="", fmt="VST3", status="ok", fl_detected=1):
    cur = conn.execute(
        "INSERT INTO plugins (name, normalized_name, host_path, format, scanned_at, vendor, plugin_type, status, fl_detected)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (name, name.lower(), str(host), fmt, "2026-11-02", vendor, "Instrument", status, fl_detected))
    conn.commit()
    return cur.lastrowid


def test_launcher_menu_and_lists_offer_only_launchable_fl_plugins(catalog, monkeypatch):
    conn, pid, installed, host = catalog
    db.seed_categories(conn)
    conn.execute("UPDATE plugins SET fl_detected=1, vendor='FabFilter' WHERE id=?", (pid,))
    db.set_plugin_categories(conn, pid, ["instrument-synth"])
    second = add_plugin(conn, host.with_name("Second.vst3"), "Second", vendor="FabFilter")
    db.set_plugin_categories(conn, second, ["instrument-synth"])
    add_plugin(conn, host.with_name("Arturia.vst3"), "Arturia Pro", vendor="Arturia")
    add_plugin(conn, host.with_name("Hidden.vst3"), "Hidden", vendor="Arturia", fl_detected=0)
    add_plugin(conn, host.with_name("Old.aaxplugin"), "Old AAX", vendor="Arturia", fmt="AAX")
    add_plugin(conn, host.with_name("Broken.vst3"), "Broken", vendor="Arturia", status="corrupted")
    quarantined = host.with_name("Quarantined.vst3")
    add_plugin(conn, quarantined, "Quarantined", vendor="Arturia")
    monkeypatch.setattr(db, "quarantined_host_paths", lambda conn: {str(quarantined).upper()})

    for name, binary in [("Exact preset", host), ("Second", host.with_name("Second.vst3")),
                         ("Arturia Pro", host.with_name("Arturia.vst3")),
                         ("Old AAX", host.with_name("Old.aaxplugin")),
                         ("Broken", host.with_name("Broken.vst3")), ("Quarantined", quarantined)]:
        preset(installed, binary, name)
    menu = launch.menu(conn)
    assert menu["vendors"] == [{"name": "Arturia", "count": 1}, {"name": "FabFilter", "count": 2}]
    assert {entry["slug"]: entry["count"] for entry in menu["categories"]} == {"fl-generators": 3, "instrument-synth": 2}

    listed = launch.plugins_for(conn, "vendor", "fabfilter")
    assert listed["title"] == "FabFilter"
    assert [p["name"] for p in listed["plugins"]] == ["Exact preset", "Second"]
    assert launch.plugins_for(conn, "category", "instrument-synth")["title"] == "Synth Instrument"
    assert len(launch.plugins_for(conn, "category", "instrument-synth")["plugins"]) == 2
    assert [p["name"] for p in launch.plugins_for(conn, "vendor", "Arturia")["plugins"]] == ["Arturia Pro"]
    with pytest.raises(ValueError, match="kind"):
        launch.plugins_for(conn, "format", "VST3")


def test_launcher_menu_is_not_configured_until_fl_and_controller_are_set_up(catalog, tmp_path):
    conn, _, _, _ = catalog
    fl_root = tmp_path / "FL Studio"
    db.set_setting(conn, "fl_studio_root", str(fl_root))
    menu = launch.menu(conn)
    assert menu["configured"] is False and "Install FL Studio" in menu["reason"]
    (fl_root / "Plugins").mkdir(parents=True)
    menu = launch.menu(conn)
    assert menu["configured"] is False and "Install the VEXT controller" in menu["reason"]
    launch.install(conn, launch.setup_info(conn)["script_path"])
    assert launch.menu(conn)["configured"] is True
    assert launch.menu(conn)["reason"] == ""
    Path(launch.setup_info(conn)["script_path"]).write_text("older controller", encoding="utf-8")
    menu = launch.menu(conn)
    assert menu["configured"] is False and "Update the VEXT controller" in menu["reason"]


def test_launcher_api_serves_menu_and_rejects_unknown_kind(catalog, monkeypatch):
    from fastapi.testclient import TestClient

    from app import fl_launch_api, plugin_host, web
    conn, pid, installed, host = catalog
    preset(installed, host, "Exact preset", section="Effects")
    database = conn.execute("PRAGMA database_list").fetchone()[2]
    monkeypatch.setattr(fl_launch_api, "DB_PATH", database)
    conn.execute("UPDATE plugins SET fl_detected=1, vendor='FabFilter', plugin_type='Effect' WHERE id=?", (pid,))
    conn.commit()
    image = Path(plugin_host.image_path(str(host)))
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(b"png fixture")
    client = TestClient(web.app, base_url="http://127.0.0.1")
    menu = client.get("/api/fl-launch/menu")
    assert menu.status_code == 200
    assert set(menu.json()) == {"configured", "reason", "categories", "vendors"}
    listed = client.get("/api/fl-launch/plugins", params={"kind": "vendor", "value": "FabFilter"})
    assert listed.status_code == 200
    assert listed.json() == {"title": "FabFilter", "plugins": [
        {"id": pid, "key": "Installed/Effects/VST3/Exact preset.fst", "name": "Exact preset", "vendor": "FabFilter", "plugin_type": "Effect",
         "format": "VST3", "has_image": True, "has_preset_image": False}]}
    assert client.get("/api/fl-launch/plugins", params={"kind": "format", "value": "VST3"}).status_code == 400


def test_launcher_includes_fst_without_catalog_or_nfo_and_excludes_catalog_only(catalog):
    conn, _, installed, _ = catalog
    fst = installed / "Effects" / "VST3" / "FL only.fst"
    fst.parent.mkdir(parents=True)
    fst.write_bytes(b"standalone FL fixture")
    items = launch.launchable(conn)
    assert len(items) == 1
    assert items[0]["id"] is None and items[0]["name"] == "FL only"
    assert items[0]["key"] == "Installed/Effects/VST3/FL only.fst"
    assert launch.plugins_for(conn, "vendor", "Unassigned")["plugins"][0]["key"] == items[0]["key"]
    selected = launch.select_preset(conn, None, items[0]["key"])
    assert selected["path"] == str(fst.resolve())
    assert selected["route"] == ["Plugin database", "Installed", "Effects", "VST3", "FL only"]
    fst.unlink()
    assert launch.launchable(conn) == []
    with pytest.raises(ValueError, match="missing"):
        launch.select_preset(conn, None, items[0]["key"])


def test_launcher_lists_only_installed_not_filed_copies(catalog):
    conn, _, installed, host = catalog
    source = preset(installed, host)
    filed = installed.parent / "Generators" / "Vendor" / source.name
    filed.parent.mkdir(parents=True)
    filed.write_bytes(source.read_bytes())
    changed = installed.parent / "Generators" / "Vendor" / "Changed" / source.name
    changed.parent.mkdir(parents=True)
    changed.write_bytes(b"different same-name preset")
    items = launch.launchable(conn)
    assert [item["key"] for item in items] == ["Installed/Generators/VST3/Exact preset.fst"]
    assert not any(item["key"].startswith(("Effects/", "Generators/")) for item in items)


@pytest.fixture
def filed_cache():
    """_filed_pictures caches each root for 10 s; every case starts and ends with an empty cache."""
    launch._filed_cache.clear()
    yield
    launch._filed_cache.clear()


def installed_plain(installed, section, fmt, name):
    fst = installed / section / fmt / (name + ".fst")
    fst.parent.mkdir(parents=True, exist_ok=True)
    fst.write_bytes(b"installed fixture")
    return fst


def filed_plain(root, section, folder, name):
    fst = root / section / folder / (name + ".fst")
    fst.parent.mkdir(parents=True, exist_ok=True)
    fst.write_bytes(b"filed fixture")
    return fst


def test_filed_copy_picture_serves_installed_preset_without_catalog_match(catalog, monkeypatch, filed_cache):
    from fastapi.testclient import TestClient

    from app import fl_launch_api, web
    conn, _, installed, _ = catalog
    root = installed.parent.resolve()
    key = "Installed/Effects/VST3/Unmatched.fst"
    installed_plain(installed, "Effects", "VST3", "Unmatched")
    filed = filed_plain(root, "Effects", "Vendor", "Unmatched")
    picture = filed.with_suffix(".png")
    picture.write_bytes(b"png fixture")
    items = launch.launchable(conn)
    assert [item["key"] for item in items] == [key]
    assert items[0]["has_filed_image"] is True
    assert items[0]["has_preset_image"] is False
    assert launch.preset_image(conn, key) == picture.resolve()
    database = conn.execute("PRAGMA database_list").fetchone()[2]
    monkeypatch.setattr(fl_launch_api, "DB_PATH", database)
    client = TestClient(web.app, base_url="http://127.0.0.1")
    response = client.get("/api/fl-launch/preset-image", params={"key": key})
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content == b"png fixture"


def test_filed_nfo_bitmap_names_the_filed_picture(catalog, filed_cache):
    conn, _, installed, _ = catalog
    root = installed.parent.resolve()
    key = "Installed/Effects/VST3/Named.fst"
    installed_plain(installed, "Effects", "VST3", "Named")
    filed = filed_plain(root, "Effects", "Vendor", "Named")
    filed.with_suffix(".nfo").write_text("Bitmap=other.png\n", encoding="utf-8")
    filed.with_suffix(".png").write_bytes(b"decoy picture")
    other = filed.with_name("other.png")
    other.write_bytes(b"named picture")
    assert launch.preset_image(conn, key) == other.resolve()
    assert launch.launchable(conn)[0]["has_filed_image"] is True


def test_same_stem_filed_copies_with_different_pictures_are_ambiguous(catalog, filed_cache):
    conn, _, installed, _ = catalog
    root = installed.parent.resolve()
    key = "Installed/Effects/VST3/Dup.fst"
    installed_plain(installed, "Effects", "VST3", "Dup")
    for folder in ("VendorA", "VendorB"):
        filed_plain(root, "Effects", folder, "Dup").with_suffix(".png").write_bytes(folder.encode())
    assert ("Effects", "dup") not in launch._filed_pictures(root)
    assert launch.launchable(conn)[0]["has_filed_image"] is False
    assert launch.preset_image(conn, key) is None


def test_own_installed_picture_wins_over_filed_copy(catalog, filed_cache):
    conn, _, installed, _ = catalog
    root = installed.parent.resolve()
    key = "Installed/Effects/VST3/Own.fst"
    own = installed_plain(installed, "Effects", "VST3", "Own")
    own.with_suffix(".png").write_bytes(b"own picture")
    filed_plain(root, "Effects", "Vendor", "Own").with_suffix(".png").write_bytes(b"filed picture")
    assert launch.preset_image(conn, key) == own.with_suffix(".png").resolve()
    item = launch.launchable(conn)[0]
    assert item["has_preset_image"] is True
    assert item["has_filed_image"] is True


def test_filed_bitmap_outside_its_section_is_ignored(catalog, filed_cache):
    conn, _, installed, _ = catalog
    root = installed.parent.resolve()
    key = "Installed/Effects/VST3/Escape.fst"
    installed_plain(installed, "Effects", "VST3", "Escape")
    (root / "outside.png").write_bytes(b"external picture")
    filed = filed_plain(root, "Effects", "Vendor", "Escape")
    filed.with_suffix(".nfo").write_text("Bitmap=..\\..\\outside.png\n", encoding="utf-8")
    filed.with_suffix(".png").write_bytes(b"decoy picture")
    assert launch.launchable(conn)[0]["has_filed_image"] is False
    assert launch.preset_image(conn, key) is None


def test_filed_copy_only_serves_the_same_section(catalog, filed_cache):
    conn, _, installed, _ = catalog
    root = installed.parent.resolve()
    installed_plain(installed, "Effects", "VST3", "Section")
    installed_plain(installed, "Generators", "VST3", "Section")
    filed_plain(root, "Generators", "Vendor", "Section").with_suffix(".png").write_bytes(b"generator picture")
    flags = {item["key"]: item["has_filed_image"] for item in launch.launchable(conn)}
    assert flags == {"Installed/Effects/VST3/Section.fst": False, "Installed/Generators/VST3/Section.fst": True}
    assert launch.preset_image(conn, "Installed/Effects/VST3/Section.fst") is None
    assert launch.preset_image(conn, "Installed/Generators/VST3/Section.fst") == \
        (root / "Generators" / "Vendor" / "Section.png").resolve()


def test_filed_pictures_cache_lasts_ten_seconds(catalog, filed_cache):
    _, _, installed, _ = catalog
    root = installed.parent.resolve()
    first = launch._filed_pictures(root)
    assert first == {}
    filed = filed_plain(root, "Effects", "Vendor", "Late")
    filed.with_suffix(".png").write_bytes(b"late picture")
    assert launch._filed_pictures(root) == {}, "A new filed picture waits for the 10 s cache"
    launch._filed_cache[str(root)] = (launch.time.monotonic() - 11, first)
    assert launch._filed_pictures(root) == {("Effects", "late"): filed.with_suffix(".png").resolve()}


@pytest.mark.parametrize("key", ["../other.fst", "Effects/../../other.fst", "Mixer/Chain.fst",
                                  "Installed/Generators/New/Unknown.fst", "Effects/Bad.dll", "",
                                  "Effects/Custom/FL only.fst"])
def test_direct_fst_rejects_escape_and_nonplugin_trees(catalog, key):
    conn, _, installed, _ = catalog
    filed = installed.parent / "Effects" / "Custom" / "FL only.fst"
    filed.parent.mkdir(parents=True, exist_ok=True)
    filed.write_bytes(b"filed FL fixture")
    with pytest.raises(ValueError):
        launch.select_preset(conn, None, key)


def test_direct_fst_blocked_catalog_binary_cannot_bypass_quarantine(catalog, monkeypatch):
    conn, _, installed, host = catalog
    preset(installed, host)
    monkeypatch.setattr(db, "quarantined_host_paths", lambda conn: [str(host)])
    assert launch.launchable(conn) == []
    with pytest.raises(ValueError, match="quarantined"):
        launch.select_preset(conn, None, "Installed/Generators/VST3/Exact preset.fst")


def test_preset_thumbnail_stays_inside_fl_database(catalog):
    conn, _, installed, host = catalog
    fst = preset(installed, host)
    image = fst.with_suffix(".png")
    image.write_bytes(b"png fixture")
    key = "Installed/Generators/VST3/Exact preset.fst"
    assert launch.preset_image(conn, key) == image.resolve()
    fst.with_suffix(".nfo").write_text("Bitmap=../../../../outside.png")
    outside = installed.parent.parent / "outside.png"
    outside.write_bytes(b"external png")
    assert launch.preset_image(conn, key) is None


def test_filed_mirror_cannot_bypass_blocked_installed_preset(catalog, monkeypatch):
    conn, _, installed, host = catalog
    source = preset(installed, host)
    mirror = installed.parent / "Generators" / "Vendor" / source.name
    mirror.parent.mkdir(parents=True)
    mirror.write_bytes(source.read_bytes())
    monkeypatch.setattr(db, "quarantined_host_paths", lambda conn: [str(host)])
    assert launch.launchable(conn) == []
    with pytest.raises(ValueError, match="Installed plugin database"):
        launch.select_preset(conn, None, "Generators/Vendor/Exact preset.fst")
    with pytest.raises(ValueError, match="quarantined"):
        launch.select_preset(conn, None, "Installed/Generators/VST3/Exact preset.fst")


def test_fst_only_api_returns_exact_key_and_revalidates_after_removal(catalog, monkeypatch):
    from fastapi.testclient import TestClient

    from app import fl_launch_api, web

    conn, _, installed, _ = catalog
    database = conn.execute("PRAGMA database_list").fetchone()[2]
    monkeypatch.setattr(fl_launch_api, "DB_PATH", database)
    fst = installed / "Effects" / "VST3" / "Uncatalogued.fst"
    fst.parent.mkdir(parents=True)
    fst.write_bytes(b"FL-only fixture")
    filed = installed.parent / "Effects" / "Uncatalogued.fst"
    filed.parent.mkdir(parents=True)
    filed.write_bytes(b"FL-only fixture")
    client = TestClient(web.app, base_url="http://127.0.0.1")
    response = client.get("/api/fl-launch/preset", params={"key": "Installed/Effects/VST3/Uncatalogued.fst"})
    assert response.status_code == 200
    assert response.json()["key"] == "Installed/Effects/VST3/Uncatalogued.fst"
    rejected = client.get("/api/fl-launch/preset", params={"key": "Effects/Uncatalogued.fst"})
    assert rejected.status_code == 409
    assert "Installed plugin database" in rejected.json()["detail"]
    fst.unlink()
    assert client.get("/api/fl-launch/preset", params={"key": "Installed/Effects/VST3/Uncatalogued.fst"}).status_code == 409


def test_launcher_reuses_listing_and_digests_but_invalidates_file_and_catalog_edits(catalog, monkeypatch):
    conn, pid, installed, host = catalog
    fst = preset(installed, host)
    builds = []
    original = launch._build_listing
    def counted(*args):
        builds.append(1)
        return original(*args)
    monkeypatch.setattr(launch, "_build_listing", counted)
    launch.menu(conn, include_plugins=True)
    launch.plugins_for(conn, "category", "fl-generators")
    assert len(builds) == 1, "Opening the plugin list must reuse the menu's listing"
    conn.execute("UPDATE plugins SET vendor='Changed vendor' WHERE id=?", (pid,))
    assert launch.plugins_for(conn, "vendor", "Changed vendor")["plugins"]
    assert len(builds) == 2
    digest = launch._fst_digest(fst)
    fst.write_bytes(b"changed preset bytes")
    assert launch._fst_digest(fst) != digest
    launch.menu(conn)
    assert len(builds) == 3
    fst.unlink()
    assert launch.launchable(conn) == []


def test_menu_snapshot_contains_exact_fst_keys_for_immediate_launcher_paint(catalog):
    conn, _, installed, host = catalog
    preset(installed, host)
    menu = launch.menu(conn, include_plugins=True)
    assert menu["plugins"][0]["key"] == "Installed/Generators/VST3/Exact preset.fst"
    assert ("Generators", "fl-generators") in menu["plugins"][0]["categories"]
    assert "plugins" not in launch.menu(conn)
