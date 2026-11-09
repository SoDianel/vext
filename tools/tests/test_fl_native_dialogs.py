"""Native exception reporting, without loading plugins or opening real data."""
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

with tempfile.TemporaryDirectory(prefix="vext-native-dialog-test-") as temporary:
    for name in ("VEXT_ROOT", "APPDATA", "LOCALAPPDATA", "USERPROFILE"):
        os.environ[name] = str(Path(temporary) / name)

    from app.hosting.native_dialogs import error_text

    message = "Access violation at address 00000001800420A3. Read of address 0000000000000000."
    assert message in error_text({"title": "python", "text": [message, "OK"]})
    assert error_text({"title": "vext-backend", "text": [message.upper()]})
    assert error_text({"title": "Error", "text": ["Native editor failed"]})
    assert error_text({"title": "Editor", "text": ["Missing shader file"]})
    assert error_text({"title": "Editor", "text": ["Runtime error 216"]})
    assert error_text({"title": "Open", "text": ["Access Violation.wav", "Open", "Cancel"]}) is None
    assert error_text({"title": "Select melodic patch for channel 1", "text": ["Music Box"]}) is None
    assert error_text({"title": "Open program or bank file", "text": ["Piano.dwp"]}) is None

print("PASS native exception dialogs reported independently of executable title; normal dialogs preserved")
