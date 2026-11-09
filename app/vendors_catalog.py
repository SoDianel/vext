"""Vendor reference data: canonical name, website, and status (active / whether
the company or product line has been discontinued/acquired/shut down).

Two separate concerns bundled here because they were discovered together:

1. **Canonicalization** — PE metadata and moduleinfo.json report the same real
   vendor under several different strings (casing, legal suffixes, punctuation:
   "SoundSpot" vs "Soundspot", "iZotope" vs "iZotope, Inc.", "Image-Line" vs
   "Image Line NV"). ALIASES maps every observed variant to one canonical name
   so the UI doesn't show duplicate vendor filter entries for the same company.

2. **Status** — url + status per canonical vendor. status is one of:
     'active'      — company/product line currently operating, site live
     'deprecated'  — company acquired or product line discontinued, but the
                     plugin still works and may still be downloadable somewhere
     'down'        — site confirmed unreachable / no successor
     None          — not yet researched; don't guess, leave it blank (see
                     AGENTS.md's AMBIGUOUS/inferred precedent — same principle)

Only vendors actually verified (via web search, 2026-09-28/29) or extremely
well-known/stable (major companies whose domain hasn't changed in years) are
filled in here. Small indie developers not covered are left for a future pass
rather than guessed at — a wrong "deprecated" flag on someone's still-active
one-person shop is worse than no flag at all.
"""

# raw vendor string (as scanned) -> canonical name
ALIASES: dict[str, str] = {
    "soundspot": "SoundSpot",
    "izotope": "iZotope",
    "izotope, inc.": "iZotope",
    "izotope inc.": "iZotope",
    "ik multimedia production": "IK Multimedia",
    "ik multimedia productions": "IK Multimedia",
    "image line nv": "Image-Line",
    "image-line": "Image-Line",
    "valhalla": "Valhalla DSP",
    "valhalla dsp, llc": "Valhalla DSP",
    "valhalla dsp": "Valhalla DSP",
    "accusonus": "accusonus",
    "unplugred": "Unplugred",
    "waves audio": "Waves Audio",
    "waves audio ltd.": "Waves Audio",
    "wave arts, inc.": "Wave Arts",
    "wave arts": "Wave Arts",
    "xfer  / support@xferrecords.com ": "Xfer Records",
    "xfer records": "Xfer Records",
    "xfer  / support@xferrecords.com": "Xfer Records",
    "bsod  / steve@xferrecords.com": "Xfer Records",
    "xferrecords.com": "Xfer Records",
    "tal software": "TAL Software",
    "tal-togu audio line": "TAL Software",
    "vienna symphonic library gmbh": "Vienna Symphonic Library",
    "native instruments gmbh": "Native Instruments",
    "kazrog inc": "Kazrog",
    "steven slate": "Steven Slate Drums",
    "z'": "ZEEK",  # STFU plugin's PE CompanyName was a stray "z'" — real vendor is ZEEK per installer archive
}


def canonicalize(raw_vendor: str | None) -> str | None:
    if not raw_vendor:
        return raw_vendor
    return ALIASES.get(raw_vendor.strip().lower(), raw_vendor.strip())


