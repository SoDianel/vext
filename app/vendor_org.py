"""Flags plugins that aren't organized into a <Vendor>\\ subfolder yet — useful
for spotting a newly-installed plugin that landed loose directly in a format
root (e.g. `...\\Common Files\\VST3\\SomePlugin.vst3`) instead of tidily inside
its own vendor folder (`...\\Common Files\\VST3\\SomeVendor\\SomePlugin.vst3`),
without having to remember/check the install path every time.

Reuses `ScanRoot.recurse_vendor_dirs` (surfaced in the Configuration tab as a
scan path's "Organizable" checkbox — see AGENTS.md) as the gate for whether a
root's plugins get vendor-subfolder organization at all — it already encodes
exactly this distinction (on by default for VST3/VST2/CLAP, off for
FL-NATIVE/WAVES/AAX, whose fixed install layouts a "move into vendor
subfolder" action wouldn't make sense for), so there's nothing new to
hardcode or keep in sync here.

Every function here takes `roots` (the currently-enabled scan roots — see
app/db.py:scan_roots_as_objects) explicitly rather than importing
app.config.ROOTS itself: ROOTS is only the seed list now, and reading it
directly would miss owner-added custom roots entirely and ignore any edit
made to a built-in root's "Organizable" setting via the Configuration tab."""


def _root_for(host_path: str, fmt: str, roots: list):
    host_path_lower = (host_path or "").lower()
    for root in roots:
        if root.format == fmt and host_path_lower.startswith(root.host_path.lower() + "\\"):
            return root
    return None


def needs_vendor_folder(host_path: str, fmt: str, roots: list) -> bool:
    root = _root_for(host_path, fmt, roots)
    if root is None or not root.recurse_vendor_dirs:
        return False
    # Manual backslash split, not pathlib — Path doesn't split on backslashes
    # on this Linux container (see AGENTS.md's scanner.py gotcha; enrichment.py
    # hit this exact bug earlier this session too).
    rel = host_path[len(root.host_path):].lstrip("\\")
    return "\\" not in rel


def vendor_from_path(host_path: str, fmt: str, known_vendor_names: set, roots: list) -> str | None:
    """General parent-folder vendor fallback — if any folder segment between the
    format root and the plugin file exactly matches an already-known vendor name
    (case-insensitive), that's the vendor. Deliberately generic (not a per-name
    NAME_OVERRIDES entry) so it covers any vendor using a `Vendor\\...\\Plugin.vst3`
    layout, including nested category subfolders (e.g.
    `MeldaProduction\\Distortion\\MWaveFolder.vst3` — the vendor is 2 levels up,
    not the immediate parent). Real bug this fixes: an earlier one-off manual fix
    for MeldaProduction's 39 plugins (`vendor_source='inferred'`) got silently
    wiped on the very next rescan, because 'inferred' isn't protected the way
    'manual' is — the same class of "no code path re-derives this" issue as the
    action_log/category bugs elsewhere this session. This function must be called
    on *every* scan, not once, for its result to survive."""
    root = _root_for(host_path, fmt, roots)
    if root is None:
        return None
    rel = host_path[len(root.host_path):].lstrip("\\")
    segments = rel.split("\\")[:-1]  # exclude the filename/bundle itself
    lower_map = {v.lower(): v for v in known_vendor_names}
    for seg in segments:
        if seg.lower() in lower_map:
            return lower_map[seg.lower()]
    return None
