"""Entrypoint for the PyInstaller-bundled backend sidecar that the Tauri
desktop shell spawns (see desktop/src-tauri/src/lib.rs). Not used for the
Docker-era workflow or plain `python -m app.main` CLI use — those still import
app.web/app.main directly. This just gives PyInstaller a single, simple script
to build from that starts the same FastAPI app uvicorn already serves.
"""

import sys

# The plugin host worker is this same executable run with --host-worker <path>
# [--format ...] [--name ...] (see app/plugin_host.py and app/host_worker.py):
# it must not start the web server, so branch before that.
if len(sys.argv) > 2 and sys.argv[1] == "--host-worker":
    from app.host_worker import main as _host_main

    sys.exit(_host_main(*sys.argv[2:]))

# Configuration > Audio's ASIO listing and Test run in a short-lived process of
# their own: --audio-tool list [--asio] | test <json> (see app/audio_setup.py).
if len(sys.argv) > 2 and sys.argv[1] == "--audio-tool":
    from app.audio_setup import main as _audio_main

    sys.exit(_audio_main(sys.argv[2:]))

import os

import uvicorn

# Import the app object directly rather than passing "app.web:app" as a string
# for uvicorn to resolve at runtime — PyInstaller's static analysis only sees
# real imports, so a string-based lazy import leaves the app/ package out of
# the bundle entirely (confirmed: ModuleNotFoundError: No module named 'app').
from app.web import app

if __name__ == "__main__":
    # The desktop shell passes VEXT_TOKEN (see app/security.py) and may pick the
    # port with VEXT_BACKEND_PORT; it builds the webviews' API address from the same value.
    port = int(os.environ.get("VEXT_BACKEND_PORT") or 8000)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
