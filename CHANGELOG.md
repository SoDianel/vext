# Changelog

All notable changes to VEXT are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.9.0-beta.1] - 2026-11-08

First beta testing candidate. VEXT began as a private tool and is free software under GPL-3.0-or-later.
The stable 1.0.0 release has not been announced. Experimental FL native support is bounded by
[compatibility](docs/FL_NATIVE_COMPATIBILITY.md) and [scope](docs/FL_NATIVE_SCOPE.md).

### Changed

- Keep development briefs and assistant instructions private; public documentation lists supported behavior and limitations. External code contributions are not accepted.

- Prepare version 0.9.0-beta.1 for testing; organize build, release, branding and test helpers under `tools/`.
- Publish maintained FL native compatibility and scope documentation in `docs/` and include it with testing builds.

### Added

- VEXT watches the plugin folders and scans by itself after an install, update or uninstall (Configuration ›
  General, on by default). New plugins appear in Scan › Plug-ins with a toast.
- Pro Tools in Configuration › DAWs: detected automatically, or set its folder.
- **Experimental FL native plugins** in Playground: opens validated installed x64 native plugins
  directly in the shared plugin container, with isolated settings, manual capture and explicit unavailable reasons.
  The confirmed Picture-page Capture all action can capture these editors one at a time in isolated editor-only workers.
  Validated stereo effects support file and browser audio, recording, bypass and Analyzer routing.
  Vocodex supports stereo L-R encoding with a setup reminder; additional sidechain buses are
  unavailable. Convolver impulse loading and wet processing are verified. Fruity Scratcher loads a WAV
  onto its deck.
  Transmitter and Transporter support stereo processing; Transmitter reminds the user to
  raise its initially muted transient or sustain level. Effector's FILTER controls and native
  bypass are verified with processing enabled.
  Thirty-seven validated native instruments support MIDI notes, sustain, audio and Analyzer routing.
  Morphine requires the user to load a preset; PLUCKED! can use Gate for prompt note release.
  SoundFont Player loads user-selected soundfonts, DirectWave user-selected programs and FPC
  user-selected pad samples; Drumaxx supports user-mapped pad keys. Wave Traveller, Fruity Slicer,
  Fruity Slicer 2 and Fruity Granulizer load WAV files through their own editors.
  Slicex loads and plays user-selected audio slices; piano-roll export is unavailable.
  Loading/mapping hints remain visible during playback. Wasp and Wasp XT report that
  their 32-bit native engines are unsupported. Kepler, Kepler Exo and Transistor Bass clear cached
  audio on reset while preserving new notes and the requested transport clock. SoundFont Player
  uses the same bounded reset handling; DirectWave drains until its released voices are silent.
  Native loaders can suspend output safely.
  Newtime and Newtone can audition clips loaded in their own editors through an explicit Monitor
  control, with gain, mute and Analyzer support. MIDI, browser/file input routing and editor export
  remain unavailable or unverified. Choose the audio rate before loading a clip.
  Other native plugins remain editor-only, with capture available and processing controls disabled.
  Plugin-specific limitation notices distinguish release scope, pending integration and unverified
  features, and remain visible in Playground during playback and in the plugin window tooltip.
  Patcher keeps its plugin menus isolated and excludes unverified preset graphs.
  Patcher's two editors and all three FL Studio Mobile editors have verified
  container startup and capture. Mobile uses only selected installed UI/DSP resources in
  its disposable workspace. FL Studio Mobile, Mobile Rack and Mobile Rack FX play through VEXT
  once a synth track or module is added. Nested hosting remains unavailable with a visible hint. VFX Script reports its unsupported embedded runtime and script-context
  services before loading, preventing its startup/Compile stalls.
  VFX Color Mapper, Envelope, Key Mapper, Keyboard Splitter and Level Scaler have verified
  editor controls and capture, with a reminder that note/controller routing is unavailable.
  Dashboard opens with enough room for its toolbar and control properties. Control Surface,
  Dashboard, X-Y Controller, VFX Sequencer and MIDI Out show their unavailable output routing.
