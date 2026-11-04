# Load plugins into FL Studio from VEXT

Status: implemented locally; automated verification and local release packaging requested.
Live interaction with FL Studio is still a separate verification step.

## Requested behavior

- Support both clicking to load and dragging a plugin card out of VEXT.
- Drag instruments onto the Channel Rack and effects onto mixer effect slots.
- Initial test environment: FL Studio 26.1.7 [5654], English, Windows x64.
  English is not a product requirement; preset identification must use FL's actual file names.
- loopMIDI is already installed. Read-only WinMM output enumeration confirmed a port named `Vext`.
- Local work only; no commits, pushes or publication. The owner subsequently requested installer
  and portable builds. FL Studio setup changes remain behind the app's explicit confirmation.

## Findings

Plugindex documents a controller script inside FL Studio and a virtual MIDI connection on Windows.
Its help describes loading through FL's Browser, including focus and language dependencies. This
establishes the general approach, but does not document its wire protocol or implementation.
Its website also describes dragging cards onto its own shortcut keyboard; that description alone
does not establish how external dragging into FL works.

The owner's Browser screenshot confirms a more specific route for VEXT: `Plugin database/Installed`,
then `Effects` or `Generators`, then `CLAP`, `Fruity`, `VST` or `VST3`. Use the actual preset name under
that format folder. Plugindex's documented language limitation is a property of its implementation;
it does not establish a language restriction for VEXT. File identity does not require translation.

VEXT already has useful building blocks:

- `app/fl_reorganize.py:inventory` associates browser `.fst` entries with catalog plugin IDs;
  `fl_links` supplies manual associations. Prefer the matching Installed entry for loading.
- `app/fl_database.py` matches ordinary plugin paths and accounts for FL-native paths. Its Waves
  lookup establishes shell availability, not a precise per-product preset association.
- `app/hosting/midi.py` receives channel messages for the Playground. It does not send commands;
  its receiver ignores SysEx. `app/midi_monitor.py` also opens selected inputs outside the Playground.
- `app/static/app.js` gives plugin rows/cards a shared `data-plugin-id` hook. No outgoing plugin
  file drag was found. `playground.js` has an incoming audio-file drop handler.
- The Tauri shell has no outgoing file-drag integration in its command list or dependencies.

The two requested interactions should share a preset resolver, with separate transports. For loading,
resolve the Installed `.nfo` association to its neighboring `.fst`, checking that it exists. Preserve
the actual path relative to the configured Plugin database root and the actual file stem. A catalog
display name is not necessarily the preset's name. Manual links and filed presets remain fallbacks
when an Installed association is unavailable or needs disambiguation.

## Drag a card into FL Studio

Proposed flow: card -> validated FL preset -> native Windows file drag -> FL drop target.

Use an existing plugin `.fst`, rather than the plugin binary, a web URL, or generated MIDI.
FL's manual describes instrument and effect preset drop targets. Native external dragging from
VEXT still needs an end-to-end proof on the target FL version. loopMIDI is not needed for this path.

Add a small Rust command using a native file-drag library. CrabNebula's `drag` crate is a candidate
with documented Windows support; evaluate its license and Tauri compatibility before adding it.
Expose only the command needed by the main VEXT webview, not the embedded research/browser pages.
Preserve existing incoming audio drops when wiring the outgoing gesture.

The resolver accepts a plugin ID and returns eligible presets with format, generator/effect kind,
and association confidence. Revalidate the chosen file before use. Resolve duplicate formats and
multiple presets explicitly; never pick a similarly named plugin silently. Waves must resolve the
individual product preset, not merely its shared WaveShell. Exclude quarantined/missing/corrupt
plugins, AAX, mixer-chain presets and unmatched entries. Missing presets show a reason and a link
to the existing FL Sync tools; do not fabricate binary `.fst` files.

Start with Browse cards and the plugin detail action menu. Add the same action to Playground
cards after verifying that dragging does not interfere with selection or preview controls.
FL owns the final drop behavior, including replacement when dropped onto an occupied destination.
An OS drag completion means a file was handed off; it does not prove FL loaded the plugin.

