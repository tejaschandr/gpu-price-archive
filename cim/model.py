"""Table schemas and the request type sources declare.

Every table row carries observed_at_utc (when that snapshot was first fetched) and raw_sha256
(the bytes it was parsed from). Keyed tables are change logs: for each entity (e.g. a series and
date, or a Kalshi ticker) a row is written only when its value differs from the entity's previous
row. So the value of an entity at time T is its latest row with observed_at_utc <= T, reruns never
duplicate, and a revision (including a change back to an earlier value) is always a new row.
"""

from __future__ import annotations

from typing import Callable, NamedTuple


class Request(NamedTuple):
    name: str                      # stable per source; parse() dispatches on it
    url: str
    ext: str                       # html | json
    method: str | None = None
    data: bytes | None = None
    headers: dict | None = None    # never stored in the manifest (may hold a key)
    transform: Callable[[bytes], bytes] | None = None  # e.g. redaction before anything is saved


# table -> (columns, entity fields, value fields). Entity None: every row of every distinct snapshot.
TABLES: dict[str, tuple[list[str], tuple[str, ...] | None, tuple[str, ...]]] = {
    "observations": (
        ["observed_at_utc", "source", "series_id", "sku", "form_factor", "segment", "term",
         "price_usd_per_gpu_hr", "as_of", "source_url", "method", "notes", "raw_sha256"],
        ("source", "series_id", "as_of"), ("price_usd_per_gpu_hr", "method", "notes"),
    ),
    "silicondata_history": (
        ["observed_at_utc", "slug", "series", "date", "price", "raw_sha256"],
        ("slug", "series", "date"), ("price",),
    ),
    "ornn_history": (
        ["observed_at_utc", "gpu", "date", "timestamp", "price", "raw_sha256"],
        ("gpu", "timestamp"), ("price",),
    ),
    "kalshi_settlements": (
        ["observed_at_utc", "series", "event_ticker", "period", "expiration_value", "bracket", "settlement_ts",
         "raw_sha256"],
        ("event_ticker",), ("expiration_value", "bracket"),
    ),
    "kalshi_quotes": (
        ["observed_at_utc", "series", "event_ticker", "ticker", "strike", "yes_bid", "yes_ask",
         "yes_bid_size", "yes_ask_size", "last", "prob", "volume", "open_interest", "close_time", "raw_sha256"],
        ("ticker",), ("yes_bid", "yes_ask", "yes_bid_size", "yes_ask_size", "last", "volume", "open_interest"),
    ),
    "kalshi_brackets": (
        ["observed_at_utc", "series", "event_ticker", "period", "close_time", "lo", "hi", "point", "raw_sha256"],
        ("event_ticker",), ("lo", "hi", "point"),
    ),
    "vast_offers": (
        ["observed_at_utc", "gpu_name", "form_factor", "num_gpus", "price_per_gpu_hr", "dph_total",
         "gpu_max_power_w", "pci_gen", "pcie_bw_gbs", "bw_nvlink_gbs", "gpu_mem_bw_gbs", "reliability",
         "geolocation", "verification", "datacenter_hosted", "raw_sha256"],
        None, (),
    ),
    "getdeploying_weekly": (
        ["observed_at_utc", "gpu_slug", "date", "billing_type", "reservation_months", "min_price", "max_price",
         "median_price", "provider_median_price", "provider_count", "offering_count", "raw_sha256"],
        ("gpu_slug", "date", "billing_type", "reservation_months"),
        ("min_price", "max_price", "median_price", "provider_median_price", "provider_count", "offering_count"),
    ),
    "documents": (
        ["observed_at_utc", "name", "url", "chars", "text_sha256", "raw_sha256"],
        ("name",), ("text_sha256",),
    ),
    "announcements": (
        ["observed_at_utc", "label", "description", "announced", "effective", "type", "restatement",
         "body_sha256", "raw_sha256"],
        # description only as first seen: the page dropped it on 2026-10-20 (body_sha256 tracks edits)
        ("label",), ("announced", "effective", "type", "restatement", "body_sha256"),
    ),
}

# One row per fetch, every outcome, rebuilt from manifests. The keyed tables record changes only;
# this records every look. outcome: ok | problem (parsed, but the parser reported something, e.g. a
# refused layout) | parse_failed | failed (no response). changed: whether the bytes differ from the
# page's previous look; an unchanged look repeats the outcome of the bytes it re-confirmed.
COVERAGE = ["observed_at_utc", "source", "name", "outcome", "changed", "http_status", "rows", "problems",
            "error", "raw_sha256"]

Rows = dict[str, list[dict]]   # table -> rows, without observed_at_utc / raw_sha256 (build adds them)


def obs(source: str, series_id: str, sku: str, form_factor: str, segment: str, term: str,
        price: float, as_of: str, url: str, method: str, notes: str = "") -> dict:
    return {"source": source, "series_id": series_id, "sku": sku, "form_factor": form_factor,
            "segment": segment, "term": term, "price_usd_per_gpu_hr": f"{price:.4f}", "as_of": as_of,
            "source_url": url, "method": method, "notes": notes}
