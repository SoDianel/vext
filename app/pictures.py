"""Plugin pictures: the cheap copies the UI shows, where a picture came from, and pictures found
on the web for plugins that have none.

A plugin's picture is `plugin_host.image_path(host_path)` (`<sha1 of the path>.png` in
IMAGES_DIR, keyed by path so it survives rescans). Next to it the UI asks for two variants, each
made once and remade when the PNG is newer:
  backdrop  `<key>.backdrop.jpg`, ~1 KB, 40 px and blurred: scaled up behind the picture it fills
            what a different aspect ratio leaves empty (no CSS filter needed);
  thumb     `<key>.thumb.jpg`, at most 480x300: Browse tiles and list rows load this instead of
            the full screenshot (often 300 KB to 1.4 MB).

**Pictures from the web** (Actions > Metadata > Missing picture). VEXT first reads the plugin's
official store page and KVR page itself (`page_candidates`: og:image / twitter:image, image_src,
JSON-LD image, the screenshots in the page, embedded videos' thumbnails); an AI agent is asked when
that gives nothing usable (or on request) and returns image URLs, each "verified" or "possible" with
a reason, plus pages VEXT reads the same way (`codex_research.run_image_search`). Pages are fetched
with the same rules as pictures (`fetch_page`). `fetch_image` downloads each candidate
defensively (http/https only; the host is resolved first and every address must be public, and the
connection goes to that vetted address, so a name can't be re-pointed at this machine or the LAN
between the check and the download; redirects are re-checked hop by hop; time limit; size cap;
the bytes must decode as a real PNG/JPEG/WebP/GIF/BMP within dimension limits). A good one is
re-encoded as PNG and kept as a *suggestion* in cache/pictures/suggestions, never as the plugin's
picture: the owner picks one and presses Apply (same propose-then-apply model as every AI action).
Apply copies it to image_path and records where it came from (`picture_meta`).
"""

import hashlib
import http.client
import io
import ipaddress
import json
import os
import re
import socket
import ssl
import threading
import time
from datetime import datetime, timezone
from urllib.parse import quote, unquote, urlencode, urljoin, urlsplit

from app import codex_research, db, plugin_host
from app.config import IMAGES_DIR, PROJECT_ROOT

SUGGESTIONS_DIR = PROJECT_ROOT / "cache" / "pictures" / "suggestions"
MAX_CANDIDATES = 6           # kept per plugin (the AI's and the pages' together)
MAX_PER_PAGE = 4             # candidates taken from one page VEXT reads itself
MAX_PAGES = 6                # pages read per plugin (store, KVR, and the pages the AI names)
PAGE_MIN_SIDE = 240         # a picture VEXT took from a page itself: smaller ones are thumbnails

# Download limits.
MAX_BYTES = 8 * 1024 * 1024
MAX_PAGE_BYTES = 3 * 1024 * 1024   # a product page read for its pictures
MAX_REDIRECTS = 4
SOCKET_TIMEOUT_S = 10      # connect, and each read
TOTAL_TIMEOUT_S = 30       # the whole download, redirects included
# Picture limits (a real interface screenshot is a few hundred to ~3000 px wide). MIN_SIDE only
# filters what VEXT finds by itself (pages, the AI): a picture the owner chooses (a pasted link, the
# image search) can be any size, a 505x85 banner included. The upper limits always apply (memory).
MIN_SIDE = 120
MAX_SIDE = 8000
MAX_PIXELS = 30_000_000
DECODERS = ("PNG", "JPEG", "WEBP", "GIF", "BMP")
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/140.0 Safari/537.36 VEXT/1.0")

THUMB_BOX = (480, 300)
VARIANTS = ("backdrop", "thumb")


class FetchRefused(Exception):
    """A candidate image that wasn't downloaded or kept; str() says why, in plain words."""


# ---------- the saved picture and its variants ----------

def picture_index() -> dict:
    """{picture file name: mtime} for every saved picture: one directory read, so lists of
    hundreds of plugins can say which have a picture without a stat per row."""
    out = {}
    try:
        with os.scandir(IMAGES_DIR) as it:
            for entry in it:
                if entry.name.endswith(".png") and entry.is_file():
                    try:
                        out[entry.name] = entry.stat().st_mtime
                    except OSError:
                        pass
    except OSError:
        pass
    return out


def picture_stamp(host_path: str, index: dict | None = None) -> int | None:
    """The picture's mtime as an int (a cache stamp for its URLs), or None without a picture."""
    path = plugin_host.image_path(host_path)
    if index is not None:
        mtime = index.get(os.path.basename(path))
    else:
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = None
    return int(mtime) if mtime is not None else None


def has_picture(host_path: str, index: dict | None = None) -> bool:
    return picture_stamp(host_path, index) is not None


def _fresh(out: str, path: str) -> bool:
    try:
        return os.path.getmtime(out) >= os.path.getmtime(path)
    except OSError:
        return False


def _save_jpeg(im, out: str, quality: int) -> None:
    tmp = f"{out}.{os.getpid()}.{id(im)}.tmp"
    im.save(tmp, "JPEG", quality=quality)
    os.replace(tmp, out)


def _rgb(im):
    """RGB for a JPEG variant; transparent parts go dark (the UI's darkest surface) rather than
    whatever colour the transparent pixels happen to hold."""
    from PIL import Image

    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        base = Image.new("RGB", im.size, (10, 9, 8))
        base.paste(im, mask=im.getchannel("A"))
        return base
    return im.convert("RGB")


def backdrop_path(path: str) -> str:
    """A tiny, blurred copy of a picture (`<name>.backdrop.jpg` beside it, remade when the picture
    is newer). The UI scales it up behind the picture to fill what a different aspect ratio leaves
    empty: blurred by the upscale itself, so no CSS filter runs (cheap even without a GPU, and in
    long lists)."""
    out = os.path.splitext(path)[0] + ".backdrop.jpg"
    if not _fresh(out, path):
        from PIL import Image, ImageFilter

        with Image.open(path) as im:
            small = _rgb(im)
            small.thumbnail((40, 40))
            small = small.filter(ImageFilter.GaussianBlur(1.5))
        _save_jpeg(small, out, 82)
    return out


def thumb_path(path: str) -> str:
    """A smaller copy for tiles and list rows (`<name>.thumb.jpg`, at most 480x300, remade when
    the picture is newer)."""
    out = os.path.splitext(path)[0] + ".thumb.jpg"
    if not _fresh(out, path):
        from PIL import Image

        with Image.open(path) as im:
            small = _rgb(im)
            small.thumbnail(THUMB_BOX, Image.Resampling.LANCZOS)
        _save_jpeg(small, out, 86)
    return out


def variant_path(path: str, variant: str) -> str:
    return backdrop_path(path) if variant == "backdrop" else thumb_path(path)


def delete_with_variants(path: str) -> None:
    base = os.path.splitext(path)[0]
    for f in (path, base + ".backdrop.jpg", base + ".thumb.jpg"):
        try:
            os.remove(f)
        except FileNotFoundError:
            pass


# ---------- where a picture came from ----------

def _key(host_path: str) -> str:
    return os.path.splitext(os.path.basename(plugin_host.image_path(host_path)))[0]


