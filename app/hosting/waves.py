"""Waves plugins: the catalog lists each `Plug-Ins V<n>\\<Name>.bundle` (see
AGENTS.md, "Waves plugins — how the files actually work"), but a host opens
them through a WaveShell, one VST3 (or VST2) file exposing every *licensed*
Waves plugin of that version, each under its own name ("C1 comp Stereo").

This module finds the WaveShell for a bundle and picks the shell plugin that
is that bundle. The names a shell exposes are listed by the host worker (it
has to load the shell for that, ~20 s) and cached by the backend.
"""

import glob
import os
import re

# Bundle names whose shell names differ (not just by a suffix).
ALIASES = {
    "okdriver": "OneKnob Driver",
    "okpumper": "OneKnob Pumper",
    "okfilter": "OneKnob Filter",
    "okphatter": "OneKnob Phatter",
    "okpresence": "OneKnob Presence",
    "okbrighter": "OneKnob Brighter",
    "oklouder": "OneKnob Louder",
    "okwetter": "OneKnob Wetter",
    "renaxx": "Renaissance Axx",
    "rcomp": "RCompressor",
    "lil tube": "Magma Lil Tube",
    "gtrsolotoolrack": "GTR Solo Tool Rack",
    "gtrtoolrack": "GTR Tool Rack",
    "gtramp": "GTR Amp",
    "gtrstomp": "GTR Stomp",
    "gtrtuner": "GTR Tuner",
    "wavestune lt": "Waves Tune LT",
    "wavestune": "Waves Tune",
    "signalgenerator": "EMO-Generator",
    "puigchild": "PuigChild 670",
    "puigtec": "PuigTec EQP1A",
    "l1": "L1 limiter",
    "c1": "C1 comp",
    "paz": "PAZ- Analyzer",
    "eddiekramer dr": "EKramer DR",
    "lineq": "LinEQ Broadband",
    "ps22": "PS22 Spread",
    "ir-l": "IR-L full",
}

_SUFFIXES = (" Mono/Stereo", " Stereo", " Mono", " 5.0/5.1", " 5.1", " 5.0")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def bundle_version(host_path: str) -> str | None:
    """'13' for ...\\Waves\\Plug-Ins V13\\C1.bundle."""
    m = re.search(r"plug-ins v(\d+)", host_path.replace("/", "\\"), re.I)
    return m.group(1) if m else None


def bundle_name(host_path: str) -> str:
    return re.sub(r"\.bundle$", "", os.path.basename(host_path.rstrip("\\/")), flags=re.I)


def _version_key(path: str):
    m = re.search(r"(\d+)\.(\d+)_x64", os.path.basename(path))
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def find_shell(host_path: str) -> tuple[str, str] | None:
    """(shell path, 'VST3'|'VST2') for a Waves bundle: the newest 64-bit
    WaveShell of the bundle's major version, VST3 preferred. None if none is
    installed."""
    version = bundle_version(host_path)
    if not version:
        return None
    waves_root = os.path.dirname(os.path.dirname(host_path.rstrip("\\/")))  # ...\Waves
    common = os.path.join(os.environ.get("CommonProgramFiles", r"C:\Program Files\Common Files"), "VST3")
    folders = [os.path.join(waves_root, f"WaveShells V{version}"), os.path.join(common, "Waves"), common]
    for pattern, fmt in ((f"WaveShell*-VST3 {version}.*_x64.vst3", "VST3"), (f"WaveShell1-VST {version}.*_x64.dll", "VST2")):
        found = []
        for folder in folders:
            found += [p for p in glob.glob(os.path.join(folder, pattern))]
        if found:
            # WaveShell1 first (the general one), then the newest minor version.
            found.sort(key=lambda p: (os.path.basename(p).lower().startswith("waveshell1"), _version_key(p)),
                       reverse=True)
            return found[0], fmt
    return None


def _split(name: str) -> tuple[str, str]:
    for suffix in _SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)], suffix.strip()
    return name, ""


_LAYOUT_ORDER = {"Stereo": 0, "Mono/Stereo": 1, "": 2, "Mono": 3}


def mono_variant(name: str | None, names: list[str]) -> str | None:
    """The plain Mono component of the same plugin, when `name` is another
    variant of it (e.g. "UltraPitch Shift Mono/Stereo" -> "UltraPitch Shift Mono")."""
    if not name:
        return None
    base, layout = _split(name)
    if layout == "Mono":
        return None
    return next((full for full in names if _split(full) == (base, "Mono")), None)


def pick(bundle: str, names: list[str]) -> str | None:
    """The shell plugin for a bundle: same base name (after the alias table),
    else the shortest base name starting with it; the Stereo variant first
    (VEXT plays stereo), then Mono/Stereo, then Mono. None: the shell doesn't
    expose it (usually: not licensed)."""
    wanted = _norm(ALIASES.get(bundle.lower(), bundle))
    variants: dict[str, list[tuple[str, str]]] = {}
    for full in names:
        base, layout = _split(full)
        variants.setdefault(base, []).append((layout, full))
    exact = [b for b in variants if _norm(b) == wanted]
    candidates = exact or sorted((b for b in variants if _norm(b).startswith(wanted)), key=lambda b: (len(b), b))
    for base in candidates:
        options = sorted(variants[base], key=lambda lv: _LAYOUT_ORDER.get(lv[0], 9))
        if options[0][0] in ("Stereo", "Mono/Stereo", ""):
            return options[0][1]
    return candidates and sorted(variants[candidates[0]], key=lambda lv: _LAYOUT_ORDER.get(lv[0], 9))[0][1] or None
