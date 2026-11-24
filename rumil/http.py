"""Minimal HTTP client (stdlib only) with per-host rate limits, retries, and a test hook.

Registry APIs have published limits; Crossref's public pool allows one request
at a time, its polite pool (requests that carry a contact email) allows three.
"""

from __future__ import annotations

import gzip
import json
import random
import ssl
import threading
import time
import zlib
from dataclasses import dataclass, field
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from . import __version__

try:  # macOS python.org builds often lack a CA store; certifi fixes that.
    import certifi

    _SSL = ssl.create_default_context(cafile=certifi.where())
except Exception:  # pragma: no cover
    _SSL = ssl.create_default_context()


class HttpError(Exception):
    def __init__(self, message: str, status: int | None = None, url: str = ""):
        super().__init__(message)
        self.status = status
        self.url = url


@dataclass
class Response:
    status: int
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""

    @property
    def text(self) -> str:
        ctype = self.headers.get("content-type", "")
        enc = "utf-8"
        if "charset=" in ctype:
            enc = ctype.split("charset=")[-1].split(";")[0].strip() or "utf-8"
        try:
            return self.body.decode(enc, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")

    def json(self):
        return json.loads(self.text)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


# A transport takes (method, url, headers, timeout) and returns a Response.
Transport = Callable[[str, str, dict, float], Response]

# Host -> (max concurrent, min seconds between request starts)
# Crossref (July 2026): single-record lookups 5/s public, 10/s polite; searches (list queries) 1/s public,
# 3/s polite. Searches get their own gate ("api.crossref.org#list").
_HOST_LIMITS = {
    "api.crossref.org": (1, 0.22),
    "api.crossref.org#list": (1, 1.1),
    "api.datacite.org": (2, 0.2),
    "doi.org": (2, 0.2),
    "export.arxiv.org": (1, 3.0),  # arXiv asks for one request every 3 s
    "api.adsabs.harvard.edu": (2, 0.2),
    "openlibrary.org": (2, 0.3),
    "www.googleapis.com": (2, 0.3),
    "eutils.ncbi.nlm.nih.gov": (1, 0.35),
}


class _HostGate:
    def __init__(self, concurrency: int, interval: float):
        self.sem = threading.BoundedSemaphore(concurrency)
        self.interval = interval
        self.lock = threading.Lock()
        self.next_time = 0.0

    def __enter__(self):
        self.sem.acquire()
        with self.lock:
            now = time.monotonic()
            wait = self.next_time - now
            self.next_time = max(now, self.next_time) + self.interval
        if wait > 0:
            time.sleep(wait)

    def __exit__(self, *exc):
        self.sem.release()


def _urllib_transport(method: str, url: str, headers: dict, timeout: float) -> Response:
    req = Request(url, method=method, headers=headers)
    try:
        with urlopen(req, timeout=timeout, context=_SSL) as r:
            body = r.read(25_000_000)
            hdrs = {k.lower(): v for k, v in r.headers.items()}
            status = r.status
            final = r.geturl()
    except HTTPError as e:
        body = e.read() if hasattr(e, "read") else b""
        hdrs = {k.lower(): v for k, v in (e.headers or {}).items()}
        status, final = e.code, url
    enc = hdrs.get("content-encoding", "")
    if enc == "gzip":
        body = gzip.decompress(body)
    elif enc == "deflate":
        body = zlib.decompress(body)
    return Response(status=status, url=final, headers=hdrs, body=body)


class Http:
    def __init__(self, contact_email: str = "", transport: Transport | None = None, timeout: float = 25.0):
        self.contact_email = (contact_email or "").strip()
        self.transport = transport or _urllib_transport
        self.timeout = timeout
        self._gates: dict[str, _HostGate] = {}
        self._gates_lock = threading.Lock()
        self._limits = dict(_HOST_LIMITS)
        if self.contact_email:  # polite pool: faster and more concurrency
            self._limits.update({"api.crossref.org": (3, 0.11), "api.crossref.org#list": (1, 0.36)})

    def set_contact_email(self, email: str) -> None:
        """Switch between Crossref's public and polite pools at runtime."""
        self.contact_email = (email or "").strip()
        self._limits = dict(_HOST_LIMITS)
        if self.contact_email:
            self._limits.update({"api.crossref.org": (3, 0.11), "api.crossref.org#list": (1, 0.36)})
        with self._gates_lock:
            self._gates.clear()

    @property
    def user_agent(self) -> str:
        ua = f"Rumil/{__version__} (personal citation manager)"
        if self.contact_email:
            ua = f"Rumil/{__version__} (mailto:{self.contact_email})"
        return ua

    @staticmethod
    def _gate_key(url: str) -> str:
        u = urlparse(url)
        host = u.hostname or ""
        if host == "api.crossref.org" and u.path.rstrip("/").endswith("/works") and u.query:
            return host + "#list"
        return host

    def _adapt(self, key: str, resp: Response) -> None:
        """Slow down to the limits a server announces (x-rate-limit-*), and back off hard after a 429."""
        g = self._gate(key)
        try:
            lim = float(resp.headers.get("x-rate-limit-limit", "0") or 0)
            per = float((resp.headers.get("x-rate-limit-interval", "1s") or "1s").rstrip("s") or 1)
            if lim > 0:
                g.interval = max(g.interval, per / lim * 1.1)
        except ValueError:
            pass
        if resp.status == 429:
            g.interval = min(max(g.interval * 2, 0.5), 10.0)

    def _gate(self, host: str) -> _HostGate:
        with self._gates_lock:
            g = self._gates.get(host)
            if g is None:
                conc, interval = self._limits.get(host, (4, 0.0))
                g = self._gates[host] = _HostGate(conc, interval)
            return g

    def request(self, url: str, *, params: dict | None = None, headers: dict | None = None,
                method: str = "GET", accept: str | None = None, retries: int = 3,
                timeout: float | None = None) -> Response:
        if params:
            url = url + ("&" if "?" in url else "?") + urlencode({k: v for k, v in params.items() if v is not None})
        h = {"User-Agent": self.user_agent, "Accept-Encoding": "gzip"}
        if accept:
            h["Accept"] = accept
        if headers:
            h.update(headers)
        host = urlparse(url).hostname or ""
        key = self._gate_key(url)
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                with self._gate(key):
                    resp = self.transport(method, url, h, timeout or self.timeout)
                self._adapt(key, resp)
            except (URLError, TimeoutError, ConnectionError, ssl.SSLError, OSError) as e:
                last_exc = e
                if attempt < retries:
                    time.sleep(min(8, 1.5 ** attempt + random.random()))
                    continue
                raise HttpError(f"network error contacting {host}: {e}", None, url) from e
            if resp.status in (429, 500, 502, 503, 504) and attempt < retries:
                ra = resp.headers.get("retry-after")
                delay = float(ra) if ra and ra.replace(".", "", 1).isdigit() else min(20, 2 ** (attempt + 1) + random.random())
                time.sleep(delay)
                continue
            return resp
        raise HttpError(f"giving up on {url}: {last_exc}", None, url)

    def get_json(self, url: str, **kw):
        resp = self.request(url, accept=kw.pop("accept", "application/json"), **kw)
        if resp.status == 404:
            return None
        if resp.status == 429:
            raise HttpError(f"{urlparse(url).hostname} is rate-limiting Rumil (HTTP 429); "
                            "set contact_email (rumil config set contact_email you@example.edu) and try again", 429, url)
        if not resp.ok:
            raise HttpError(f"HTTP {resp.status} from {urlparse(url).hostname}", resp.status, url)
        try:
            return resp.json()
        except ValueError as e:
            raise HttpError(f"invalid JSON from {urlparse(url).hostname}", resp.status, url) from e
