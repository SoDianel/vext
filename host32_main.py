"""Entrypoint for the 32-bit plugin host that the installer ships as
host32\vext-host32.exe (vext-host32.spec, built by tools/build_host32.ps1 with
the 32-bit Python in .python-x86). The 64-bit backend starts it for 32-bit
plugins (plugin_host.x86_worker) exactly like its own worker:

  vext-host32.exe --host-worker <plugin path> [options]

Nothing else: no web server, no scanner (see AGENTS.md "32-bit plugins").
"""

import sys

if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--host-worker":
        from app.host_worker import main

        sys.exit(main(*sys.argv[2:]))
    sys.exit("usage: vext-host32.exe --host-worker <plugin path> [options]")
