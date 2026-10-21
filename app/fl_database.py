"""Cross-references our catalog against FL Studio's own plugin database
(`%USERPROFILE%\\Documents\\Image-Line\\FL Studio\\Presets\\Plugin database\\Installed\\`,
mounted read-only at FL_PLUGIN_DB_ROOT — see docker-compose.yml).

FL Studio actually queries each plugin for its real name, which our static
metadata reading (moduleinfo.json / PE version info) sometimes can't get right
for a plugin built on a shared white-label engine — the shared binary reports
the same generic product name for every product built on it, not the specific
one. Confirmed real bugs this fixes (see AGENTS.md): 31 Acustica Audio ("Acqua")
effects all read back as literally "NUW" from PE metadata; Kilohearts effects
missing the "kHs " brand prefix; Blue Cat Audio plugins showing raw filename-
derived names. It also gives us `ps_file_type_0` (1=effect, 2=generator/
instrument) — a real, independent signal for filling `plugin_type`, used by
the Type tab.

Each plugin gets one `.nfo` file — plain text `key=value` per line (not strict
INI, not JSON; Image-Line's own ad-hoc format) plus a `.fst` (binary
plugin-state cache, not needed). The fields we care about:

  ps_name              -- FL's resolved display name.
  ps_file_filename_0    -- absolute path to the actual binary — *usually*. Two
                           real wrinkles, both confirmed against real files:
                           (1) for a VST3 **bundle** this is the inner binary
                           (...\\Foo.vst3\\Contents\\x86_64-win\\Foo.vst3), not
                           the bundle folder our own host_path uses — handled
                           by truncating at the first plugin extension
                           (_bundle_root). (2) for an **FL-NATIVE** plugin this
                           is a `%FLPath%\\Plugins\\Fruity\\...\\Foo\\Foo_x64.dll`
                           *template* path (not an absolute path — `%FLPath%`
                           needs resolving to FL's real install dir, derived
                           from ROOTS rather than hardcoded so a future FL
                           version-folder rename doesn't silently break this),
                           pointing at the inner .dll — our own host_path for
                           FL-NATIVE is the *containing folder*
                           (app/scanner.py:_process_fl_native), so the match
                           key here is the inner file's parent folder, not an
                           extension-truncated path. Originally missed this
                           entirely (assumed no FL-NATIVE data existed at all,
                           without checking) — see AGENTS.md.
  ps_file_type_0        -- 1 = effect, 2 = generator/instrument. Confirmed
                           against both a Fruity effect and a Fruity/VST3
                           generator.
  ps_file_vendorname_0  -- FL's vendor guess. Not used for the name/type
                           cross-reference on purpose — our own vendor pipeline
                           is more sophisticated than a straight overwrite.

Waves plugins each *do* get their own `.nfo` (`ps_name` is the real per-plugin
name, e.g. "C4 Stereo.nfo") — an earlier version of this docstring claimed
otherwise ("no per-Waves-plugin entry exists anywhere"), which was wrong;
re-checked directly against real files and found dozens. What's actually true:
every one of those per-plugin `.nfo` files' `ps_file_filename_0`/`_1` points at
the shared "WaveShell1-VST3 <version>_x64.vst3" / "WaveShell1-VST <version>_x64.dll"
wrapper (FL loads every Waves plugin of a given install through that one
wrapper binary), never at anything resembling our own per-plugin
"Plug-Ins V<N>" + "<Name>.bundle" host_path — so a normal path/filename match
can never succeed for Waves, by design, regardless of how the fallback logic
improves.

Instead: the wrapper filename's version number (e.g. "17.1") tells us which
major Waves version FL has a working shell for. Our own "Plug-Ins V<N>"
folders are exactly that same version number. So `build_index()` also
collects every distinct major version seen across WaveShell filenames
(`waves_versions`, e.g. `{"13", "17"}`), and `lookup()` special-cases the
WAVES format: extract "V<N>" from the plugin's own host_path and check it
against that set. Confirmed real on this machine: WaveShell wrappers exist
for V13 and V17 (both VST3 *and* VST2 shells for 13.0; VST3-only for 17.1) —
**not** V12 or V16, even though `Plug-Ins V12`/`V16` folders exist with real
plugin bundles in them. FL genuinely cannot and does not recognize those two
versions (no corresponding shell is installed at all) — that's not a gap in
this cross-reference, it's an honest reflection of what FL can actually load.
"""

