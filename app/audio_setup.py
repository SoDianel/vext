"""Configuration > Audio: what the audio settings can be set to (driver
types, devices, the sample rates a device opens at) and the Test button.

ASIO, and every Test, run in a short-lived process of their own
(`python -m app.audio_setup ...`, or the sidecar's `--audio-tool ...`):
PortAudio's ASIO build loads every installed ASIO driver while it starts, and
drivers are native vendor code, so neither runs inside the backend. The other
driver types are listed in-process (milliseconds).

  python -m app.audio_setup list [--asio]       devices (+ the rates each opens at)
  python -m app.audio_setup test '<audio json>' plays the test tone like the worker would

Each prints one JSON object. The test opens its stream with the host worker's
own code (host_worker.Player._open_stream), so it reports the same fallbacks
(rate refused, exclusive mode refused, device gone) the Playground would use.
"""

import json
import os
import subprocess
import sys
import threading
import time

from app import host_worker

ASIO_CACHE_S = 60  # an ASIO listing costs seconds: reused this long unless refreshed
_lock = host_worker.PORTAUDIO_LOCK  # the in-process PortAudio restarts (shared with plugin_host)
_asio_lock = threading.Lock()  # one ASIO listing process at a time
_asio_cache: dict = {"at": 0.0, "data": None}


def tool_command(args: list[str]) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--audio-tool", *args]
    return [sys.executable, "-m", "app.audio_setup", *args]


