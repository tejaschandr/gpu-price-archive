"""GetDeploying GPU rental price history: weekly min/median/max of listed prices per GPU and billing
type, across ~80 cloud providers. Licensed CC BY 4.0 (attribution: getdeploying.com), the one source
here whose terms allow keeping and sharing the data. Listed prices, not transactions. Its latest
week "may be partial and can change"; each daily look records how it changed.
"""

from __future__ import annotations

import csv
import io

from ..model import Request, Rows

NAME = "getdeploying"
URL = "https://getdeploying.com/dataset/gpu-prices/{slug}.csv"
SLUGS = ["nvidia-h100", "nvidia-h200", "nvidia-b200", "nvidia-b300", "nvidia-a100", "amd-mi300x", "nvidia-rtx-5090"]
EXPECTED_ROWS = {"getdeploying_weekly": len(SLUGS) * 100}  # ~200-530 per GPU on 2026-10-19
HEADER = ["gpu_slug", "date", "billing_type", "reservation_months", "min_price", "max_price", "median_price",
          "provider_median_price", "provider_count", "offering_count"]


def requests() -> tuple[list[Request], list[str]]:
    return [Request(s, URL.format(slug=s), "csv") for s in SLUGS], []


def parse(name: str, body: bytes, url: str) -> tuple[Rows, list[str]]:
    rows = list(csv.reader(io.StringIO(body.decode("utf-8"))))
    if not rows or rows[0] != HEADER:
        return {"getdeploying_weekly": []}, [f"{name}: unexpected CSV header {rows[0][:4] if rows else []}"]
    out = [dict(zip(HEADER, r)) for r in rows[1:] if r]
    bad = [r for r in out if r["gpu_slug"] != name]
    return ({"getdeploying_weekly": []}, [f"{name}: rows for another GPU ({bad[0]['gpu_slug']})"]) if bad \
        else ({"getdeploying_weekly": out}, [])
