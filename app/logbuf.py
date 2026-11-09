"""In-memory ring buffer of this session's backend activity, for the sidebar's
Log section. Fed by:

- `install()`: a `logging.Handler` on the root logger and on "uvicorn.error"
  (uvicorn's own loggers don't propagate to root; its access log is left out on
  purpose, the Log section's own polling would flood it).
- `app/plugin_host.py`: every host worker's stderr, tagged "worker:<file>", and
  the host's own load/crash errors, tagged "host:<file>".

In memory only, never written to disk unless exported: closing VEXT ends the
backend process, so every launch starts with an empty log.
"""

import logging
import threading
from collections import deque
from datetime import datetime
from itertools import count

MAX_LINES = 4000

_lock = threading.Lock()
_lines: deque = deque(maxlen=MAX_LINES)
_seq = count(1)
_last_error_seq = 0
_handler = None


def add(source: str, text: str, level: str = "info") -> None:
    global _last_error_seq
    text = text.rstrip()
    if not text:
        return
    with _lock:
        seq = next(_seq)
        _lines.append({"seq": seq, "time": datetime.now().strftime("%H:%M:%S"),
                       "source": source, "level": level, "text": text})
        if level in ("error", "critical"):
            _last_error_seq = seq


def recent(after: int = 0) -> list[dict]:
    """Every buffered line with seq > after (0: everything currently held)."""
    with _lock:
        return [l for l in _lines if l["seq"] > after]


def status() -> dict:
    """{latest, last_error}: the newest line's seq and the newest error's (0: none)."""
    with _lock:
        return {"latest": _lines[-1]["seq"] if _lines else 0, "last_error": _last_error_seq}


def clear() -> None:
    """Empties the buffer. Sequence numbers keep counting, so a client's cursor stays valid."""
    global _last_error_seq
    with _lock:
        _lines.clear()
        _last_error_seq = 0


def as_text() -> str:
    with _lock:
        return "".join(f"[{l['time']}] {l['level'].upper():<8} {l['source']}: {l['text']}\n" for l in _lines)


class _Handler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            add("backend", self.format(record), record.levelname.lower())
        except Exception:  # noqa: BLE001 - a logging handler must never raise
            pass


def install() -> None:
    """Idempotent. Call after uvicorn configured logging (app startup): its dictConfig
    removes handlers from the loggers it configures."""
    global _handler
    if _handler is None:
        _handler = _Handler()
        _handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
    for name in ("", "uvicorn.error"):
        logger = logging.getLogger(name)
        if _handler not in logger.handlers:
            logger.addHandler(_handler)
