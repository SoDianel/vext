# FL native support scope

Testing version: **VEXT 0.9.0-beta.1**.

VEXT previews installed plugins with an isolated editor, a stereo audio path,
MIDI notes for supported instruments, and local tempo/transport. It does not
provide FL Studio's mixer, piano roll, automation graph or hardware control.

## Available features

- Validated x64 stereo effects and MIDI instruments, as listed in
  [compatibility](FL_NATIVE_COMPATIBILITY.md).
- Editor-only viewing and picture capture for entries without audio support.
- Explicit Newtime/Newtone Monitor playback of clips loaded in their own editors.
  Monitor starts off; switching it off silences output while preserving editor Play
  state. Choose the audio rate before loading; reload clips after rate changes.
- Big Clock follows Playground tempo and position without an audio stream.

Availability is enforced by exact binary path and SHA-256. Catalog inclusion does
not mean every feature is supported. An updated binary requires renewed validation.

## Unavailable or limited features

- Patcher nested plugins and graph playback; Fruity Wrapper nested hosting.
  Open an ordinary VST2/VST3/CLAP target directly in Playground.
- FLEX, Fast Dist and VFX Script remain blocked. Peak Controller remains editor-only.
- Video/visualizer transport, Fruity Dance's animation overlay, and Pitcher MIDI harmony.
- FL mixer sends, automation destinations, piano-roll export, VFX note routing,
  extra audio buses, external MIDI/ReWire, lighting and voltage/CV hardware.
- HTML NoteBook document display and 32-bit FL native engines.
- General native preset/state persistence and sample formats beyond each plugin's
  listed support. VEXT does not select a sample, soundfont or Morphine preset for you.
- Newtime/Newtone browser/file input routing, Audio-tab recording, export and
  synchronized page playback remain unavailable or unverified.

These limits describe the current beta and do not promise future integrations.

## Per-plugin notices

These match the notices shown in Playground and the plugin-frame tooltip.
The [compatibility reference](FL_NATIVE_COMPATIBILITY.md) lists available capabilities.