## Click to load through the Vext port

Proposed flow: Load in FL -> resolve exact Installed preset -> authenticated backend -> WinMM MIDI
output `Vext` -> VEXT controller script in FL -> navigate the Installed hierarchy to that preset ->
activate -> report outcome.

Examples of the route (the last component is the actual `.fst` stem):

- `Installed/Generators/VST3/<instrument>`
- `Installed/Effects/CLAP/<effect>`

Read route segments from the indexed preset path. VEXT's format `VST2` corresponds to the `VST`
folder and `FL-NATIVE` commonly to `Fruity`; do not synthesize a route from format alone. A Waves
product must use its own preset under the actual shell format folder. Reject ambiguous associations.

The script can use `ui.setFocused` with the Browser window constant, `ui.navigateBrowser`, and
`ui.getFocusedNodeCaption` to locate the expected item, checking the enclosing hierarchy as it goes.
Read the node type with `ui.getFocusedNodeFileType`. Test direct node opening with the navigation
API's open option or `ui.enter`, without looking up a translated context-menu command. These are
documented API building blocks, not yet a verified loading sequence. The experiment must establish
root anchoring, caption behavior with hidden extensions, and activation behavior on this FL build.
Do not match an identically named node in a different folder or format.

Add MIDI output separately from Playground input. Keep the port configurable and persist its
name, resolving the current device index after reconnects. Do not implicitly select the command
port as a Playground/toolbar input, including when the user selected all inputs.

Use a versioned, bounded SysEx command format with request IDs and 7-bit-safe payload encoding.
The script validates commands and marks bridge messages handled so they cannot become notes or
automation. Serialize requests, bound Browser traversal, and reject duplicates and stale requests.
On timeout, report uncertainty; never automatically repeat a load that may already have happened.

The public MIDI scripting reference supplies SysEx callbacks and Browser navigation, but does not
document a general plugin-path loading function. The first experiment should therefore check the
owner's exact-name Installed navigation approach. The unresolved question is reliable activation
and destination behavior, rather than translating plugin names. Do not impose an English-language
check. Test another FL language before claiming verified multilingual operation, and avoid translated
menu/root-label dependencies in the implementation. Do not promise arbitrary slot loading through
MIDI before proving it.

Implemented click behavior:

- Every load asks for confirmation. Instrument loading explicitly warns that FL may replace the
  selected channel; a new-channel guarantee is not available through the documented Browser API.
- Effects target the selected mixer insert. The controller refuses a full insert; confirmation also
  explains that an occupied slot may be replaced and asks the user to select an empty slot.
- The controller snapshots the selected channel and insert and refuses a changed selection before
  activation. It reports Browser handoff, without claiming the plugin finished loading.

The existing `Vext` port carries versioned SysEx request IDs. Authenticated request and response JSON
files under `<data>/fl-bridge` provide payloads and acknowledgments, avoiding a second MIDI port and
MIDI echo loops. Setup creates a shared random secret and the script's fixed IPC directory after
confirmation. Requests expire and duplicate IDs are rejected. The connection test performs only a
ping and never loads a plugin. Port availability and script response are separate states.

Provide a reviewed install/update action for the controller script in the configured FL user-data
folder's `Settings/Hardware/VEXT/` directory. Preview the destination and confirm before writing;
back up an existing VEXT script. Do not modify other scripts or automatically edit FL's MIDI settings.
Setup explains selecting `Vext` under FL MIDI inputs, enabling it, and choosing the VEXT controller.

## Implementation order and evidence needed

1. Build the preset resolver against temporary FL/VEXT fixtures. Cover ambiguous names, formats,
   manual links, individual Waves products, missing files and quarantine exclusions.
2. Prove native dragging with a disposable FL project and an isolated FL user-data folder: instrument
   to empty Channel Rack space; effect to an empty mixer slot; cancellation; occupied destinations;
   Unicode paths. Use stock plugins. Do not use the real catalog or FL folders as test fixtures.
