"""Eligibility, isolation and API capability guards use only temporary fake data."""
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import threading
from unittest import mock

work = tempfile.TemporaryDirectory(prefix="vext-native-test-")
root = Path(work.name)
for key in ("VEXT_ROOT", "LOCALAPPDATA", "APPDATA", "USERPROFILE"):
    os.environ[key] = str(root / key)
os.environ["VEXT_TOKEN"] = "native-editor-test"
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app import fl_native as native, plugin_host as host

# Every limited native entry has actionable presentation data, separate from grants.
entries = list(native.compatibility().values())
for item in entries:
    notices = native.limitations(item)
    assert item.get("audio") or notices, item["relative"]
    for notice in notices:
        assert notice["status"] in {"out_of_scope", "pending", "limited", "unverified", "by_design"}
        assert notice["feature"] and notice["message"].strip(), item["relative"]
    if notices:
        notices[0]["message"] = "changed in caller"
        assert native.limitations(item)[0]["message"] != "changed in caller"
    if not item["editor"]:
        assert item.get("reason"), item["relative"]
for name in ("Fruity HTML NoteBook", "ReWired", "MIDI Out", "Razer Chroma"):
    item = next(e for e in entries if e["name"] == name)
    assert not any(item.get(key) for key in ("audio", "midi", "clock")), name
    assert any(n["status"] == "out_of_scope" for n in native.limitations(item)), name
print("PASS: every limited plugin has classified warnings; scope notices do not grant capabilities")

# The Mobile workspace must not acquire unrelated catalog/settings/audio files
# just because they share an extension with an editor dependency.
mobile_source = root / "resources/Generators/FL Studio Mobile/Installed"
mobile_files = ("SampleVertexShader.cso", "SamplePixelShader.cso", "TexturePixelShader.cso",
                "CirclePixelShader.cso", "FeatherPixelShader.cso", "FontPixelShader.cso",
                "title.png", "wrap.png", "gamepad.png", "gamepad_fill.png",
                "oscdata.dat", "FilterTable_Ctf_Res.data", "Fonts/OpenSans_SemiCondensed-SemiBold.ttf")
for name in (*mobile_files, "samplesList.data", "settings.dat", "sample.wav", "engine.dll"):
    resource = mobile_source / name
    resource.parent.mkdir(parents=True, exist_ok=True)
    resource.write_bytes(name.encode())
mobile_workspace = root / "resource-workspace"
native.prepare_resources(mobile_workspace, root / "resources",
                         {"relative": "Generators/FL Studio Mobile/FL Studio Mobile_x64.dll"})
copied = mobile_workspace / "path-selector-1048"
assert {p.relative_to(copied).as_posix() for p in copied.rglob("*") if p.is_file()} == set(mobile_files)
print("PASS: Mobile editor resource allowlist excludes catalogs, settings, samples and binaries")

# Display-name matches must never populate an executable nested-preset menu.
patcher_factory = root / "patcher-FL/Plugins/Fruity"
patcher_database = root / "patcher-FL/Data/Patches/Plugin database"
for section in ("Generators", "Effects"):
    candidate = patcher_database / section / "Nested" / "Example.fst"
    candidate.parent.mkdir(parents=True)
    candidate.write_bytes(b"unverified graph with a validated display name")
validated = {"example": {"name": "Example", "audio": True, "instrument": True}}
with mock.patch.object(native, "compatibility", return_value=validated):
    for section in ("Generators", "Effects"):
        workspace = root / ("patcher-workspace-" + section)
        native.prepare_resources(workspace, patcher_factory,
                                 {"relative": section + "/Patcher/Patcher_x64.dll"})
        for kind in ("generators", "effects"):
            picker = workspace / "patcher-list" / kind
            assert picker.is_dir() and not list(picker.iterdir())
assert all(p.read_bytes() == b"unverified graph with a validated display name"
           for p in patcher_database.rglob("*.fst"))
print("PASS: both Patcher menus exclude name-matched, unverified preset graphs")

