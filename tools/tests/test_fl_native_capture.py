"""Regression checks for explicitly confirmed FL-native picture capture.

Uses fake catalog rows, workers and temporary VEXT data; it never loads an FL plugin.
Run: .venv-native\\Scripts\\python.exe tools\\tests\\test_fl_native_capture.py
"""
import os
import argparse
from pathlib import Path
import sys
import tempfile
import time
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
WORK = Path(tempfile.mkdtemp(prefix="vext-native-capture-test-"))
for key in ("VEXT_ROOT", "LOCALAPPDATA", "APPDATA", "USERPROFILE"):
    os.environ[key] = str(WORK / key)
os.environ["VEXT_TOKEN"] = "native-capture-test"
sys.path.insert(0, str(ROOT))

from app import fl_native, host_worker, plugin_host  # noqa: E402


def row(plugin_id=1, fmt="FL-NATIVE"):
    return {"id": plugin_id, "name": "Native test", "vendor": "Fixture", "format": fmt,
            "host_path": str(WORK / "plugin"), "status": "ok", "status_reason": None}


def wait_job(timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        status = plugin_host.pictures_status()
        if not status["running"]:
            return status
        time.sleep(.01)
    raise AssertionError("picture job did not finish")


def case_native_default_denied_and_explicit_opt_in():
    seen = []

    class NoSpares:
        def __init__(self, _): pass
        def fill(self): pass
        def close(self): pass

    def fake_capture(r, spares=None, hint=None, native_capture=False):
        seen.append((r["format"], spares, native_capture))
        return {"ok": True, "reason": None, "code": None, "method": "screen"}

    with mock.patch.object(plugin_host, "_Spares", NoSpares), \
            mock.patch.object(plugin_host, "_capture_one", side_effect=fake_capture):
        plugin_host.pictures_start([row()])
        default = wait_job()
        default_seen = list(seen)
        # The opt-in parameter is asserted at the job boundary, where the UI confirm is enforced.
        plugin_host.pictures_start([row(2)], include_native=True)
        explicit = wait_job()
    return (default["total"] == 0 and not default_seen and explicit["total"] == 1
            and seen and seen[0][0] == "FL-NATIVE" and seen[0][1] is None and seen[0][2] is True), \
        f"default total={default['total']}; explicit total={explicit['total']}; calls={seen}"


class FakeProc:
    def __init__(self, events, code=0):
        import json
        self.stdout = [json.dumps({"event": event, **data}) for event, data in events]
        self.stdin = mock.Mock()
        self.code = code
    def poll(self): return self.code
    def wait(self, timeout=None): return self.code


def case_native_cold_worker_and_workspace_cleanup():
    workspace = str(WORK / "workspaces" / ("a" * 32))
    proc = FakeProc([("loaded", {"load_s": .1}), ("captured_auto", {"method": "screen", "seconds": .2})])
    calls = {}
    def launch(*_): return "C:/fake/native.dll", ["--format", "FL-NATIVE", "--native-data", workspace]
    def spawn(path, args):
        calls["args"] = args
        return proc, None
    with mock.patch.object(plugin_host, "launch_target", side_effect=launch), \
            mock.patch.object(plugin_host, "image_path", return_value=str(WORK / "out.png")), \
            mock.patch.object(plugin_host, "_spawn_worker", side_effect=spawn), \
            mock.patch.object(plugin_host, "worker_arch", return_value="x64"), \
            mock.patch.object(plugin_host, "kill_tree"), \
            mock.patch.object(plugin_host, "memory_reason", return_value=None), \
            mock.patch.object(fl_native, "cleanup") as cleanup:
        result = plugin_host._capture_one(row(), spares=mock.Mock(take=mock.Mock(side_effect=AssertionError("native used warm spare"))),
                                          native_capture=True)
    argv = calls.get("args", [])
    return (result and result["ok"] and "--native-capture" in argv and "--capture" in argv
            and cleanup.call_args_list == [mock.call(workspace)]), \
        f"result={result}, args={argv}, cleanup={cleanup.call_args_list}"


def case_native_workspace_cleanup_after_spawn_error_and_cancel():
    workspace = str(WORK / "workspaces" / ("b" * 32))
    def launch(*_): return "C:/fake/native.dll", ["--format", "FL-NATIVE", "--native-data", workspace]
    base = [mock.patch.object(plugin_host, "launch_target", side_effect=launch),
            mock.patch.object(plugin_host, "image_path", return_value=str(WORK / "out.png")),
            mock.patch.object(plugin_host, "kill_tree"),
            mock.patch.object(plugin_host, "memory_reason", return_value=None)]
    # A worker launch failure happens after launch_target has allocated an isolated workspace.
    with base[0], base[1], mock.patch.object(plugin_host, "_spawn_worker", side_effect=OSError("spawn failed")), \
            mock.patch.object(fl_native, "cleanup") as cleanup:
        try:
            plugin_host._capture_one(row(), native_capture=True)
        except OSError:
            pass
        else:
            raise AssertionError("spawn failure was swallowed")
        spawn_cleaned = cleanup.call_args_list == [mock.call(workspace)]

    proc = FakeProc([("loaded", {"load_s": .1})])
    with base[0], base[1], mock.patch.object(plugin_host, "_spawn_worker", return_value=(proc, None)), \
            base[2], base[3], mock.patch.object(fl_native, "cleanup") as cleanup:
        old_cancelled = plugin_host._pictures["cancelled"]
        plugin_host._pictures["cancelled"] = True
        try:
            result = plugin_host._capture_one(row(), native_capture=True)
        finally:
            plugin_host._pictures["cancelled"] = old_cancelled
    return spawn_cleaned and result is None and cleanup.call_args_list == [mock.call(workspace)], \
        f"spawn-cleaned={spawn_cleaned}, cancellation result={result}, cleanup={cleanup.call_args_list}"


def case_native_worker_probe_and_generic_capture_rejected():
    events = []
    with mock.patch.object(host_worker, "_prepare"), \
            mock.patch.object(host_worker, "emit", side_effect=lambda event, **kw: events.append((event, kw))), \
            mock.patch("app.fl_native_worker.run", side_effect=AssertionError("unexpected native worker")):
        probe = host_worker.main("fake.dll", "--format", "FL-NATIVE", "--probe")
        probe_events = list(events)
        events.clear()
        generic = host_worker.main("fake.dll", "--format", "FL-NATIVE", "--capture", "fake.png")
    return (probe != 0 and generic != 0 and any(e[0] == "closed" for e in probe_events)
            and any(e[0] == "closed" for e in events)
            and any(e[1].get("code") == "native_unavailable" for e in probe_events)
            and any(e[1].get("code") == "native_unavailable" for e in events)), \
        f"probe={probe_events}; generic={events}"


def case_only_explicit_native_capture_reaches_editor_worker():
    events = []
    opts_seen = []
    def run(_path, opts, emit):
        opts_seen.append(opts)
        return 0
    with mock.patch.object(host_worker, "_prepare"), \
            mock.patch.object(host_worker, "emit", side_effect=lambda event, **kw: events.append((event, kw))), \
            mock.patch("app.fl_native_worker.run", side_effect=run):
        code = host_worker.main("fake.dll", "--format", "FL-NATIVE", "--capture", "out.png", "--native-capture")
    return (code == 0 and len(opts_seen) == 1 and opts_seen[0].get("capture") == "out.png"
            and opts_seen[0].get("native_capture") is True), f"code={code}, opts={opts_seen}, events={events}"


def case_voltage_controller_denied_before_loading():
    import hashlib
    source = next(p for p in fl_native.compatibility().values() if p["name"] == "Fruity Voltage Controller")
    parts = Path(source["relative"]).parts
    folder = WORK / "voltage-fixture" / "Plugins" / "Fruity" / parts[0] / parts[1]
    folder.mkdir(parents=True)
    binary = folder / Path(source["relative"]).name
    binary.write_bytes(b"fixture voltage binary")
    blocked = {**source, "sha256": hashlib.sha256(binary.read_bytes()).hexdigest()}
    with mock.patch.object(fl_native, "compatibility", return_value={blocked["relative"].casefold(): blocked}), \
            mock.patch.object(fl_native, "adapter_path", side_effect=AssertionError("adapter reached before gate")):
        try:
            fl_native.resolve(str(folder), refresh=True)
        except fl_native.NativeUnavailable as exc:
            return (not source["editor"] and source["reason"] in str(exc)), str(exc)
    return False, "the voltage controller was not denied"


def case_authenticated_picture_api_forwards_explicit_opt_in():
    from contextlib import contextmanager
    from fastapi.testclient import TestClient
    from app import db, security, web
    @contextmanager
    def connect(_path):
        yield object()
    rows = [row()]
    started = {"running": True, "total": 1}
    with mock.patch.object(db, "connect", side_effect=connect), \
            mock.patch.object(db, "all_plugins", return_value=rows), \
            mock.patch.object(db, "quarantined_host_paths", return_value=set()), \
            mock.patch.object(web, "_hostable_rows", return_value=rows), \
            mock.patch.object(plugin_host, "pictures_start", return_value=started) as start:
        client = TestClient(web.app, base_url="http://127.0.0.1:8000",
                            headers={security.TOKEN_HEADER: security.TOKEN})
        default = client.post("/api/pictures/capture", json={"only_missing": False})
        explicit = client.post("/api/pictures/capture", json={"only_missing": False, "include_native": True})
    calls = [call.kwargs.get("include_native") for call in start.call_args_list]
    return (default.status_code == 200 and explicit.status_code == 200 and calls == [False, True]
            and all(call.kwargs.get("on_saved") is not None for call in start.call_args_list)), \
        f"statuses={default.status_code}/{explicit.status_code}, calls={start.call_args_list}"


CASES = [
    ("native capture is excluded by default and requires explicit opt-in", case_native_default_denied_and_explicit_opt_in),
    ("native capture uses a cold worker and cleans its isolated workspace", case_native_cold_worker_and_workspace_cleanup),
    ("native workspace is cleaned after spawn errors and cancellation", case_native_workspace_cleanup_after_spawn_error_and_cancel),
    ("native probes and generic capture remain rejected", case_native_worker_probe_and_generic_capture_rejected),
    ("only the private native capture flag reaches the editor worker", case_only_explicit_native_capture_reaches_editor_worker),
    ("Voltage Controller is denied before the native adapter loads", case_voltage_controller_denied_before_loading),
    ("authenticated Picture API forwards only the explicit include_native choice", case_authenticated_picture_api_forwards_explicit_opt_in),
]


def installed_acceptance(installed_root, backend_exe=None):
    """Optional manual acceptance using real installed binaries but disposable VEXT state.

    `installed_root` is FL Studio's `Plugins/Fruity` folder.
    This is intentionally opt-in; normal unit runs never load native binaries.
    """
    root = Path(installed_root).resolve()
    names = ["Fruity Balance", "Fruit Kick", "Newtone", "Fruity Big Clock"]
    formats = ["Effects", "Generators", "Effects", "Effects"]
    rows = []
    for i, (kind, name) in enumerate(zip(formats, names), 100):
        folder = root / kind / name
        if not folder.is_dir():
            raise AssertionError(f"installed acceptance plugin is missing: {folder}")
        rows.append({"id": i, "name": name, "vendor": "Image-Line", "format": "FL-NATIVE",
                     "host_path": str(folder), "status": "ok", "status_reason": None})
    from app import db
    from app.web import DB_PATH
    db.init_db(DB_PATH)
    if backend_exe:
        exe = Path(backend_exe).resolve()
        if not exe.is_file():
            raise AssertionError(f"packaged backend does not exist: {exe}")
        plugin_host.worker_command = lambda path, argv: [str(exe), "--host-worker", path, *argv]
        # This job is editor-only, and must never open a physical playback device.
        plugin_host._Session.start_sound = lambda self: None
    before = plugin_host.active_tap()
    plugin_host.pictures_start(rows, include_native=True)
    status = wait_job(120)
    images = {r["name"]: Path(plugin_host.image_path(r["host_path"])) for r in rows}
    from PIL import Image
    for name, image in images.items():
        if not image.is_file():
            raise AssertionError(f"{name}: no picture was saved; status={status}")
        with Image.open(image) as im:
            im.load()
            colors = im.convert("RGB").getcolors(maxcolors=1_000_000)
            if im.width < 100 or im.height < 80 or colors is None or len(colors) < 8:
                raise AssertionError(f"{name}: capture is blank or icon-sized ({im.size})")
    if status["ok"] != len(rows) or status["failed"] or status["running"]:
        raise AssertionError(f"native capture job did not fully pass: {status}")
    if plugin_host.active_tap() != before or plugin_host.sessions_busy():
        raise AssertionError("picture capture left an Analyzer tap or Playground MIDI owner active")
    leftovers = [p for p in fl_native.WORKSPACES.glob("*") if p.exists()]
    if leftovers:
        raise AssertionError(f"native workspaces were not cleaned: {leftovers}")
    print(f"PASS  installed acceptance: {', '.join(names)} captured; Voltage Controller pre-load gate covered by unit fixture")


def main():
    failed = 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installed-root", help="FL Studio Plugins/Fruity directory for a real capture acceptance run")
    parser.add_argument("--backend-exe", help="optional packaged vext-backend.exe for a frozen-worker capture run")
    args = parser.parse_args()
    if args.backend_exe and not args.installed_root:
        parser.error("--backend-exe requires --installed-root")
    try:
        for title, case in CASES:
            try:
                passed, detail = case()
            except Exception as exc:  # report each regression independently
                passed, detail = False, f"{type(exc).__name__}: {exc}"
            print(f"{'PASS' if passed else 'FAIL'}  {title}\n      {detail}", flush=True)
            failed += not passed
        if args.installed_root and not failed:
            try:
                installed_acceptance(args.installed_root, args.backend_exe)
            except Exception as exc:
                failed += 1
                print(f"FAIL  installed acceptance\n      {type(exc).__name__}: {exc}", flush=True)
    finally:
        import shutil
        shutil.rmtree(WORK, ignore_errors=True)
    print(f"{len(CASES) - failed} of {len(CASES)} passed")
    return bool(failed)


if __name__ == "__main__":
    raise SystemExit(main())
