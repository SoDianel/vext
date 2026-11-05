"""Startup and ownership tests use a fake driver and isolated data."""
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

work = tempfile.TemporaryDirectory(prefix='vext-midi-startup-')
for key in ('VEXT_ROOT', 'APPDATA', 'LOCALAPPDATA', 'USERPROFILE'):
    os.environ[key] = str(Path(work.name) / key)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import db, virtual_midi_service as service
from app.hosting import virtual_midi as provider
assert service.status()['state'] == 'stopped'
from app import web, midi_loopback, fl_launch, host_worker
from app.config import DB_PATH
from fastapi.testclient import TestClient
Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
db.init_db(DB_PATH)

class FakeCable:
    made = []
    def __init__(self, dll, name, on_fault):
        assert name == 'VEXT'
        self.name = name; self.on_fault = on_fault; self.closed = 0
        self.muted = False; self.problem = ''; self.received = self.sent = self.failures = self.overflows = 0
        self.made.append(self)
    def close(self): self.closed += 1

def selected():
    with db.connect(DB_PATH) as conn: return db.get_setting(conn, 'fl_launch_port', None)
def set_port(value):
    with db.connect(DB_PATH) as conn:
        if value is None: conn.execute("DELETE FROM settings WHERE key='fl_launch_port'")
        else: db.set_setting(conn, 'fl_launch_port', value)

with mock.patch.object(provider, 'native', return_value=object()) as native, mock.patch.object(provider, 'Cable', FakeCable), \
     mock.patch.object(web.midi_watch, 'start') as watch_start, mock.patch.object(web.midi_watch, 'shutdown') as watch_stop, \
     mock.patch.object(web.ai_agents, 'warm'), mock.patch.object(web.runtime_check, 'status', return_value={'ok': True, 'version': 'test', 'path': 'test'}):
    started_states = []
    watch_start.side_effect = lambda: started_states.append(service.status()['state'])
    # Import and GET never create a port; actual ASGI lifespan does.
    client = TestClient(web.app, base_url='http://127.0.0.1:8000')
    with mock.patch.object(midi_loopback, 'inspect', return_value={'port': 'VEXT'}), mock.patch.object(fl_launch, 'connection', return_value={'connected': False}):
        assert client.get('/api/midi/loopback').json()['managed']['state'] == 'stopped'
    assert native.call_count == 0
    set_port('Vext')
    with client:
        assert service.status()['owned'] and service.status()['state'] == 'active'
        assert selected() == 'VEXT'
        assert watch_start.call_count == 1 and len(FakeCable.made) == 1 and started_states == ['active']
        web._start_midi_monitor()
        assert len(FakeCable.made) == 1
        cable = FakeCable.made[-1]
        cable.muted = True; cable.problem = 'driver lost'; cable.failures = 1; cable.on_fault(cable.problem)
        assert service.status()['state'] == 'fault' and service.status()['owned'] and service.status()['failures'] == 1
    assert cable.closed == 1 and service.status()['state'] == 'stopped'
    service.stop(); assert cable.closed == 1
    for previous, expected in [('VEXT Bridge', 'VEXT'), ('External synth', 'External synth'), (None, None)]:
        set_port(previous); web._start_midi_monitor(); assert selected() == expected
        web._stop_midi_monitor()
    set_port('Vext')
    with mock.patch.object(provider, 'native', side_effect=OSError('collision Windows error 183')):
        web._start_midi_monitor()
        assert service.status()['state'] == 'unavailable' and not service.status()['owned'] and selected() == 'Vext'
        assert 'collision' in service.status()['message']
        web._stop_midi_monitor()
    with mock.patch.dict(os.environ, {'VEXT_VIRTUAL_MIDI': '0'}):
        before = native.call_count; web._start_midi_monitor()
        assert service.status()['state'] == 'disabled' and native.call_count == before and selected() == 'Vext'
        web._stop_midi_monitor()
    assert watch_stop.call_count >= 1
print('Startup-only create, idempotence, migration, collision, opt-out and fault status passed')

