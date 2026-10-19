"""Silicon Data rental indices (SDH100RT etc.); planned underlying for CME/NYMEX compute futures.

Each page has an "As of <date> <value> USD / GPU-hour" headline and, in its embedded page data,
the vendor's last 30 daily points per series. H100 and A100 pages have a Neo-Cloud/Hyperscaler
toggle and server-render only the pressed tab; the others publish one market-wide reading.

A headline is accepted only if the page matches its expected layout: one headline; the toggle
present (with Neo-Cloud pressed) exactly where expected; and the headline equal to the page data's
single non-hyperscaler reading. Anything else records nothing and reports why.
"""

from __future__ import annotations

import re

from ..model import Request, Rows, obs
from ..text import DATE_RE, html_to_text, parse_date

NAME = "silicondata"
EXPECTED_ROWS = {"observations": 6, "silicondata_history": 240}  # 8 series x 30 days
BASE = "https://www.silicondata.com/products/silicon-index/{slug}"

# slug -> (sku, series_id, has_segment_toggle). Tickers are Silicon Data's where published.
PAGES = {
    "h100": ("H100", "SDH100RT", True),
    "h200": ("H200", "SD-H200", False),
    "a100": ("A100", "SDA100RT", True),
    "b200": ("B200", "SDB200RT", False),
    "b300": ("B300", "SD-B300", False),
    "mi300x": ("MI300X", "SD-MI300X", False),
}
# The vendor's series titles per page (2026-10-14). A renamed or added title would silently start
# a new history series, so any other set is reported.
TITLES = {"h100": {"H100(SDH100RT)", "H100-hyperscaler"}, "a100": {"A100(SDA100RT)", "A100-hyperscaler"},
          "h200": {"H200"}, "b200": {"B200(SDB200RT)"}, "b300": {"B300"}, "mi300x": {"MI300X"}}

_HEADLINE = re.compile(r"As of " + DATE_RE + r"\s*\$?\s*([0-9]+(?:\.[0-9]+)?)\s*USD\s*/\s*GPU-hour", re.I)
_FAQ = re.compile(r"current \w+ rental price is \$([0-9]+(?:\.[0-9]+)?) per GPU-hour", re.I)
# Quantifiers are bounded throughout: hostile input stays linear.
_TAB = re.compile(r'aria-pressed="(true|false)"[^<>]{0,300}>([^<>]{0,100})<')
_SEGMENTS = {"Neo-Cloud", "Hyperscaler"}
_READING = re.compile(r'"title":"([^"]{1,80})"[^{}]{0,2000}?"rawValue":"([0-9]+(?:\.[0-9]+)?)"')
_HISTORY = re.compile(r'"title":"([^"]{1,80})"[^{}]{0,2000}?"points":\[([^\]]{0,20000})\]')
_POINT = re.compile(r'\{"date":"(\d{4}-\d{2}-\d{2})","price":([0-9]+(?:\.[0-9]+)?)\}')


def requests() -> tuple[list[Request], list[str]]:
    return [Request(slug, BASE.format(slug=slug), "html") for slug in PAGES], []


def _page_data(body: bytes) -> str:
    return body.decode("utf-8", errors="replace").replace('\\"', '"')


def parse_page(body: bytes, toggle: bool) -> dict | None:
    """{as_of, price, faq_price, segment}, or None if the page doesn't match its expected layout."""
    text, _ = html_to_text(body)
    heads = _HEADLINE.findall(text)
    if len(heads) != 1:
        return None
    html = body.decode("utf-8", errors="replace")
    tabs = [(state, label.strip()) for state, label in _TAB.findall(html) if label.strip() in _SEGMENTS]
    if toggle:
        if (len(tabs) != 2 or {label for _, label in tabs} != _SEGMENTS
                or [label for state, label in tabs if state == "true"] != ["Neo-Cloud"]):
            return None
    elif tabs:
        return None
    date, price = heads[0]
    readings = _READING.findall(_page_data(body))
    neo = [float(v) for t, v in readings if "hyperscaler" not in t.lower()]
    if len(readings) != (2 if toggle else 1) or neo != [float(price)]:
        return None
    faq = _FAQ.search(text)
    return {"as_of": parse_date(date), "price": float(price),
            "faq_price": float(faq.group(1)) if faq else None, "segment": "neocloud" if toggle else ""}


def parse_history(body: bytes) -> list[tuple[str, str, float]]:
    """(series title as the vendor names it, date, price) for every embedded history point."""
    return [(title, d, float(p)) for title, pts in _HISTORY.findall(_page_data(body))
            for d, p in _POINT.findall(pts)]


def parse(name: str, body: bytes, url: str) -> tuple[Rows, list[str]]:
    sku, sid, toggle = PAGES[name]
    # History is labelled by the vendor's own series titles, so it is kept even if the headline is refused.
    history = parse_history(body)
    rows: Rows = {"silicondata_history": [{"slug": name, "series": t, "date": d, "price": f"{p:.4f}"}
                                          for t, d, p in history]}
    problems = []
    titles = {t for t, _, _ in history}
    if titles != TITLES[name]:
        problems.append(f"{name}: history series titles {sorted(titles)} differ from expected {sorted(TITLES[name])}")
    r = parse_page(body, toggle)
    if not r:
        want = "Neo-Cloud tab with segment toggle" if toggle else "single reading, no segment toggle"
        return rows, problems + [f"{name}: page does not match expected layout ({want}); not recorded"]
    notes = ""
    if r["faq_price"] is not None and abs(r["faq_price"] - r["price"]) > 0.005:
        notes = f"page_faq_states={r['faq_price']:.2f}"
    method = "public page headline (Neo-Cloud tab)" if toggle else "public page headline (single reading)"
    rows["observations"] = [obs(NAME, sid, sku, "", r["segment"], "", r["price"], r["as_of"], url, method, notes)]
    return rows, problems