import re
from pathlib import Path

from app.config import FL_PLUGIN_DB_ROOT, ROOTS

_EXT_RE = re.compile(r"\.(vst3|clap|dll|aaxplugin)", re.IGNORECASE)
_WAVESHELL_RE = re.compile(r"WaveShell1-VST3?\s+(\d+)(?:\.\d+)?_x(?:64|86)\.(?:vst3|dll)", re.IGNORECASE)
_PLUGINS_VERSION_RE = re.compile(r"Plug-Ins V(\d+)", re.IGNORECASE)

# Derived from ROOTS rather than hardcoded, so a future FL Studio version-folder
# rename (e.g. "FL Studio 2027") doesn't silently break this substitution.
_FL_NATIVE_ROOT = next((r for r in ROOTS if r.format == "FL-NATIVE"), None)
_FL_PATH = _FL_NATIVE_ROOT.host_path.split("\\Plugins\\Fruity\\")[0] if _FL_NATIVE_ROOT else None


def _bundle_root(path: str) -> str:
    """Truncates at the first known plugin extension, so a bundle's
    inner-binary path collapses back to the bundle folder our own scanner
    uses as host_path. A no-op for flat-file formats (already at their
    natural end)."""
    m = _EXT_RE.search(path or "")
    return path[: m.end()] if m else (path or "")


def _parent_folder(path: str) -> str:
    parts = (path or "").replace("/", "\\").split("\\")
    return "\\".join(parts[:-1])


def build_index(fl_plugin_db_root: str = FL_PLUGIN_DB_ROOT) -> dict:
    """Returns {"by_path": {...}, "by_filename": {...}} — see lookup() for why
    there are two. fl_plugin_db_root defaults to the hardcoded constant but
    is normally passed explicitly by app/scan_service.py, resolved from the
    "fl_plugin_db_root" setting (see the Configuration tab).

    by_path: normalized/lowercased bundle-root path -> {"name", "vendor",
    "type_code"}, the primary match (path is the most specific signal FL
    gives us).
    by_filename: lowercased filename only (no directory) -> [entry, ...],
    a fallback for when this app's own "Reorganize" action has since moved a
    plugin into a vendor subfolder — FL's cached path goes stale until FL
    itself rescans, but the file's own name never changes. Confirmed real:
    Xfer Records' DeltaModulator_x64.dll / Op1DrumUtil.dll stopped matching
    the moment they were reorganized into a "Xfer Records\" subfolder, even
    though FL Studio's own plugin picker still listed them fine — its cache
    just hadn't been refreshed. A list (not a single entry) because two
    different vendors could coincidentally ship a same-named file."""
    root = Path(fl_plugin_db_root)
    by_path: dict[str, dict] = {}
    by_filename: dict[str, list[dict]] = {}
    waves_versions: set[str] = set()
    if not root.exists():
        return {"by_path": by_path, "by_filename": by_filename, "waves_versions": waves_versions}

    for nfo_path in root.rglob("*.nfo"):
        try:
            text = nfo_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        fields = {}
        for line in text.splitlines():
            key, sep, value = line.partition("=")
            if sep:
                fields[key.strip()] = value.strip()

        name = fields.get("ps_name")
        if not name:
            continue

        # Real bug found and fixed: FL Studio writes ONE .nfo per plugin, but
        # when it has both a 32-bit and 64-bit build of the exact same
        # plugin, it records BOTH under a single entry as `ps_files=2` with
        # indexed fields (`ps_file_filename_0`, `ps_file_filename_1`, ...) —
        # confirmed real on "Ample Bass P Lite II.nfo": `ps_files=2`,
        # `_filename_0` = the 32-bit `ABPL2x86.dll`, `_filename_1` = the
        # 64-bit `ABPL2.dll`. Only ever reading index 0 silently dropped
        # every 64-bit path for any plugin shipped as a 32+64 pair — the
        # owner's exact reported case (Ample Sound plugins never matching,
        # even after the filename-fallback below, because the 64-bit
        # filename was never in the index at all). Now iterates every file
        # index `ps_files` declares, not just the first.
        num_files_raw = fields.get("ps_files")
        num_files = int(num_files_raw) if num_files_raw and num_files_raw.isdigit() else 1

        for i in range(num_files):
            filename = fields.get(f"ps_file_filename_{i}")
            if not filename:
                continue
            type_code_raw = fields.get(f"ps_file_type_{i}")
            type_code = int(type_code_raw) if type_code_raw and type_code_raw.isdigit() else None
            entry = {"name": name, "vendor": fields.get(f"ps_file_vendorname_{i}") or None, "type_code": type_code,
                     "nfo": str(nfo_path)}

            waveshell_match = _WAVESHELL_RE.search(filename)
            if waveshell_match:
                # Points at the shared shell wrapper, not this plugin's own
                # file — never a useful by_path/by_filename key (see the
                # module docstring's Waves section). What it tells us is
                # which major Waves version FL has a working shell for.
                waves_versions.add(waveshell_match.group(1))
                continue

            if "%FLPath%" in filename:
                if _FL_PATH is None:
                    continue
                resolved = filename.replace("%FLPath%", _FL_PATH)
                key = _parent_folder(resolved).lower()
            else:
                key = _bundle_root(filename).lower()

            by_path[key] = entry
            base = key.rsplit("\\", 1)[-1] if "\\" in key else key.rsplit("/", 1)[-1]
            if base:
                by_filename.setdefault(base, []).append(entry)

    return {"by_path": by_path, "by_filename": by_filename, "waves_versions": waves_versions}