- **Playground tempo**: editable BPM and Auto beside the title, with local detection for loaded,
  recorded and live browser audio, manual override and a 100 BPM fallback. Native plugins receive
  tempo and play/pause/seek information; Big Clock can follow playback without an audio stream.
  Native instruments receive a bounded voice limit; the host releases a voice before killing it on note replacement or All Sounds Off.
  DrumSynth Live supports panning and its main stereo output.
  Native wavetable and buffer tables are stable before plugin construction. Native keyboard previews,
  Gross Beat initialization and main audio buffers are repaired;
  Edison sample loading and WAV export are verified in isolation. A per-plugin compatibility
  ledger records verified behavior and outstanding reports. Voltage Controller is blocked before loading.
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
  **Signal** (test signals through a plugin: one oscillator with unison, noise and impulses, a master
  volume, a full-height scope, and Record, which turns what comes out of the plugin into the Audio tab's
  file) and **Audio** (waveform editor, record, play selection, loop, mute / compare, fades, Capture).
  Alt + double-click resets a knob. The Signal values and the Audio tab's file and recordings are cleared
  when VEXT closes.
- **Analyzer**: live Spectrum, Spectrogram, Correlation, Imager, meters, a timeline and a settings window.
- **Plugin pictures**: found automatically, chosen from candidates, from a link or from an in-app image
  search; crop in VEXT; learned vendor patterns. Automatic capture runs two plugins at a time (never two
  of one vendor) in pre-started workers, fastest-loading first, uses a VST3 bundle's own editor snapshot
  without opening the plugin, and starts editors that only draw on screen there at once.
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
- **FL Studio launch**: load an exact FL preset from a plugin, using a VEXT controller script and a
  configurable MIDI output (`VEXT` or an external cable). FL Studio Sync installs the controller and tests the connection; its light shows
  FL closed, running or connected, and click-to-load stays off until the controller sends a fresh, authenticated
  heartbeat. Drag Browse and Playground cards into FL's Channel Rack or mixer slots; several matching presets
  ask which one. Missing or quarantined plugins cannot load, and the FL command port is kept out of the
  Playground and toolbar MIDI inputs.
- **Bubble mode**: a global header button turns VEXT into an always-on-top bubble, independent of FL setup.
  Its browser header restores the full app with the current page and Playground session preserved,
  including after opening the Playground's embedded browser. VEXT
  starts in the full interface. Hover or click the bubble to open the
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
  and `tools\release\publish_release.ps1`.

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
- Licensed under GPL-3.0-or-later (earlier private commits were MIT). The builds bundle `pedalboard`
  (GPL-3.0). See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
- The backend runs natively on Windows; Docker is gone.
- Configuration > DAWs shows each DAW as a collapsible panel with its own logo; the FL controller setup (MIDI
  output, install, connection test) lives in the FL Studio panel. The FL connection light is in FL Studio Sync.
- Plugins have no "Load in FL Studio" action; sending to FL is done from the bubble or by dragging a card.
- Database: the Plugin types pane is gone (the two types are fixed), and Categories / Vendors sort by clicking a
  column header (no order selector).

### Fixed

- Native instruments played an octave low (3x Osc and FL Keys were correct): MIDI A4 now
  sounds at 440 Hz, and samplers and slicers play at the pitch of their source.

- DirectWave can open factory programs: its loader no longer crashes when a host path
  is unavailable, and samples next to the program load as usual.

- Newtone can load and edit audio without a processing stream: its editor receives valid
  static rate, tempo and time-signature metadata, preventing a clock-period division by zero.
  Newtime and Newtone remain editor-only and show that playback is unavailable.

- Native controller editors keep their controls inside the plugin frame. Internal controller
  notifications no longer terminate the editor when outputs are disconnected; Formula, Peak,
  Envelope, Keyboard and X-Y-Z Controller show that controller output is unavailable.

- Category tags wiped by a rescan; metadata orphaned when files move outside VEXT.
- Every plugin crashing in the packaged app because of a stale bundled `MSVCP140.dll`; the build now fails if
  the bundle carries a private C++ runtime.
- Xfer plugins (OTT, DJMFilter) that would not open; the JUCE 7/8 click crash in plugin editors.
- A blank first load while the backend was still starting.
- FL click-to-load is robust against FL's own behavior: it hands focus to FL and stops if Windows refuses,
  selects the Plugins Browser tab, walks the folders without collapsing them, checks the running controller's
  revision, keeps its exchange files inside the controller folder, and works in FL's embedded Python.
- "Could not find the WebView2 Runtime" on PCs that have WebView2's files but not its registry entry
  (Windows Sandbox, some company images): VEXT now points WebView2 at the files it finds.

[Unreleased]: https://github.com/SoDianel/vext/compare/v0.9.0-beta.1...HEAD
[0.9.0-beta.1]: https://github.com/SoDianel/vext/releases/tag/v0.9.0-beta.1
