"""Regenerates the Rust crate table in THIRD_PARTY_NOTICES.md.

Run before a release (and after `cargo update`):

    python tools/gen_notices.py

It asks `cargo metadata` for the crates the Windows build links (normal dependencies only, no build or
dev tools) and rewrites the block between the two CRATES markers. The rest of the file is written by hand.
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NOTICES = ROOT / "THIRD_PARTY_NOTICES.md"
BEGIN, END = "<!-- CRATES:BEGIN -->", "<!-- CRATES:END -->"


def runtime_crates() -> list[tuple[str, str, str]]:
    out = subprocess.run(
        ["cargo", "metadata", "--format-version", "1", "--filter-platform", "x86_64-pc-windows-msvc", "--locked"],
        cwd=ROOT / "desktop" / "src-tauri", capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout
    meta = json.loads(out)
    by_id = {p["id"]: p for p in meta["packages"]}
    nodes = {n["id"]: n for n in meta["resolve"]["nodes"]}
    root = meta["resolve"]["root"]
    seen: set[str] = set()
    stack = [root]
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        for dep in nodes[pid]["deps"]:
            if any(k["kind"] is None for k in dep["dep_kinds"]):
                stack.append(dep["pkg"])
    seen.discard(root)
    return sorted((by_id[i]["name"], by_id[i]["version"], by_id[i]["license"] or "see crate") for i in seen)


def main() -> int:
    rows = runtime_crates()
    table = ["| Crate | Version | License |", "|---|---|---|"]
    table += [f"| [{n}](https://crates.io/crates/{n}/{v}) | {v} | {lic} |" for n, v, lic in rows]
    block = f"{BEGIN}\n" + "\n".join(table) + f"\n{END}"
    text = NOTICES.read_text(encoding="utf-8")
    if BEGIN not in text or END not in text:
        print("THIRD_PARTY_NOTICES.md has no CRATES markers", file=sys.stderr)
        return 1
    head, rest = text.split(BEGIN, 1)
    tail = rest.split(END, 1)[1]
    NOTICES.write_text(head + block + tail, encoding="utf-8", newline="\n")
    print(f"{len(rows)} crates written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
