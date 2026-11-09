"""Tests for the automatic picture job (app/plugin_host.py pictures_start, app/host_worker.py _auto_capture).
No real plugin and no worker process: the capture of one plugin is replaced where a case is about the job
(order, vendors, lanes), and the editor window is replaced where it is about telling when it stopped
drawing. VST3 bundles are fake folders with a Snapshots folder. Everything is in a temporary VEXT_ROOT.

Run:  .venv-native\\Scripts\\python.exe tools\\tests\\test_picture_capture.py
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
WORK = Path(tempfile.mkdtemp(prefix="vext-picture-capture-"))
os.environ["VEXT_ROOT"] = str(WORK / "app")  # set before app.config is imported

from PIL import Image  # noqa: E402

from app import host_worker as hw  # noqa: E402
from app import plugin_host as ph  # noqa: E402

UID_A = "0123456789ABCDEF0123456789ABCDEF"
UID_B = "FEDCBA9876543210FEDCBA9876543210"


def bundle(name: str, snapshots: dict, classes: list | None = None) -> str:
    """A fake .vst3 bundle: {file name: (width, height)} in Contents/Resources/Snapshots, and a
    moduleinfo.json listing `classes` (audio class ids) when given."""
    root = WORK / "plugins" / f"{name}.vst3"
    shots = root / "Contents" / "Resources" / "Snapshots"
    shots.mkdir(parents=True, exist_ok=True)
    for file, size in snapshots.items():
        Image.new("RGB", size, (40, 90, 160)).save(shots / file)
    if classes is not None:
        info = {"Name": name, "Classes": [{"CID": c, "Category": "Audio Module Class", "Name": name} for c in classes]}
        (root / "Contents" / "Resources" / "moduleinfo.json").write_text(json.dumps(info), encoding="utf-8")
    return str(root)


def row(i: int, vendor: str | None, fmt: str = "VST3", path: str | None = None) -> dict:
    return {"id": i, "name": f"Plugin {i}", "vendor": vendor, "format": fmt,
            "host_path": path or str(WORK / "plugins" / f"p{i}.dll")}


def wait_job(timeout: float = 20.0) -> dict:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        status = ph.pictures_status()
        if not status["running"]:
            return status
        time.sleep(0.02)
    raise AssertionError("the picture job never finished")


# ---------- VST3 snapshots ----------

def case_snapshot_largest_scale():
    path = bundle("one", {f"{UID_A}_snapshot.png": (300, 200), f"{UID_A}_snapshot_2.0x.png": (600, 400)},
                  classes=[UID_A])
    found = ph.vst3_snapshot(path)
    return found is not None and found.endswith("_2.0x.png"), f"chosen: {found}"


def case_snapshot_refused_when_unsure():
    several = bundle("several", {f"{UID_A}_snapshot.png": (300, 200), f"{UID_B}_snapshot.png": (300, 200)})
    other = bundle("other", {f"{UID_A}_snapshot.png": (300, 200)}, classes=[UID_B])
    two_classes = bundle("two", {f"{UID_A}_snapshot.png": (300, 200)}, classes=[UID_A, UID_B])
    none = bundle("none", {})
    found = [ph.vst3_snapshot(p) for p in (several, other, two_classes, none)]
    return found == [None] * 4, f"found: {found}"


def case_snapshot_saved_as_picture():
    path = bundle("saved", {f"{UID_A}_snapshot.png": (320, 240)})
    result = ph._use_snapshot(row(1, "Acme", path=path))
    target = Path(ph.image_path(path))
    ok = bool(result and result["ok"] and result["method"] == "vst3-snapshot" and target.is_file())
    size = Image.open(target).size if target.is_file() else None
    return ok and size == (320, 240), f"result: {result}, saved: {size}"


def case_snapshot_too_small_is_captured():
    path = bundle("tiny", {f"{UID_A}_snapshot.png": (64, 64)})  # an icon, not an editor
    result = ph._use_snapshot(row(2, "Acme", path=path))
    return result is None and not Path(ph.image_path(path)).exists(), f"result: {result}"


def case_snapshot_only_for_vst3():
    path = bundle("clap", {f"{UID_A}_snapshot.png": (320, 240)})
    result = ph._use_snapshot(row(3, "Acme", fmt="CLAP", path=path))
    return result is None, f"result: {result}"


# ---------- the job: order, vendors, lanes ----------

def case_order_fastest_first():
    rows = [row(10, "A"), row(11, "B"), row(12, "C"), row(13, "D")]
    ph.record_result(rows[0]["host_path"], ok=True, load_s=40.0)
    ph.record_result(rows[1]["host_path"], ok=True, load_s=0.5)
    ph.record_result(rows[3]["host_path"], ok=False, code="failed", reason="x", load_s=0.1)  # failed: unknown
    order = [r["id"] for r in ph._picture_order(rows)]
    return order == [11, 12, 13, 10], f"order: {order}"


def case_method_hint():
    results = {
        row(20, "gpu")["host_path"].lower(): {"ok": True, "method": "screen", "vendor": "gpu"},
        row(21, "gpu")["host_path"].lower(): {"ok": True, "method": "screen", "vendor": "gpu"},
        row(22, "mixed")["host_path"].lower(): {"ok": True, "method": "screen", "vendor": "mixed"},
        row(23, "mixed")["host_path"].lower(): {"ok": True, "method": "print", "vendor": "mixed"},
        row(24, "mixed")["host_path"].lower(): {"ok": True, "method": "print", "vendor": "mixed"},
    }
    hints = {
        "itself shown": ph._method_hint(row(22, "mixed"), results),
        "itself cloaked": ph._method_hint(row(23, "mixed"), results),
        "new, vendor mostly shown": ph._method_hint(row(25, "GPU"), results),
        "new, vendor mostly cloaked": ph._method_hint(row(26, "mixed"), results),
        "new, unknown vendor": ph._method_hint(row(27, None), results),
    }
    want = {"itself shown": "screen", "itself cloaked": None, "new, vendor mostly shown": "screen",
            "new, vendor mostly cloaked": None, "new, unknown vendor": None}
    return hints == want, f"hints: {hints}"


class NoSpares:
    def __init__(self, size):
        pass

    def fill(self):
        pass

    def take(self):
        return None

    def close(self):
        pass


def case_lanes_and_vendors():
    """Two at a time, never two of one vendor (Waves by format), every plugin once, results recorded."""
    rows = [row(30, "Waves", fmt="WAVES"), row(31, "Waves Audio", fmt="WAVES"), row(32, "Acme"),
            row(33, "acme "), row(34, None), row(35, None), row(36, "Zed")]
    lock = threading.Lock()
    running: list = []
    seen: list = []
    peak = {"n": 0, "clash": []}

    def fake_one(r, spares, results):
        key = ph._vendor_key(r)
        with lock:
            if key in [ph._vendor_key(x) for x in running]:
                peak["clash"].append(r["id"])
            running.append(r)
            peak["n"] = max(peak["n"], len(running))
        time.sleep(0.08)
        with lock:
            running.remove(r)
            seen.append(r["id"])
        return {"ok": r["id"] != 36, "code": None if r["id"] != 36 else "blank", "reason": None if r["id"] != 36
                else "its window stayed blank", "method": "print", "timings": {"total_s": 0.08}}

    saved: list = []
    real = (ph._picture_one, ph._Spares)
    ph._picture_one, ph._Spares = fake_one, NoSpares
    try:
        ph.pictures_start(rows, on_saved=lambda r: saved.append(r["id"]))
        status = wait_job()
    finally:
        ph._picture_one, ph._Spares = real
    recorded = ph.picture_results()
    ok = (sorted(seen) == [r["id"] for r in rows] and peak["n"] == ph.PICTURE_JOBS and not peak["clash"]
          and status["ok"] == 6 and status["failed"] == 1 and sorted(saved) == [30, 31, 32, 33, 34, 35]
          and recorded[rows[0]["host_path"].lower()]["vendor"] == "waves"
          and recorded[rows[6]["host_path"].lower()]["code"] == "blank" and status["current"] is None)
    return ok, f"seen {seen}, peak {peak}, ok {status['ok']}, failed {status['failed']}, saved {saved}"


def case_cancel_stops_lanes():
    rows = [row(40 + i, f"v{i}") for i in range(8)]
    seen: list = []

    def fake_one(r, spares, results):
        seen.append(r["id"])
        time.sleep(0.1)
        return None if ph.pictures_status()["cancelled"] else {"ok": True, "code": None, "reason": None,
                                                               "method": "print"}

    real = (ph._picture_one, ph._Spares)
    ph._picture_one, ph._Spares = fake_one, NoSpares
    try:
        ph.pictures_start(rows)
        time.sleep(0.15)
        ph.pictures_cancel()
        status = wait_job()
    finally:
        ph._picture_one, ph._Spares = real
    return status["cancelled"] and len(seen) < len(rows), f"tried {len(seen)} of {len(rows)}, done {status['done']}"


# ---------- the worker: telling when the editor stopped drawing ----------

class FakeEditor:
    """Frames over time: `frame(t)` gives the editor's picture `t` seconds after the capture started."""

    def __init__(self, frame):
        self.frame = frame
        self.start = time.monotonic()

    def find(self):
        return 1