# Owned reservation follows settings to the monitor and worker, even for custom output.
from app.hosting import midi
from app import midi_monitor, plugin_host
set_port('External')
with mock.patch.object(provider, 'native', return_value=object()), mock.patch.object(provider, 'Cable', FakeCable):
    service.start()
    settings = web._midi_settings()
    assert settings['reserved'] == ['VEXT']
    inputs = [{'index': 0, 'id': 'VEXT', 'name': 'VEXT'}, {'index': 1, 'id': 'External', 'name': 'External'}, {'index': 2, 'id': 'Keyboard', 'name': 'Keyboard'}]
    class FakeWinMM:
        def __init__(self): self.attempted = []
        def midiInOpen(self, handle, index, *args): self.attempted.append(index); return 4
    fakewin = FakeWinMM(); actual = object.__new__(midi.MidiInputs)
    import threading
    actual._thread_id = 1; actual._lock = threading.Lock(); actual._open_ids = []; actual.close = lambda: None
    with mock.patch.object(midi, '_winmm', fakewin), mock.patch.object(midi, 'list_inputs', return_value=inputs):
        result = actual.open('*', settings['reserved'])
        assert fakewin.attempted == [2] and result['available'] == ['Keyboard']
    class Inputs:
        def __init__(self): self.calls = []
        def open(self, wanted, reserved): self.calls.append((wanted, reserved)); return {'open': [], 'failed': [], 'available': []}
        def close(self): pass
        def shutdown(self): pass
    monitor = midi_monitor.MidiMonitor(lambda: {'inputs': '*', 'reserved': ['VEXT']}, lambda: False, lambda: None)
    fake_inputs = Inputs(); monitor._inputs = fake_inputs; monitor.reconcile()
    assert fake_inputs.calls == [('*', ['VEXT'])]
    worker = host_worker._MidiRouter()
    worker._inputs = fake_inputs; worker.set_inputs('*', 0, ['VEXT'])
    assert fake_inputs.calls[-1] == ('*', ['VEXT'])
    service.stop()
print('Owned and external inputs reserved in wildcard input, monitor and worker passed')
work.cleanup()

# The host manager sends reservation through the same worker command for x64 and x86.
class Session:
    def __init__(self): self.calls = []
    def command(self, command, **kwargs): self.calls.append((command, kwargs))
sessions = [Session(), Session()]
with mock.patch.object(plugin_host, '_midi_settings', return_value={'inputs': '*', 'channel': 0, 'reserved': ['VEXT']}), \
     mock.patch.object(plugin_host, '_open_sessions', return_value=sessions), mock.patch.object(plugin_host, '_release_midi'):
    plugin_host.apply_midi_settings()
assert all(s.calls[0][0] == 'midi_inputs' and s.calls[0][1]['reserved'] == ['VEXT'] for s in sessions)
print('MIDI worker command carries ownership reservation to both session types')
import time
m = midi_monitor.MidiMonitor(lambda: {'inputs': []}, lambda: False, lambda: None)
m.start(); first = m._thread; m.start(); assert m._thread is first
m.shutdown(); assert not first.is_alive() and m._thread is None
m.start(); second = m._thread; assert second is not first
m.shutdown(); assert not second.is_alive()
print('Monitor startup/shutdown is repeatable and leaves no live watcher')
# Sidecar worker and audio-tool entry branches exit before importing the web app.
import runpy
import types
for args, module_name in [(['sidecar_main.py', '--audio-tool', 'list'], 'app.audio_setup'), (['sidecar_main.py', '--host-worker', 'fixture'], 'app.host_worker')]:
    fake_module = types.ModuleType(module_name)
    fake_module.main = lambda *a: 0
    with mock.patch.dict(sys.modules, {module_name: fake_module}), mock.patch.object(sys, 'argv', args), mock.patch.object(service, 'start', side_effect=AssertionError('unexpected creation')):
        try:
            runpy.run_path(str(Path(__file__).resolve().parents[1] / 'sidecar_main.py'), run_name='__main__')
        except SystemExit as exc:
            assert exc.code == 0
        else:
            raise AssertionError('sidecar did not exit')
print('Audio-tool and worker entry branches never start the managed cable')
