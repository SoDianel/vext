"""Draws the clean logo mark (the V E X T letters, no background) as an SVG from
the master vector file, for the sidebar logo button (app/static/logo-mark.svg,
symbol id "mark").

  python make_logo_svg.py sources/logo.vectorcraft app/static/logo-mark.svg [artboard]

Run by update_logo.ps1. Reads the layer named "Text" (VectorCraft's JSON format:
paths of anchors with optional "in"/"out" Bezier handles, in artboard pixels,
y down). Each letter keeps its fill, except near-white ones, which become
`currentColor` so they follow the page's text colour: White Smoke letters
would vanish on the light theme. The viewBox is cropped to the letters.

The file can hold several artboards with a version of the letters each; only
the paths inside one artboard are drawn: `artboard` (1-based), default the
last one (where the owner keeps the current design: the app logo, matching the
logo_app_*.png exports).
"""

import json
import sys


def _fill(node) -> tuple[float, float, float] | None:
    for item in node.get("appearance", {}).get("items", []):
        paint = item.get("paint", {})
        if item.get("kind") == "fill" and paint.get("type") == "solid":
            c = paint["color"]
            return c["r"], c["g"], c["b"]
    return None


def _num(v: float) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".")


def _path_data(path) -> tuple[str, list[tuple[float, float]]]:
    """SVG path data for a VectorCraft path, and every point used (for the bbox)."""
    parts, points = [], []
    for sub in path.get("subpaths", []):
        anchors = sub.get("anchors", [])
        if not anchors:
            continue
        x, y = anchors[0]["p"]
        parts.append(f"M{_num(x)} {_num(y)}")
        points.append((x, y))
        pairs = list(zip(anchors, anchors[1:]))
        if sub.get("closed"):
            pairs.append((anchors[-1], anchors[0]))
        for a, b in pairs:
            c1, c2, end = a.get("out", a["p"]), b.get("in", b["p"]), b["p"]
            points.extend([c1, c2, end])
            if c1 == a["p"] and c2 == end:
                parts.append(f"L{_num(end[0])} {_num(end[1])}")
            else:
                parts.append("C" + " ".join(f"{_num(px)} {_num(py)}" for px, py in (c1, c2, end)))
        if sub.get("closed"):
            parts.append("Z")
    return "".join(parts), points


def main(src: str, dst: str, artboard: str | None = None) -> None:
    with open(src, encoding="utf-8") as f:
        doc = json.load(f)["document"]
    layer = next((lay for lay in doc["layers"] if lay.get("name") == "Text"), None)
    if layer is None:
        raise SystemExit(f"{src}: no layer named 'Text'")
    boards = doc.get("artboards") or []
    board = boards[int(artboard) - 1 if artboard else -1]["rect"] if boards else None
    shapes, points = [], []
    for node in layer["kind"].get("children", []):
        if node.get("kind", {}).get("type") != "path":
            continue
        d, pts = _path_data(node["kind"]["path"])
        if board:  # only the letters whose centre lies on the chosen artboard
            cx = (min(p[0] for p in pts) + max(p[0] for p in pts)) / 2
            cy = (min(p[1] for p in pts) + max(p[1] for p in pts)) / 2
            if not (board["x0"] <= cx <= board["x1"] and board["y0"] <= cy <= board["y1"]):
                continue
        points.extend(pts)
        rgb = _fill(node) or (1.0, 1.0, 1.0)
        light = 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2] > 0.8
        fill = "currentColor" if light else "#" + "".join(f"{round(v * 255):02x}" for v in rgb)
        shapes.append(f'<path d="{d}" fill="{fill}"/>')
    if not shapes:
        raise SystemExit(f"{src}: the 'Text' layer has no paths")
    x0, y0 = min(p[0] for p in points), min(p[1] for p in points)
    x1, y1 = max(p[0] for p in points), max(p[1] for p in points)
    pad = max(x1 - x0, y1 - y0) * 0.02
    side = max(x1 - x0, y1 - y0) + 2 * pad  # square, letters centred
    vx, vy = (x0 + x1 - side) / 2, (y0 + y1 - side) / 2
    # A <symbol> the page draws with <svg><use href="/static/logo-mark.svg#mark"/></svg>:
    # `currentColor` then resolves against the page (an <img> couldn't inherit it).
    # The outer <use> only makes the file viewable on its own.
    box = f"{_num(vx)} {_num(vy)} {_num(side)} {_num(side)}"
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{box}">'
           f'<symbol id="mark" viewBox="{box}">' + "".join(shapes) + '</symbol><use href="#mark"/></svg>\n')
    with open(dst, "w", encoding="utf-8", newline="\n") as f:
        f.write("<!-- Made by make_logo_svg.py from sources/logo.vectorcraft (update_logo.ps1). Don't edit. -->\n")
        f.write(svg)


if __name__ == "__main__":
    main(*sys.argv[1:4])