folder = root / "FL/Plugins/Fruity/Effects/Example"
folder.mkdir(parents=True)
binary = folder / "Example_x64.dll"
binary.write_bytes(b"fake-native-binary")
adapter = root / "adapter.dll"
adapter.write_bytes(b"fake-adapter")
entry = {"relative": "Effects/Example/Example_x64.dll", "name": "Example", "instrument": False,
         "editor": True, "sha256": hashlib.sha256(binary.read_bytes()).hexdigest()}
row = {"format": "FL-NATIVE", "host_path": str(folder), "name": "Example", "bitness": None,
       "status": "ok", "status_reason": None}


def refused(fn, text):
    try:
        fn()
    except (native.NativeUnavailable, host.HostError, ValueError) as exc:
        assert text in str(exc), str(exc)
    else:
        raise AssertionError("operation was not refused")


with mock.patch.object(native, "compatibility", return_value={entry["relative"].casefold(): entry}), \
        mock.patch.object(native, "adapter_path", return_value=adapter):
    assert native.resolve(str(folder))[0] == binary
    assert native.resolve(str(binary), refresh=True)[0] == binary
    (folder / "engine_x64.dll").write_bytes(b"component")
    refused(lambda: native.resolve(str(folder / "engine_x64.dll")), "not this plugin")
    assert host.host_issue(row) is None
    host.record_result(str(folder), False, "failed", "Old native adapter crash", native_adapter=native.ADAPTER_VERSION - 1)
    assert host.host_issue(row) is None
    host.record_result(str(folder), False, "failed", "Current native adapter crash", native_adapter=native.ADAPTER_VERSION)
    assert host.host_issue(row)["reason"] == "Current native adapter crash"
    host.record_result(str(folder), True, native_adapter=native.ADAPTER_VERSION)
    assert host.host_issue({**row, "status": "corrupted"})["code"] == "flagged"
    assert host.host_issue(row, {str(folder)})["code"] == "quarantined"
    path, args = host.launch_target(str(folder), "FL-NATIVE")
    assert path == str(binary) and "--native-data" in args
    native.workspace_path(args[args.index("--native-data") + 1])
    binary.write_bytes(b"another-native-binary")
    refused(lambda: native.resolve(str(folder), refresh=True), "version has not been validated")
    binary.write_bytes(b"fake-native-binary")
    with mock.patch.dict(entry, editor=False, reason="Known unsupported runtime"):
        refused(lambda: native.resolve(str(folder), refresh=True), "Known unsupported runtime")

refused(lambda: native.workspace_path(str(root)), "invalid native editor workspace")
refused(lambda: native.workspace_path(str(native.WORKSPACES / ".." / ("a" * 32))), "invalid native editor workspace")

session = mock.Mock()
session.tap_path = None
session.alive.return_value = True
session.snapshot.return_value = {**host._CLOSED, "state": "open", "editor_only": True}
with mock.patch.object(host, "_session", return_value=session):
    for fn in (lambda: host.play(1, "fake.wav"), lambda: host.set_source(1, "browser"),
               lambda: host.send_midi(1, [[0x90, 60, 100]]), lambda: host.record(1, True),
               lambda: host.set_gain(1, in_db=1), lambda: host.bypass(1, True),
               lambda: host.set_sound(1, True)):
        refused(fn, "editor only")
session.command.assert_not_called()
session.snapshot.return_value["clock_only"] = True
with mock.patch.object(host, "_session", return_value=session):
    refused(lambda: host.play(1, "fake.wav"), "editor only")
session.command.assert_not_called()
with mock.patch.dict(host._sessions, {1: session}, clear=True):
    assert not host.sessions_busy()
    assert host.active_tap() is None

# The same guard is enforced by the authenticated API, not just button state.
from fastapi.testclient import TestClient
from app import web

client = TestClient(web.app, base_url="http://127.0.0.1:8000")
with mock.patch.object(host, "_session", return_value=session), \
        mock.patch.object(web, "_hostable_plugin", return_value=row), \
        mock.patch.object(web, "_playback_settings", return_value={}):
    response = client.post("/api/plugins/1/host/source", json={"kind": "browser"})
    assert response.status_code == 403
    response = client.post("/api/plugins/1/host/source", json={"kind": "browser"}, headers={"X-VEXT-Token": "native-editor-test"})
    assert response.status_code == 409 and "editor only" in response.json()["detail"]