3. Prove MIDI command delivery and exact-name loading through `Installed/<kind>/<format>` with a
   minimal controller script. Verify duplicate names across formats, folder ancestry, hidden file
   extensions, instrument/effect activation and destination, and acknowledgment routing. Repeat in
   another FL language to validate that the route does not depend on translated interface labels.
4. Integrate the two actions and connection setup into the existing UI. Test unavailable ports,
   disabled script, FL closed, blocked UI, changed Browser layout and stale preset paths.
5. Add user-facing changelog entries for implemented behavior. Run each relevant test script in
   its own process, check changed Python/JS/Rust, rebuild the backend sidecar, and remirror the
   frontend before desktop verification. The owner subsequently authorized local installer and portable builds; do not commit or publish them.

Verification: `tools/test_fl_launch.py` covers the resolver, script installation, API authentication,
confirmation, output buffer lifetime, reserved input ownership, request expiry, duplicates and a
simulated FL Browser with duplicate names across formats. `tools/test_fl_launch_ui.mjs` checks load
confirmation and held/released pointer timing for native drags. Existing FL Sync tests remain relevant.
These simulations do not replace a live FL Studio compatibility check. No real catalog or FL user-data
folder is used as a test fixture. Model-heavy GUI tooling remains subject to the repository's approval rule.

## Sources

