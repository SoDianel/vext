"""Routes the in-app browser's audio to the open plugin.

The Playground's browser webview captures what its pages play and streams it
to WS /api/browser/audio?token=<token> as binary messages of interleaved
float32 stereo at `rate`. The server tells each connection whether to route:

  on connect:                    {"route": bool, "rate": n, "tap": bool}
  when route or tap changes      {"route": bool, "rate": n, "tap": bool}

route = a plugin is open and ready, and its source is the browser. While it's
false the page plays its own audio as usual; while it's true the page mutes
itself and the audio is only heard after the plugin (app/host_worker.py live
input, reached over a local TCP socket).

tap = somebody wants to see the sound (an Analyzer stream is connected or a
browser recording runs, tap_wanted()). The page then sends its frames even when
it isn't routed, and stays audible. Frames that aren't routed to a plugin go to
the in-process analysis ring (app/analysis_tap.py, mode 4) while tap is on; the
routed ones reach the same tap through the worker (mode 2).

Each frame and tab has its own connection. The one that most recently started
sending sound wins: a connection that has been silent (or is new) takes over as
soon as it sends sound, and the current one keeps the route while it plays.
Frames are forwarded whole; when the worker falls behind, whole frames are
dropped (it keeps its own short buffer), so the stream never misaligns.

The page also reports, as a text message, the capture blocks it lost because it
was too busy to run its capture callback (its ScriptProcessor, 2048 frames a
block):

  page -> server, text           {"gaps": n}   (the total on this connection; sent at most once a second while it grows)

The count shows in the status (page_gaps), and the Log gets a warning at most
every 10 s when it grows.

The page's player (the media element that played last in that frame) is the
Analyzer's timeline and its Play / Pause (GET / POST /api/browser/media,
POST /api/browser/media/loop):

  page -> server, text           {"media": {playing, t, d, rate, loop, title}}
                                 (on play / pause / seek, and 4 times a second while it plays)
  server -> page                 {"media": {"action": "play"|"pause"|"toggle"|"seek", "t": s}}
                                 {"loop": [a, b] | "all" | null}   (the page's script keeps the loop)

Commands go to the page whose player plays (else the one that reported last).
Anything else in a text message is ignored.
"""

import asyncio
import json
import logging
import math
import socket
import time

import numpy as np
from fastapi import APIRouter, HTTPException, WebSocket
from pydantic import BaseModel

from app import analysis_tap, db, plugin_host, security
from app.config import DB_PATH

router = APIRouter()
log = logging.getLogger(__name__)

LOUD = 1e-4  # a frame quieter than this counts as silence
TAKEOVER_S = 0.3  # silence that ends a connection's claim to the route
# ~1 s at 48 kHz stereo float32: a burst after the page stalled is delivered, not dropped
# (the worker's own buffer takes it from there). Beyond this, whole frames are dropped.
MAX_PENDING_BYTES = 384 * 1024
GAP_LOG_S = 10  # at most one "lost blocks" warning per this many seconds


class _Conn:
    def __init__(self, ws: WebSocket, rate: int):
        self.ws = ws
        self.rate = rate
        self.last_loud = float("-inf")
        self.gaps = 0  # capture blocks the page reported lost (its own running total)
        self.media: dict | None = None  # its player: {playing, t, d, rate, loop, title} as last reported
        self.media_at = float("-inf")  # time.monotonic() of that report


