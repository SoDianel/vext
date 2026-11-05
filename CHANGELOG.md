# Changelog

All notable changes to VEXT are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.0.0] - 2026-11-05

First public release. VEXT began as a private tool and is now free software under GPL-3.0-or-later.
Everything done before the official release is announced belongs to this version: it is a draft until then,
and its date is the day it was last updated.

### Added

- **Virtual MIDI**: creates the `VEXT` port at startup when the installed virtual MIDI driver is
  available, with ownership status, output availability and external setup guidance.
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
- **FL Studio launch**: load an exact FL preset from a plugin, using a VEXT controller script and a
  configurable MIDI output (`VEXT` or an external cable). FL Studio Sync installs the controller and tests the connection; its light shows
  FL closed, running or connected, and click-to-load stays off until the controller sends a fresh, authenticated
  heartbeat. Drag Browse and Playground cards into FL's Channel Rack or mixer slots; several matching presets
  ask which one. Missing or quarantined plugins cannot load, and the FL command port is kept out of the
  Playground and toolbar MIDI inputs.
- **FL bubble**: an optional always-on-top bubble, switched on in FL Studio Sync. Hover or click it to open the
  complete FL preset index inside the bubble: categories or vendors with counts, Effects / Instruments scope,
  search of the full `.fst` index, and Cards, List or Compact list views (remembered). It lists FL's Installed
  plugin database only, so presets are not duplicated; ones without a VEXT picture show their filed copy's.
  The header shows Connected, Waiting or FL closed. Click a plugin to send it to FL, or drag it in.
- FL controller errors and tracebacks appear in the Log, with the preset and build context, an error badge and
  duplicate suppression.
- **Branches, builds and releases**: pull-request CI, rebase-only merges and protected linear history on `main`.
  Every `main` push builds the Windows installer and portable download, retained with checksums and source
  provenance for 90 days; release automation prepares drafts for manual publication.
- Optional Authenticode signing in the build (`VEXT_SIGN_*` variables), a version check, third-party notices
  and `tools\publish_release.ps1`.

### Changed

- Licensed under GPL-3.0-or-later (earlier private commits were MIT). The builds bundle `pedalboard`
  (GPL-3.0). See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
- The backend runs natively on Windows; Docker is gone.
- Configuration > DAWs shows each DAW as a collapsible panel with its own logo; the FL controller setup (MIDI
  output, install, connection test) lives in the FL Studio panel. The FL connection light is in FL Studio Sync.
- Plugins have no "Load in FL Studio" action; sending to FL is done from the bubble or by dragging a card.

### Fixed

- Category tags wiped by a rescan; metadata orphaned when files move outside VEXT.
- Every plugin crashing in the packaged app because of a stale bundled `MSVCP140.dll`; the build now fails if
  the bundle carries a private C++ runtime.
- Xfer plugins (OTT, DJMFilter) that would not open; the JUCE 7/8 click crash in plugin editors.
- A blank first load while the backend was still starting.
- FL click-to-load is robust against FL's own behavior: it hands focus to FL and stops if Windows refuses,
  selects the Plugins Browser tab, walks the folders without collapsing them, checks the running controller's
  revision, keeps its exchange files inside the controller folder, and works in FL's embedded Python.

[Unreleased]: https://github.com/SoDianel/vext/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/SoDianel/vext/releases/tag/v1.0.0
