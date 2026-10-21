"""Finds stale Windows "installed program" registry traces after a plugin's
files have already been deleted through this app — the owner's reported
case: deleted a VST3 (superseded by its CLAP version), but the *next*
installer run still detected VST2/AAX/VST3 as "already installed" and
offered to repair/skip them. That's the installer reading its own or
Windows' registered-product bookkeeping, not checking whether the file is
still actually there.

Scans the standard "Programs and Features" registry locations (one subkey
per registered product, under "...Uninstall") across both 64-bit and 32-bit
views plus the per-user hive, fuzzy-matches each
entry's DisplayName/Publisher against the plugin's own vendor/name, and
flags whether the entry's own InstallLocation still exists on disk — a
strong, concrete signal for "this is genuinely orphaned" versus "this just
happens to share a word with an unrelated, still-installed product."

Read-only by default (find_uninstall_entries). Deletion
(delete_uninstall_entry) is a separate, explicit, per-entry action driven
entirely by the owner reviewing the results in the UI — never automatic,
same propose-then-apply model as every other action in this app (see
AGENTS.md). Matching is inherently fuzzy (there's no universal standard
tying a plugin file to a specific registry entry across different vendors'
installer technologies), so results are for human review, not an
auto-cleanup list.
"""

import re
import winreg
from pathlib import Path

_UNINSTALL_ROOTS = [
    ("HKLM", winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ("HKLM", winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    ("HKCU", winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
]

_HIVE_BY_NAME = {"HKLM": winreg.HKEY_LOCAL_MACHINE, "HKCU": winreg.HKEY_CURRENT_USER}


def _norm(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _read_value(key, field: str) -> str | None:
    try:
        return winreg.QueryValueEx(key, field)[0]
    except OSError:
        return None


def find_uninstall_entries(vendor: str | None, name: str | None) -> list[dict]:
    """Substring-matches DisplayName/Publisher against the given vendor/name
    (either may be blank; at least one non-empty match term is expected from
    the caller). Returns every match found across all three registry roots,
    each annotated with whether its own InstallLocation still exists."""
    vendor_norm = _norm(vendor)
    name_norm = _norm(name)
    results = []

    for hive_label, hive, base in _UNINSTALL_ROOTS:
        try:
            base_key = winreg.OpenKey(hive, base)
        except OSError:
            continue
        with base_key:
            i = 0
            while True:
                try:
                    subkey_name = winreg.EnumKey(base_key, i)
                except OSError:
                    break
                i += 1
                try:
                    sk = winreg.OpenKey(base_key, subkey_name)
                except OSError:
                    continue
                with sk:
                    display_name = _read_value(sk, "DisplayName")
                    if not display_name:
                        continue
                    publisher = _read_value(sk, "Publisher")
                    install_location = _read_value(sk, "InstallLocation")
                    display_version = _read_value(sk, "DisplayVersion")

                dn_norm = _norm(display_name)
                pub_norm = _norm(publisher)
                name_match = bool(name_norm) and name_norm in dn_norm
                vendor_match = bool(vendor_norm) and (vendor_norm in pub_norm or vendor_norm in dn_norm)
                if not (name_match or vendor_match):
                    continue

                location_exists = None
                if install_location:
                    try:
                        location_exists = Path(install_location).exists()
                    except OSError:
                        location_exists = None

                results.append({
                    "hive": hive_label,
                    "key_path": f"{base}\\{subkey_name}",
                    "display_name": display_name,
                    "publisher": publisher,
                    "install_location": install_location,
                    "install_location_exists": location_exists,
                    "display_version": display_version,
                    "match_reason": "name" if name_match else "vendor",
                })

    return results


def _delete_key_recursive(root, path: str) -> None:
    try:
        key = winreg.OpenKey(root, path, 0, winreg.KEY_ALL_ACCESS)
    except FileNotFoundError:
        return
    with key:
        while True:
            try:
                sub = winreg.EnumKey(key, 0)
            except OSError:
                break
            _delete_key_recursive(root, f"{path}\\{sub}")
    winreg.DeleteKey(root, path)


def delete_uninstall_entry(hive: str, key_path: str) -> None:
    """Deletes exactly one registry key (and its subkeys, if any) — the
    owner picked this specific key_path from a find_uninstall_entries()
    result, reviewed in the UI first. HKLM entries need the app running
    elevated; phrased to match this app's existing "Permission denied"
    pattern (app/static/app.js:handleActionError()) so the same "relaunch as
    Administrator?" offer already used for file-permission errors applies
    here too, without any special-casing on the frontend."""
    root = _HIVE_BY_NAME.get(hive)
    if root is None:
        raise ValueError(f"unknown hive: {hive!r} (expected HKLM or HKCU)")
    try:
        _delete_key_recursive(root, key_path)
    except PermissionError as exc:
        raise PermissionError(
            f"Windows denied deleting this registry key (Permission denied). "
            f"{hive} entries need this app running as Administrator — see the elevation prompt."
        ) from exc