def record_source(conn, host_path: str, source: str, url: str | None = None,
                  page_url: str | None = None) -> None:
    """Remembers how the current picture was made: 'web' (applied from a suggestion), or
    'auto-capture' (the automatic capture job may call this after saving one). A picture with no
    record, or replaced since (its file is newer than the record), counts as a capture."""
    path = plugin_host.image_path(host_path)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return
    conn.execute(
        """INSERT INTO picture_meta (key, source, url, page_url, mtime, set_at) VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET source=excluded.source, url=excluded.url,
               page_url=excluded.page_url, mtime=excluded.mtime, set_at=excluded.set_at""",
        (_key(host_path), source, url, page_url, mtime, _now()),
    )


def picture_info(conn, host_path: str) -> dict | None:
    """{source: capture | auto-capture | web, url, page_url, stamp} for the plugin's picture, or None."""
    path = plugin_host.image_path(host_path)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    row = conn.execute("SELECT source, url, page_url, mtime FROM picture_meta WHERE key=?",
                       (_key(host_path),)).fetchone()
    if row and row[3] is not None and abs(row[3] - mtime) < 1:
        return {"source": row[0], "url": row[1], "page_url": row[2], "stamp": int(mtime)}
    return {"source": "capture", "url": None, "page_url": None, "stamp": int(mtime)}


# ---------- downloading a candidate safely ----------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _public_address(host: str, port: int) -> str:
    """Resolves `host` and returns an address to connect to. Refuses the whole name if ANY of its
    addresses isn't public (loopback, private, link-local, carrier-grade NAT, multicast, reserved,
    unspecified): a name that also points inside this machine or the LAN is never fetched."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError):
        raise FetchRefused(f"the address {host} doesn't resolve")
    addresses = []
    for info in infos:
        raw = info[4][0].split("%")[0]
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            raise FetchRefused(f"{host} resolves to an unusable address")
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if (not ip.is_global or ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
                or ip.is_reserved or ip.is_unspecified):
            raise FetchRefused(f"{host} points to a local or private address ({ip}), which is never fetched")
        addresses.append((info[0], str(ip)))
    if not addresses:
        raise FetchRefused(f"the address {host} doesn't resolve")
    # Prefer IPv4: some home networks resolve AAAA records without routing IPv6.
    addresses.sort(key=lambda a: a[0] != socket.AF_INET)
    return addresses[0][1]


class _PinnedHTTP(http.client.HTTPConnection):
    """Connects to an address vetted by _public_address, whatever the name resolves to later."""

    def __init__(self, host, port, address, timeout):
        super().__init__(host, port, timeout=timeout)
        self._address = address

    def connect(self):
        self.sock = socket.create_connection((self._address, self.port), self.timeout)


def _tls_contexts() -> list:
    """Certificate stores to verify against, in order: Mozilla's bundle (certifi), then Windows'
    own. Both verify fully; the second is only tried when the first rejects the certificate.
    (Seen: Windows' store builds an expired path for a valid Let's Encrypt chain, which the
    Mozilla bundle verifies.)"""
    contexts = []
    try:
        import certifi
        contexts.append(ssl.create_default_context(cafile=certifi.where()))
    except (ImportError, OSError, ssl.SSLError):
        pass
    contexts.append(ssl.create_default_context())
    return contexts


class _PinnedHTTPS(http.client.HTTPSConnection):
    """HTTPS to a vetted address; the certificate is still checked against the host NAME."""

    def __init__(self, host, port, address, timeout):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._address = address

    def connect(self):
        contexts = _tls_contexts()
        for i, context in enumerate(contexts):
            sock = socket.create_connection((self._address, self.port), self.timeout)
            try:
                self.sock = context.wrap_socket(sock, server_hostname=self.host)
                return
            except ssl.SSLCertVerificationError:
                sock.close()
                if i == len(contexts) - 1:
                    raise


def _download(url: str, referer: str | None, want: str = "image") -> tuple[bytes, str]:
    """GETs `url` (following up to MAX_REDIRECTS redirects, each one checked again). Returns
    (body, final url). Raises FetchRefused. `want` is "image" (a picture file) or "html" (a web
    page to read candidates from: only text/html is accepted, at most MAX_PAGE_BYTES)."""
    cap = MAX_PAGE_BYTES if want in ("html", "json") else MAX_BYTES
    deadline = time.monotonic() + TOTAL_TIMEOUT_S
    for _hop in range(MAX_REDIRECTS + 1):
        if time.monotonic() > deadline:
            raise FetchRefused(f"the download took longer than {TOTAL_TIMEOUT_S} s")
        parts = urlsplit(url)
        scheme = (parts.scheme or "").lower()
        if scheme not in ("http", "https"):
            raise FetchRefused("only http and https addresses are fetched")
        if parts.username or parts.password:
            raise FetchRefused("addresses with a user name or password aren't fetched")
        host = parts.hostname
        if not host:
            raise FetchRefused("the address has no host")
        try:
            port = parts.port or (443 if scheme == "https" else 80)
        except ValueError:
            raise FetchRefused("the address has an invalid port")
        address = _public_address(host, port)
        conn_cls = _PinnedHTTPS if scheme == "https" else _PinnedHTTP
        conn = conn_cls(host, port, address, SOCKET_TIMEOUT_S)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": ("text/html,application/xhtml+xml;q=0.9,*/*;q=0.5" if want == "html"
                       else "application/json, text/javascript, */*; q=0.01" if want == "json"
                       # No image/avif: a CDN that gets it sends AVIF, which VEXT doesn't decode.
                       else "image/webp,image/png,image/jpeg,image/gif;q=0.9,image/*;q=0.8"),
            "Accept-Encoding": "identity",
        }
        if referer and referer.lower().startswith(("http://", "https://")):
            headers["Referer"] = referer
        try:
            conn.request("GET", path, headers=headers)
            resp = conn.getresponse()
            if resp.status in (301, 302, 303, 307, 308):
                location = resp.getheader("Location")
                resp.close()
                if not location:
                    raise FetchRefused(f"the server answered {resp.status} without a new address")
                url = urljoin(url, location)
                continue
            if resp.status != 200:
                raise FetchRefused(f"the server answered {resp.status} {resp.reason}".strip())
            ctype = (resp.getheader("Content-Type") or "").split(";")[0].strip().lower()
            if want == "html":
                if ctype not in ("text/html", "application/xhtml+xml", ""):
                    raise FetchRefused(f"it isn't a web page (the server sent {ctype})")
            elif want == "json":
                if ctype not in ("application/json", "application/x-javascript", "text/javascript",
                                 "application/javascript", "text/plain", ""):
                    raise FetchRefused(f"the search answered {ctype}, not results")
            elif "svg" in ctype:
                raise FetchRefused("it is an SVG drawing, not a picture VEXT can store")
            elif ctype.startswith(("text/", "application/json", "application/xhtml")):
                raise FetchRefused(f"it isn't an image (the server sent {ctype})")
            length = resp.getheader("Content-Length")
            if length and length.isdigit() and int(length) > cap:
                raise FetchRefused(f"it is too large ({int(length) // (1024 * 1024)} MB; the limit is "
                                   f"{cap // (1024 * 1024)} MB)")
            chunks, total = [], 0
            while True:
                if time.monotonic() > deadline:
                    raise FetchRefused(f"the download took longer than {TOTAL_TIMEOUT_S} s")
                chunk = resp.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > cap:
                    raise FetchRefused(f"it is larger than {cap // (1024 * 1024)} MB")
                chunks.append(chunk)
            return b"".join(chunks), url
        except FetchRefused:
            raise
        except ssl.SSLError as exc:
            raise FetchRefused(f"the secure connection failed ({exc.reason or exc})")
        except (socket.timeout, TimeoutError):
            raise FetchRefused("the server didn't answer in time")
        except (OSError, http.client.HTTPException) as exc:
            raise FetchRefused(f"the download failed ({exc.__class__.__name__})")
        finally:
            conn.close()
    raise FetchRefused(f"too many redirects (more than {MAX_REDIRECTS})")


def decode_picture(data: bytes, min_side: int = MIN_SIDE):
    """The first frame of a real PNG/JPEG/WebP/GIF/BMP within the size limits, ready to save as
    PNG. `min_side`: the smallest side accepted (1 = any). Raises FetchRefused."""
    from PIL import Image, UnidentifiedImageError

    try:
        im = Image.open(io.BytesIO(data), formats=DECODERS)
    except (UnidentifiedImageError, Image.DecompressionBombError):
        raise FetchRefused("it isn't a picture VEXT can read (PNG, JPEG, WebP, GIF or BMP)")
    except (OSError, ValueError, SyntaxError):
        raise FetchRefused("it isn't a valid picture")
    with im:
        width, height = im.size
        if width * height > MAX_PIXELS or max(width, height) > MAX_SIDE:
            raise FetchRefused(f"it is too large ({width}x{height})")
        if min(width, height) < min_side:
            raise FetchRefused(f"it is too small ({width}x{height}): an icon or a logo, not a screenshot")
        try:
            im.seek(0)
            im.load()
        except (OSError, ValueError, SyntaxError, EOFError):
            raise FetchRefused("the picture is damaged or incomplete")
        has_alpha = im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info)
        return im.convert("RGBA" if has_alpha else "RGB")


def fetch_image(url: str, referer: str | None = None, min_side: int = MIN_SIDE) -> dict:
    """Downloads one candidate and checks it. Returns {png: bytes, width, height, bytes (download
    size), final_url, sha1}. Raises FetchRefused with the reason."""
    data, final_url = _download(url, referer)
    if not data:
        raise FetchRefused("the server sent nothing")
    im = decode_picture(data, min_side)
    out = io.BytesIO()
    im.save(out, "PNG")
    return {"png": out.getvalue(), "width": im.size[0], "height": im.size[1], "bytes": len(data),
            "final_url": final_url, "sha1": hashlib.sha1(data).hexdigest(), "dhash": _dhash(im)}


def _data_image(url: str, min_side: int = MIN_SIDE) -> dict:
    """fetch_image's result for a data:image/...;base64 address (an inline thumbnail)."""
    import base64
    import binascii

    head, _, body = url.partition(",")
    if ";base64" not in head.lower() or len(body) > 2 * 1024 * 1024:
        raise FetchRefused("that inline image can't be read")
    try:
        data = base64.b64decode(body, validate=False)
    except (binascii.Error, ValueError):
        raise FetchRefused("that inline image is damaged") from None
    im = decode_picture(data, min_side)
    out = io.BytesIO()
    im.save(out, "PNG")
    return {"png": out.getvalue(), "width": im.size[0], "height": im.size[1], "bytes": len(data),
            "final_url": "data:", "sha1": hashlib.sha1(data).hexdigest(), "dhash": _dhash(im)}