| Installed plugin | Classification | User-facing limitation |
|---|---|---|
| Effects/Control Surface/Control Surface_x64.dll | Outside VEXT beta scope | FL Studio automation destinations are not provided by VEXT. Controls can be edited, but they do not control other plugins. |
| Effects/Edison/Edison_x64.dll | Not validated | Edison WAV loading and WAV export are validated. Other encoders and recording modes have not been validated in VEXT. |
| Effects/FL Studio Mobile Rack FX/FL Studio Mobile Rack FX_x64.dll | Supported with limits | The isolated Mobile project starts empty. Add an internal synth or effect module in the editor; VEXT does not load factory samples or an existing FL project automatically. |
| Effects/Fruity Big Clock/Fruity Big Clock_x64.dll | By design | Big Clock displays Playground tempo and playback position. It does not process audio or take MIDI notes. |
| Effects/Fruity Fast Dist/Fruity Fast Dist_x64.dll | Pending integration | Fruity Fast Dist requires FL Studio's host distortion service even to draw its editor. That service is unimplemented; the plugin stays blocked. No substitute distortion is used. |
| Effects/Fruity Formula Controller/Fruity Formula Controller_x64.dll | Outside VEXT beta scope | FL Studio automation destinations are not provided by VEXT. Controls can be edited, but they do not control other plugins. |
| Effects/Fruity HTML NoteBook/Fruity HTML NoteBook_x64.dll | Outside VEXT beta scope | HTML document display is disabled in VEXT. Browser helper processes and page network access remain disabled; the editor cannot display documents. |
| Effects/Fruity LSD/Fruity LSD_x64.dll | Outside VEXT beta scope | Fruity LSD requires a DirectX/MIDI output device. VEXT does not connect that device; selecting a patch does not produce sound. |
| Effects/Fruity NoteBook/Fruity NoteBook_x64.dll | Not validated | Text and pages can be edited in this session. VEXT does not restore notebook contents after closing the plugin. |
| Effects/Fruity NoteBook 2/Fruity NoteBook 2_x64.dll | Not validated | Text and pages can be edited in this session. VEXT does not restore notebook contents after closing the plugin. |
| Effects/Fruity Peak Controller/Fruity Peak Controller_x64.dll | Outside VEXT beta scope | FL Studio automation destinations are not provided by VEXT. Controls can be edited, but they do not control other plugins. |
| Effects/Fruity Peak Controller/Fruity Peak Controller_x64.dll | Pending integration | Peak Controller's LFO processing depends on an unavailable host table. Audio and ticks remain disabled to avoid its observed fault. |
| Effects/Fruity Scratcher/Fruity Scratcher_x64.dll | Supported with limits | Scratcher's folder button accepts WAV only. Its deck assumes 44.1 kHz samples, so playback speed changes with the engine rate. |
| Effects/Fruity Send/Fruity Send_x64.dll | Outside VEXT beta scope | Fruity Send targets FL Studio mixer tracks. VEXT has no mixer-track destinations; its send controls do not route audio. |
| Effects/Fruity Wrapper/Fruity Wrapper_x64.dll | Pending integration | Fruity Wrapper cannot select or host another plugin in VEXT yet. Open the target VST2, VST3 or CLAP plugin directly in Playground instead. |
| Effects/Fruity X-Y Controller/Fruity X-Y Controller_x64.dll | Outside VEXT beta scope | FL Studio automation destinations are not provided by VEXT. Controls can be edited, but they do not control other plugins. |
| Effects/Fruity X-Y-Z Controller/Fruity X-Y-Z Controller_x64.dll | Outside VEXT beta scope | FL Studio automation destinations are not provided by VEXT. Controls can be edited, but they do not control other plugins. |
| Effects/Newtime/Newtime_x64.dll | Supported with limits | Newtime plays clips loaded in its own editor. Enable Monitor and use its Play/Stop controls. Monitor off silences output but preserves the editor transport state. |
| Effects/Newtime/Newtime_x64.dll | Not validated | Export, Audio-tab recording and synchronized page playback are not validated. Browser/file audio is not routed through this editor. |
| Effects/Newtime/Newtime_x64.dll | Supported with limits | Choose the audio sample rate before loading a clip. If the rate changes or the device falls back to another rate, reload the clip in the editor. |
| Effects/Newtone/Newtone_x64.dll | Supported with limits | Newtone plays clips loaded in its own editor. Enable Monitor and use its Play/Stop controls. Monitor off silences output but preserves the editor transport state. |
| Effects/Newtone/Newtone_x64.dll | Not validated | Export, Audio-tab recording and synchronized page playback are not validated. Browser/file audio is not routed through this editor. |
| Effects/Newtone/Newtone_x64.dll | Supported with limits | Choose the audio sample rate before loading a clip. If the rate changes or the device falls back to another rate, reload the clip in the editor. |
| Effects/Patcher/Patcher_x64.dll | Pending integration | Patcher's graph editor opens, but nested plugin loading and graph playback are not supported yet. Add plugin folders stay empty until nested targets and routing are validated. |
| Effects/Pitcher/Pitcher_x64.dll | Pending integration | Pitcher's stereo audio processing is available. MIDI-controlled harmony and additional sidechains are not validated or enabled. |
| Effects/Razer Chroma/Razer Chroma_x64.dll | Outside VEXT beta scope | Razer lighting hardware and SDK output are not connected in VEXT. The editor does not control device lighting. |
| Effects/VFX Color Mapper/VFX Color Mapper_x64.dll | Outside VEXT beta scope | VFX Color Mapper needs a connected note graph for color-based note routing. VEXT does not provide that graph; editing does not send notes to another plugin. |
| Effects/VFX Envelope/VFX Envelope_x64.dll | Outside VEXT beta scope | VFX Envelope needs a connected note graph for per-note controller envelopes. VEXT does not provide that graph; editing does not send notes to another plugin. |
| Effects/VFX Key Mapper/VFX Key Mapper_x64.dll | Outside VEXT beta scope | VFX Key Mapper needs a connected note graph for note remapping. VEXT does not provide that graph; editing does not send notes to another plugin. |
| Effects/VFX Keyboard Splitter/VFX Keyboard Splitter_x64.dll | Outside VEXT beta scope | VFX Keyboard Splitter needs a connected note graph for keyboard-zone routing. VEXT does not provide that graph; editing does not send notes to another plugin. |
| Effects/VFX Level Scaler/VFX Level Scaler_x64.dll | Outside VEXT beta scope | VFX Level Scaler needs a connected note graph for note-level scaling. VEXT does not provide that graph; editing does not send notes to another plugin. |
| Effects/VFX Script/VFX Script_x64.dll | Pending integration | VFX Script startup or Compile can stall and needs unimplemented script-context services. The plugin stays blocked; scripts are not executed. |
| Effects/VFX Sequencer/VFX Sequencer_x64.dll | Outside VEXT beta scope | VFX Sequencer needs a connected note graph for sequenced note output. VEXT does not provide that graph; editing does not send notes to another plugin. |
| Effects/Vocodex/Vocodex_x64.dll | Outside VEXT beta scope | Vocodex supports stereo L-R encoding only. Enable that mode in its editor; separate carrier/modulator buses are not provided by VEXT. |
| Effects/ZGameEditor Visualizer/ZGameEditor Visualizer_x64.dll | Pending integration | ZGameEditor rendering, transport and resource cleanup are not validated. Only its editor is available; video rendering and audio processing remain disabled. |
| Generators/Dashboard/Dashboard_x64.dll | Outside VEXT beta scope | FL Studio automation destinations are not provided by VEXT. Controls can be edited, but they do not control other plugins. |
| Generators/FL Slayer/FL Slayer_x64.dll | Outside VEXT beta scope | FL Slayer needs a 32-bit FL native engine bridge. VEXT's 32-bit VST2 host cannot run that native engine; this plugin stays blocked. |
| Generators/FL Studio Mobile/FL Studio Mobile_x64.dll | Supported with limits | The isolated Mobile project starts empty. Add an internal synth or effect module in the editor; VEXT does not load factory samples or an existing FL project automatically. |
| Generators/FL Studio Mobile Rack/FL Studio Mobile Rack_x64.dll | Supported with limits | The isolated Mobile project starts empty. Add an internal synth or effect module in the editor; VEXT does not load factory samples or an existing FL project automatically. |
| Generators/FLEX/FLEX_x64.dll | Pending integration | FLEX startup is not validated in VEXT's isolated host. The plugin stays blocked; this is an unresolved integration, not proof that FLEX cannot be supported. |
| Generators/FPC/FPC_x64.dll | Outside VEXT beta scope | FPC uses its main stereo output only. Multi-output pad routing is not provided by VEXT; route pads to the main output in FPC. |
| Generators/Fruity Dance/Fruity dance_x64.dll | Pending integration | Fruity Dance's character uses a separate overlay window. Animation stays disabled until that window can be hosted and captured correctly. |
| Generators/Fruity Envelope Controller/Fruity Envelope Controller_x64.dll | Outside VEXT beta scope | FL Studio automation destinations are not provided by VEXT. Controls can be edited, but they do not control other plugins. |
| Generators/Fruity Granulizer/Fruity Granulizer_x64.dll | Supported with limits | Granulizer's sample area accepts WAV only. Load a sample before playing notes; VEXT does not select one automatically. |
| Generators/Fruity Keyboard Controller/Fruity Keyboard Controller_x64.dll | Outside VEXT beta scope | FL Studio automation destinations are not provided by VEXT. Controls can be edited, but they do not control other plugins. |
| Generators/Fruity Slicer/Fruity Slicer_x64.dll | Supported with limits | This slicer accepts WAV only and uses its own slicing. Embedded file regions are ignored; notes from MIDI 60 play the slices. |
| Generators/Fruity Slicer 2/Fruity Slicer 2_x64.dll | Supported with limits | This slicer accepts WAV only and uses its own slicing. Embedded file regions are ignored; notes from MIDI 60 play the slices. |
| Generators/Fruity Slicer 2/Fruity Slicer 2_x64.dll | Outside VEXT beta scope | Edit > Open does not launch an external sample editor in VEXT. Use the native toolbar picker to load a WAV. |
| Generators/Fruity Video Player/Fruity Video Player_x64.dll | Pending integration | Fruity Video Player can select a video, but advancing frames and synchronized playback are not validated. Video transport, audio and MIDI remain disabled. |
| Generators/Fruity Voltage Controller/Fruity Voltage Controller_x64.dll | Outside VEXT beta scope | Voltage/CV hardware output is outside VEXT's plugin-preview scope. This plugin is blocked before loading. |
| Generators/Fruity Wrapper/Fruity Wrapper_x64.dll | Pending integration | Fruity Wrapper cannot select or host another plugin in VEXT yet. Open the target VST2, VST3 or CLAP plugin directly in Playground instead. |
| Generators/MIDI Out/MIDI Out_x64.dll | Outside VEXT beta scope | MIDI Out does not open an external MIDI port in VEXT. Its controls do not send data to hardware or other applications. |
| Generators/Morphine/Morphine_x64.dll | Pending integration | Morphine starts with a silent program. Load a preset in its editor; factory-library browsing through the host is not available. |
| Generators/Patcher/Patcher_x64.dll | Pending integration | Patcher's graph editor opens, but nested plugin loading and graph playback are not supported yet. Add plugin folders stay empty until nested targets and routing are validated. |
| Generators/ReWired/ReWired_x64.dll | Outside VEXT beta scope | ReWired requires an external ReWire application and transport routing. VEXT does not launch or connect ReWire applications. |
| Generators/Slicex/Slicex_x64.dll | Outside VEXT beta scope | Slicex plays loaded slices, but VEXT has no piano roll. Dump score and automatic note export do not create or replay notes. |
| Generators/Wasp/Wasp_x64.dll | Outside VEXT beta scope | The installed x64 Wasp file is a compatibility notice, not a synth engine. 32-bit FL native engines are outside VEXT beta support. |
| Generators/Wasp XT/Wasp XT_x64.dll | Outside VEXT beta scope | The installed x64 Wasp file is a compatibility notice, not a synth engine. 32-bit FL native engines are outside VEXT beta support. |
| Generators/Wave Traveller/Wave Traveller_x64.dll | Supported with limits | Wave Traveller accepts WAV only. Its default curve traverses the sample in one bar, so playback pitch follows Playground tempo. |
