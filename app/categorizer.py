"""Best-effort category tagging for plugins that don't ship a moduleinfo.json
(i.e. most VST2/flat-VST3/CLAP/Waves/AAX/FL-Native plugins — 756 of 798 as of the
last scan). This is a transparent, inspectable keyword heuristic against the
plugin's own name, matched against the same Steinberg-sourced taxonomy in
app/categories.py — NOT a researched, per-plugin fact. Results are stored with
plugin_categories.source='heuristic' so the web UI (and DATABASE.md) can show
these as suggestions rather than ground truth, and a human can correct them.

Deliberately conservative: only high-precision, low-ambiguity keywords are
included, matched on whole words. A plugin matching nothing stays uncategorized
rather than getting a guessed tag — false "uncategorized" is a much smaller
problem than a false category.
"""

import re

# slug -> list of regex patterns (word-boundary matched, case-insensitive) that,
# if any match the plugin name, tag it with that category.
RULES: list[tuple[str, list[str]]] = [
    ("delay", [r"\bdelay\b"]),
    ("reverb", [r"\breverb\b", r"\breeverb\b", r"\bverb\b", r"\bplate\b", r"\bconvolution\b"]),
    ("eq", [r"\beq\b", r"\bequali[sz]er\b"]),
    ("dynamics", [r"\bcompress(?:or)?\b", r"\bcomp\b", r"\blimiter\b", r"\bgate\b", r"\bexpander\b",
                  r"\bmaximi[sz]er\b", r"\bde-?esser\b", r"\btransient\b", r"\benvelope\b"]),
    ("distortion", [r"\bdistortion\b", r"\bdrive\b", r"\bsaturat\w*\b", r"\bcrush(?:er)?\b",
                     r"\bfuzz\b", r"\boverdrive\b", r"\bamp\b", r"\btube\b", r"\bdistructor\b"]),
    ("modulation", [r"\bchorus\b", r"\bflanger\b", r"\bphaser\b", r"\btremolo\b", r"\bvibrato\b",
                     r"\brotary\b", r"\bcloner\b", r"\bensemble\b", r"\bautopan\b"]),
    ("filter", [r"\bfilter\b", r"\bwah\b"]),
    ("pitch-shift", [r"\bpitch\b", r"\bauto-?tune\b", r"\bvocoder\b", r"\bharmoni[sz]er\b"]),
    ("restoration", [r"\bdenois\w*\b", r"\bdeclick\w*\b", r"\bdeclip\w*\b", r"\brestoration\b",
                      r"\brnoise\b", r"\bhum\s*remov\w*\b"]),
    ("mastering", [r"\bmaster(?:ing)?\b", r"\bdither\b"]),
    ("spatial", [r"\bwiden(?:er)?\b", r"\bimager\b", r"\bpanner\b", r"\bm\/?s\b"]),
    ("analyzer", [r"\banaly[sz]er\b", r"\bmeter\b", r"\bscope\b", r"\bloudness\b", r"\bspectrum\b"]),
    ("tools", [r"\bmixer\b", r"\btuner\b", r"\butility\b"]),
    ("channel-strip", [r"\bchannel\s*strip\b"]),
    ("vocals", [r"\bvocal\b", r"\bvoice\b", r"\bvox\b", r"\bchoir\b"]),
    ("instrument-drum", [r"\bdrum\b", r"\bpercussion\b", r"\bkick\b"]),
    ("instrument-piano", [r"\bpiano\b", r"\bkeys\b"]),
    ("instrument-sampler", [r"\bsampler\b", r"\bsample\s*tank\b"]),
    ("instrument-synth", [r"\bsynth\b", r"\boscillator\b", r"\bosc\b", r"\bbass\b(?!\s*(boost|drum))"]),
]

_COMPILED = [(slug, [re.compile(p, re.IGNORECASE) for p in patterns]) for slug, patterns in RULES]


def categorize_name(name: str) -> list[str]:
    if not name:
        return []
    matches = []
    for slug, patterns in _COMPILED:
        if any(p.search(name) for p in patterns):
            matches.append(slug)
    return matches