def _dhash(im) -> int:
    """A 64-bit difference hash: the same picture at another size or quality hashes (nearly) the same."""
    from PIL import Image

    small = im.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    px = list(small.getdata())
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (1 if px[row * 9 + col] > px[row * 9 + col + 1] else 0)
    return bits


def _same_picture(a: dict, b: dict) -> bool:
    if a["sha1"] == b["sha1"]:
        return True
    ra, rb = a["width"] / a["height"], b["width"] / b["height"]
    return abs(ra - rb) < 0.03 and bin(a["dhash"] ^ b["dhash"]).count("1") <= 5


# ---------- suggestions (pending, until the owner applies one) ----------

def suggestion_file(name: str) -> str:
    """The stored candidate PNG (names are generated here, never taken from a request)."""
    return str(SUGGESTIONS_DIR / os.path.basename(name))


def list_suggestions(conn, plugin_ids=None) -> dict:
    """{plugin_id: [suggestion dict, ...]} ordered by rank."""
    rows = conn.execute(
        "SELECT id, plugin_id, n, file, image_url, page_url, kind, note, width, height, bytes, created_at, tier, origin "
        "FROM picture_suggestions ORDER BY plugin_id, n").fetchall()
    out: dict = {}
    for r in rows:
        if plugin_ids is not None and r[1] not in plugin_ids:
            continue
        out.setdefault(r[1], []).append({
            "id": r[0], "plugin_id": r[1], "n": r[2], "file": r[3], "image_url": r[4], "page_url": r[5],
            "kind": r[6], "note": r[7], "width": r[8], "height": r[9], "bytes": r[10], "created_at": r[11],
            "tier": r[12] or "possible", "origin": r[13] or "ai",
        })
    return out


def get_suggestion(conn, suggestion_id: int) -> dict | None:
    r = conn.execute(
        "SELECT id, plugin_id, n, file, image_url, page_url, kind, note, width, height, bytes, created_at, tier, origin "
        "FROM picture_suggestions WHERE id=?", (suggestion_id,)).fetchone()
    if r is None:
        return None
    return {"id": r[0], "plugin_id": r[1], "n": r[2], "file": r[3], "image_url": r[4], "page_url": r[5],
            "kind": r[6], "note": r[7], "width": r[8], "height": r[9], "bytes": r[10], "created_at": r[11],
            "tier": r[12] or "possible", "origin": r[13] or "ai"}


def discard_suggestions(conn, plugin_id: int) -> int:
    """Drops every candidate waiting for this plugin (rows and files). Returns how many."""
    files = [r[0] for r in conn.execute("SELECT file FROM picture_suggestions WHERE plugin_id=?", (plugin_id,))]
    conn.execute("DELETE FROM picture_suggestions WHERE plugin_id=?", (plugin_id,))
    for name in files:
        delete_with_variants(suggestion_file(name))
    return len(files)


