# FL native plugin compatibility

Testing version: **VEXT 0.9.0-beta.1**.

VEXT supports the installed x64 native binaries identified in
`app/hosting/fl-native-compat.json`. Support is checked against the exact binary
path and SHA-256. An unrecognized version remains unavailable until validated.
Image-Line plugins and factory content are not bundled with VEXT.

The catalog contains 141 primary entries: 65 stereo effects, 37 MIDI instruments,
two editor Monitor plugins, one clock display, and editor-only or blocked entries.
Stereo audio and MIDI validation covers 44.1, 48 and 96 kHz for enabled entries.
These capabilities do not establish support for every preset, editor feature,
additional output, or FL Studio version.

- **Stereo audio:** preview through VEXT's stereo audio path.
- **MIDI + stereo audio:** play the instrument through VEXT; sample-dependent
  instruments may start silent until you load their content in the editor.
- **Editor Monitor:** Newtime/Newtone play editor-loaded clips only after you enable
  Monitor. Use the native editor's transport. Choose the audio rate before loading.
- **Clock display:** follows Playground tempo and position without producing audio.
- **Editor only:** open the editor and capture its picture; audio and MIDI stay disabled.
- **Unavailable:** the installed entry is blocked before loading.

See [support scope](FL_NATIVE_SCOPE.md) for release limitations. Notes below match
the plugin-specific notices in Playground. A limitation notice never enables a capability.

