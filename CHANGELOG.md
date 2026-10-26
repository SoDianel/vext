# Changelog

All notable changes to VEXT are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

- Branch and pull request CI, rebase-only merges and protected linear history on `main`.
- Every `main` push builds the Windows installer and portable download, retained with checksums and source
  provenance for 90 days. Release automation prepares drafts for manual publication.

## [1.0.0] - 2026-10-26

First public release. VEXT began as a private tool and is now free software under GPL-3.0-or-later.

### Added

- **Catalog**: scans FL Studio native plugins, VST3, VST2, CLAP, Waves and AAX folders (read-only), with stable
  plugin IDs across rescans, vendor resolution and canonical vendor names, categories, types, descriptions,
  store / KVR / Equipboard links, and a full-drive scan for plugins installed somewhere unexpected.
- **Cleanup**: duplicate detection across formats and install locations (CLAP > VST3 > VST2, 64-bit first),
  corrupted-file and AAX review, an Organize tab that files plugins into `<Vendor>\` folders. Every action is
  propose, review, explicit apply; "delete" moves to a quarantine you can restore from. Optional elevation for
  Windows-protected folders, and per-folder access.
- **FL Studio Sync**: mirrors the catalog into FL Studio's plugin browser tree: categories, pictures as
  thumbnails, `.fst` links, new-only or complete sync, and a Check & fix view (fix picture, resize, remove,
  ignore, add).
- **Plugin hosting** for VST3, VST2, CLAP and Waves, including **32-bit plugins** in their own host process.
  Plugin frame with a control bar and Bypass, Capture menu, IN / OUT and Auto-Balance, crash isolation, a
  memory limit and a gapless audio buffer with delay compensation.
- **Playground** with three tabs: **Browser** (a plugin's editor and an in-app browser with audio capture),
  **Audio** (waveform editor, record, play selection, loop, mute / compare, fades, Capture) and **Signal**
  (test signals through a plugin).
- **Analyzer**: live Spectrum, Spectrogram, Correlation, Imager, meters, a timeline and a settings window.
- **Plugin pictures**: found automatically, chosen from candidates, from a link or from an in-app image
  search; crop in VEXT; learned vendor patterns.
- **Configuration**: audio devices (WASAPI, ASIO) and MIDI input, Automatic switches, a Storage page that
  measures and clears caches and backups, a Log section and failure reports.
- **Desktop app**: Tauri 2 shell with the Python backend as a local sidecar (loopback only, per-launch token);
  view modes per section; light and dark themes with the official palette.
- **Self-contained installer** (per-user): brings WebView2 and Microsoft's Visual C++ runtimes if the PC lacks
  them, and an in-app **Repair** for the runtimes. A portable folder with the same files.
- Optional Authenticode signing in the build (`VEXT_SIGN_*` variables), a version check, third-party notices
  and `tools\publish_release.ps1`.

### Changed

- Licensed under GPL-3.0-or-later (earlier private commits were MIT). The builds bundle `pedalboard`
  (GPL-3.0). See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
- The backend runs natively on Windows; Docker is gone.

### Fixed

- Category tags wiped by a rescan; metadata orphaned when files move outside VEXT.
- Every plugin crashing in the packaged app because of a stale bundled `MSVCP140.dll`; the build now fails if
  the bundle carries a private C++ runtime.
- Xfer plugins (OTT, DJMFilter) that would not open; the JUCE 7/8 click crash in plugin editors.
- A blank first load while the backend was still starting.

[Unreleased]: https://github.com/SoDianel/vext/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/SoDianel/vext/releases/tag/v1.0.0