def add_link_candidate(conn, plugin_id: int, url: str, page_url: str | None = None,
                       note: str = "Your link") -> dict:
    """The owner's own image link (pasted, or chosen in the in-app image search, which passes the
    page it was on): downloaded with the same safety rules as any candidate (public address, size
    and dimension caps, a real image), except the minimum size: the owner chose it, so a small
    banner or logo is fine (owner, 2026-10-20). Added to the plugin's waiting pictures (tier "verified",
    origin "link"), so the picker shows it like the rest. Returns {id}. Raises FetchRefused."""
    url = (url or "").strip()
    if page_url and not page_url.lower().startswith(("http://", "https://")):
        page_url = None
    if url[:11].lower() == "data:image/":
        # A thumbnail the image browser could only read inline (Google inlines its first
        # rows): decoded here with the same checks as a download; no address to keep.
        got = _data_image(url, min_side=1)
    elif url.lower().startswith(("http://", "https://")):
        got = fetch_image(url, referer=page_url, min_side=1)
    else:
        raise FetchRefused("the link must start with http:// or https://")
    SUGGESTIONS_DIR.mkdir(parents=True, exist_ok=True)
    n = (conn.execute("SELECT COALESCE(MAX(n), 0) FROM picture_suggestions WHERE plugin_id=?",
                      (plugin_id,)).fetchone()[0] or 0) + 1
    fname = f"{plugin_id}-{int(time.time() * 1000)}-{n}.png"
    tmp = suggestion_file(fname) + ".tmp"
    with open(tmp, "wb") as f:
        f.write(got["png"])
    os.replace(tmp, suggestion_file(fname))
    cur = conn.execute(
        """INSERT INTO picture_suggestions (plugin_id, n, file, image_url, page_url, kind, note, width,
                                             height, bytes, agent, created_at, tier, origin)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (plugin_id, n, fname, got["final_url"], page_url, "link", note, got["width"], got["height"],
         got["bytes"], None, _now(), "verified", "link"))
    return {"id": cur.lastrowid}


def apply_suggestion(conn, suggestion_id: int, replace: bool = False) -> dict:
    """Makes one candidate the plugin's picture: copied to image_path (so the backdrop, the
    thumbnail, the Playground and the plugin sheet all pick it up), its source recorded, and the
    plugin's other candidates dropped. Raises ValueError / FileExistsError."""
    sug = get_suggestion(conn, suggestion_id)
    if sug is None:
        raise ValueError("That picture suggestion no longer exists.")
    row = conn.execute("SELECT host_path FROM plugins WHERE id=?", (sug["plugin_id"],)).fetchone()
    if row is None:
        raise ValueError("That plugin is no longer in the catalog.")
    host_path = row[0]
    target = plugin_host.image_path(host_path)
    if os.path.exists(target) and not replace:
        raise FileExistsError("This plugin has a picture now (captured meanwhile?). Nothing was replaced.")
    source = suggestion_file(sug["file"])
    if not os.path.exists(source):
        raise ValueError("The downloaded picture is gone; investigate again.")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmp = f"{target}.{os.getpid()}.tmp"
    with open(source, "rb") as src, open(tmp, "wb") as dst:
        dst.write(src.read())
    delete_with_variants(target)  # the old picture's backdrop and thumbnail go with it
    os.replace(tmp, target)
    record_source(conn, host_path, "web", sug["image_url"], sug["page_url"])
    discard_suggestions(conn, sug["plugin_id"])
    return {"plugin_id": sug["plugin_id"], "stamp": picture_stamp(host_path)}


# ---------- cropping (the owner keeps only the plugin's own window from a bigger picture) ----------

def _crop(path: str, box) -> tuple:
    """The picture at `path` cut to box = (x, y, width, height) in its own pixels (clamped to the
    picture; at least 1x1). Returns (png bytes, width, height). Raises ValueError."""
    from PIL import Image

    try:
        x, y, w, h = (int(round(float(v))) for v in box)
    except (TypeError, ValueError):
        raise ValueError("The crop needs a position and a size.") from None
    with Image.open(path) as im:
        im.load()
        width, height = im.size
        x, y = max(0, min(x, width - 1)), max(0, min(y, height - 1))
        w, h = max(1, min(w, width - x)), max(1, min(h, height - y))
        if (x, y, w, h) == (0, 0, width, height):
            raise ValueError("That's the whole picture: nothing to crop.")
        out = im.crop((x, y, x + w, y + h))
        buf = io.BytesIO()
        out.save(buf, "PNG")
    return buf.getvalue(), w, h