- [Plugindex FL Studio overview](https://plugindex.app/fl-studio/)
- [Plugindex connection and loading help](https://plugindex.app/help/)
- [Image-Line MIDI scripting reference](https://www.image-line.com/fl-studio-learning/fl-studio-online-manual/html/midi_scripting.htm)
- [Image-Line Browser and plugin preset behavior](https://www.image-line.com/fl-studio-learning/fl-studio-online-manual/html/browser.htm)
- [CrabNebula native file dragging](https://docs.crabnebula.dev/plugins/drag-rs/)
- [loopMIDI developer documentation](https://www.tobias-erichsen.de/software/loopmidi.html)

## Follow-up: local controller inspection and connection state

The owner authorized read-only inspection of the installed Plugindex controller, with no GUI testing.
Public API call inspection confirmed Browser item navigation with 0/1, separate left/right calls and
Enter activation. VEXT implements its own bounded exact-route traversal and transport independently.
The catalog drag selector now covers the actual catalog containers and the Playground pick button.

The toolbar reports FL closed, running/disconnected, or connected. Connection requires the running FL
process, the current installed VEXT controller and a matching-secret heartbeat less than six seconds old.
The controller updates that file during OnIdle and removes it during OnDeInit. Both UI and backend block
click-to-load while disconnected; native preset dragging stays independent of the MIDI controller.
The controller must be updated and reloaded in FL after installing this build. Live loading and external
drop behavior remain unverified because the owner selected integration-file inspection only.


FL runtime correction: the installed Plugindex log confirms that FL does not supply `__file__`.
VEXT now captures its settings path while initializing, considering its current working directory and
module search paths, before another controller can change the working directory. A regression check
executes the controller without `__file__`, changes cwd, and verifies heartbeat creation and teardown.
The owner authorizes stopping VEXT for builds and requires a single `release/` output location.


FL file-API correction: the owner supplied a traceback at the AppData heartbeat write and at
os.remove during shutdown. Plugindex's controller-folder log continues to update on the same FL
runtime. Keep exchange files directly in the confirmed VEXT controller folder; only the backend's
connection configuration remains in its app data directory. Controller replies and heartbeats use
direct JSON writes, no rename or delete operations; backend polling tolerates partial JSON. Shutdown
writes a zero timestamp. SystemError is handled and repeated identical diagnostics are suppressed.
Regression checks simulate restricted external writes plus denied rename/remove, execute the controller
without __file__, change cwd, and verify heartbeat, ping reply and shutdown. Live FL remains unverified.


Browser traversal correction: the owner's real FL screenshot/error showed the expanded Plugin database
closing before Installed could be selected. After explicit expansion, use Down to select the first child;
do not send Right. Ancestry verification ends at the known root, so the second descent skips the global
Browser reset. The regression Browser models Right as a folder toggle and forbids its use after expansion.
Preset lookup caches parsed NFO fields by file timestamps/size, checks FL installation root once per
lookup and canonicalizes/validates only matching preset files. The frontend overlaps connection lookup
with preset lookup. Metadata edits and existing ancestry safety checks remain covered by tests.


Tab/scheduling correction: API inspection of the installed Plugindex script confirms explicit Plugins
selection and host-frame waits after Browser rebuilds. VEXT uses its own bounded first-tab/forward scan,
real-frame generator markers (the OnIdle pump returns immediately on those markers), and pure next-node
caption searches. Previous-node movement uses FPT_Up, while next-node movement uses 1. Tree mutations
and Left wait across actual host callbacks. Runtime heartbeat/reply/request revision 6 detects an older
loaded script even when its on-disk source was replaced. Regression tests model deferred tree rebuilds
that drop navigation until host callbacks return, missing Plugins tabs and stale runtime revisions.


Revision 7 reduces reset latency: one bounded Left burst for the selected branch, one timed settle,
then seek the Plugin database root. Remove the old nested per-node collapse/ascend loops. Verification
uses Left without an extra collapse, polls for the expected parent caption across real callbacks and
allows one more Left only if selection stayed in place. Failures report expected/observed captions.
Regressions cover a Left that collapses and ascends, a Left that first collapses in place, asynchronous
rebuilds and rejection of a genuinely wrong parent. No live GUI interaction was authorized.


Revision 8: user screenshots show VEXT remaining foreground after the Browser selects the correct
preset, while generic Left never changes that selection. Add a native main-webview-only fl_focus
command: enumerate visible unowned FL main windows, verify the process image name, restore if minimized,
request foreground focus from the foreground VEXT process, and read back the target HWND. Reject zero
or multiple FL instances, focus denial and calls while an unrelated app is foreground. No injected keys,
thread attachment, privilege changes or arbitrary target-window arguments. Frontend invokes the handoff
only after confirmation and before the load POST; denial prevents the POST. Controller verifies its own
Browser focus before ancestry traversal and activation. Live computer-use was authorized, but the UI
driver could not retain focus for keyboard input; a disposable-project readiness question remains pending.


Script-only direct-descent patch (explicit maintainer request, no VEXT rebuild): keep wire revision 8
and report SCRIPT_BUILD=direct-descent-1. Installed/Effects|Generators/format paths validate folder
levels during descent. Folder matching requires a folder node; known section/format boundaries stop a search from
escaping into another branch. The format's leaf scan stops at the next folder and accepts only SBN_FST.
No folders are collapsed during caption searches. After
finding the leaf, activate without Left or a second descent. Generic filed/manual routes keep their
existing conservative check. Test coverage includes duplicate presets in another format/branch, a missing
requested format, wrong leaf type, and generic Left being ineffective once the target is selected.
Back up and replace the installed script plus the running backend's extracted script reference so the
current byte-equality check accepts the hotpatch. Keep VEXT running during this test; restarting it
restores its bundled reference. No executable, release folder or installer is rebuilt in this step.


Revision 9 forwards controller runtime diagnostics into VEXT's existing session Log buffer. The script
writes at most 64 events with a controller-session ID/sequence, stage, message, traceback, request ID,
preset and build. The backend authenticates the snapshot, ignores pre-launch history, redacts the
shared secret and imports each event once. Load replies reference their diagnostic event to avoid
logging the same error twice; timeouts and send failures get backend fallback records. Log read,
status, text/export and clear routes collect diagnostics; clearing does not replay consumed events.
Tracebacks raised before controller initialization or while the diagnostic channel is inaccessible
cannot be forwarded; the bridge timeout remains visible. No permanent error history is added.
