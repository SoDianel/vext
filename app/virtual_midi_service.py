"""Per-backend local virtual MIDI evaluation, never started by import or inspection."""
import os
import threading

from app import logbuf

NAME = 'VEXT'
_lock = threading.RLock()
_cable = None
_attempted = False
_state = 'stopped'
_message = 'VEXT virtual MIDI is stopped.'


def _log_failure(message):
    global _state, _message
    with _lock:
        _state = 'fault'
        _message = str(message)[:400]
    logbuf.add('backend', 'VEXT virtual MIDI stopped: ' + _message, 'warning')


def start():
    global _cable, _attempted, _state, _message
    with _lock:
        if _attempted:
            return status()
        _attempted = True
        if os.environ.get('VEXT_VIRTUAL_MIDI', '').strip().lower() in ('0', 'off', 'false'):
            _state, _message = 'disabled', 'VEXT virtual MIDI is disabled. Select an external MIDI output instead.'
            return status()
        try:
            from app.hosting.virtual_midi import Cable, native
            _cable = Cable(native(), NAME, on_fault=_log_failure)
            if _cable.muted:
                _state, _message = 'fault', _cable.problem
            else:
                _state, _message = 'active', 'VEXT owns the VEXT MIDI port. Other apps can select its input.'
        except Exception as exc:  # Driver failures must not prevent backend startup.
            _state = 'unavailable'
            _message = ('Could not create VEXT MIDI port: ' + str(exc) +
                        '. Check the installed driver or select an external MIDI output.')[:400]
            logbuf.add('backend', _message, 'warning')
        return status()


def status():
    with _lock:
        cable = _cable
        muted = bool(cable and cable.muted)
        return {'name': NAME, 'state': 'fault' if muted else _state, 'owned': cable is not None,
                'message': (cable.problem if muted else _message),
                'received': cable.received if cable else 0, 'sent': cable.sent if cable else 0,
                'failures': cable.failures if cable else 0, 'overflows': cable.overflows if cable else 0}


def stop():
    global _cable, _attempted, _state, _message
    with _lock:
        cable = _cable
        if cable is None:
            _attempted = False
            _state, _message = 'stopped', 'VEXT virtual MIDI is stopped.'
            return
    # The reader can report failure while close waits for it; don't hold the manager lock.
    try:
        cable.close()
    except Exception as exc:
        _log_failure('Could not stop VEXT MIDI safely: ' + str(exc))
        return
    with _lock:
        if _cable is cable:
            _cable = None
            _attempted = False
            _state, _message = 'stopped', 'VEXT virtual MIDI is stopped.'
