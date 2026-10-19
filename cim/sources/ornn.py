"""Ornn Compute Price Index (OCPI): volume-weighted, winsorized mean of transacted on-demand rental
prices over a rolling one-hour window; the daily settle is the 4:00 PM ET window. Settles Kalshi's
compute markets; planned underlying for ICE OCPI futures.

Public page: data.ornn.com lists the latest settle per GPU, twice (cards, then a table).
Settlement source: Kalshi names dashboard.ornnai.com, whose public API serves the last ~3 months
of daily 4 PM ET settles per GPU (`access: public-3mo`). Hourly values, which the monthly-mean
contracts average, are not public. A daily look at the history catches revisions to past settles.
"""

from __future__ import annotations

import json
import re
import urllib.parse

from ..model import Request, Rows, obs
from ..text import DATE_RE, html_to_text, parse_date

NAME = "ornn"
EXPECTED_ROWS = {"observations": 4, "ornn_history": 5 * 60}  # per healthy run; fewer marks the run degraded
PAGE_URL = "https://data.ornn.com/"
HISTORY_URL = "https://dashboard.ornnai.com/api/gpu/{gpu}/index-history"
HISTORY_GPUS = ["H100 SXM", "H200", "B200", "A100 SXM4", "RTX 5090"]  # the dashboard's own names
_TS = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z")

# Display name on the page -> (sku, form_factor, series_id). Form factor only where the page states it.
GPUS = {
    "H100 SXM": ("H100", "SXM", "OCPI-H100"),
    "H200": ("H200", "", "OCPI-H200"),
    "B200": ("B200", "", "OCPI-B200"),
    "A100 SXM4": ("A100", "SXM", "OCPI-A100"),
}

_ROW = re.compile(r"(H100 SXM|H200|B200|A100 SXM4)\s*\$\s*([0-9]+(?:\.[0-9]+)?)\s*" + DATE_RE
                  + r"\s*OCPI-(H100|H200|B200|A100)")
_META = re.compile(r"(H100 SXM) at \$([0-9.]+) per GPU-hour, settled " + DATE_RE)
_ANY_SERIES = re.compile(r"\bOCPI-([A-Za-z0-9-]+)")


def requests() -> tuple[list[Request], list[str]]:
    return [Request("home", PAGE_URL, "html")] + [
        Request("history_" + g.split()[0].lower(), HISTORY_URL.format(gpu=urllib.parse.quote(g)), "json")
        for g in HISTORY_GPUS], []


def parse_history(body: bytes) -> tuple[list[dict], list[str]]:
    """Dashboard history: every point must carry an ISO timestamp and a number, or nothing is kept."""
    d = json.loads(body)
    data = d.get("data") if isinstance(d, dict) and d.get("success") is True else None
    if not isinstance(data, list) or not data:
        return [], ["history: not a successful response with data"]
    if not all(isinstance(p, dict) and _TS.fullmatch(str(p.get("timestamp", ""))) and
               isinstance(p.get("index_value"), (int, float)) for p in data):
        return [], ["history: unexpected point shape; nothing kept"]
    return [{"gpu": d.get("gpu_type", ""), "date": p["timestamp"][:10], "timestamp": p["timestamp"],
             "price": f"{p['index_value']:.4f}"} for p in data], []


def parse_page(body: bytes) -> list[dict]:
    """[{display, price, as_of, fallback?}]. A GPU whose two copies disagree is dropped, not guessed."""
    text, meta = html_to_text(body)
    out: dict[str, dict] = {}
    conflicted: set[str] = set()
    for m in _ROW.finditer(text):
        display, price, date, _ = m.groups()
        row = {"display": display, "price": float(price), "as_of": parse_date(date)}
        if out.setdefault(display, row) != row:
            conflicted.add(display)
    for display in conflicted:
        del out[display]
    if "H100 SXM" not in out and "H100 SXM" not in conflicted:
        m = _META.search(meta.get("description", ""))
        if m:
            out["H100 SXM"] = {"display": m.group(1), "price": float(m.group(2)),
                               "as_of": parse_date(m.group(3)), "fallback": True}
    return list(out.values())


def parse(name: str, body: bytes, url: str) -> tuple[Rows, list[str]]:
    if name.startswith("history_"):
        hist, problems = parse_history(body)
        return {"ornn_history": hist}, [f"{name}: {p}" for p in problems]
    rows, problems = [], []
    found = parse_page(body)
    for r in found:
        sku, ff, sid = GPUS[r["display"]]
        method = "public page, latest daily settle"
        if r.get("fallback"):
            method = "public page meta description (card not found)"
            problems.append(f"{sid} read from meta description fallback; check the page layout")
        rows.append(obs(NAME, sid, sku, ff, "transacted", "settle", r["price"], r["as_of"], url, method))
    missing = sorted(set(GPUS) - {r["display"] for r in found})
    if missing:
        problems.append(f"not parsed (absent or conflicting on page): {', '.join(missing)}")
    known = {sid for _, _, sid in GPUS.values()}
    unknown = sorted({f"OCPI-{m}" for m in _ANY_SERIES.findall(html_to_text(body)[0])} - known)
    if unknown:
        problems.append(f"unrecognized series on page, not parsed: {', '.join(unknown)}")
    return {"observations": rows}, problems

