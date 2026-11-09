"""Research-backed vendor identifications for plugins that binary metadata and the
local installer archive couldn't resolve on their own (app/scanner.py, app/installer_index.py).

Every lookup below is keyed by the plugin's **filename stem** (`_filename_stem(host_path)`),
not its display `name` — the name can legitimately change (a manual edit, a better
metadata source, or app/fl_database.py's FL Studio cross-reference correcting a
shared-white-label-engine name) while the installed *file* stays exactly the same.
Keying by name was a real bug: FL Studio's cross-reference renaming e.g. "BC Chorus 4
VST3(Mono)" to "Blue Cat's Chorus 4 (Mono)" silently broke every one of the 15 Blue Cat
Audio entries here (and the "K1v_x64"/"BBC Symphony Orchestra (64 Bit)" ones), because
`apply()` was matching against `row["name"]` after it had already been corrected. It also
explains a second, older bug this uncovered: PREFIX_OVERRIDES' "uaudio_" check was
matching against the *display* name (already "UADx ..." from moduleinfo.json, not
"uaudio_...") so it had never actually fired — filename-based matching fixes that too.

Each entry here was verified during a manual research pass (2026-09-28) via one or
more of: the plugin's own install-folder vendor evidence, the local installer archive
(the installer archive), or a web search cross-checked against product naming.
Because the scanner does a full rescan-and-replace on every run, any such finding has
to live here (code) to survive — not as a one-off SQL edit against plugins.db.

Confidence is intentionally lower than 'binary'/'moduleinfo'/'installer' — these are
recorded as vendor_source='inferred' with a note, not presented as certain. Where two
real vendors ship a same-named plugin and the installed copy couldn't be told apart
(exactly the failure mode that caused the "DeEsser" mis-grouping bug earlier this
session — see AGENTS.md), the entry is listed under AMBIGUOUS instead of guessed.
"""

