"""Shared test helpers: synthetic source responses, a fake network, and an offline run."""

import contextlib
import io
import itertools
import json
import urllib.error
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from urllib.parse import quote, urlsplit

from cim import run
from cim.sources import docs, getdeploying, kalshi, kalshi_settled, ornn, semianalysis, silicondata, vast

LIVE = Path(__file__).parent / "fixtures"  # synthetic pages in each source's layout; no vendor bytes
DOCS_MD = LIVE / "docs__silicondata_announcements.md"
GD_H100 = LIVE / "getdeploying__nvidia-h100.csv"  # real file, CC BY 4.0, GetDeploying (getdeploying.com)
DOC_HTML = b"<html><body><p>" + b"These terms apply. " * 40 + b"</p></body></html>"  # synthetic: no vendor bytes
EMPTY_MARKETS = b'{"cursor": "", "markets": []}'


def ornn_history(gpu: str, value: float, days: int = 61, end: str = "2026-10-13") -> bytes:
    """Synthetic dashboard response in the live format, constant at the page's 2026-10-13 value."""
    t = datetime.fromisoformat(end + "T20:00:00+00:00")
    pts = [{"timestamp": (t - timedelta(days=i)).strftime("%Y-%m-%dT%H:%M:%S.000Z"), "index_value": value}
           for i in reversed(range(days))]
    return json.dumps({"success": True, "gpu_type": gpu, "access": "public-3mo", "data": pts}).encode()


ORNN_PAGE = {"H100 SXM": 3.17, "H200": 5.51, "B200": 7.83, "A100 SXM4": 0.96, "RTX 5090": 0.40, "RTX PRO 6000 WS": 1.40}


def live(name: str) -> bytes:
    return (LIVE / name).read_bytes()


def live_bodies() -> dict[str, bytes]:
    """URL -> body for every request a full run makes. Kalshi series not captured live are empty."""
    b = {ornn.PAGE_URL: live("ornn__home.html"), semianalysis.URL: live("semianalysis__index.html"),
         vast.URL: b'{"offers": []}'}
    for slug in silicondata.PAGES:
        b[silicondata.BASE.format(slug=slug)] = live(f"silicondata__{slug}.html")
    for r in kalshi.requests()[0] + kalshi_settled.requests()[0]:
        f = LIVE / f"kalshi__{r.name}.json"
        b[r.url] = f.read_bytes() if f.exists() else EMPTY_MARKETS
    for g in ornn.HISTORY_GPUS.values():
        b[ornn.HISTORY_URL.format(gpu=quote(g))] = ornn_history(g, ORNN_PAGE[g])
    for slug in getdeploying.SLUGS:
        b[getdeploying.URL.format(slug=slug)] = GD_H100.read_bytes().replace(b"nvidia-h100,", slug.encode() + b",")
    for n, u in docs.PAGES.items():
        b[u] = DOCS_MD.read_bytes() if u.endswith(".md") else DOC_HTML
    return b


HOSTS = {"data.ornn.com": "ornn", "dashboard.ornnai.com": "ornn", "www.silicondata.com": "silicondata", "gpu-index.semianalysis.com": "semianalysis",
         "api.elections.kalshi.com": "kalshi", "console.vast.ai": "vast",
         "getdeploying.com": "getdeploying"}


class FakeResp:
    def __init__(self, body: bytes):
        self.status, self._body, self.headers = 200, body, {"Content-Type": "text/html"}

    def read(self, n=-1):
        return self._body if n < 0 else self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeNet:
    """Stands in for urlopen. fail: source -> HTTP status to raise. bodies: URL overrides."""

    def __init__(self, bodies=None, fail=None):
        self.bodies = {**live_bodies(), **(bodies or {})}
        self.fail = fail or {}
        self.calls: Counter = Counter()
        self.requests = []

    def __call__(self, req, timeout=None):
        url = req.full_url
        src = "docs" if url in docs.PAGES.values() else HOSTS[urlsplit(url).hostname]
        self.calls[src] += 1
        self.requests.append(req)
        if src in self.fail:
            raise urllib.error.HTTPError(url, self.fail[src], "fail", {}, io.BytesIO(b""))
        return FakeResp(self.bodies[url])


def clock(start: str):
    """A utcnow() stand-in: each call returns the next second after start."""
    t0 = datetime.fromisoformat(start.replace("Z", "+00:00"))
    ticks = itertools.count()
    return lambda: (t0 + timedelta(seconds=next(ticks))).isoformat().replace("+00:00", "Z")


def run_offline(root, net=None, start="2026-10-13T21:37:00Z", argv=(), sleeps=None) -> int:
    with mock.patch("urllib.request.urlopen", net or FakeNet()), mock.patch("cim.run.utcnow", clock(start)), \
            mock.patch("cim.http.MIN_INTERVAL", 0), mock.patch("cim.run.code_commit", lambda: "test"), \
            mock.patch("cim.http.time.sleep", sleeps.append if sleeps is not None else (lambda s: None)), \
            contextlib.redirect_stderr(io.StringIO()):
        return run.main(["--data-dir", str(root), *argv])