def crop_suggestion(conn, suggestion_id: int, box) -> dict:
    """A waiting picture cropped: saved as a new candidate next to it (same source, note "Cropped"),
    so the original stays to compare and both can be chosen. Returns {id}. Raises ValueError."""
    sug = get_suggestion(conn, suggestion_id)
    if sug is None:
        raise ValueError("That picture suggestion no longer exists.")
    source = suggestion_file(sug["file"])
    if not os.path.exists(source):
        raise ValueError("The downloaded picture is gone; find it again.")
    png, w, h = _crop(source, box)
    n = (conn.execute("SELECT COALESCE(MAX(n), 0) FROM picture_suggestions WHERE plugin_id=?",
                      (sug["plugin_id"],)).fetchone()[0] or 0) + 1
    fname = f"{sug['plugin_id']}-{int(time.time() * 1000)}-{n}.png"
    tmp = suggestion_file(fname) + ".tmp"
    with open(tmp, "wb") as f:
        f.write(png)
    os.replace(tmp, suggestion_file(fname))
    cur = conn.execute(
        """INSERT INTO picture_suggestions (plugin_id, n, file, image_url, page_url, kind, note, width,
                                             height, bytes, agent, created_at, tier, origin)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (sug["plugin_id"], n, fname, sug["image_url"], sug["page_url"], sug["kind"], "Cropped", w, h,
         len(png), None, _now(), sug["tier"], sug["origin"]))
    return {"id": cur.lastrowid, "width": w, "height": h}


def crop_picture(conn, host_path: str, box) -> dict:
    """The plugin's own picture cropped in place (its thumbnail and backdrop are remade from it).
    Where it came from stays recorded. Returns {stamp, width, height}. Raises ValueError."""
    target = plugin_host.image_path(host_path)
    if not os.path.exists(target):
        raise ValueError("This plugin has no picture to crop.")
    info = picture_info(conn, host_path)
    png, w, h = _crop(target, box)
    tmp = f"{target}.{os.getpid()}.tmp"
    with open(tmp, "wb") as f:
        f.write(png)
    delete_with_variants(target)
    os.replace(tmp, target)
    # The stamp (whole seconds) must change, or the lists keep showing the cached picture.
    old = int(info["stamp"]) if info else 0
    if int(os.path.getmtime(target)) <= old:
        os.utime(target, (old + 1, old + 1))
    if info and info["source"] != "capture":
        record_source(conn, host_path, info["source"], info["url"], info["page_url"])
    return {"stamp": picture_stamp(host_path), "width": w, "height": h}


def last_search(conn, plugin_ids=None) -> dict:
    """{plugin_id: {at, found, confidence, notes, rejected: [{url, reason}], agent, pages, ai_used}}:
    what the last web search for each plugin came back with (kept so a row can say why nothing was
    found, and so a group run can tell whether a new store / KVR page is worth another try)."""
    out = {}
    for r in conn.execute("SELECT plugin_id, at, found, confidence, notes, rejected, agent, pages, ai_used FROM picture_searches"):
        if plugin_ids is not None and r[0] not in plugin_ids:
            continue
        try:
            rejected = json.loads(r[5] or "[]")
        except ValueError:
            rejected = []
        try:
            pages = json.loads(r[7] or "[]")
        except ValueError:
            pages = []
        out[r[0]] = {"at": r[1], "found": r[2], "confidence": r[3], "notes": r[4], "rejected": rejected,
                     "agent": r[6], "pages": pages, "ai_used": r[8]}
    return out


# Stock-photo sites are never a candidate. Retailers are: their product IMAGE is acceptable (the
# vendor's own pages may have none), even though a retailer is never a link for the catalog.
_BLOCKED_HOSTS = ("shutterstock.", "istockphoto.", "gettyimages.", "alamy.", "dreamstime.", "123rf.",
                  "depositphotos.", "adobestock.", "stock.adobe.")


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


# ---------- candidates VEXT finds itself, without the AI ----------
# A product page names its own picture for link previews (og:image / twitter:image, link
# rel=image_src, JSON-LD "image"); its main content holds the screenshots; an embedded official
# video has a thumbnail at a fixed address. Reading those is cheap and runs before any AI call.

_SKIP_IMG_WORDS = ("logo", "icon", "favicon", "sprite", "avatar", "badge", "flag", "payment", "paypal", "visa",
                   "mastercard", "spinner", "loader", "placeholder", "pixel", "tracking", "emoji", "arrow",
                   "button", "social", "facebook", "twitter", "instagram", "youtube-logo", "rating", "star")
_IMG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")


def _yt_id(url: str) -> str | None:
    import re
    m = re.search(r"(?:youtube(?:-nocookie)?\.com/(?:embed/|watch\?(?:.*&)?v=|shorts/)|youtu\.be/)([A-Za-z0-9_-]{11})", url or "")
    return m.group(1) if m else None


def youtube_thumbnail(video_id: str) -> str:
    return f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg"


def _largest_from_srcset(srcset: str) -> str | None:
    best, best_w = None, -1
    for part in (srcset or "").split(","):
        bits = part.strip().split()
        if not bits:
            continue
        w = 0
        if len(bits) > 1:
            d = bits[1].lower()
            try:
                w = int(float(d[:-1]) * (1000 if d.endswith("x") else 1)) if d[-1] in "wx" else 0
            except ValueError:
                w = 0
        if w > best_w:
            best, best_w = bits[0], w
    return best


def page_images(html: str, base_url: str, name: str = "") -> list:
    """Picture candidates named by one web page, best first: [{image_url, how}]. `how` is plain words
    ("its share image (og:image)", "a screenshot in the page", "an embedded video's thumbnail")."""
    from html.parser import HTMLParser

    all_words = "".join(c.lower() if c.isalnum() else " " for c in (name or "")).split()
    words = [w for w in all_words if len(w) > 2] or [w for w in all_words if len(w) > 1]
    found: list = []        # (score, order, url, how, named)
    seen: set = set()

    def add(url, how, score, named=True):
        if not url:
            return
        url = urljoin(base_url, url.strip())
        if not url.lower().startswith(("http://", "https://")) or url in seen:
            return
        low = url.lower()
        if low.split("?")[0].endswith(".svg") or low.startswith("data:"):
            return
        # A site's own logo is a common share image (KVR adds two to every page).
        if any(w in urlsplit(low).path for w in ("logo", "favicon", "/icon", "-icon", "_icon")):
            return
        seen.add(url)
        found.append((score, len(found), url, how, named))

    class P(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.main_depth = 0
            self.skip_depth = 0
            self.in_ld = False
            self.ld = []

        def handle_starttag(self, tag, attrs):
            a = {k.lower(): (v or "") for k, v in attrs}
            if tag in ("main", "article"):
                self.main_depth += 1
            if tag in ("header", "footer", "nav", "aside"):
                self.skip_depth += 1
            if tag == "meta":
                key = (a.get("property") or a.get("name") or "").lower()
                if key in ("og:image", "og:image:url", "og:image:secure_url"):
                    add(a.get("content"), "share image", 100)
                elif key in ("twitter:image", "twitter:image:src"):
                    add(a.get("content"), "share image", 95)
            elif tag == "link" and "image_src" in (a.get("rel") or "").lower():
                add(a.get("href"), "share image", 90)
            elif tag == "script" and "ld+json" in (a.get("type") or "").lower():
                self.in_ld = True
            elif tag in ("iframe", "a", "lite-youtube", "div"):
                src = a.get("src") or a.get("href") or a.get("data-src") or ""
                vid = _yt_id(src) or (a.get("videoid") if tag == "lite-youtube" else None)
                if vid:
                    add(youtube_thumbnail(vid), "video thumbnail", 45 if tag != "a" else 30)
            elif tag == "img" or tag == "source":
                src = (_largest_from_srcset(a.get("srcset") or a.get("data-srcset") or "")
                       or a.get("data-src") or a.get("data-lazy-src") or a.get("data-original") or a.get("src"))
                if not src:
                    return
                low = src.lower()
                ext = os.path.splitext(urlsplit(src).path)[1].lower() if "://" in src or src.startswith("/") else ""
                if ext and ext not in _IMG_EXT:
                    return
                if any(w in low for w in _SKIP_IMG_WORDS) or any(w in (a.get("alt") or "").lower() for w in ("logo", "icon")):
                    return
                try:
                    w = int(a.get("width") or 0)
                    h = int(a.get("height") or 0)
                except ValueError:
                    w = h = 0
                if (w and w < MIN_SIDE) or (h and h < MIN_SIDE):
                    return
                text = (low + " " + (a.get("alt") or "").lower() + " " + (a.get("title") or "").lower())
                hits = sum(1 for wd in words if wd in text)
                score = 20 + (15 if self.main_depth else 0) - (15 if self.skip_depth else 0) + 15 * min(hits, 3)
                score += min(20, (w * h) // 40000) if w and h else 0
                if "screenshot" in text or "gui" in text or "interface" in text:
                    score += 10
                alt = (a.get("alt") or a.get("title") or "").strip()
                add(src, f"picture “{alt[:50]}”" if alt else "picture in the page", score,
                    named=hits > 0)

        def handle_endtag(self, tag):
            if tag in ("main", "article") and self.main_depth:
                self.main_depth -= 1
            if tag in ("header", "footer", "nav", "aside") and self.skip_depth:
                self.skip_depth -= 1
            if tag == "script":
                self.in_ld = False

        def handle_data(self, data):
            if self.in_ld:
                self.ld.append(data)

    parser = P()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 - a broken page just yields what was read so far
        pass

    def walk(node, depth=0):
        if depth > 8:
            return
        if isinstance(node, list):
            for x in node:
                walk(x, depth + 1)
        elif isinstance(node, dict):
            for key in ("image", "thumbnailUrl", "screenshot"):
                val = node.get(key)
                if isinstance(val, str):
                    add(val, "product data image", 85)
                elif isinstance(val, list):
                    for v in val[:4]:
                        if isinstance(v, str):
                            add(v, "product data image", 85)
                        elif isinstance(v, dict):
                            add(v.get("url") or v.get("contentUrl"), "product data image", 85)
                elif isinstance(val, dict):
                    add(val.get("url") or val.get("contentUrl"), "product data image", 85)
            for v in node.values():
                if isinstance(v, (dict, list)):
                    walk(v, depth + 1)

    for block in parser.ld:
        try:
            walk(json.loads(block))
        except ValueError:
            continue
    found.sort(key=lambda f: (-f[0], f[1]))
    return [{"image_url": url, "how": how, "named": named} for _score, _n, url, how, named in found]


def fetch_page(url: str) -> tuple[str, str]:
    """Downloads one web page with the same safety rules as a picture (public addresses only,
    pinned connection, redirects re-checked, time and size limits). Returns (html, final url)."""
    data, final_url = _download(url, None, want="html")
    head = data[:4096].decode("ascii", errors="ignore").lower()
    charset = "utf-8"
    i = head.find("charset=")
    if i >= 0:
        cand = head[i + 8:i + 40].strip("\"' ").split("\"")[0].split("'")[0].split(";")[0].split(">")[0].strip()
        if cand:
            charset = cand
    try:
        return data.decode(charset, errors="replace"), final_url
    except LookupError:
        return data.decode("utf-8", errors="replace"), final_url


def _site(url: str) -> str:
    h = _host(url)
    return h[4:] if h.startswith("www.") else h


def page_candidates(page_url: str, label: str, name: str, tier: str = "possible",
                    strict: bool = False) -> tuple[list, str | None]:
    """Reads one page and returns (candidates, error). Each candidate: {image_url, page_url, tier,
    kind, reason, origin: "page"}. `strict` (a page that isn't the plugin's own, such as an article
    the AI named): only the page's share image, its JSON-LD image, video thumbnails and pictures
    whose name or caption mentions the plugin, not the other articles' pictures around it."""
    try:
        html, final = fetch_page(page_url)
    except FetchRefused as exc:
        return [], f"{label} ({_site(page_url)}) couldn't be read: {exc}"
    except Exception as exc:  # noqa: BLE001 - untrusted page: anything odd just skips it
        return [], f"{label} ({_site(page_url)}) couldn't be read ({exc.__class__.__name__})"
    out = []
    for img in [i for i in page_images(html, final, name) if i["named"] or not strict][:MAX_PER_PAGE]:
        video = img["how"] == "video thumbnail"
        out.append({"image_url": img["image_url"], "page_url": page_url, "tier": tier,
                    "kind": "video" if video else "other",
                    "reason": f"{img['how']} of {label}", "origin": "page"})
    return out, None


# ---------- one plugin's search ----------

def _fetch_candidates(cands: list, cache: dict) -> tuple[list, list]:
    """Downloads candidates side by side (each with the safety rules above). Returns (kept [(c, got)],
    rejected [{url, reason}]), in the candidates' order, duplicates (same bytes) dropped."""
    from concurrent.futures import ThreadPoolExecutor

    def one(c):
        url = c["image_url"]
        if url in cache:
            return cache[url]
        try:
            got = ("ok", fetch_image(url, referer=c.get("page_url")))
        except FetchRefused as exc:
            got = ("no", str(exc))
        except Exception as exc:  # noqa: BLE001 - untrusted bytes: anything odd just rejects this one
            got = ("no", f"it couldn't be read ({exc.__class__.__name__})")
        cache[url] = got
        return got

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(one, cands))
    kept, rejected = [], []
    for c, (state, val) in zip(cands, results):
        if state == "ok":
            kept.append((c, val))
        else:
            rejected.append({"url": c["image_url"], "reason": val})
    return kept, rejected