# exact plugin `name` (as stored in plugins.name) -> (vendor, note)
NAME_OVERRIDES: dict[str, tuple[str, str]] = {
    # Blue Cat Audio "BC ..." product line — confirmed via installer archive\Blue Cat Audio
    # (installer files named e.g. BlueCatChorusVST3-x64Setup.exe; product subfolders weren't
    # used so the normal installer cross-reference couldn't match them).
    "BC Chorus 4 VST3(Mono)": ("Blue Cat Audio", "Matches installer BlueCatChorusVST3-x64Setup.exe"),
    "BC Chorus 4 VST3(Stereo)": ("Blue Cat Audio", "Matches installer BlueCatChorusVST3-x64Setup.exe"),
    "BC Flanger 3 VST3(Mono)": ("Blue Cat Audio", "Matches installer BlueCatFlangerVST3-x64Setup.exe"),
    "BC Flanger 3 VST3(Stereo)": ("Blue Cat Audio", "Matches installer BlueCatFlangerVST3-x64Setup.exe"),
    "BC Free Amp VST": ("Blue Cat Audio", "Matches installer BlueCatFreeAmpVST-x64Setup.exe"),
    "BC Free Amp VST3": ("Blue Cat Audio", "Matches installer BlueCatFreeAmpVST-x64Setup.exe"),
    "BC FreqAnalyst 2 VST3(Mono)": ("Blue Cat Audio", "Matches installer BlueCatFreqAnalystVST3-x64Setup.exe"),
    "BC FreqAnalyst 2 VST3(Stereo)": ("Blue Cat Audio", "Matches installer BlueCatFreqAnalystVST3-x64Setup.exe"),
    "BC Gain 3 VST3(Dual)": ("Blue Cat Audio", "Matches installer BlueCatGainSuiteVST3-x64Setup.exe"),
    "BC Gain 3 VST3(Mono)": ("Blue Cat Audio", "Matches installer BlueCatGainSuiteVST3-x64Setup.exe"),
    "BC Gain 3 VST3(Stereo)": ("Blue Cat Audio", "Matches installer BlueCatGainSuiteVST3-x64Setup.exe"),
    "BC Phaser 3 VST3(Mono)": ("Blue Cat Audio", "Matches installer BlueCatPhaserVST3-x64Setup.exe"),
    "BC Phaser 3 VST3(Stereo)": ("Blue Cat Audio", "Matches installer BlueCatPhaserVST3-x64Setup.exe"),
    "BC Triple EQ 4 VST3(Dual)": ("Blue Cat Audio", "Matches installer BlueCatTripleEQVST3-x64Setup.exe"),
    "BC Triple EQ 4 VST3(Mono)": ("Blue Cat Audio", "Matches installer BlueCatTripleEQVST3-x64Setup.exe"),
    "BC Triple EQ 4 VST3(Stereo)": ("Blue Cat Audio", "Matches installer BlueCatTripleEQVST3-x64Setup.exe"),

    # Second research pass (2026-09-29): VST2 plugins with no vendor metadata; each product page
    # confirmed via KVR / vendor announcement.
    "Amplio2": ("VST Zone", "Freeware multiband effect toolbox by VST Zone (zOne.sk)"),
    "Eclipsis": ("VST Zone", "Freeware hybrid wavetable synth by VST Zone (zOne.sk)"),
    "PPD Echobox": ("Minimal System Instruments", "Freeware analogue echo by Minimal System Instruments"),
    "Twisted Energy": ("Minimal System Instruments", "Freeware 3-oscillator synth by Minimal System Instruments"),
    "472_Delay+Studio+KVR12": ("Minimal System Instruments", "Delay Studio KVR12, freeware multiband delay by Minimal System Instruments"),
    "basiQ": ("Kuassa", "Baxandall-style EQ; basiQ2 is Kuassa's successor product"),
    "OzoneImager": ("iZotope", "VST2 build of iZotope's Ozone Imager"),

    "Filter Bank v3": ("Minimal System Instruments", "Freeware mastering/mixing plugin in the Minimal System Instruments line (KVR/Looperman listings)"),
    "MasterComp Compressor v3.1": ("Minimal System Instruments", "Freeware mastering/mixing plugin in the Minimal System Instruments line (KVR/Looperman listings)"),
    "Mastering Limiter": ("Minimal System Instruments", "Freeware mastering/mixing plugin in the Minimal System Instruments line (KVR/Looperman listings)"),
    "MSI Stereo Buss Compressor": ("Minimal System Instruments", "Freeware mastering/mixing plugin in the Minimal System Instruments line (KVR/Looperman listings)"),
    "UltraComp V3": ("Minimal System Instruments", "Freeware mastering/mixing plugin in the Minimal System Instruments line (KVR/Looperman listings)"),
    "APC": ("Ample Sound Co., Ltd.", "Ample Percussion Cloudrum (owner-confirmed); 64-bit build"),
    "APCx86": ("Ample Sound Co., Ltd.", "Ample Percussion Cloudrum (owner-confirmed); 32-bit build"),
    "K1v_x64": ("Nils Schneider", "Exact folder match: installer archive\\Nils Schneider\\K1v_x64"),

    "ZebraHZ": ("u-he", "Installer archive has a u-he vendor folder; matches the u-he/ZebraHZ.clap already found"),
    "ZebraHZ(x64)": ("u-he", "Installer archive has a u-he vendor folder; matches the u-he/ZebraHZ.clap already found"),

    "AVOX CHOIR": ("Antares", "Part of Antares' AVOX vocal bundle; AntaresTech installer folder confirmed present"),
    "AVOX DUO": ("Antares", "Part of Antares' AVOX vocal bundle; AntaresTech installer folder confirmed present"),

    "BBC Symphony Orchestra (64 Bit)": ("Spitfire Audio", "installer archive\\SpitfireAudio installer confirmed"),

    "CamelCrusher": ("Camel Audio", "installer archive\\Camel Audio folder confirmed"),

    "CC-Sampler": ("CinematicComposing", "Matches installer CC-Sampler-Windows*.exe under installer archive\\CinematicComposing"),

    # Sits inside a "MeldaProduction"/"iZotope" vendor subfolder already, just lacked
    # moduleinfo.json/PE CompanyName — the scanner does not currently use parent-folder
    # name as a vendor signal (worth adding as a general fallback in a future pass).
    "MSoundFactory": ("MeldaProduction", "Installed inside its own MeldaProduction vendor subfolder"),
    "MSoundFactory6out": ("MeldaProduction", "Installed inside its own MeldaProduction vendor subfolder"),
    "Ozone Imager": ("iZotope", "File is Ozone Imager.vst3, installed inside its own iZotope vendor subfolder"),

    # SoundSpot "Mercury Bundle" — web-confirmed roster (rekkerd.org bundle announcement)
    # matches these plugin names exactly, and all share an identical technical fingerprint
    # (flat non-bundle .vst3 directly in Common Files/VST3, no CompanyName, no moduleinfo.json)
    # not shared by most of the rest of the library — strong clustering evidence.
    "Axis": ("SoundSpot", "Name + install fingerprint matches SoundSpot Mercury Bundle roster"),
    "Firefly": ("SoundSpot", "Name + install fingerprint matches SoundSpot Mercury Bundle roster"),
    "Glitch": ("SoundSpot", "Name + install fingerprint matches SoundSpot Mercury Bundle roster"),
    "Halcyon": ("SoundSpot", "Web-confirmed: SoundSpot Halcyon is a harmonic saturation plugin"),
    "MSW2": ("SoundSpot", "Name + install fingerprint matches SoundSpot Mercury Bundle roster"),
    "Oracle": ("SoundSpot", "Name + install fingerprint matches SoundSpot Mercury Bundle roster"),
    "Paradox": ("SoundSpot", "Name + install fingerprint matches SoundSpot Mercury Bundle roster"),

    "Mirror": ("United Plugins", "Web-confirmed: 'Mirror' delay plugin by JMG Sound, distributed via United Plugins"),
    "FirePresser": ("United Plugins", "Web-confirmed: 'FirePresser' compressor by FireSonic, distributed via United Plugins"),

    "Auburn Sounds Graillon 3": ("Auburn Sounds", "Vendor name is literally in the filename"),
    "Auburn Sounds Inner Pitch 2": ("Auburn Sounds", "Vendor name is literally in the filename"),
    "Auburn Sounds Panagement 2-64": ("Auburn Sounds", "Vendor name is literally in the filename"),

    "TAL-Reverb-4": ("TAL Software", "TAL = Togu Audio Line, a well-known plugin vendor"),

    "TR5 Suite": ("IK Multimedia", "TR5 = T-RackS 5; matches this library's other IK Multimedia TR5-branded "
                  "plugins (e.g. TR5 White Channel)"),
}

