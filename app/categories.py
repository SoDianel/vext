"""Plugin category/type reference data.

Sourced from Steinberg's official VST3 SDK plug-in sub-category constants
(PlugType), which is the same vocabulary VST3 bundles already report in their
own moduleinfo.json "Sub Categories" field — so scanned data lines up with the
reference table instead of us inventing a parallel taxonomy.

Source: https://steinbergmedia.github.io/vst3_doc/vstinterfaces/group__plugType.html
(fetched 2026-09-28). Pure channel-configuration/processing-mode flags (Mono,
Stereo, Surround, OnlyRT, OnlyARA, NoOfflineProcess, OnlyOfflineProcess) are
intentionally excluded — those describe how a plugin processes audio, not what
kind of tool it is, so they don't belong in a vendor/category tagging system.
"""

SOURCE_URL = "https://steinbergmedia.github.io/vst3_doc/vstinterfaces/group__plugType.html"

# (slug, display name, VST3 "Sub Categories" string value, description)
CATEGORIES = [
    ("fx", "Effect (uncategorized)", "Fx", "Other effect type, not further categorized."),
    ("analyzer", "Analyzer", "Fx|Analyzer", "Scope, FFT display, loudness processing/metering."),
    ("bass", "Bass Tools", "Fx|Bass", "Tools dedicated to bass guitar/instrument processing."),
    ("channel-strip", "Channel Strip", "Fx|Channel Strip", "Combined channel-strip style processing tools."),
    ("delay", "Delay", "Fx|Delay", "Delay, multi-tap delay, ping-pong delay."),
    ("distortion", "Distortion", "Fx|Distortion", "Amp simulator, sub-harmonic, soft clipper, saturation."),
    ("drums", "Drum Tools", "Fx|Drums", "Tools dedicated to drums."),
    ("dynamics", "Dynamics", "Fx|Dynamics", "Compressor, expander, gate, limiter, maximizer, tape simulator, envelope shaper."),
    ("eq", "EQ", "Fx|EQ", "Equalization, graphical EQ."),
    ("filter", "Filter", "Fx|Filter", "Wah-wah, tone booster, and other specific filters."),
    ("generator", "Generator", "Fx|Generator", "Tone generator, noise generator."),
    ("guitar", "Guitar Tools", "Fx|Guitar", "Tools dedicated to guitar."),
    ("fx-instrument", "Effect Usable as Instrument", "Fx|Instrument", "Effect that can also be loaded as an instrument."),
    ("fx-instrument-external", "External Effect/Instrument", "Fx|Instrument|External", "Fx usable as instrument, wrapping external hardware."),
    ("mastering", "Mastering", "Fx|Mastering", "Dither, noise shaping, and other mastering-chain tools."),
    ("microphone", "Microphone Tools", "Fx|Microphone", "Tools dedicated to microphone processing."),
    ("modulation", "Modulation", "Fx|Modulation", "Phaser, flanger, chorus, tremolo, vibrato, auto-pan, rotary, cloner."),
    ("network", "Network", "Fx|Network", "Effect that uses network connectivity."),
    ("pitch-shift", "Pitch Shift", "Fx|Pitch Shift", "Pitch processing, pitch correction, vocal tuning."),
    ("restoration", "Restoration", "Fx|Restoration", "Denoiser, declicker, and other audio-restoration tools."),
    ("reverb", "Reverb", "Fx|Reverb", "Reverberation, room simulation, convolution reverb."),
    ("spatial", "Spatial", "Fx|Spatial", "Mono-to-stereo, stereo enhancer, and other spatial processing."),
    ("surround", "Surround", "Fx|Surround", "Surround-dedicated processing: LFE splitter, bass manager."),
    ("tools", "Utility Tools", "Fx|Tools", "Volume, mixer, tuner, and other general-purpose utilities."),
    ("vocals", "Vocal Tools", "Fx|Vocals", "Tools dedicated to vocals."),
    ("instrument", "Instrument", "Instrument", "Sound generator loaded as an instrument, not an insert effect."),
    ("instrument-drum", "Drum Instrument", "Instrument|Drum", "Instrument for drum sounds."),
    ("instrument-external", "External Instrument", "Instrument|External", "External instrument wrapping hardware."),
    ("instrument-piano", "Piano Instrument", "Instrument|Piano", "Instrument for piano sounds."),
    ("instrument-sampler", "Sampler Instrument", "Instrument|Sampler", "Instrument based on samples."),
    ("instrument-synth", "Synth Instrument", "Instrument|Synth", "Instrument based on synthesis."),
    ("instrument-synth-sampler", "Synth/Sampler Instrument", "Instrument|Synth|Sampler", "Instrument based on both synthesis and samples."),
    ("ambisonics", "Ambisonics", "Ambisonics", "Ambisonics channel effect, panner, or up/down-mixer."),
    ("analyzer-standalone", "Analyzer (meter)", "Analyzer", "Meter/scope/FFT display, not selectable as an insert."),
    ("spatial-panner", "Spatial Panner", "Spatial", "Surround panner."),
    ("spatial-fx", "Spatial Panner + Effect", "Spatial|Fx", "Surround panner usable as an insert effect."),
    ("up-downmix", "Up/Down-mix", "Up-Downmix", "Mix converter, up-mixer, or down-mixer."),
]

_BY_STRING_VALUE = {c[2]: c for c in CATEGORIES}


def resolve_subcategory_tokens(tokens: list[str]) -> list[str]:
    """Map a VST3 moduleinfo.json 'Sub Categories' list (e.g. ['Fx', 'Distortion'])
    to our category slugs. Falls back to the raw joined string as an ad-hoc slug
    if a vendor used a non-standard subcategory (kept for visibility, not silently
    dropped)."""
    if not tokens:
        return []
    joined = "|".join(tokens)
    match = _BY_STRING_VALUE.get(joined)
    if match:
        return [match[0]]
    # try progressively shorter prefixes (e.g. "Fx|Instrument|External" -> "Fx|Instrument")
    parts = tokens[:]
    while len(parts) > 1:
        parts = parts[:-1]
        match = _BY_STRING_VALUE.get("|".join(parts))
        if match:
            return [match[0]]
    return [f"custom:{joined}"]