def run_capture(frame, screen=False, **constants):
    """_auto_capture against a fake editor; returns (emitted events, seconds it took, uncloaked)."""
    events: list = []
    saved = {}
    editor = FakeEditor(frame)
    uncloaked = []
    patches = {
        "_editor": editor, "_visible_windows": lambda exclude=None: [], "_client_size": lambda h: (160, 100),
        "_print_window": lambda h, w, ht: frame(time.monotonic() - editor.start),
        "emit": lambda event, **kw: events.append((event, kw)),
        "CAPTURE_POLL_S": 0.02, "CAPTURE_STEADY_S": 0.15, "CAPTURE_SETTLE_S": 0.2, "CAPTURE_WAIT_S": 3.0,
        **constants,
    }
    real = {name: getattr(hw, name) for name in patches}
    real_uncloak = hw._hold.uncloak_all
    for name, value in patches.items():
        setattr(hw, name, value)
    hw._hold.uncloak_all = lambda bottom=False: uncloaked.append(bottom)
    try:
        target = WORK / "captures" / f"{time.monotonic_ns()}.png"
        started = time.monotonic()
        hw._auto_capture(str(target), threading.Event(), screen=screen)
        saved["seconds"] = time.monotonic() - started
    finally:
        for name, value in real.items():
            setattr(hw, name, value)
        hw._hold.uncloak_all = real_uncloak
    return events, saved["seconds"], uncloaked