def _big_enough(kept: list) -> list:
    """Drops the small copies a page carries (thumbnails, related-article tiles) that VEXT picked
    up itself; what the AI proposed is kept at any size the download rules allow."""
    return [(c, g) for c, g in kept
            if c.get("origin") != "page" or min(g["width"], g["height"]) >= PAGE_MIN_SIDE]


def _usable(c: dict) -> str | None:
    """Why a candidate is dropped before downloading, or None."""
    host = _host(c.get("image_url") or "")
    if not host:
        return "not a web address"
    if any(b in host for b in _BLOCKED_HOSTS):
        return "a stock-photo site"
    return None


# ---------- learning from earlier finds ----------
# The pictures the owner applied from the web and the plugins' store/KVR links show where a vendor
# keeps its pictures (Image-Line: the FL Studio manual's img_plug/<Name>.png). An address with the
# plugin's name in it becomes a template ("…/img_plug/{nospace}.png"); a template that rebuilds the
# address of other plugins of the same vendor is tried for this one, and named to the AI as a hint.

_FORM_LABELS = {"raw": "<name>", "nospace": "<name without spaces>", "under": "<name_with_underscores>",
                "slug": "<name-in-lowercase-with-dashes>", "flat": "<namelowercase>"}


def _name_forms(name: str) -> dict:
    n = " ".join((name or "").split())
    return {"raw": n, "nospace": n.replace(" ", ""), "under": n.replace(" ", "_"),
            "slug": re.sub(r"[^a-z0-9]+", "-", n.lower()).strip("-"),
            "flat": re.sub(r"[^a-z0-9]+", "", n.lower())}


def _norm_url(url: str) -> str | None:
    parts = urlsplit(url or "")
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not host:
        return None
    return (host[4:] if host.startswith("www.") else host) + unquote(parts.path).rstrip("/")


def _templates_of(url: str, name: str) -> set:
    """Every template of `url` with one spelling of `name` (3+ characters) in its path."""
    parts = urlsplit(url or "")
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return set()
    path = unquote(parts.path).rstrip("/")
    out = set()
    for form, token in _name_forms(name).items():
        i = path.rfind(token) if len(token) >= 3 else -1
        if i >= 0:
            out.add(f"{parts.scheme}://{parts.hostname.lower()}"
                    + path[:i].replace("{", "").replace("}", "") + "{" + form + "}"
                    + path[i + len(token):].replace("{", "").replace("}", ""))
    return out


def fill_template(template: str, name: str) -> str:
    forms = _name_forms(name)
    for form, token in forms.items():
        template = template.replace("{" + form + "}", token)
    parts = urlsplit(template)
    return f"{parts.scheme}://{parts.netloc}{quote(parts.path, safe='/-._~!$&()*+,;=:@')}"


def vendor_patterns(conn, vendor: str | None, exclude_host_path: str | None = None) -> dict:
    """{images: [...], pages: [...]}: templates for this vendor's pictures and picture pages, best
    first, each {template, pictures (applied web pictures it rebuilds), links (store/KVR links it
    rebuilds)}. Only templates that rebuild at least one applied picture's address, or two links."""
    if not vendor:
        return {"images": [], "pages": []}
    rows = conn.execute("SELECT name, host_path, store_url, kvr_url FROM plugins WHERE vendor=? AND name IS NOT NULL",
                        (vendor,)).fetchall()
    examples = []   # (kind, weight, url, name)
    for name, host_path, store_url, kvr_url in rows:
        if host_path and exclude_host_path and host_path.lower() == exclude_host_path.lower():
            continue
        info = picture_info(conn, host_path) if host_path else None
        if info and info["source"] == "web":
            if info.get("url"):
                examples.append(("images", "pictures", info["url"], name))
            if info.get("page_url"):
                examples.append(("pages", "pictures", info["page_url"], name))
        for link in (store_url, kvr_url):
            if link:
                examples.append(("pages", "links", link, name))
    out = {"images": [], "pages": []}
    for kind in out:
        mine = [e for e in examples if e[0] == kind]
        templates = set().union(*[_templates_of(url, name) for _k, _w, url, name in mine]) if mine else set()
        for t in templates:
            score = {"pictures": set(), "links": set()}
            for _k, weight, url, name in mine:
                if _norm_url(fill_template(t, name)) == _norm_url(url):
                    score[weight].add(name)
            if score["pictures"] or len(score["links"]) >= 2:
                out[kind].append({"template": t, "pictures": len(score["pictures"]), "links": len(score["links"])})
        out[kind].sort(key=lambda p: (-p["pictures"], -p["links"], not p["template"].startswith("https://www."),
                                      p["template"]))
        # The same address under two spellings (a name without spaces fits {raw} and {nospace}), or
        # with and without www / https: keep the best one.
        seen, kept = set(), []
        for p in out[kind]:
            sig = re.sub(r"^https?://(www\.)?", "", re.sub(r"\{\w+\}", "{}", p["template"]))
            if sig not in seen:
                seen.add(sig)
                kept.append(p)
        out[kind] = kept[:3]
    return out


