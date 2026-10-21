# VEXT

**Inventory, deduplicate and organize every audio plugin on your PC, then try them out.**
By [Dianel Records](https://dianel-records.com) (SoDianel). Free software under the GPL.

VEXT scans your VST3, VST2, CLAP, AAX and Waves plugins and FL Studio's native ones, shows them in one
catalog, finds the duplicates, and cleans up with a preview and a confirm before every change. It can also
open plugins to listen to them, and keep FL Studio's plugin browser in step with the catalog.

<!-- Screenshots go in docs/images/ (captured from sample data, never a real library) -->

## Features

- **Catalog**: every plugin with vendor, format, type, category, description and links. Scanning is read-only.
- **Dedup and cleanup**: finds the same plugin in several formats or install locations, corrupted files and
  AAX leftovers, and files plugins into vendor folders. Each action is **propose, review, apply**; deleting
  means moving to a quarantine you can restore from.
- **FL Studio Sync**: builds FL Studio's plugin browser tree from the catalog, with pictures as thumbnails,
  and a Check & fix view for what drifted.
- **Playground**: **Browser** (a plugin's own editor), **Audio** (record, edit a waveform, play a selection
  through a plugin, compare) and **Signal** (test tones and noise through a plugin).
- **Analyzer**: live spectrum, spectrogram, correlation, imager and meters.
- **Plugin hosting**: VST3, VST2, CLAP and Waves, **including 32-bit plugins**, each in a worker process so a
  crashing plugin does not take VEXT down. ASIO and WASAPI audio, MIDI input.

## Requirements

- Windows 10 or 11, x64.
- FL Studio is **optional**: without it you still get the catalog, cleanup, Playground and Analyzer.
- The installer brings what a new PC needs (Edge WebView2 and Microsoft's Visual C++ runtimes).

## Download

VEXT is free. Get it from **[GitHub Releases](https://github.com/SoDianel/vext/releases/latest)**:

- `VEXT-<version>-setup.exe`: the installer (per-user; brings WebView2 and the Visual C++ runtimes if your PC
  lacks them).
- `VEXT-<version>-portable.zip`: unzip and run `VEXT.exe`.
- `SHA256SUMS.txt`: check your download against it.

Builds may be unsigned, in which case Windows SmartScreen warns on first run ("More info", then "Run anyway").
You can also build it yourself, see [Build from source](#build-from-source).

## Support

VEXT is made and maintained by one person. If it saves you time, you can support its development with a
donation: [Ko-fi](https://ko-fi.com/sodianel). It is optional; nothing is locked behind it.

## Privacy

Everything stays on your PC. VEXT has **no telemetry and no accounts**, and does not upload your plugin list.
Its backend listens on `127.0.0.1` only. It goes online only when *you* ask: opening a plugin's store page,
searching for a picture, or downloading a picture from a link. The optional **AI research** feature runs
command-line agents you installed and signed in to yourself (Codex, Claude Code, Gemini CLI, Antigravity);
only if you start it, plugin names and questions go to that vendor. Your data (database, quarantine, caches, logs)
lives in `%APPDATA%\com.vext.app`.

## Build from source

You need Windows 10/11 x64, Python 3.12+, Node.js, Rust (`rustup`) and the MSVC C++ Build Tools
("Desktop development with C++"). Full notes: [LOCAL_SETUP.md](LOCAL_SETUP.md).

Run the backend and open <http://localhost:8000> (the quickest loop):

```
python -m venv .venv-native
.venv-native\Scripts\pip install -r requirements.txt
.venv-native\Scripts\python -m uvicorn app.web:app --host 127.0.0.1 --port 8000
```

Run the desktop app:

```
.\build_sidecar.ps1
cd desktop
npm install
npm run tauri dev
```

Build the installer and portable folder into `release\`:

```
.\build_release.ps1
```

32-bit plugin support needs `.\tools\setup_x86_host.ps1` once. Unsigned builds show a Windows SmartScreen
warning; signing is optional and documented in LOCAL_SETUP.md.

## Contributing

Issues and pull requests are welcome: see [CONTRIBUTING.md](CONTRIBUTING.md) and the
[Code of Conduct](CODE_OF_CONDUCT.md). Security reports go through [SECURITY.md](SECURITY.md). The design,
safety rules and history of the project are in [AGENTS.md](AGENTS.md).

## License

VEXT is free software: you can redistribute it and modify it under the terms of the **GNU General Public
License, version 3 or (at your option) any later version**. See [LICENSE](LICENSE).

The builds bundle other software under its own license, listed in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md), including `pedalboard` (GPL-3.0) and FFmpeg. FL Studio,
VST, Waves, CLAP and AAX are trademarks of their owners; VEXT is not affiliated with them.