def drawn(level: int) -> Image.Image:
    im = Image.new("RGB", (160, 100), (level, level, level))
    for x in range(0, 160, 20):
        im.paste((250, 250, 250) if level < 128 else (5, 5, 5), (x, 0, x + 10, 100))  # enough detail to count
    return im


def case_static_editor_ends_soon_after_settle():
    events, seconds, _ = run_capture(lambda t: drawn(60))
    captured = [kw for e, kw in events if e == "captured_auto"]
    return bool(captured) and 0.2 <= seconds < 0.6, f"{seconds:.2f} s, events {[e for e, _ in events]}"


def case_slow_fade_is_waited_for():
    """A fade that moves little per step (under the per-step threshold) but a lot over the span."""
    fade_s = 1.0

    def frame(t):
        return drawn(int(20 + 200 * min(t, fade_s) / fade_s))

    events, seconds, _ = run_capture(frame)
    captured = [kw for e, kw in events if e == "captured_auto"]
    return bool(captured) and seconds >= fade_s, f"{seconds:.2f} s (fade {fade_s} s)"


def case_screen_hint_shows_at_once():
    events, seconds, uncloaked = run_capture(lambda t: drawn(60), screen=True)
    captured = [kw for e, kw in events if e == "captured_auto"]
    return (uncloaked == [True] and captured and captured[0]["method"] == "screen"), \
        f"uncloaked {uncloaked}, method {captured[0]['method'] if captured else None}"


def case_options_capture_screen():
    opts = hw._options(["--format", "VST3", "--capture", "x.png", "--capture-screen"])
    plain = hw._options(["--capture", "x.png"])
    return opts["capture_screen"] is True and opts["capture"] == "x.png" and plain["capture_screen"] is False, \
        f"{opts['capture_screen']}, {plain['capture_screen']}"


CASES = [
    ("VST3 snapshot: the largest scale", case_snapshot_largest_scale),
    ("VST3 snapshot: none when it isn't certain whose it is", case_snapshot_refused_when_unsure),
    ("VST3 snapshot: saved as the plugin's picture", case_snapshot_saved_as_picture),
    ("VST3 snapshot: an icon-sized one is captured instead", case_snapshot_too_small_is_captured),
    ("VST3 snapshot: only VST3 rows", case_snapshot_only_for_vst3),
    ("Order: fastest known load first, unknown in between", case_order_fastest_first),
    ("Method hint: the plugin's own, then its vendor's majority", case_method_hint),
    ("Lanes: two at a time, never one vendor twice", case_lanes_and_vendors),
    ("Cancel: the lanes stop", case_cancel_stops_lanes),
    ("Worker: a static editor is saved soon after the settle time", case_static_editor_ends_soon_after_settle),
    ("Worker: a slow fade is waited for", case_slow_fade_is_waited_for),
    ("Worker: the screen hint shows the editor at once", case_screen_hint_shows_at_once),
    ("Worker: --capture-screen is read", case_options_capture_screen),
]


def main() -> int:
    results = []
    try:
        for name, case in CASES:
            try:
                passed, detail = case()
            except Exception as e:  # noqa: BLE001 - a crash is a failed case, the rest still run
                passed, detail = False, f"exception: {type(e).__name__}: {e}"
            results.append(bool(passed))
            print(f"{'PASS' if passed else 'FAIL'}  {name}\n      {detail}", flush=True)
    finally:
        shutil.rmtree(WORK, ignore_errors=True)
    print(f"\n{sum(results)} of {len(results)} cases PASS")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