| Plugin | Section | Available in VEXT | Limits and setup |
|---|---|---|---|
| Control Surface | Effects | Editor only | FL Studio automation destinations are not provided by VEXT. Controls can be edited, but they do not control other plugins. |
| Distructor | Effects | Stereo audio | ? |
| Edison | Effects | Stereo audio | Edison WAV loading and WAV export are validated. Other encoders and recording modes have not been validated in VEXT. |
| Effector | Effects | Stereo audio | ? |
| Emphasis | Effects | Stereo audio | ? |
| Emphasizer | Effects | Stereo audio | ? |
| EQUO | Effects | Stereo audio | ? |
| Mobile Rack FX | Effects | Stereo audio | The isolated Mobile project starts empty. Add an internal synth or effect module in the editor; VEXT does not load factory samples or an existing FL project automatically. |
| Frequency Shifter | Effects | Stereo audio | ? |
| Frequency Splitter | Effects | Stereo audio | ? |
| Fruity 7 Band Equalizer | Effects | Stereo audio | ? |
| Fruity Balance | Effects | Stereo audio | ? |
| Fruity Bass Boost | Effects | Stereo audio | ? |
| Fruity Big Clock | Effects | Clock display | Big Clock displays Playground tempo and playback position. It does not process audio or take MIDI notes. |
| Fruity Blood Overdrive | Effects | Stereo audio | ? |
| Fruity Center | Effects | Stereo audio | ? |
| Fruity Chorus | Effects | Stereo audio | ? |
| Fruity Compressor | Effects | Stereo audio | ? |
| Fruity Convolver | Effects | Stereo audio | ? |
| Fruity dB meter | Effects | Stereo audio | ? |
| Fruity Delay | Effects | Stereo audio | ? |
| Fruity delay 2 | Effects | Stereo audio | ? |
| Fruity Delay 3 | Effects | Stereo audio | ? |
| Fruity delay bank | Effects | Stereo audio | ? |
| Fruity Fast Dist | Effects | Unavailable | Fruity Fast Dist requires FL Studio's host distortion service even to draw its editor. That service is unimplemented; the plugin stays blocked. No substitute distortion is used. |
| Fruity Fast LP | Effects | Stereo audio | ? |
| Fruity Filter | Effects | Stereo audio | ? |
| Fruity Flanger | Effects | Stereo audio | ? |
| Fruity Flangus | Effects | Stereo audio | ? |
| Fruity formula controller | Effects | Editor only | FL Studio automation destinations are not provided by VEXT. Controls can be edited, but they do not control other plugins. |
| Fruity Free Filter | Effects | Stereo audio | ? |
| Fruity HTML NoteBook | Effects | Editor only | HTML document display is disabled in VEXT. Browser helper processes and page network access remain disabled; the editor cannot display documents. |
| Fruity Limiter | Effects | Stereo audio | ? |
| Fruity Love Philter | Effects | Stereo audio | ? |
| Fruity LSD | Effects | Editor only | Fruity LSD requires a DirectX/MIDI output device. VEXT does not connect that device; selecting a patch does not produce sound. |
| Fruity Multiband Compressor | Effects | Stereo audio | ? |
| Fruity Mute 2 | Effects | Stereo audio | ? |
| Fruity NoteBook | Effects | Editor only | Text and pages can be edited in this session. VEXT does not restore notebook contents after closing the plugin. |
| Fruity NoteBook 2 | Effects | Editor only | Text and pages can be edited in this session. VEXT does not restore notebook contents after closing the plugin. |
| Fruity PanOMatic | Effects | Stereo audio | ? |
| Fruity parametric EQ | Effects | Stereo audio | ? |
| Fruity parametric EQ 2 | Effects | Stereo audio | ? |
| Fruity peak controller | Effects | Editor only | FL Studio automation destinations are not provided by VEXT. Controls can be edited, but they do not control other plugins. Peak Controller's LFO processing depends on an unavailable host table. Audio and ticks remain disabled to avoid its observed fault. |
| Fruity Phase Inverter | Effects | Stereo audio | ? |
| Fruity Phaser | Effects | Stereo audio | ? |
| Fruity Reeverb | Effects | Stereo audio | ? |
| Fruity Reeverb 2 | Effects | Stereo audio | ? |
| Fruity Scratcher | Effects | Stereo audio | Scratcher's folder button accepts WAV only. Its deck assumes 44.1 kHz samples, so playback speed changes with the engine rate. |
| Fruity send | Effects | Editor only | Fruity Send targets FL Studio mixer tracks. VEXT has no mixer-track destinations; its send controls do not route audio. |
| Fruity soft clipper | Effects | Stereo audio | ? |
| Fruity Spectroman | Effects | Stereo audio | ? |
| Fruity Squeeze | Effects | Stereo audio | ? |
| Fruity Stereo Enhancer | Effects | Stereo audio | ? |
| Fruity Stereo Shaper | Effects | Stereo audio | ? |
| Fruity Vocoder | Effects | Stereo audio | ? |
| Fruity WaveShaper | Effects | Stereo audio | ? |
| Fruity Wrapper | Effects | Editor only | Fruity Wrapper cannot select or host another plugin in VEXT yet. Open the target VST2, VST3 or CLAP plugin directly in Playground instead. |
| Fruity X-Y controller | Effects | Editor only | FL Studio automation destinations are not provided by VEXT. Controls can be edited, but they do not control other plugins. |
| Fruity X-Y-Z Controller | Effects | Editor only | FL Studio automation destinations are not provided by VEXT. Controls can be edited, but they do not control other plugins. |
| Gross Beat | Effects | Stereo audio | ? |
| Hardcore | Effects | Stereo audio | ? |
| Hyper Chorus | Effects | Stereo audio | ? |
| Low Lifter | Effects | Stereo audio | ? |
| LuxeVerb | Effects | Stereo audio | ? |
| Maximus | Effects | Stereo audio | ? |
| Multiband Delay | Effects | Stereo audio | ? |
| Newtime | Effects | Editor Monitor | Newtime plays clips loaded in its own editor. Enable Monitor and use its Play/Stop controls. Monitor off silences output but preserves the editor transport state. Export, Audio-tab recording and synchronized page playback are not validated. Browser/file audio is not routed through this editor. Choose the audio sample rate before loading a clip. If the rate changes or the device falls back to another rate, reload the clip in the editor. |
| Newtone | Effects | Editor Monitor | Newtone plays clips loaded in its own editor. Enable Monitor and use its Play/Stop controls. Monitor off silences output but preserves the editor transport state. Export, Audio-tab recording and synchronized page playback are not validated. Browser/file audio is not routed through this editor. Choose the audio sample rate before loading a clip. If the rate changes or the device falls back to another rate, reload the clip in the editor. |
| Patcher | Effects | Editor only | Patcher's graph editor opens, but nested plugin loading and graph playback are not supported yet. Add plugin folders stay empty until nested targets and routing are validated. |
| Pitch Shifter | Effects | Stereo audio | ? |
| Pitcher | Effects | Stereo audio | Pitcher's stereo audio processing is available. MIDI-controlled harmony and additional sidechains are not validated or enabled. |
| Razer Chroma | Effects | Editor only | Razer lighting hardware and SDK output are not connected in VEXT. The editor does not control device lighting. |
| Soundgoodizer | Effects | Stereo audio | ? |
| Spreader | Effects | Stereo audio | ? |
| Transient Processor | Effects | Stereo audio | ? |
| Transmitter | Effects | Stereo audio | ? |
| Transporter | Effects | Stereo audio | ? |
| Tuner | Effects | Stereo audio | ? |
| VFX Color Mapper | Effects | Editor only | VFX Color Mapper needs a connected note graph for color-based note routing. VEXT does not provide that graph; editing does not send notes to another plugin. |
| VFX Envelope | Effects | Editor only | VFX Envelope needs a connected note graph for per-note controller envelopes. VEXT does not provide that graph; editing does not send notes to another plugin. |
| VFX Key Mapper | Effects | Editor only | VFX Key Mapper needs a connected note graph for note remapping. VEXT does not provide that graph; editing does not send notes to another plugin. |
| VFX Keyboard Splitter | Effects | Editor only | VFX Keyboard Splitter needs a connected note graph for keyboard-zone routing. VEXT does not provide that graph; editing does not send notes to another plugin. |
| VFX Level Scaler | Effects | Editor only | VFX Level Scaler needs a connected note graph for note-level scaling. VEXT does not provide that graph; editing does not send notes to another plugin. |
| VFX Script | Effects | Unavailable | VFX Script startup or Compile can stall and needs unimplemented script-context services. The plugin stays blocked; scripts are not executed. |
| VFX Sequencer | Effects | Editor only | VFX Sequencer needs a connected note graph for sequenced note output. VEXT does not provide that graph; editing does not send notes to another plugin. |
| Vintage Chorus | Effects | Stereo audio | ? |
| Vintage Phaser | Effects | Stereo audio | ? |
| Vocodex | Effects | Stereo audio | Vocodex supports stereo L-R encoding only. Enable that mode in its editor; separate carrier/modulator buses are not provided by VEXT. |
| Wave Candy | Effects | Stereo audio | ? |
| ZGameEditor Visualizer | Effects | Editor only | ZGameEditor rendering, transport and resource cleanup are not validated. Only its editor is available; video rendering and audio processing remain disabled. |
| 3x Osc | Generators | MIDI + stereo audio | ? |
| Autogun | Generators | MIDI + stereo audio | ? |
| Bassdrum | Generators | MIDI + stereo audio | ? |
| BeepMap | Generators | MIDI + stereo audio | ? |
| BooBass | Generators | MIDI + stereo audio | ? |
| Dashboard | Generators | Editor only | FL Studio automation destinations are not provided by VEXT. Controls can be edited, but they do not control other plugins. |
| DirectWave Player | Generators | MIDI + stereo audio | ? |
| Drumaxx | Generators | MIDI + stereo audio | ? |
| Drumpad | Generators | MIDI + stereo audio | ? |
| FL Keys | Generators | MIDI + stereo audio | ? |
| FL Slayer | Generators | Unavailable | FL Slayer needs a 32-bit FL native engine bridge. VEXT's 32-bit VST2 host cannot run that native engine; this plugin stays blocked. |
| FL Studio Mobile | Generators | MIDI + stereo audio | The isolated Mobile project starts empty. Add an internal synth or effect module in the editor; VEXT does not load factory samples or an existing FL project automatically. |
| Mobile Rack | Generators | MIDI + stereo audio | The isolated Mobile project starts empty. Add an internal synth or effect module in the editor; VEXT does not load factory samples or an existing FL project automatically. |
| FLEX | Generators | Unavailable | FLEX startup is not validated in VEXT's isolated host. The plugin stays blocked; this is an unresolved integration, not proof that FLEX cannot be supported. |
| FPC | Generators | MIDI + stereo audio | FPC uses its main stereo output only. Multi-output pad routing is not provided by VEXT; route pads to the main output in FPC. |
| Fruit kick | Generators | MIDI + stereo audio | ? |
| Fruity Dance | Generators | Editor only | Fruity Dance's character uses a separate overlay window. Animation stays disabled until that window can be hosted and captured correctly. |
| Fruity DrumSynth Live | Generators | MIDI + stereo audio | ? |
| Fruity DX10 | Generators | MIDI + stereo audio | ? |
| Fruity Envelope Controller | Generators | Editor only | FL Studio automation destinations are not provided by VEXT. Controls can be edited, but they do not control other plugins. |
| Fruity granulizer | Generators | MIDI + stereo audio | Granulizer's sample area accepts WAV only. Load a sample before playing notes; VEXT does not select one automatically. |
| Fruity Keyboard Controller | Generators | Editor only | FL Studio automation destinations are not provided by VEXT. Controls can be edited, but they do not control other plugins. |
| Fruity Slicer | Generators | MIDI + stereo audio | This slicer accepts WAV only and uses its own slicing. Embedded file regions are ignored; notes from MIDI 60 play the slices. |
| Fruity Slicer 2 | Generators | MIDI + stereo audio | This slicer accepts WAV only and uses its own slicing. Embedded file regions are ignored; notes from MIDI 60 play the slices. Edit > Open does not launch an external sample editor in VEXT. Use the native toolbar picker to load a WAV. |
| Fruity Video Player | Generators | Editor only | Fruity Video Player can select a video, but advancing frames and synchronized playback are not validated. Video transport, audio and MIDI remain disabled. |
| Fruity Voltage Controller | Generators | Unavailable | Voltage/CV hardware output is outside VEXT's plugin-preview scope. This plugin is blocked before loading. |
| Fruity Wrapper | Generators | Editor only | Fruity Wrapper cannot select or host another plugin in VEXT yet. Open the target VST2, VST3 or CLAP plugin directly in Playground instead. |
| GMS | Generators | MIDI + stereo audio | ? |
| Harmless | Generators | MIDI + stereo audio | ? |
| Harmor | Generators | MIDI + stereo audio | ? |
| Kepler | Generators | MIDI + stereo audio | ? |
| Kepler Exo | Generators | MIDI + stereo audio | ? |
| MIDI Out | Generators | Editor only | MIDI Out does not open an external MIDI port in VEXT. Its controls do not send data to hardware or other applications. |
| MiniSynth | Generators | MIDI + stereo audio | ? |
| Morphine | Generators | MIDI + stereo audio | Morphine starts with a silent program. Load a preset in its editor; factory-library browsing through the host is not available. |
| Ogun | Generators | MIDI + stereo audio | ? |
| Patcher | Generators | Editor only | Patcher's graph editor opens, but nested plugin loading and graph playback are not supported yet. Add plugin folders stay empty until nested targets and routing are validated. |
| PLUCKED! | Generators | MIDI + stereo audio | ? |
| Poizone | Generators | MIDI + stereo audio | ? |
| ReWired | Generators | Editor only | ReWired requires an external ReWire application and transport routing. VEXT does not launch or connect ReWire applications. |
| Sakura | Generators | MIDI + stereo audio | ? |
| Sawer | Generators | MIDI + stereo audio | ? |
| SimSynth Live | Generators | MIDI + stereo audio | ? |
| Slicex | Generators | MIDI + stereo audio | Slicex plays loaded slices, but VEXT has no piano roll. Dump score and automatic note export do not create or replay notes. |
| SoundFont Player | Generators | MIDI + stereo audio | ? |
| Sytrus | Generators | MIDI + stereo audio | ? |
| Toxic Biohazard | Generators | MIDI + stereo audio | ? |
| Transistor Bass | Generators | MIDI + stereo audio | ? |
| WASP | Generators | Unavailable | The installed x64 Wasp file is a compatibility notice, not a synth engine. 32-bit FL native engines are outside VEXT beta support. |
| WASP XT | Generators | Unavailable | The installed x64 Wasp file is a compatibility notice, not a synth engine. 32-bit FL native engines are outside VEXT beta support. |
| Wave Traveller | Generators | MIDI + stereo audio | Wave Traveller accepts WAV only. Its default curve traverses the sample in one bar, so playback pitch follows Playground tempo. |