# Monitor controls are authenticated and cannot be used to bypass capability gates.
session.snapshot.return_value = {**host._CLOSED, "state": "open", "editor_only": False}
with mock.patch.object(host, "_session", return_value=session):
    refused(lambda: host.set_audition(1, True), "does not support editor audition")
session.snapshot.return_value["native_audition"] = True
with mock.patch.object(host, "_session", return_value=session), \
        mock.patch.object(host, "_audio_settings", return_value={"samplerate": 48000}):
    host.set_audition(1, True)
    session.command.assert_called_with("audition", timeout=15, on=True, samplerate=48000)
    refused(lambda: host.set_source(1, "browser"), "editor's file")
    refused(lambda: host.set_source(1, "file"), "editor's file")
with mock.patch.object(host, "_session", return_value=session):
    assert client.post("/api/plugins/1/host/audition", json={"on": True}).status_code == 403
assert {e["name"] for e in entries if e.get("audition")} == {"Newtime", "Newtone"}
print("PASS: monitor capability, source and API-auth guards")

# Audio-enabled native effects use the regular source commands, without taking MIDI.
session.snapshot.return_value = {**host._CLOSED, "state": "open", "format": "FL-NATIVE",
                                 "editor_only": False, "external_editor": False}
session.lock = threading.RLock()
session.state = session.snapshot.return_value
session.launch_args = ["--format", "FL-NATIVE"]
with mock.patch.object(host, "_session", return_value=session):
    host.set_source(1, "browser", audio={"samplerate": 44100})
    session.command.assert_called_with("live", timeout=10, on=True, rate=44100, samplerate=44100)
    refused(lambda: host.send_midi(1, [[0x90, 60, 100]]), "doesn't take MIDI")
    with mock.patch.object(host, "_capture_to") as capture_to:
        host.capture(1, str(folder), "Example")
        capture_to.assert_called_once()
with mock.patch.dict(host._sessions, {1: session}, clear=True):
    assert not host.sessions_busy()  # the toolbar's MIDI inputs remain available
    before = session.command.call_count
    with mock.patch.object(host, "_midi_settings", return_value={"inputs": "", "channel": 0}):
        host.apply_midi_settings()
    assert session.command.call_count == before

# Native instruments own musical inputs and receive settings like other synths.
session.snapshot.return_value.update(is_instrument=True, accepts_midi=True)
with mock.patch.dict(host._sessions, {1: session}, clear=True):
    assert host.sessions_busy()
    with mock.patch.object(host, "_midi_settings", return_value={"inputs": "", "channel": 0}):
        host.apply_midi_settings()
    session.command.assert_called_with("midi_inputs", timeout=10, inputs="", channel=0,
                                       block_pitchbend=False, reserved=[])

# No bulk job can launch a native editor. Avoid invoking any real worker.
with mock.patch.object(host, "_probe", side_effect=AssertionError("native probe launched")):
    state = host.check_start([row])
    assert state["total"] == 0

print("PASS: native identity, version/scope gates, editor/audio capabilities, MIDI ownership and API auth")

# The adapter denies the hardware controller before LoadLibrary, even when
# called directly with a Windows path using forward separators. No vendor DLL
# is present in this fixture; a missing-file failure would not establish denial.
real_adapter = native.adapter_path()
if real_adapter.is_file():
    import subprocess
    script = """
import ctypes, os, sys
os.environ['VEXT_FL_NATIVE_DLL'] = sys.argv[2]
dll = ctypes.CDLL(sys.argv[1])
dll.VSTPluginMain.argtypes = [ctypes.c_void_p]
dll.VSTPluginMain.restype = ctypes.c_void_p
assert not dll.VSTPluginMain(None)
"""
    for separator in ("/", "\\"):
        target = str(root / "Fruity Voltage Controller_x64.dll").replace("\\", separator)
        result = subprocess.run([sys.executable, "-c", script, str(real_adapter), target], capture_output=True, timeout=10)
        assert result.returncode == 0 and b"Voltage/CV controller blocked before DLL load" in result.stderr, result.stderr
    print("PASS: native voltage controller denied before loading with both path separators")
