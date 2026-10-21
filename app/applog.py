"""Plain-text audit trail for terminal action events that the UI deliberately
does not keep as browsable history (see AGENTS.md — action_log only ever holds
current/actionable state now, never resolved/historical rows). The UI shows a
one-line generic notification for these ("see logs for details"); the actual
detail — which plugin, which path, when, why — lives only here.
"""

from datetime import datetime, timezone
from pathlib import Path

from app.config import LOG_PATH


def log_event(message: str) -> None:
    path = Path(LOG_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with path.open("a", encoding="utf-8") as f:
        f.write(f"[{ts}] {message}\n")
