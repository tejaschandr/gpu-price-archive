"""SemiAnalysis GPU Spot-Contract Composite Index, from the server-rendered "Current Market Pricing"
table. The public value has been stale since Apr 2026."""

from __future__ import annotations

import re

from ..model import Request, Rows, obs
from ..text import table_with

NAME = "semianalysis"
EXPECTED_ROWS = {"observations": 1}  # B200; H100 has had no period since 2026-10-16
URL = "https://gpu-index.semianalysis.com/"

# The column header. Matches both "Spot-Contract Composite Index" and, since 2026-10-16, "Composite
# Index" (same index: the page's methodology still names it Spot-Contract, values unchanged). That
# page also added a "p25–p75 / Midpoint" filter; a range cell is refused (a cell must be one price).
COMPOSITE = "Composite Index"
_PRICE = re.compile(r"\$\s*([0-9]+\.[0-9]+)")
_PERIOD = re.compile(r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) \d{4}")
NO_VALUE = "—"  # the page's own em dash for "no value published"


def requests() -> tuple[list[Request], list[str]]:
    return [Request("index", URL, "html")], []


def parse_page(body: bytes) -> tuple[list[dict], list[str]]:
    """Read the composite column by header position; a cell must be exactly one price."""
    found = table_with(body, COMPOSITE, ["SKU", "Period"])  # the history table starts with "Period"
    if not found:
        return [], [f"no single table with exactly one '{COMPOSITE}' column"]
    header, rows, col = found
    out, problems = [], []
    for r in rows:
        if len(r) != len(header):
            problems.append(f"row {r[:1]} has {len(r)} cells, header has {len(header)}")
            continue
        sku, period, cell = r[0], r[1], r[col]
        # "—" is the page's "no value"; a price with no period (H100 since 2026-10-16) can't be dated,
        # so it isn't a point-in-time value either. Both are skipped, not reported: the vendor chose them.
        if cell == NO_VALUE or period == "":
            continue
        m = _PRICE.fullmatch(cell)
        if not m or not _PERIOD.fullmatch(period):
            problems.append(f"{sku}: composite cell {cell!r} / period {period!r} is not a single price")
            continue
        out.append({"sku": sku, "period": period, "price": float(m.group(1))})
    return out, problems


def parse(name: str, body: bytes, url: str) -> tuple[Rows, list[str]]:
    found, problems = parse_page(body)
    rows = [obs(NAME, f"SA-{r['sku']}-COMPOSITE", r["sku"], "", "composite", "", r["price"], r["period"], url,
                "public page, current composite (monthly period)") for r in found]
    return {"observations": rows}, problems