def _norm_vendor(v: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (v or "").lower())


def lookup(host_path: str, vendor: str | None, index: dict, format_: str | None = None) -> dict | None:
    if format_ == "WAVES":
        # Never path/filename-matchable (see module docstring) — the only
        # signal FL's data gives us for Waves is which major version(s) it
        # has a shell installed for. host_path is always our own scanner's
        # "...\Plug-Ins V<N>\<Name>.bundle" — resolve() elsewhere always
        # returns that shape for WAVES.
        m = _PLUGINS_VERSION_RE.search(host_path or "")
        if m and m.group(1) in index.get("waves_versions", set()):
            # Same name back, deliberately: FL's per-plugin ps_name (e.g.
            # "C4 Stereo") doesn't reliably correspond to which specific
            # bundle we scanned (Waves' Mono/Stereo variants aren't exposed
            # as separate files on our side), so renaming would risk being
            # wrong — this is a detection confirmation, not a name source.
            return {"name": None, "vendor": "Waves", "type_code": None}
        return None

    by_path = index.get("by_path", {})
    bundle_root = _bundle_root(host_path).lower()
    hit = by_path.get(bundle_root)
    if hit:
        return hit
    hit = by_path.get((host_path or "").lower())
    if hit:
        return hit

    # Path didn't match — fall back to filename (+ vendor, if more than one
    # candidate shares that filename). See build_index()'s docstring.
    by_filename = index.get("by_filename", {})
    base = bundle_root.rsplit("\\", 1)[-1] if "\\" in bundle_root else bundle_root
    candidates = by_filename.get(base, [])
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    # Real bug found and fixed: FL Studio's plugin database has a "New\"
    # subfolder that mirrors "VST\"/"VST3\"/etc. (see build_index()'s
    # docstring) — the exact same plugin gets a *second*, identical .nfo,
    # so a same-filename lookup routinely finds 2+ candidates that all
    # agree on name/vendor, not genuinely different plugins. Confirmed
    # real: "APC.dll" and "Filter Bank v3.dll" both have blank PE vendor
    # metadata (no CompanyName resource at all) *and* duplicate FL
    # candidates, so the old code could neither pick a single candidate
    # nor disambiguate by vendor (nothing to compare against) and gave up
    # — even though the "ambiguity" was fake, just a mirrored duplicate.
    first = candidates[0]
    if all(c["name"] == first["name"] and c.get("vendor") == first.get("vendor") for c in candidates):
        return first

    # Candidates genuinely disagree — only safe to pick one if our own
    # scanned vendor agrees with exactly one of them.
    if vendor:
        vendor_norm = _norm_vendor(vendor)
        for c in candidates:
            c_vendor = _norm_vendor(c.get("vendor"))
            if c_vendor and (c_vendor in vendor_norm or vendor_norm in c_vendor):
                return c
    return None