# plugin `name` prefix -> (vendor, note)
PREFIX_OVERRIDES: list[tuple[str, str, str]] = [
    ("uaudio_", "Universal Audio",
     "uaudio_ prefix + product names (1176, LA-2A, 610, Century, Pure Plate, Verve Essentials, ...) "
     "match Universal Audio's UAD Native hardware-emulation catalog"),
]

# name -> note explaining why vendor was deliberately left unresolved (two+ real, differently
# vendored products share this exact name; picking one would be a guess, not a finding).
AMBIGUOUS: dict[str, str] = {
    "Overtone": (
        "Two known candidates share this name: Voxengo 'OvertoneGEQ' (installer confirmed present "
        "at installer archive\\Voxengo) and SoundSpot 'Overtone' EQ (Mercury Bundle roster, "
        "matches the install fingerprint of 7 confirmed SoundSpot siblings). Could not tell which "
        "is actually installed without opening the plugin — verify manually."
    ),
    "Voxbox": (
        "Two known candidates share this name: Universal Audio 'Manley VoxBox' channel strip and "
        "SoundSpot 'VoxBox' vocal widener (Mercury Bundle roster). Installed file is small "
        "(~1MB), which fits SoundSpot's lightweight plugins better than UA's heavier UAD emulations, "
        "but this is not confirmed — verify manually."
    ),
    "Chameleon": (
        "Multiple real plugins share this name (accentize 'Chameleon' AI reverb matcher, Darkpalace "
        "Studio 'Chameleon' allpass filter suite, possibly others) with no strong signal pointing to "
        "one over another — verify manually."
    ),
}


def _filename_stem(host_path: str) -> str:
    # pathlib.Path does NOT split on backslashes on this Linux container (a real,
    # previously-hit bug — see AGENTS.md's scanner.py gotcha), so a Windows path
    # here needs manual splitting, not Path(...).stem.
    filename = host_path.replace("\\", "/").rsplit("/", 1)[-1]
    return filename.rsplit(".", 1)[0] if "." in filename else filename


def apply(conn) -> dict:
    from app import db

    filled, ambiguous = 0, 0
    for row in db.all_plugins(conn):
        if row["vendor"]:
            continue
        stem = _filename_stem(row["host_path"])
        if stem in NAME_OVERRIDES:
            vendor, note = NAME_OVERRIDES[stem]
            db.fill_vendor_override(conn, row["id"], vendor, note)
            filled += 1
            continue
        prefix_hit = next((p for p in PREFIX_OVERRIDES if stem.startswith(p[0])), None)
        if prefix_hit:
            _, vendor, note = prefix_hit
            db.fill_vendor_override(conn, row["id"], vendor, note)
            filled += 1
            continue
        if stem in AMBIGUOUS:
            db.mark_vendor_ambiguous(conn, row["id"], AMBIGUOUS[stem])
            ambiguous += 1

    return {"filled": filled, "ambiguous": ambiguous}