class _Router:
    """All of its state is only touched on the event loop's thread; other
    threads only call notify()."""

    def __init__(self):
        self.loop: asyncio.AbstractEventLoop | None = None
        self.conns: set[_Conn] = set()
        self.active: _Conn | None = None
        self.route = False
        self.tap = False  # tap_wanted(): the pages send their frames even when not routed
        self.target: tuple | None = None  # (port, key, rate) of the worker's live input
        self.writer: asyncio.StreamWriter | None = None
        self.forwarded_bytes = 0
        self.dropped_frames = 0
        self.gap_pending = 0  # lost capture blocks reported since the last warning
        self._gap_logged_at = float("-inf")
        self._gap_timer: asyncio.TimerHandle | None = None
        plugin_host.add_listener(self.notify)
        analysis_tap.add_listener(self.notify)

    def attach(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    def notify(self) -> None:
        """Called from any thread when the plugin host's state changed."""
        loop = self.loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(self.refresh)
            except RuntimeError:  # the loop is shutting down
                pass

    def refresh(self) -> None:
        target = plugin_host.browser_target()
        if target != self.target:
            self._drop_writer()
            self.target = target
            if target:
                asyncio.ensure_future(self._connect(target))
        route = target is not None
        tap = tap_wanted()
        if route != self.route or tap != self.tap:
            self.route, self.tap = route, tap
            if not tap:
                analysis_tap.browser.idle()
            for conn in list(self.conns):
                asyncio.ensure_future(self._tell(conn))

    def message(self, conn: _Conn) -> dict:
        if self.target:
            conn.rate = self.target[2]
        return {"route": self.route, "rate": conn.rate, "tap": self.tap}

    async def _tell(self, conn: _Conn) -> None:
        try:
            await conn.ws.send_json(self.message(conn))
        except Exception:  # noqa: BLE001 - the page went away
            pass

    def _drop_writer(self) -> None:
        if self.writer is not None:
            try:
                self.writer.close()
            except Exception:  # noqa: BLE001
                pass
            self.writer = None

    async def _connect(self, target: tuple) -> None:
        port, key, _rate = target
        try:
            _reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), timeout=3)
        except (OSError, asyncio.TimeoutError):
            return  # the worker is gone; its state change will refresh again
        sock = writer.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        writer.write(key.encode("ascii") + b"\n")
        if self.target != target or self.writer is not None:
            writer.close()
            return
        self.writer = writer

    async def pause_media(self) -> int:
        """Tells every page that has media connected to pause it; returns how many were told."""
        told = 0
        for conn in list(self.conns):
            try:
                await conn.ws.send_json({"pause": True})
                told += 1
            except Exception:  # noqa: BLE001 - the page went away
                pass
        return told

    def add(self, conn: _Conn) -> None:
        self.conns.add(conn)

    def remove(self, conn: _Conn) -> None:
        self.conns.discard(conn)
        if self.active is conn:
            self.active = None

    def frame(self, conn: _Conn, data: bytes) -> None:
        if not data or len(data) % 8:
            return  # not whole stereo float32 frames
        now = time.monotonic()
        loud = float(np.abs(np.frombuffer(data, dtype=np.float32)).max()) > LOUD
        started = loud and now - conn.last_loud > TAKEOVER_S  # silence (or nothing) -> sound
        if loud:
            conn.last_loud = now
        if conn is not self.active:
            active = self.active
            if active is None or active not in self.conns or started or (
                    loud and now - active.last_loud > TAKEOVER_S):
                self.active = conn
            else:
                return
        if not self.route:
            if self.tap:  # no plugin takes it: the page plays it itself, the analysis tap sees it
                sound = np.frombuffer(data, dtype=np.float32).reshape(-1, 2).T
                analysis_tap.browser.ensure(conn.rate, analysis_tap.BROWSER, True)
                analysis_tap.browser.write(sound, sound)
            return
        writer = self.writer
        if writer is None or writer.is_closing():
            return
        if writer.transport.get_write_buffer_size() > MAX_PENDING_BYTES:
            self.dropped_frames += len(data) // 8
            return
        writer.write(data)
        self.forwarded_bytes += len(data)

    def text(self, conn: _Conn, text: str) -> None:
        """A text message from a page: {"gaps": n}, its lost capture blocks so far, or {"media": {...}}, its
        player's state. Anything else is ignored."""
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            return
        if isinstance(data, dict) and isinstance(data.get("media"), dict):
            self._media_report(conn, data["media"])
            return
        gaps = data.get("gaps") if isinstance(data, dict) else None
        if isinstance(gaps, bool) or not isinstance(gaps, int) or gaps < 0:
            return
        new = gaps - conn.gaps
        conn.gaps = gaps
        if new > 0:
            self.gap_pending += new
            if self._gap_timer is None:
                wait = max(0.0, self._gap_logged_at + GAP_LOG_S - time.monotonic())
                self._gap_timer = asyncio.get_running_loop().call_later(wait, self._gaps_log)

    def _gaps_log(self) -> None:
        self._gap_timer = None
        if self.gap_pending:
            log.warning("The browser page lost %d audio blocks (it was too busy to capture them): "
                        "what you hear has gaps.", self.gap_pending)
            self.gap_pending = 0
            self._gap_logged_at = time.monotonic()

    # ---- the page's player (the Analyzer's timeline and transport) ----

    @staticmethod
    def _media_report(conn: _Conn, m: dict) -> None:
        def num(key):
            v = m.get(key)
            return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else 0.0

        loop = m.get("loop")
        if not (loop == "all" or (isinstance(loop, list) and len(loop) == 2
                                  and all(isinstance(v, (int, float)) for v in loop))):
            loop = None
        conn.media = {"playing": bool(m.get("playing")), "t": max(0.0, num("t")), "d": max(0.0, num("d")),
                      "rate": num("rate") or 1.0, "loop": loop, "title": str(m.get("title") or "")[:200]}
        conn.media_at = time.monotonic()

    def player(self) -> _Conn | None:
        """The page whose player the timeline shows: one that plays (the latest report), else the latest."""
        have = [c for c in self.conns if c.media is not None]
        if not have:
            return None
        playing = [c for c in have if c.media["playing"]]
        return max(playing or have, key=lambda c: c.media_at)

    def media_state(self) -> dict:
        """{has, playing, t (now: moved on since the report while playing), d, loop, title}."""
        conn = self.player()
        if conn is None:
            return {"has": False, "playing": False, "t": 0.0, "d": 0.0, "loop": None, "title": ""}
        m = dict(conn.media)
        if m["playing"]:
            m["t"] += (time.monotonic() - conn.media_at) * m["rate"]
            if m["d"]:
                m["t"] = min(m["t"], m["d"])
        m["t"] = round(m["t"], 3)
        return {"has": True, **{k: m[k] for k in ("playing", "t", "d", "loop", "title")}}

    async def media_command(self, message: dict) -> bool:
        """Sends {"media": {...}} or {"loop": ...} to the page of the timeline's player."""
        conn = self.player()
        if conn is None:
            return False
        try:
            await conn.ws.send_json(message)
            return True
        except Exception:  # noqa: BLE001 - the page went away
            return False

    def status(self) -> dict:
        return {
            "connections": len(self.conns), "route": self.route, "tap": self.tap, "has_active": self.active is not None,
            "worker_connected": self.writer is not None and not self.writer.is_closing(),
            "forwarded_frames": self.forwarded_bytes // 8, "dropped_frames": self.dropped_frames,
            "page_gaps": sum(conn.gaps for conn in self.conns),
        }


