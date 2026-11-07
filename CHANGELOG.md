# Changelog

All notable changes to VEXT are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- FL controller runtime errors and tracebacks now appear in VEXT?s Log tab, with preset/build context, error badge support, and duplicate suppression.

- FL Studio connection light shows closed, running/disconnected, or connected. Click-to-load stays disabled until the VEXT controller has a fresh authenticated heartbeat.

- Load an exact FL Studio preset from a plugin's actions, using a VEXT controller script and a
  configurable loopMIDI output. FL Studio Sync includes controller installation and a connection test.
- Drag Browse and Playground plugin cards from the desktop app into FL Studio's Channel Rack or mixer
  effect slots. Multiple matching presets offer an explicit choice; missing or quarantined plugins cannot load.
- The FL command port is reserved from Playground and toolbar MIDI inputs, including the all-inputs option.
- FL launcher bubble: an optional always-on-top bubble, switched on in FL Studio Sync. Hover it for a radial
  menu of categories and vendors (seven at a time, scroll for more); pick one to open a picture grid of its
  plugins, then click a plugin to send it to FL or drag it into FL. Needs FL Studio and the VEXT controller set up.

### Added

- VEXT watches the plugin folders and scans by itself after an install, update or uninstall (Configuration ›
  General, on by default). New plugins appear in Scan › Plug-ins with a toast.
- Pro Tools in Configuration › DAWs: detected automatically, or set its folder.

### Changed

- Scan › Plug-ins shows one row per plugin, with a badge for each copy (VST, VST2, VST3, AAX, CLAP, x86 for
  32-bit). Click a badge for that copy's details. Copies are matched more reliably: format and bitness tags and
  the vendor's name in front of the plugin name no longer keep them apart.
- Suggestions to clean up obsolete formats and older copies are now optional and off by default
  (Configuration › General, Cleanup suggestions).
- AAX plugins are suggested for removal only when Pro Tools isn't installed, and a VST3 or CLAP copy no longer
  counts as replacing an AAX one.
- "Mark as FL Cloud" is now "Mark as Unorganizable" (details, ⋯ menu, badge, dashboard): same pin, no longer
  tied to FL Cloud by name.
- FL Studio Sync's "Left at the root" list has an Unorganizable tick for plugins VEXT knows; Apply marks them
  as part of the run.
- The FL Cloud, FL Synced and FL Native tag filters are gone from Scan › Plug-ins (FL Native is already a format).

### Fixed

- Keep the FL bubble logo anchored during hover by retaining one drawing surface and one logo.
  Refit selectors after dragging from a screen edge; closed bubbles clip input to their own circle.
- Show launcher cards immediately from the current menu snapshot while refreshing in the background;
  reuse unchanged preset matches and file digests instead of rebuilding them on each list request.
- Animate selector entrance and vertical wheel scrolling without replacing the list on each step.

- Fix the FL launcher radial menu staying hidden on hover by updating the SVG hidden attribute.
- Keep the FL launcher bubble visible while hover expansion is pending or fails; prevent menu requests
  from delaying pointer tracking, and keep the center/ring gap available for mouse input.

- Installed FL presets now load directly after checking folder levels and the FST leaf; remove the post-search parent walk and second search.

- Hand foreground focus from VEXT to FL Studio before click-to-load; stop before sending MIDI if Windows refuses. Verify FL Browser keyboard focus before folder checks and activation.

- Reset only the selected FL Browser branch in a single burst; remove per-folder collapse waits. Parent verification now waits for the expected caption, handles collapse-in-place, and names any mismatched folders.

- Select FL?s Plugins Browser tab automatically, allow real UI frames for tab/tree rebuilds, and avoid collapsing nodes during caption searches.
- Check the running FL controller revision so an updated file cannot hide an older script still running in FL.

- FL Browser traversal enters expanded folders with Down instead of toggling them closed with Right; avoid a second full Browser reset.
- Reduce click-to-load delay by caching unchanged preset metadata, resolving only matching presets and overlapping the connection check with lookup.

- Keep FL bridge exchange files inside the controller folder and avoid restricted rename/delete calls; report file API failures without repeated tracebacks.

- FL controller settings discovery in FL?s embedded Python runtime, which does not define `__file__`.

### Changed

- Hovering or clicking the VEXT bubble opens a complete FL preset index inside the bubble itself, not a
  separate window; it closes back to the bubble when the pointer leaves. The curved five-item selectors
  have been removed. Connection status (green Connected, yellow Waiting, red FL closed) is in its header.
- Browse all categories or vendors in a sidebar with counts, scope to Effects or Instruments,
  and search the full .fst index. Switch between Cards, List and Compact list with icon buttons; the
  view is remembered.
- The FL index lists only FL's Installed plugin database, so presets no longer appear twice.
  Presets without a VEXT catalog picture, such as Waves and Image-Line natives, show the picture of
  their filed copy in FL's Effects/Generators folders.

- Refine the FL bubble with curved selectors, horizontal wrapping labels, larger targets, counts and
  a short opening animation. Drag its VEXT center while the selectors are open.
- Show connection status only in the upper bubble pill: green Connected, yellow Waiting, red FL closed.
- The FL launcher lists existing FL .fst entries, including presets without a VEXT catalog match.
  Catalog-only entries are excluded; exact filed mirrors are combined and FL preset thumbnails are shown.

- Configuration > DAWs shows each DAW as a collapsible panel with its own logo. The FL controller setup
  (MIDI output, install, connection test) now lives in the FL Studio panel.
- The FL connection light moved from the toolbar to FL Studio Sync.
- Plugins no longer have a "Load in FL Studio" action; sending to FL is done from the launcher bubble.
  Dragging a catalog card into FL still works.
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
  **Signal** (test signals through a plugin: one oscillator with unison, noise and impulses, a master
  volume, a full-height scope, and Record, which turns what comes out of the plugin into the Audio tab's
  file) and **Audio** (waveform editor, record, play selection, loop, mute / compare, fades, Capture).
  Alt + double-click resets a knob. The Signal values and the Audio tab's file and recordings are cleared
  when VEXT closes.
- **Analyzer**: live Spectrum, Spectrogram, Correlation, Imager, meters, a timeline and a settings window.
- **Plugin pictures**: found automatically, chosen from candidates, from a link or from an in-app image
  search; crop in VEXT; learned vendor patterns.
- **Database › Metadata**: researched data (links, descriptions, your edits, other vendors, categories,
  pictures) is kept when a plugin is uninstalled and given back when it returns, at the same path or moved
  (same name and format). Vendors and the categories you made are kept the same way. The Metadata pane lists all
  metadata, installed or not, with filters (installed, no plugin, images without plugin, leftovers), search,
  order, select all and **Clear selected (N)**; Vendors and Categories show kept entries as "No plugin" rows.
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
- Database: the Plugin types pane is gone (the two types are fixed), and Categories / Vendors sort by clicking a
  column header (no order selector).

### Fixed

- Category tags wiped by a rescan; metadata orphaned when files move outside VEXT.
- Every plugin crashing in the packaged app because of a stale bundled `MSVCP140.dll`; the build now fails if
  the bundle carries a private C++ runtime.
- Xfer plugins (OTT, DJMFilter) that would not open; the JUCE 7/8 click crash in plugin editors.
- A blank first load while the backend was still starting.
- "Could not find the WebView2 Runtime" on PCs that have WebView2's files but not its registry entry
  (Windows Sandbox, some company images): VEXT now points WebView2 at the files it finds.

[Unreleased]: https://github.com/SoDianel/vext/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/SoDianel/vext/releases/tag/v1.0.0
