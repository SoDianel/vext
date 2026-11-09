"""Locks the local API down to VEXT's own pages.

The backend listens on 127.0.0.1, but any web page the user visits (in any
browser, or in VEXT's own in-app browser) can still send requests to it.
Three things keep those out:

- CORS only lets VEXT's own origins (the Tauri shell, and this server's own
  pages) read responses.
- Every request that can change something (any method but GET/HEAD/OPTIONS)
  must carry the header `X-VEXT-Token: <TOKEN>`; a custom header can't be sent
  cross-origin without a CORS preflight, which foreign origins fail. The
  browser-audio WebSocket takes the token as `?token=` (it can't send headers).
- The Host header must name this machine (localhost / 127.0.0.1), which stops
  DNS-rebinding pages from posing as a same-origin page.

TOKEN comes from VEXT_TOKEN (the desktop shell generates one per launch and
also gives it to its webviews); a plain dev server makes up its own, which it
hands to the page it serves at "/" (see bootstrap_script).
"""

import hmac
import json
import os
import re
import secrets

TOKEN = (os.environ.get("VEXT_TOKEN") or "").strip() or secrets.token_hex(16)
TOKEN_HEADER = "X-VEXT-Token"

PORT = int(os.environ.get("VEXT_BACKEND_PORT") or 8000)

ALLOWED_ORIGINS = [
    "http://tauri.localhost",
    "https://tauri.localhost",
    f"http://localhost:{PORT}",
    f"http://127.0.0.1:{PORT}",
]
ALLOWED_HOSTS = ["127.0.0.1", "localhost"]
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def token_ok(value: str | None) -> bool:
    return hmac.compare_digest((value or "").encode("utf-8"), TOKEN.encode("utf-8"))


def bootstrap_script() -> str:
    """What the page needs to call the API: where it is, and the token."""
    token = json.dumps(TOKEN).replace("</", "<\\/")
    return f"<script>window.__VEXT__={{apiBase:location.origin,token:{token}}};</script>"


_HEAD = re.compile(r"<head(\s[^>]*)?>", re.IGNORECASE)


def inject_bootstrap(html: str) -> str:
    """index.html with the bootstrap script as the very first thing in <head>,
    so it runs before any of the page's own scripts."""
    match = _HEAD.search(html)
    at = match.end() if match else 0
    return html[:at] + "\n" + bootstrap_script() + html[at:]