def _run_tool(args: list[str], timeout: float = 40) -> dict:
    try:
        proc = subprocess.run(tool_command(args), capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, creationflags=subprocess.CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"the audio check didn't answer in {int(timeout)} s"}
    except OSError as e:
        return {"ok": False, "error": f"the audio check couldn't start ({e})"}
    for line in reversed((proc.stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                break
    return {"ok": False, "error": f"the audio check failed (exit code {proc.returncode})"}


# ---------- what the settings can be ----------

def drivers() -> list[dict]:
    """The driver types, with ASIO only usable when an ASIO driver is installed."""
    asio = host_worker.asio_driver_names()
    out = []
    for driver_id, label in host_worker.DRIVER_LABELS.items():
        entry = {"id": driver_id, "label": label, "available": True}
        if driver_id == "asio":
            entry.update(available=bool(asio), drivers=asio)
        out.append(entry)
    return out


def _public(d: dict) -> dict:
    keys = ("id", "name", "hostapi", "default", "api_default", "channels", "default_samplerate", "low_latency_ms",
            "rates")
    return {k: d[k] for k in keys if k in d}


def _local_listing() -> tuple:
    """(sd, outputs) of the in-process PortAudio (no ASIO), restarted so
    devices plugged in since show up."""
    sd = host_worker.sounddevice_module(asio=False)
    try:
        sd._terminate()
        sd._initialize()
    except Exception:  # noqa: BLE001 - a stale list beats no list
        pass
    return sd, host_worker.output_devices(sd)


def _asio_listing(refresh: bool) -> dict:
    with _asio_lock:
        now = time.monotonic()
        if refresh or _asio_cache["data"] is None or now - _asio_cache["at"] > ASIO_CACHE_S:
            data = _run_tool(["list", "--asio"])
            _asio_cache.update(at=now, data=data)
        return _asio_cache["data"]


def _pick(devices: list[dict], wanted: str, api: str) -> dict | None:
    mine = [d for d in devices if d["hostapi"] == api]
    if wanted:
        found = (next((d for d in mine if d["id"] == wanted), None)
                 or next((d for d in mine if d["name"] == wanted.rsplit(", ", 1)[0]), None))
        if found:
            return found
    return next((d for d in mine if d.get("api_default")), None) or (mine[0] if mine else None)


def describe(driver: str, device: str = "", refresh: bool = False) -> dict:
    """Everything Configuration > Audio shows for a driver type: its output
    devices, the one the setting resolves to, and the sample rates it opens
    at ({rates, native}; see host_worker.supported_rates)."""
    if driver not in host_worker.DRIVERS:
        driver = "wasapi"
    api = host_worker.DRIVERS[driver]
    out = {"driver": driver, "drivers": drivers(), "outputs": [], "device": None,
           "rates": {"rates": list(host_worker.SAMPLE_RATES), "native": []}, "error": None}
    if driver == "asio":
        if not host_worker.asio_driver_names():
            out["error"] = "No ASIO driver is installed."
            return out
        data = _asio_listing(refresh)
        if not data.get("ok", True) or "outputs" not in data:
            out["error"] = data.get("error") or "Couldn't list the ASIO drivers."
            return out
        outputs = [d for d in data["outputs"] if d["hostapi"] == api]
        chosen = _pick(outputs, device, api)
        if chosen and chosen.get("rates"):
            out["rates"] = chosen["rates"]
    else:
        with _lock:
            try:
                sd, outputs = _local_listing()
            except Exception as e:  # noqa: BLE001 - PortAudio failing to start
                out["error"] = f"Couldn't list the audio devices: {e}"
                return out
            outputs = [d for d in outputs if d["hostapi"] == api]
            chosen = _pick(outputs, device, api)
            if chosen:
                out["rates"] = host_worker.supported_rates(sd, chosen, driver)
    out.update(outputs=[_public(d) for d in outputs], device=chosen["id"] if chosen else None,
               device_rate=int(chosen["default_samplerate"]) if chosen else None)
    return out


def device_known(value: str) -> bool:
    """A device setting value names a device that exists now. ASIO devices
    ("<driver>, ASIO") are checked against the installed drivers (registry)."""
    name, _, api = value.rpartition(", ")
    if api == "ASIO":
        return name in host_worker.asio_driver_names()
    with _lock:
        _sd, outputs = _local_listing()
    return any(value in (d["id"], d["name"]) for d in outputs)


def test(audio: dict) -> dict:
    """The Test button: the test tone through the audio settings `audio`.
    While the open plugin plays, the tone is mixed into its sound (it holds
    the device); otherwise a short-lived process plays it. Returns {ok, via:
    'plugin'|'test', audio: <stream info>, notes: [...]} or {ok: False, error}."""
    from app import plugin_host

    info = plugin_host.test_tone()
    if info:
        return {"ok": True, "via": "plugin", "audio": info, "notes": []}
    result = _run_tool(["test", json.dumps(audio)], timeout=40)
    result.setdefault("via", "test")
    return result


# ---------- the short-lived process ----------

def _list(asio: bool) -> dict:
    sd = host_worker.sounddevice_module(asio)
    outputs = host_worker.output_devices(sd)
    if asio:  # each ASIO driver's rates now, while it's loaded anyway
        for d in outputs:
            if d["hostapi"] == "ASIO":
                d["rates"] = host_worker.supported_rates(sd, d, "asio")
    return {"ok": True, "asio": host_worker.has_asio(sd), "outputs": outputs}


def _test(audio: dict) -> dict:
    import numpy as np

    sd = host_worker.sounddevice_module(audio.get("driver") == "asio")
    notes: list[str] = []
    try:
        out, rate, block, info = host_worker.Player._open_stream(sd, audio, notes)
    except Exception as e:  # noqa: BLE001
        text = str(e)
        busy = "unavailable" in text.lower() or "-9985" in text or "busy" in text.lower()
        why = ("the device is busy (another app, or a plugin playing in exclusive mode / ASIO, holds it)"
               if busy else text)
        return {"ok": False, "error": f"The device refused to play: {why}", "busy": busy}
    total = int(host_worker.TEST_TONE["seconds"] * rate)
    xruns = 0
    try:
        out.start()
        for start in range(0, total, block):
            n = min(block, total - start)
            chunk = np.zeros((2, block), dtype=np.float32)
            chunk[:, :n] = host_worker.tone_block(start, n, rate, total)
            if out.write(np.ascontiguousarray(chunk.T)):
                xruns += 1
        out.stop()  # plays out what's queued
    finally:
        out.close()
    return {"ok": True, "audio": {**info, "xruns": xruns}, "notes": notes}


def main(argv: list[str]) -> int:
    try:
        if argv and argv[0] == "list":
            result = _list("--asio" in argv)
        elif argv and argv[0] == "test" and len(argv) > 1:
            result = _test(json.loads(argv[1]))
        else:
            result = {"ok": False, "error": "usage: list [--asio] | test <audio json>"}
    except Exception as e:  # noqa: BLE001
        result = {"ok": False, "error": str(e) or type(e).__name__}
    sys.stdout.write(json.dumps(result) + "\n")
    sys.stdout.flush()
    os._exit(0)  # no driver unload code (some hang)


if __name__ == "__main__":
    main(sys.argv[1:])
