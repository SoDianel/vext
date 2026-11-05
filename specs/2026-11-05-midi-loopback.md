# MIDI loopback: Windows devices for VEXT and other applications

Status: startup integration implemented and tested. The maintainer confirmed that the rebuilt desktop app creates `VEXT` and FL Studio lists it. The installed teVirtualMIDI driver remains required; no SDK files are bundled. Distribution clearance, end-to-end controller/plugin loading, second-DAW and broader Windows 11 acceptance remain open.

## Intended result

VEXT should have a clear MIDI connection setup, and ultimately own a virtual MIDI cable
that FL Studio and other Windows MIDI applications can select like a hardware device.
Device availability, successful transmission and an authenticated FL controller connection
are distinct states. None alone proves that a plugin loaded.

Implement the public setup/diagnostics foundation and a private native-port evaluation now.
Shipping an integrated teVirtualMIDI provider is a separate licensing gate, not a reason to
stop the evaluation or improvements to the existing WinMM integration.

## Evidence and constraints

Examined the installed SDK's PDF (1.3.0.43), C header/sample, C# binding and both products'
release notes. Resolve its location from ProgramFiles(x86) when needed; do not put a
machine-specific absolute path in source, tests or documentation.

- loopMIDI is a user-space application above the teVirtualMIDI DLL and kernel driver.
  Creating a WinMM output handle does not create a Windows device. VEXT currently does
  only the former. The SDK supplies the missing device creation/private endpoint API.
- The installed x64 and x86 system DLLs report 1.3.0.43. File presence is evidence of an
  installation, not proof that the driver can create a port or supports every target OS.
- The SDK PDF, page 1, allows personal/internal use and forbids distributing the SDK or
  software using it without prior written permission. The official SDK page repeats the
  distribution restriction. A user-installed DLL or ctypes loading does not establish
  distribution permission. VEXT is GPL-3.0-or-later; clearance must address that model.
- The vendor offers a driver installer module to licensees. Do not copy its installed
  DLLs, drivers, samples, headers, import libraries or SDK PDF into VEXT or its builds.
- The vendor's pages/PDF describe Windows through Windows 10. Windows 11 is a required
  VEXT acceptance target, but compatibility remains to be verified on that OS.
- Ports live with their creating application. Closing/dying releases them, or leaves an
  inactive public endpoint while a client still holds it. DAWs may require a MIDI-device
  refresh or restart after creation/recreation. Persistent settings do not make a port
  permanent, and no Windows service/autostart registration is needed for this increment.

Primary references:

- [virtualMIDI architecture](https://www.tobias-erichsen.de/software/virtualmidi.html)
- [SDK and distribution conditions](https://www.tobias-erichsen.de/software/virtualmidi/virtualmidi-sdk.html)
- [loopMIDI lifecycle](https://www.tobias-erichsen.de/software/loopmidi.html)

Local reference filenames: `teVirtualMIDISDK/teVirtualMIDISDK.pdf`,
`teVirtualMIDISDK/C-Binding/teVirtualMIDI.h`, `teVirtualMIDITest.c`,
`teVirtualMIDISDK/Cs-Binding/teVirtualMIDI.cs`, and each product's `release_notes.txt`.
These are reference materials, not VEXT dependencies or redistributable resources.

## Existing VEXT integration

| Area | Current implementation and requirement |
| --- | --- |
| Musical input | `app/hosting/midi.py`: WinMM channel messages; reserve `fl_launch_port`, including `*` selection. |
| Input ownership | `app/midi_monitor.py` and plugin workers transfer ownership; preserve this. |
| FL output | `app/hosting/midi_output.py`: resolve exact output name on each send; reject missing/duplicate names; SysEx request ID. |
| FL bridge | `app/fl_launch.py`: confirmed controller install, authenticated files, revision 9 heartbeat, serialized requests and no automatic load retry. |
| Setup UI | `app/static/js/fl-launch.js`: output selection/controller install/test in Configuration > DAWs > FL Studio. |
| MIDI UI | `app/static/js/audio-settings.js`: Configuration > MIDI input/channel/pitch-bend settings. |
| API/security | `app/web.py`, `app/fl_launch_api.py`, `app/security.py`: local API and mutation token. GETs inspect only. |

## First implementation: public application code

1. Add a small driver-independent MIDI loopback diagnostics module and read-only authenticated-
   API integration (following existing security middleware conventions). Suggested endpoint:
   `GET /api/midi/loopback`. Inspect inputs and outputs without opening them. Enumerate
   safely if WinMM is unavailable. Keep optional native integrations lazy.
2. Report the configured `fl_launch_port` (retain default `Vext` and existing saved values),
   exact input/output match counts, unambiguous availability and an actionable short message.
   Include output choices if useful to both setup panes. A same-name input/output pair is
   not proof of loopback capability or ownership: arbitrary drivers can expose both.
3. Distinguish missing, ambiguous, unavailable/enumeration error, and available. An available
   output can be busy; enumeration does not prove opening/sending works. FL controller
   connection remains a separate field sourced from existing authenticated heartbeat checks.
   Do not present DLL presence or a loopMIDI process as proof of a usable port.
4. Show a compact Virtual MIDI subsection in Configuration > MIDI: selected command port,
   availability, Refresh, plain manual setup guidance and a route to FL controller setup.
   Make output selection available here or reuse the DAW setup selector without duplicating
   setting state. Any saved selection must still use the existing validated settings API.
   Explain that other apps select the matching input, and that loopMIDI must remain running
   for its ports. Distinguish creating a cable in loopMIDI from configuring FL's controller.
   Do not show an apparently functional Create driver button in the public build.
5. Reuse diagnostics in FL setup so a missing/ambiguous port is apparent before testing.
   Keep manual selection of a physical/external output supported; do not silently rename,
   replace or take ownership of an existing port. All visible text/HTML follows existing
   escaping, accessibility, configuration navigation and palette conventions.
6. Improve MIDI error messages to describe the selected output and operation accurately;
   loopMIDI is an external-provider example, not the only possible driver. Preserve existing
   send behavior and exact-name resolution. No new generic HTTP endpoint for arbitrary MIDI
   bytes, no unsolicited test notes/clock, and no automatic ping on page load.
7. Keep the FL protocol/controller unchanged for this transport/setup increment. Preserve
   focus handoff, preset validation, heartbeat, confirmations and unknown-outcome handling.
   Port changes must continue to release/reconcile reserved inputs in monitor and workers.

## First implementation: private native-port evaluation

Keep every SDK-dependent executable source and test under git-ignored
`notes/midi-loopback-evaluation/`. Do not import it from `app/`, add it to a spec, or copy
it to build outputs. Public code uses its public WinMM endpoints exactly as it uses
loopMIDI; this avoids linking the public application to the SDK during evaluation.

Write an original Python ctypes evaluation helper using only the installed system driver:

- Explicit manual CLI start/stop; no driver installation, service, scheduled task,
  autostart or edits to FL/loopMIDI settings. Document a scratch VEXT_ROOT dev-server flow
  for evaluating it with VEXT, without touching the installed app's real database.
- Discover/load the correct installed DLL through a trusted absolute Windows system path;
  avoid PATH/current-directory search and SDK binaries. Use WinDLL with last-error support,
  correct argtypes/restype and pointer-sized handles/instance fields. x64 helper only;
  public endpoints should be testable from x86 and x64 client applications later.
- Use `virtualMIDICreatePortEx2`, parsed receive, bidirectional creation, and a 65,535-byte
  maximum SysEx length. Validate nonempty Unicode port names (at most 31 UTF-16 code units
  and no forbidden path characters). Never auto-suffix a collision or reuse another
  application's device. Manual helper default may be `VEXT Bridge`; tests use unique names.
- Implement the cable: public MIDI output -> SDK receive -> unchanged complete message ->
  SDK send -> public MIDI input. Simply creating the bidirectional device does not relay
  messages. Preserve channel voice, system common, realtime and complete SysEx messages.
  Keep the existing FL SysEx request byte sequence unchanged.
- Prefer blocking receive on a dedicated thread, or a properly pinned native callback
  copying into a bounded FIFO. Keep sends serialized per port; no sends concurrent with
  close. If callbacks are used, never close from inside one; keep it alive through close.
- Use explicit shutdown/abort to unblock receive, join the worker, then close exactly once.
  No unbounded joins, DLL unload with a live callback/read, or freed buffers still in use.
  Handle Ctrl+C, partial initialization and driver disappearance without thread tracebacks.
- Add bounded feedback detection (e.g. configurable sustained message/byte rate guard),
  counted overflow/failure diagnostics and latched mute on feedback/overflow. Recovery is
  explicit; do not automatically resend or silently discard a note-off and continue playing.
  Do not log every packet or payload by default. Never route this endpoint into VEXT's
  musical-input selection automatically or echo FL control commands through the monitor.
- Surface DLL missing, inactive driver, version mismatch, name collision, disabled port,
  allocation/resource exhaustion and send failure with operation and Windows error code.
  Transmit count means handed to the driver, not acknowledged by a DAW.

Provide a private README with commands, architecture, shutdown/feedback policy and test
evidence. Prefer a private opt-in smoke script which creates a unique temporary port,
opens its own WinMM endpoints and checks byte-identical channel messages, realtime and
VEXT-shaped SysEx. It must not use existing ports, real app data, FL presets or plugin lists.
No GUI/Playwright/computer-use or further subagents are authorized by this dispatch.

## Verification and delivery

- Meaningful public tests: missing/duplicate endpoints; enumeration failure; exact selected
  port; read-only status; API mutation protection unaffected; UI states and escaped names;
  reserved-input exclusion after output selection; existing FL bridge tests remain green.
- Private fake-native tests: ABI declarations, message order and bytes, send serialization,
  size/name validation, driver loss, feedback/overflow and close/read races. Run each Python
  test file in its own process, with temp VEXT_ROOT/LOCALAPPDATA/APPDATA and fake FL trees.
- An optional live private smoke test may create only a uniquely named temporary port and
  its own clients. Clean up on every path. Record what was actually observed. No automatic
  interaction with FL Studio or real user files; an FL/second-DAW end-to-end test remains
  manual and must not be claimed from mocks or local echo alone.
- Use the existing `.venv-native` Python and local lint/test tools. Avoid unrelated test
  suites, model review workflows or dependency changes. Update `CHANGELOG.md` under 1.0.0
  for the final public behavior only; keep Unreleased empty and versions at 1.0.0.
- Run the required backend sidecar rebuild after public `app/` changes. Before it, inspect
  for running VEXT processes. If any are running, report the exact blocker instead of
  terminating a user's session. Use the standard output locations and bundle check; do
  not create alternate release folders or a full release/Sandbox workflow for this task.
- Verify the private helper/SDK/DLLs are absent from tracked changes and public bundles.
  A source-only prototype and passing tests do not satisfy the distribution licensing gate.
- Development used `feat/midi-loopback`; the maintainer subsequently requested a rebase-only
  PR merge into `main`, retaining version 1.0.0. Installer publication and vendor contact
  remain separate actions. Record test evidence and remaining interoperability gaps.

## Local startup evaluation (2026-11-05)

This increment supersedes the earlier private-only implementation restriction for a local evaluation build. The backend creates the exact `VEXT` port at startup using the installed teVirtualMIDI driver and releases it on shutdown; there is no bundled driver or independent kernel implementation. The provider is optional and `VEXT_VIRTUAL_MIDI=0` disables it. Driver failure leaves external outputs available. Only after successful ownership are legacy `Vext` and `VEXT Bridge` selections migrated; custom selections remain. Both the owned port and configured command port are excluded from musical inputs. Ownership, endpoint listing, driver transmission and FL's authenticated heartbeat remain separate states.

The installed driver and adapter are subject to the vendor's distribution terms. This local build is not cleared for distribution, and moving the adapter into application code does not grant that clearance. A stale public endpoint may remain visible while another application holds it; visibility does not mean the backend owns it. FL/second-DAW acceptance and broader Windows 11 coverage remain manual work.

The maintainer confirmed startup device visibility in FL Studio using the updated desktop
executable. The existing portable folder contained an older backend and did not create the
port; build paths must be checked when diagnosing a missing device. Restarting FL cleared
the stale standalone prototype entry. This verifies listing, not controller acknowledgments
or plugin loading. Targeted startup, reservation, diagnostics and UI checks passed, alongside
69 FL bridge tests, isolated five-message WinMM echo and backend/x86 bundle checks.

## Later shipping phase

After written SDK/GPL distribution clearance (or selection and separate analysis of a
compatible alternative), complete a distributable app-owned port provider with explicit enable/disable,
persisted intent, per-launch ownership and orderly shutdown. Installer integration must use
authorized signed driver packages with explicit user consent. Never disable Secure Boot or
driver signature enforcement. Reconcile owned ports before DAW/device enumeration and
retain external-port fallback. Decide musical routing and additional virtual cables in a
separate brief; this increment creates no arbitrary routing graph or custom kernel driver.