# canonical name -> (url, status, note, source_note)
VENDORS: dict[str, tuple[str | None, str | None, str | None]] = {
    # --- verified during this session (web search, 2026-09-28/29) ---
    "Camel Audio": ("https://www.kvraudio.com/product/camelcrusher-by-camel-audio", "deprecated",
                     "Acquired by Apple in 2015; the company closed and CamelCrusher/CamelPhat were discontinued with no official download source since. The plugin still works, just unsupported."),
    "accusonus": ("https://www.kvraudio.com/developer/accusonus", "deprecated",
                  "Acquired by Meta in Feb 2022; the ERA plugin line was sunset in 2023 with no further development. Still usable, unsupported."),
    "SoundSpot": ("https://www.pluginboutique.com/product/81-Bundles/97-Various-Category/12539-Mercury-Bundle", "deprecated",
                  "soundspot.audio is gone (site/activation server down since 2023); Plugin Boutique took over the catalog and re-released some plugins under new names (e.g. Union -> WaveOSC). Existing installs still work."),
    "Variety of Sound": ("https://varietyofsound.wordpress.com", "active",
                          "Active freeware developer (WordPress blog is the de facto site); their older epicVerb was discontinued but the developer keeps shipping new plugins."),

    # --- major/stable companies (long-established, well-known domains) ---
    "IK Multimedia": ("https://www.ikmultimedia.com", "active", None),
    "Image-Line": ("https://www.image-line.com", "active", None),
    "Waves Audio": ("https://www.waves.com", "active", None),
    "Arturia": ("https://www.arturia.com", "active", None),
    "Native Instruments": ("https://www.native-instruments.com", "active",
                            "iZotope was acquired by Native Instruments' parent group in 2023, but iZotope continues operating under its own brand — see the separate iZotope entry."),
    "Universal Audio": ("https://www.uaudio.com", "active", None),
    "Plugin Alliance": ("https://www.plugin-alliance.com", "active", None),
    "iZotope": ("https://www.izotope.com", "active",
                "Acquired by Native Instruments in 2023; products continue to be developed and sold under the iZotope brand."),
    "u-he": ("https://u-he.com", "active", None),
    "Valhalla DSP": ("https://valhalladsp.com", "active", None),
    "SoundToys": ("https://www.soundtoys.com", "active", None),
    "Xfer Records": ("https://xferrecords.com", "active", None),
    "Spitfire Audio": ("https://www.spitfireaudio.com", "active", None),
    "Vienna Symphonic Library": ("https://www.vsl.co.at", "active", None),
    "Eventide": ("https://www.eventideaudio.com", "active", None),
    "Antares": ("https://www.antarestech.com", "active", None),
    "MeldaProduction": ("https://www.meldaproduction.com", "active", None),
    "Voxengo": ("https://www.voxengo.com", "active", None),
    "Blue Cat Audio": ("https://www.bluecataudio.com", "active", None),
    "Kilohearts": ("https://kilohearts.com", "active", None),
    "Klanghelm": ("https://klanghelm.com", "active", None),
    "Slate Digital": ("https://slatedigital.com", "active", None),
    "Steven Slate Drums": ("https://www.stevenslatedrums.com", "active", None),
    "Cherry Audio": ("https://cherryaudio.com", "active", None),
    "Vital Audio": ("https://vital.audio", "active", None),
    "TAL Software": ("https://tal-software.com", "active", None),
    "Tokyo Dawn Labs": ("https://www.tokyodawn.net", "active", None),
    "Polyverse Music": ("https://polyversemusic.com", "active", None),
    "Cableguys": ("https://www.cableguys.com", "active", None),
    "Caelum Audio": ("https://caelumaudio.com", "active", None),
    "AudioThing": ("https://www.audiothing.net", "active", None),
    "Acon Digital": ("https://acondigital.com", "active", None),
    "Acustica Audio": ("https://www.acustica-audio.com", "active", None),
    "W.A.Production": ("https://waproduction.com", "active", None),
    "Fuse Audio Labs": ("https://fuseaudiolabs.com", "active", None),
    "United Plugins": ("https://unitedplugins.com", "active", None),
    "Techivation": ("https://techivation.com", "active", None),
    "Youlean": ("https://youlean.co", "active", None),
    "sonible": ("https://www.sonible.com", "active", None),
    "Kazrog": ("https://www.kazrog.com", "active", None),
    "UVI": ("https://www.uvi.net", "active", None),
    "SSL": ("https://www.solidstatelogic.com", "active", None),
    "Heavyocity": ("https://www.heavyocity.com", "active", None),
    "inMusic Brands Inc.": ("https://inmusicbrands.com", "active", None),
    "Newfangled Audio": ("https://www.newfangledaudio.com", "active", None),
    "Auburn Sounds": ("https://www.auburnsounds.com", "active", None),
    "Wavesfactory": ("https://wavesfactory.com", "active", None),
    "Cymatics": ("https://cymatics.fm", "active", None),
    "BABY Audio": ("https://babyaud.io", "active", None),
}


def lookup(canonical_vendor: str) -> dict | None:
    hit = VENDORS.get(canonical_vendor)
    if not hit:
        return None
    url, status, note = hit
    return {"url": url, "status": status, "note": note}
