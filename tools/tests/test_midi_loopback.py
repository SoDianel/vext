"""Device inspection uses fake endpoints and never opens a MIDI handle."""
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

root = Path(tempfile.mkdtemp(prefix='vext-midi-test-'))
for key in ('VEXT_ROOT', 'LOCALAPPDATA', 'APPDATA'):
    os.environ[key] = str(root / key)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from app import midi_loopback
from app.hosting import midi, midi_output

with mock.patch.object(midi, 'list_inputs', return_value=[{'name': 'Port'}, {'name': 'Port'}]), mock.patch.object(midi_output, 'list_outputs', return_value=['Port']):
    result = midi_loopback.inspect('Port')
    assert (result['state'], result['input_matches'], result['output_matches']) == ('available', 2, 1)
    assert midi_loopback.inspect('port')['state'] == 'missing'
with mock.patch.object(midi, 'list_inputs', return_value=[]), mock.patch.object(midi_output, 'list_outputs', return_value=['Port', 'Port']):
    assert midi_loopback.inspect('Port')['state'] == 'ambiguous'
with mock.patch.object(midi, 'list_inputs', side_effect=OSError('device disappeared')):
    assert midi_loopback.inspect('Port')['state'] == 'unavailable'
print('MIDI diagnostics: exact matches, missing, duplicate and enumeration failure passed')

# Device state is read-only, and settings mutations still require the launch token.
from fastapi.testclient import TestClient
from app import db, web, fl_launch
from app.config import DB_PATH
Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
db.init_db(DB_PATH)
with db.connect(DB_PATH) as conn:
    db.set_setting(conn, 'fl_launch_port', 'Exact <port>')
with mock.patch.object(midi_loopback, 'inspect', return_value={'port': 'Exact <port>', 'state': 'missing'}) as inspection, mock.patch.object(fl_launch, 'connection', return_value={'connected': False, 'message': 'disconnected'}):
    client = TestClient(web.app, base_url='http://127.0.0.1:8000')
    response = client.get('/api/midi/loopback')
    assert response.status_code == 200 and response.json()['port'] == 'Exact <port>'
    inspection.assert_called_once_with('Exact <port>')
    assert client.put('/api/settings/fl_launch_port', json={'value': 'changed'}).status_code == 403
with db.connect(DB_PATH) as conn:
    assert db.get_setting(conn, 'fl_launch_port', '') == 'Exact <port>'
print('MIDI diagnostics API: saved selection, read-only GET and token protection passed')


# An output change must exclude that name even when all inputs are selected.
with db.connect(DB_PATH) as conn:
    db.set_setting(conn, 'fl_launch_port', 'Reserved')
class FakeWinMM:
    def __init__(self): self.opened = []
    def midiInOpen(self, handle, index, *args):
        self.opened.append(index)
        return 4  # Busy is enough to prove which indices were attempted.
monitor = object.__new__(midi.MidiInputs)
monitor.close = lambda: None
monitor._thread_id = 1
fake = FakeWinMM()
inputs = [{'index': 0, 'id': 'Reserved', 'name': 'Reserved'}, {'index': 1, 'id': 'Keyboard', 'name': 'Keyboard'}]
with mock.patch.object(midi, '_winmm', fake), mock.patch.object(midi, 'list_inputs', return_value=inputs):
    monitor._lock = __import__('threading').Lock()
    monitor._open_ids = []
    result = monitor.open('*')
    assert fake.opened == [1] and result['available'] == ['Keyboard']
print('MIDI reserved input excluded from all-input selection')

# Exercise authenticated settings update and its monitor/worker reconciliation.
from app import security
import threading
worker_applied = threading.Event()
with db.connect(DB_PATH) as conn:
    db.set_setting(conn, 'fl_launch_port', 'Previous')
with mock.patch.object(web.midi_watch, 'release') as release, mock.patch.object(web.midi_watch, 'poke') as poke, mock.patch.object(web.plugin_host, 'apply_midi_settings', side_effect=worker_applied.set):
    response = client.put('/api/settings/fl_launch_port', json={'value': 'Reserved'}, headers={'X-VEXT-Token': security.TOKEN})
    assert response.status_code == 200, response.text
    assert worker_applied.wait(2)
    release.assert_called_once()
    poke.assert_called_once()
with db.connect(DB_PATH) as conn:
    assert db.get_setting(conn, 'fl_launch_port', '') == 'Reserved'
fake = FakeWinMM()
with mock.patch.object(midi, '_winmm', fake), mock.patch.object(midi, 'list_inputs', return_value=inputs):
    result = monitor.open('*')
    assert fake.opened == [1] and result['available'] == ['Keyboard']
print('Authenticated output update releases, applies, wakes and reserves the new input')

# Output errors identify the operation and selected output without assuming a provider.
with mock.patch.object(midi_output, '_outputs', return_value=[]):
    try:
        midi_output.send_sysex('External', b'\xf0\xf7')
        raise AssertionError('missing output accepted')
    except ValueError as exc:
        assert 'External' in str(exc) and 'send' in str(exc).lower() and 'loopMIDI' not in str(exc)
class OutputFailure:
    def __init__(self, stage): self.stage = stage
    def midiOutOpen(self, *args): return self.stage == 'open'
    def midiOutPrepareHeader(self, *args): return self.stage == 'prepare'
    def midiOutLongMsg(self, *args): return self.stage == 'send'
    def midiOutReset(self, *args): return 0
    def midiOutUnprepareHeader(self, *args): return 0
    def midiOutClose(self, *args): return 0
for stage in ('open', 'prepare', 'send'):
    with mock.patch.object(midi_output, '_outputs', return_value=[(0, 'External')]), mock.patch.object(midi_output, '_api', return_value=OutputFailure(stage)):
        try:
            midi_output.send_sysex('External', b'\xf0\xf7')
            raise AssertionError('expected output failure')
        except OSError as exc:
            assert 'External' in str(exc) and stage in str(exc).lower() and '?' not in str(exc)
print('Selected output and operation appear in generic send errors')