def tap_wanted() -> bool:
    """An Analyzer stream is connected or a browser recording runs."""
    return analysis_tap.wanted()


_router = _Router()


def attach(loop: asyncio.AbstractEventLoop) -> None:
    """Called once the server's event loop runs (startup)."""
    _router.attach(loop)
    loop.call_soon(_router.refresh)


def _setting_rate() -> int:
    with db.connect(DB_PATH) as conn:
        value = db.get_setting(conn, "audio_sample_rate", "48000")
    try:
        return int(value or 48000)
    except ValueError:
        return 48000


@router.websocket("/api/browser/audio")
async def browser_audio(ws: WebSocket, token: str = ""):
    if not security.token_ok(token):
        await ws.close(code=1008)  # before accept: the handshake is refused (HTTP 403)
        return
    await ws.accept()
    if _router.loop is None:
        attach(asyncio.get_running_loop())
    conn = _Conn(ws, await asyncio.to_thread(_setting_rate))
    _router.add(conn)
    try:
        _router.refresh()
        await ws.send_json(_router.message(conn))
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("bytes"):
                _router.frame(conn, msg["bytes"])
            elif msg.get("text"):
                _router.text(conn, msg["text"])
    except Exception:  # noqa: BLE001 - a dropped connection ends the same way
        pass
    finally:
        _router.remove(conn)


@router.post("/api/browser/audio/pause")
async def browser_audio_pause():
    """Pauses what plays in the Playground's browser (a YouTube video, say): the page's capture script
    pauses every <video> / <audio> in its frame. Used when a recording of the browser ends and the Audio
    tab opens, so the page doesn't play on behind it."""
    return {"told": await _router.pause_media()}


class MediaBody(BaseModel):
    action: str  # play | pause | toggle | seek
    t: float | None = None  # seek: seconds


class MediaLoopBody(BaseModel):
    range: list[float] | str | None = None  # [start_s, end_s], "all", or None (no loop)


@router.get("/api/browser/media")
async def browser_media():
    """The browser's player (the media element that played last in a page of the Playground's browser):
    {has, playing, t, d, loop, title}. The Analyzer's timeline and its Play / Pause show it."""
    return _router.media_state()


@router.post("/api/browser/media")
async def browser_media_control(body: MediaBody):
    """Plays, pauses, toggles or seeks the browser's player. 409 when no page has one."""
    if body.action not in ("play", "pause", "toggle", "seek") or (body.action == "seek" and body.t is None):
        raise HTTPException(400, "action must be play, pause, toggle or seek (with t)")
    sent = {"action": body.action, **({"t": max(0.0, float(body.t))} if body.action == "seek" else {})}
    if not await _router.media_command({"media": sent}):
        raise HTTPException(409, "Nothing in the browser can be played: start a video or a song there first.")
    return _router.media_state()


@router.post("/api/browser/media/loop")
async def browser_media_loop(body: MediaLoopBody):
    """Loops the browser's player: [start_s, end_s], "all", or None. The page's script keeps the loop."""
    r = body.range
    if isinstance(r, list) and not (len(r) == 2 and 0 <= r[0] < r[1]):
        raise HTTPException(400, "range must be [start_s, end_s] with start < end, \"all\" or null")
    if isinstance(r, str) and r != "all":
        raise HTTPException(400, "range must be [start_s, end_s], \"all\" or null")
    if not await _router.media_command({"loop": r}):
        raise HTTPException(409, "Nothing in the browser can be played: start a video or a song there first.")
    return _router.media_state()


@router.get("/api/browser/audio/status")
async def browser_audio_status():  # async: reads the router on its own thread
    """Routing diagnostics: connections, whether audio is routed, frames forwarded/dropped."""
    return _router.status()