def vendor_hints(patterns: dict) -> list:
    """The templates as lines for the AI prompt."""
    lines = []
    for kind, what in (("images", "picture file"), ("pages", "page")):
        for p in patterns.get(kind) or []:
            shown = re.sub(r"\{(\w+)\}", lambda m: _FORM_LABELS.get(m.group(1), m.group(0)), p["template"])
            n = p["pictures"] or p["links"]
            lines.append(f"{what}: {shown} ({n} earlier {'picture' if p['pictures'] else 'link'}{'s' if n != 1 else ''})")
    return lines


def investigate(db_path: str, item: dict, cfg, ask_ai: bool | None = None) -> dict:
    """One plugin's picture search. First VEXT reads the plugin's official store page and KVR page
    itself (share image, JSON-LD, the screenshots in the page, embedded videos' thumbnails). Then the
    AI (if `cfg`) is asked: when those gave nothing usable, or always with ask_ai=True (never with
    False). It verifies and ranks what VEXT found, adds its own finds and names pages, which VEXT
    reads the same way. Every candidate is downloaded and checked; the good ones (up to
    MAX_CANDIDATES, "verified" first) are stored as suggestions. A run that keeps nothing leaves
    earlier candidates alone. Returns a summary for the batch status. Raises
    codex_research.ResearchError when the AI failed and nothing was found without it."""
    name = item["name"]
    pages = []
    if item.get("store_url"):
        pages.append((item["store_url"], "the official product page", "possible"))
    if item.get("kvr_url"):
        pages.append((item["kvr_url"], "its KVR page", "possible"))
    # Where this vendor's earlier pictures were: the same address with this plugin's name is tried
    # first (a guess that misses is silent), and the AI is told about it.
    try:
        with db.connect(db_path) as conn:
            learned = vendor_patterns(conn, item.get("vendor"), item.get("host_path"))
    except Exception:  # noqa: BLE001 - a hint only
        learned = {"images": [], "pages": []}
    known_pages = {_norm_url(u) for u, _l, _t in pages}
    for p in learned["pages"][:2]:
        url = fill_template(p["template"], name)
        if _norm_url(url) not in known_pages:
            known_pages.add(_norm_url(url))
            pages.append((url, f"its page on {_site(url)} (learned from earlier pictures)", "possible", True))
    page_notes, read_pages, cands = [], [], []
    for url, label, tier, *guess in pages:
        found, err = page_candidates(url, label, name, tier)
        if err and guess:
            continue
        read_pages.append(url)
        if err:
            page_notes.append(err)
        cands += found
    guessed = set()
    guess_page = fill_template(learned["pages"][0]["template"], name) if learned["pages"] else None
    for p in learned["images"][:2]:
        url = fill_template(p["template"], name)
        guessed.add(url)
        same_site = guess_page if guess_page and _site(guess_page) == _site(url) else None
        cands.insert(0, {"image_url": url, "page_url": same_site, "tier": "possible", "kind": "interface",
                         "reason": f"{_site(url)} (address learned from earlier pictures)", "origin": "page"})
    cache: dict = {}
    pre_rejected = [{"url": c["image_url"], "reason": _usable(c)} for c in cands if _usable(c)]
    cands = [c for c in cands if not _usable(c)]
    det_kept, det_rejected = _fetch_candidates(cands, cache)
    det_rejected = [r for r in det_rejected if r["url"] not in guessed]
    det_kept = _big_enough(det_kept)

    result, ai_error, ai_used = None, None, None
    want_ai = cfg is not None and ask_ai is not False and (ask_ai is True or not det_kept)
    if want_ai:
        known = [{"image_url": got["final_url"], "page_url": c["page_url"], "reason": c["reason"]} for c, got in det_kept]
        try:
            result = codex_research.run_image_search(
                name, item["format"], item["vendor"], item["host_path"], item.get("store_url"), cfg,
                kvr_url=item.get("kvr_url"), known=known, hints=vendor_hints(learned))
            ai_used = cfg.agent
        except codex_research.ResearchCancelled:
            raise
        except codex_research.ResearchError as exc:
            if not det_kept:
                raise
            ai_error = str(exc)

    ai_cands, ai_rejected = [], []
    if result:
        for c in result.get("images") or []:
            c = {**c, "origin": "ai"}
            why = _usable(c)
            if why:
                ai_rejected.append({"url": c["image_url"], "reason": why})
            else:
                ai_cands.append(c)
        for page in (result.get("pages") or [])[:MAX_PAGES]:
            if page in read_pages or len(read_pages) >= MAX_PAGES:
                continue
            if urlsplit(page).path.strip("/") == "":
                continue  # a site's home page: its share image is the site's, not the plugin's
            read_pages.append(page)
            vid = _yt_id(page)
            if vid:
                # A video page: its thumbnail has a fixed address (YouTube answers scripted page reads 429).
                ai_cands.append({"image_url": youtube_thumbnail(vid), "page_url": page, "tier": "possible",
                                 "kind": "video", "reason": "thumbnail of a video the AI named", "origin": "page"})
                continue
            found, err = page_candidates(page, f"an article on {_site(page)}", name, "possible", strict=True)
            if err:
                page_notes.append(err)
            ai_cands += [c for c in found if not _usable(c)]
    ai_kept, ai_fetch_rejected = _fetch_candidates(ai_cands, cache)
    ai_kept = _big_enough(ai_kept)

    # One list: what the AI verified first, then the rest (the AI's order, then the pages'). An image
    # the AI also returned takes the AI's label. Same bytes = one candidate.
    by_url = {}
    for c, got in ai_kept + det_kept:
        key = got["final_url"]
        if key in by_url:
            continue
        by_url[key] = (c, got)
    ordered = sorted(by_url.values(), key=lambda cg: (0 if cg[0].get("tier") == "verified" else 1))
    # The same picture at two sizes (KVR's "b" and "m" copies, a store's thumbnail) is one candidate:
    # the larger copy, in the better place of the two.
    fetched = []
    for c, got in ordered:
        twin = next((k for k, (_c, g) in enumerate(fetched) if _same_picture(g, got)), None)
        if twin is None:
            fetched.append((c, got))
        elif got["width"] * got["height"] > fetched[twin][1]["width"] * fetched[twin][1]["height"]:
            first = fetched[twin][0]
            tier = "verified" if "verified" in (first.get("tier"), c.get("tier")) else c.get("tier")
            fetched[twin] = ({**c, "tier": tier, "reason": first.get("reason") if first.get("tier") == "verified" else c.get("reason")}, got)
    fetched = fetched[:MAX_CANDIDATES]
    # What the pages VEXT read offered and failed (tracking pixels, small badges) is noise; only the
    # AI's own proposals that failed are worth showing.
    page_failed = [r for r in det_rejected + ai_fetch_rejected
                   if not r["reason"].startswith(("it is too small", "it isn't a picture"))
                   or r["url"] in {c["image_url"] for c in ai_cands if c.get("origin") == "ai"}]
    rejected = pre_rejected + ai_rejected + page_failed

    notes = [n for n in ((result or {}).get("notes"), ) if n]
    if ai_error:
        notes.append(f"The AI couldn't help this time ({ai_error})")
    elif cfg is None and not det_kept:
        notes.append("No AI agent was used: only the store and KVR pages were read.")
    notes += page_notes
    notes = [n.strip() if n.strip().endswith((".", "!", "?")) else n.strip() + "." for n in notes if n and n.strip()]

    SUGGESTIONS_DIR.mkdir(parents=True, exist_ok=True)
    plugin_id = item["id"]
    agent = cfg.agent if cfg is not None else None
    with db.connect(db_path) as conn:
        if fetched:
            discard_suggestions(conn, plugin_id)
            stamp = int(time.time() * 1000)
            for n, (c, got) in enumerate(fetched, start=1):
                fname = f"{plugin_id}-{stamp}-{n}.png"
                tmp = suggestion_file(fname) + ".tmp"
                with open(tmp, "wb") as f:
                    f.write(got["png"])
                os.replace(tmp, suggestion_file(fname))
                conn.execute(
                    """INSERT INTO picture_suggestions (plugin_id, n, file, image_url, page_url, kind, note, width,
                                                         height, bytes, agent, created_at, tier, origin)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (plugin_id, n, fname, got["final_url"], c.get("page_url"), c.get("kind"), c.get("reason"),
                     got["width"], got["height"], got["bytes"], agent if c.get("origin") == "ai" else None, _now(),
                     c.get("tier") or "possible", c.get("origin") or "ai"),
                )
        conn.execute(
            """INSERT INTO picture_searches (plugin_id, at, found, confidence, notes, rejected, sources, agent, pages, ai_used)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(plugin_id) DO UPDATE SET at=excluded.at, found=excluded.found,
                   confidence=excluded.confidence, notes=excluded.notes, rejected=excluded.rejected,
                   sources=excluded.sources, agent=excluded.agent, pages=excluded.pages, ai_used=excluded.ai_used""",
            (plugin_id, _now(), len(fetched), (result or {}).get("confidence"), " ".join(notes) or None,
             json.dumps(rejected), json.dumps((result or {}).get("sources") or []), agent,
             json.dumps(read_pages), ai_used),
        )
        db.record_attempt(conn, "image", plugin_id)
    return {"found": len(fetched), "rejected": len(rejected), "confidence": (result or {}).get("confidence"),
            "notes": " ".join(notes) or None, "ai": ai_used, "ai_error": ai_error,
            "verified": sum(1 for c, _ in fetched if c.get("tier") == "verified"),
            "pages_read": len(read_pages)}


def prune_orphans(conn) -> int:
    """Removes candidate files no suggestion row points to (a plugin deleted from the catalog
    takes its rows with it through the foreign key, not its files)."""
    try:
        names = {e.name for e in os.scandir(SUGGESTIONS_DIR) if e.name.endswith(".png")}
    except OSError:
        return 0
    known = {r[0] for r in conn.execute("SELECT file FROM picture_suggestions")}
    gone = names - known
    for name in gone:
        delete_with_variants(suggestion_file(name))
    return len(gone)


# ---------- in-app image search (no AI) ----------
# The plugin sheet's "Search images…" and the "No AI" choice: an image search run by VEXT itself
# (Bing's image results, read server-side, no key), shown five at a time inside VEXT. Thumbnails are
# proxied (only the addresses a search returned, with the same public-address and size rules); the
# image the owner chooses goes through add_link_candidate like a pasted link.

SEARCH_TTL_S = 600
SEARCH_BATCH = 35
_search_lock = threading.Lock()
_searches: dict = {}     # query (lowercase) -> {"at", "results", "done"}
_thumbs: dict = {}       # thumbnail address -> time it was returned by a search


def _bing_batch(query: str, first: int) -> list:
    import html as html_lib

    url = "https://www.bing.com/images/async?" + urlencode(
        {"q": query, "first": first, "count": SEARCH_BATCH, "mmasync": 1, "adlt": "moderate"})
    body, _final = _download(url, "https://www.bing.com/images/search", want="html")
    text = body.decode("utf-8", "replace")
    out = []
    for raw in re.findall(r'\bm="(\{[^"]+\})"', text):
        try:
            m = json.loads(html_lib.unescape(raw))
        except ValueError:
            continue
        image, page, thumb = m.get("murl") or "", m.get("purl") or "", m.get("turl") or ""
        if not image.lower().startswith(("http://", "https://")) or not thumb.lower().startswith("https://"):
            continue
        if _usable({"image_url": image}) or urlsplit(image).path.lower().endswith(".svg"):
            continue
        out.append({"image_url": image, "thumb_url": thumb,
                    "page_url": page if page.lower().startswith(("http://", "https://")) else None,
                    "title": html_lib.unescape(m.get("t") or "")[:160],
                    "site": _site(page or image)})
    return out


def image_search(query: str, offset: int = 0, count: int = 5) -> dict:
    """{query, offset, results: [{image_url, thumb_url, page_url, title, site}], more}. Results are
    kept per query for SEARCH_TTL_S, and more are read when a later page needs them. Raises
    FetchRefused when the search can't be read."""
    query = " ".join((query or "").split())[:200]
    if not query:
        raise FetchRefused("type what to search for")
    offset, count = max(0, int(offset)), max(1, min(int(count), 20))
    key = query.lower()
    with _search_lock:
        entry = _searches.get(key)
        if not entry or time.time() - entry["at"] > SEARCH_TTL_S:
            entry = {"at": time.time(), "results": [], "done": False, "first": 0}
    tries = 0
    while len(entry["results"]) < offset + count and not entry["done"] and tries < 3:
        tries += 1
        batch = _bing_batch(query, entry["first"])
        entry["first"] += SEARCH_BATCH
        seen = {r["image_url"] for r in entry["results"]}
        new = [r for r in batch if r["image_url"] not in seen]
        if not new:
            entry["done"] = True
        entry["results"] += new
    if not entry["results"] and tries:
        raise FetchRefused("the image search gave no results (it may be busy, or the words too specific)")
    now = time.time()
    with _search_lock:
        _searches[key] = entry
        for k in [k for k, e in _searches.items() if now - e["at"] > SEARCH_TTL_S]:
            del _searches[k]
        for r in entry["results"]:
            _thumbs[r["thumb_url"]] = now
        for k in [k for k, at in _thumbs.items() if now - at > SEARCH_TTL_S * 3]:
            del _thumbs[k]
    page = entry["results"][offset:offset + count]
    return {"query": query, "offset": offset, "results": page,
            "more": len(entry["results"]) > offset + count or not entry["done"]}


def search_thumbnail(url: str) -> bytes:
    """A search result's thumbnail as a small JPEG. Only addresses a search returned are fetched."""
    from PIL import Image, UnidentifiedImageError

    with _search_lock:
        known = url in _thumbs
    if not known:
        raise FetchRefused("not a thumbnail from a search")
    data, _final = _download(url, "https://www.bing.com/")
    try:
        im = Image.open(io.BytesIO(data), formats=DECODERS)
        if im.size[0] * im.size[1] > MAX_PIXELS:
            raise FetchRefused("the thumbnail is too large")
        im.thumbnail((360, 360))
        out = io.BytesIO()
        _rgb(im).save(out, "JPEG", quality=82)
        return out.getvalue()
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        raise FetchRefused("the thumbnail isn't a picture") from None
