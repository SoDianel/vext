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

from app import scanner
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


_MIN_VENDOR_PREFIX = 4   # shorter vendor names ("Pro", "AA") are too likely to be part of a real name
_MIN_NAME_LEFT = 3


def _strip_vendor(key: str, vendor: str | None) -> str:
    """key without the vendor's name in front, trying the whole vendor name first and then
    fewer of its words: "FabFilter Software Instruments" strips "fabfilter" from
    "fabfilterproq3", "Acme Ltd" strips "acme"."""
    words = re.findall(r"[a-z0-9]+", (vendor or "").lower())
    for k in range(len(words), 0, -1):
        prefix = "".join(words[:k])
        if (len(prefix) >= _MIN_VENDOR_PREFIX and key.startswith(prefix)
                and len(key) - len(prefix) >= _MIN_NAME_LEFT):
            return key[len(prefix):]
    return key


def group_key(row) -> str:
    """The name two copies of one plugin share. Normalized again from the name (so an
    older row's stored normalized_name can't keep it apart), without the vendor's name
    in front: a VST2 .dll often reports "FabFilter Pro-Q 3" where the VST3 says "Pro-Q 3"."""
    key = scanner.normalize_name(row["name"] or "") or row["normalized_name"] or ""
    return _strip_vendor(key, row["vendor"])


def group_by_name(rows: list) -> dict:
    """Groups rows by group_key(); manually linked rows (link_group, set from the
    plugin modal) are additionally indexed under ("link", group_id) so siblings_of can
    join them regardless of name or vendor."""
    by_name = defaultdict(list)
    for r in rows:
        by_name[group_key(r)].append(r)
        if _get(r, "link_group"):
            by_name[("link", r["link_group"])].append(r)
    return by_name


def siblings_of(row, by_name: dict) -> list:
    sibs = [x for x in by_name[group_key(row)] if vendor_match(row["vendor"], x["vendor"])]
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
        # AAX is Pro Tools' format and nothing else loads it, so a VST3 or CLAP copy
        # doesn't replace it; only another AAX copy can (64-bit over 32-bit). Whether
        # AAX is needed at all is the Pro Tools rule (scan_service.apply_aax_rule).
        if r["format"] == "AAX":
            siblings = [x for x in siblings if x["format"] == "AAX"]
        else:
            siblings = [x for x in siblings if x["format"] != "AAX"]
        if len(siblings) <= 1:
            continue
        best = max(siblings, key=priority)
        if priority(best) > priority(r):
            out.append({"superseded": r, "keeper": best, "reason": reason(r, best)})
    return out


# Browse's format badges, in the order they're shown. VST2 has two folder conventions: the
# classic VstPlugins folder ("VST") and Common Files\VST2 ("VST2").
_LABEL_ORDER = ["VST", "VST2", "VST3", "AAX", "CLAP", "WAVES", "FL-NATIVE"]


def format_label(row) -> str:
    if row["format"] == "VST2":
        return "VST2" if _vst2_folder_rank(row["host_path"]) else "VST"
    return row["format"]


def label_order(label: str) -> int:
    return _LABEL_ORDER.index(label) if label in _LABEL_ORDER else len(_LABEL_ORDER)


def unify_groups(rows: list) -> dict[int, int]:
    """plugin id -> group id (the smallest id in the group): every copy of one plugin,
    whatever its format, bitness or folder, for Browse's one row per plugin. Same rule
    as siblings_of, made transitive, plus one display-only relaxation: a copy without a
    vendor joins its name's group when that group has exactly one vendor (a VST2 .dll
    often has no vendor field). Proposals never use this: superseded_candidates keeps
    requiring a vendor on both sides."""
    parent = {r["id"]: r["id"] for r in rows}

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    # Longest first, so "Native Instruments GmbH" is tried before "Native Instruments".
    vendors = sorted({r["vendor"] for r in rows if r["vendor"]}, key=lambda v: (-len(v), v))
    buckets = defaultdict(list)
    links = defaultdict(list)
    for r in rows:
        key = group_key(r)
        if not r["vendor"]:
            # A copy without a vendor may still carry one in its name ("FabFilter Pro-Q 3").
            for v in vendors:
                stripped = _strip_vendor(key, v)
                if stripped != key:
                    key = stripped
                    break
        buckets[key].append(r)
        if _get(r, "link_group"):
            links[r["link_group"]].append(r["id"])

    for members in buckets.values():
        vendored = [r for r in members if r["vendor"]]
        for i, a in enumerate(vendored):
            for b in vendored[i + 1:]:
                if vendor_match(a["vendor"], b["vendor"]):
                    union(a["id"], b["id"])
        bare = [r for r in members if not r["vendor"]]
        for b in bare[1:]:
            union(bare[0]["id"], b["id"])
        if bare and vendored and len({find(r["id"]) for r in vendored}) == 1:
            union(bare[0]["id"], vendored[0]["id"])
    for ids in links.values():
        for i in ids[1:]:
            union(ids[0], i)
    return {i: find(i) for i in parent}
