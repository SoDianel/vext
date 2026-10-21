"""Duplicate-plugin detection, vendor-aware, on two axes:

1. Format priority: CLAP > VST3 > VST2 (app.config.FORMAT_PRIORITY).
2. Location priority *within* the same format: a 64-bit install outranks the
   32-bit (x86) copy of the exact same plugin, and for Waves specifically, a
   newer "Plug-Ins V16" copy outranks an older "Plug-Ins V13"/"V12" one of the
   same plugin. This catches duplicates format-priority alone misses — e.g. the
   same VST3 installed in both `Program Files\\Common Files\\VST3` and
   `Program Files (x86)\\Common Files\\VST3`.

Shared by app/report.py (DATABASE.md) and app/web.py (cleanup review UI) so
there's exactly one implementation of "which entries are the same plugin" —
see the "DeEsser" bug in AGENTS.md for why vendor-agreement is required before
two rows are even considered candidates for the same plugin.
"""

import re
from collections import defaultdict

from app.config import FORMAT_PRIORITY

_WAVES_VERSION_RE = re.compile(r"Plug-Ins V(\d+)", re.IGNORECASE)


def _norm_vendor(v: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (v or "").lower())


def vendor_match(v1: str | None, v2: str | None) -> bool:
    a, b = _norm_vendor(v1), _norm_vendor(v2)
    if not a and not b:
        return True
    if not a or not b:
        return False
    return a in b or b in a


def _get(row, key):
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def group_by_name(rows: list) -> dict:
    """Groups rows by normalized name; manually linked rows (link_group, set from the
    plugin modal) are additionally indexed under ("link", group_id) so siblings_of can
    join them regardless of name or vendor."""
    by_name = defaultdict(list)
    for r in rows:
        by_name[r["normalized_name"]].append(r)
        if _get(r, "link_group"):
            by_name[("link", r["link_group"])].append(r)
    return by_name


def siblings_of(row, by_name: dict) -> list:
    sibs = [x for x in by_name[row["normalized_name"]] if vendor_match(row["vendor"], x["vendor"])]
    gid = _get(row, "link_group")
    if gid:
        for x in by_name[("link", gid)]:
            if all(x["id"] != s["id"] for s in sibs):
                sibs.append(x)
    return sibs


def _is_32bit(row) -> bool:
    b = _get(row, "bitness")
    if b in (32, 64):
        return b == 32
    return "(x86)" in (row["host_path"] or "")  # unknown bitness: fall back to the folder convention


def location_rank(host_path: str, bitness: int | None = None) -> int:
    """Higher = preferred install location, within the same format. The PE-header
    bitness (when known) beats the "(x86)" folder guess — e.g. APC.dll / APCx86.dll
    sit side by side in the same folder, so only the header tells them apart."""
    m = _WAVES_VERSION_RE.search(host_path or "")
    if m:
        return int(m.group(1))  # newer Waves version folder wins
    if bitness in (32, 64):
        return 1 if bitness == 64 else 0
    return 0 if "(x86)" in (host_path or "") else 1  # 64-bit install wins over 32-bit


def _vst2_folder_rank(host_path: str) -> int:
    """Within VST2: the Common Files\\VST2 folder ranks above the classic
    VstPlugins folder (the owner's order: VST2 > VST)."""
    return 1 if "\\common files\\vst2\\" in (host_path or "").lower() else 0


def priority(row) -> tuple:
    # format first (CLAP > VST3 > VST2), then bitness/version (x64 > x32), then the VST2 folder.
    return (
        FORMAT_PRIORITY.get(row["format"], 0),
        location_rank(row["host_path"], _get(row, "bitness")),
        _vst2_folder_rank(row["host_path"]) if row["format"] == "VST2" else 0,
    )


def best_format(siblings: list) -> str:
    """Kept for the DATABASE.md 'Format' column, which groups by format only."""
    return max((x["format"] for x in siblings), key=lambda f: FORMAT_PRIORITY.get(f, 0))


def reason(superseded, keeper) -> str:
    if superseded["format"] != keeper["format"]:
        return f"{superseded['format']} copy superseded by the {keeper['format']} copy of the same plugin"
    if _is_32bit(superseded) and not _is_32bit(keeper):
        return f"32-bit ({superseded['format']}) copy superseded by the 64-bit copy of the same plugin"
    if _WAVES_VERSION_RE.search(superseded["host_path"] or ""):
        return f"older Waves version folder superseded by a newer one ({keeper['host_path']})"
    return f"duplicate {superseded['format']} install superseded by the copy at {keeper['host_path']}"


def superseded_candidates(rows: list) -> list[dict]:
    """One entry per plugin row that's strictly outranked by a same-vendor sibling
    (whether that sibling is a better format, or the same format at a better
    location). Ties (identical priority) are never flagged — nothing here is
    applied automatically, this only proposes."""
    by_name = group_by_name(rows)
    out = []
    for r in rows:
        siblings = siblings_of(r, by_name)
        if len(siblings) <= 1:
            continue
        best = max(siblings, key=priority)
        if priority(best) > priority(r):
            out.append({"superseded": r, "keeper": best, "reason": reason(r, best)})
    return out
