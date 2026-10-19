"""Small HTTP client: stdlib only, retries with backoff, honors HTTPS_PROXY."""

from __future__ import annotations

import hashlib
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_UA = os.environ.get(
    "CIM_USER_AGENT",
    "compute-index-monitor/0.2 (private research archive; each page fetched at most once per run, "
    "daily, Kalshi every 3 hours; max 2 requests/s per host)",
)


# Live pages are ~200 KB. Anything far larger is refused before parsing (regex cost, memory).
MAX_BYTES = 5_000_000
# Minimum gap between requests to the same host. Kalshi answered HTTP 429 to 62 back-to-back
# requests on 2026-10-14; at 2/s a full run takes about 35 s.
MIN_INTERVAL = 0.5
HOST_INTERVAL = {"web.archive.org": 12.0}  # Save Page Now allows ~6 captures a minute
_last: dict[str, float] = {}

# Response headers kept as evidence of what the server said: its own clock (Date), content
# validators (ETag, Last-Modified) and CDN request ids, which the operator could look up.
EVIDENCE_HEADERS = ("date", "last-modified", "etag", "age", "cache-control", "server", "content-type",
                    "content-length", "cf-ray", "x-vercel-id", "x-amz-cf-id", "x-amz-cf-pop", "x-cache",
                    "content-location")


class FetchError(RuntimeError):
    pass


def api_key(name: str) -> tuple[str | None, str]:
    """(key, error) from the environment. A key that is not 16+ printable ASCII characters is
    never sent: http.client would echo a malformed one inside an exception message."""
    v = os.environ.get(name, "").strip()
    if not v:
        return None, ""
    if not re.fullmatch(r"[\x21-\x7e]{16,}", v):
        return None, f"{name} has an invalid format (need 16+ printable ASCII characters); not used"
    return v, ""


class _NoAuthRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect with an Authorization header: urllib would resend the key
    to whatever host (or plain-http URL) the redirect names. The 3xx surfaces as HTTPError."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if req.has_header("Authorization"):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# Process-wide opener: this is a CLI, nothing else shares the process.
urllib.request.install_opener(urllib.request.build_opener(_NoAuthRedirect))


def _evidence(resp) -> dict:
    """What the server and its TLS connection said: selected headers, the final URL after any
    redirect, the peer address and the sha256 of the server's certificate. Corroboration, not proof:
    a third party can check headers and certificate against the vendor, but they could be forged."""
    ev = {"final_url": resp.geturl() if hasattr(resp, "geturl") else ""}
    headers = {k.lower(): v for k, v in resp.headers.items()}
    for h in EVIDENCE_HEADERS:
        v = headers.get(h)
        if v:
            ev[h] = " ".join(str(v).split())[:200]
    try:
        sock = resp.fp.raw._sock  # the live connection; absent in tests and plain-http
        ev["peer_ip"] = sock.getpeername()[0]
        ev["cert_sha256"] = hashlib.sha256(sock.getpeercert(binary_form=True)).hexdigest()
    except (AttributeError, OSError, TypeError, ValueError):
        pass
    return ev


def get(url: str, headers: dict | None = None, timeout: float = 30.0,
        retries: int = 3, data: bytes | None = None, method: str | None = None) -> tuple[int, bytes, dict]:
    """Return (status, body, evidence). Raises FetchError.

    Any HTTP status is final (one request per page per run, so 429/5xx are not retried).
    Only failures with no response at all (DNS, reset, timeout) are retried."""
    hdrs = {"User-Agent": DEFAULT_UA, "Accept": "*/*"}
    if headers:
        hdrs.update(headers)
    last = None
    host = urllib.parse.urlsplit(url).hostname or ""
    for attempt in range(retries):
        wait = _last.get(host, 0.0) + HOST_INTERVAL.get(host, MIN_INTERVAL) - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last[host] = time.monotonic()
        req = urllib.request.Request(url, headers=hdrs, data=data, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                evidence = _evidence(resp)  # before reading: a fully read response drops its socket
                body = resp.read(MAX_BYTES + 1)
                if len(body) > MAX_BYTES:
                    raise FetchError(f"response over {MAX_BYTES} bytes for {url}")
                return resp.status, body, evidence
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code} for {url}"
            break
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = f"{type(e).__name__}: {e} for {url}"
        if attempt < retries - 1:
            time.sleep(2 ** attempt)
    raise FetchError(last or f"failed: {url}")

