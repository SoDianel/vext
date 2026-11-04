# FL index pictures for Installed presets

Owner request (2026-11-03): since the index lists only `Plugin database\Installed`, presets without a
verified catalog match show no picture. On the maintainer's PC: 892 Installed presets, 600 with a VEXT
catalog picture, 292 without (155 Waves shell entries, 135 Image-Line natives, 2 others). Every one of the
292 has exactly one filed copy (`Effects\...` / `Generators\...`, same section, same file name) with a PNG:
158 written by VEXT's FL Sync, 134 Image-Line's own.

## Picture order (presentation only)

For each Installed preset the index shows the first that exists:

1. The Installed preset's own picture: `.nfo` `Bitmap=` or `<stem>.png` beside the `.fst` (unchanged, `has_preset_image`).
2. VEXT's catalog picture through a verified match (unchanged, `has_image`).
3. **New:** the filed copy's picture (`has_filed_image`).
4. The VEXT logo placeholder.

A picture never identifies a preset. Loading and dragging keep using only the exact Installed key, and
nothing here changes which presets are listed.

## Filed copy lookup (`app/fl_launch.py`)

- `_filed_pictures(root) -> dict[(section, stem.casefold()), Path]`: walk `root/Effects` and
  `root/Generators` (never `root/Installed`) for `*.fst`. For each, the picture is its `.nfo` `Bitmap=`
  value if present, else `<stem>.png`, resolved; keep it only if it is a `.png` file inside
  `root/<section>`. A (section, stem) with more than one distinct picture is ambiguous: drop it, never guess.
- Cache the result for 10 s under a lock (image requests arrive in bursts after a listing).
- `launchable()` includes the filed pictures (path and file stamp) in its cache signature, and
  `_build_listing()` sets `has_filed_image` = the preset's (section, stem) is in the map.
- `preset_image(conn, key)`: the own picture as now; otherwise the filed picture for that key's
  section and stem. Both must stay inside the Plugin database root and be `.png` files.

## Index page (`app/static/js/fl-launcher.js`)

`cardHtml()` uses the order above: own picture or filed picture both load from
`/api/fl-launch/preset-image?key=`, the catalog picture from `/api/plugins/<id>/image`. Catalog wins over
filed when both exist. The `is-empty` class applies only when none of the three exists.
