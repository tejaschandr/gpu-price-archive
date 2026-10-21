"""Row validity rules and jump alerts.

invalid(table, row) is applied to every row at build time. A row that fails never enters a table
and is reported as a build problem, which fails cim.verify: tables are valid by construction.
Rules check shape and physically possible ranges, not plausibility; real moves are never rejected.

jumps() compares a run's new observations with the last known value of each series. A big move
is recorded as published but flagged for a human (the run is marked degraded).
"""

from __future__ import annotations

import re

_TS = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z")
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
_MONTH = re.compile(r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) \d{4}")
_SHA = re.compile(r"[0-9a-f]{64}")
# USD per GPU-hour. The highest real value seen is ~$8 (Silicon Data hyperscaler H100 $7.15; Ornn
# B200 $7.83). 100 leaves room for any GPU while rejecting a switch to cents (317 for $3.17).
PRICE = (0.01, 100.0)
JUMP = 0.30             # flag a series moving more than 30% from its last known value
# Jump alerts only for published indices. Kalshi medians and Vast medians of a few offers move
# more than 30% routinely; alerting on them would bury real alerts.
JUMP_SOURCES = {"ornn", "silicondata"}


def _num(v, lo, hi, empty_ok=False) -> bool:
    if v in ("", None):
        return empty_ok
    try:
        return lo <= float(v) <= hi
    except (TypeError, ValueError):
        return False


def _period(v, empty_ok=True) -> bool:
    return (v == "" and empty_ok) or bool(_DAY.fullmatch(v) or _MONTH.fullmatch(v))


def invalid(table: str, r: dict) -> str | None:
    """Why the row is invalid, or None."""
    if not _TS.fullmatch(r["observed_at_utc"]) or not _SHA.fullmatch(r["raw_sha256"]):
        return "bad observed_at_utc or raw_sha256"
    if any(isinstance(v, str) and ("\n" in v or "\r" in v) for v in r.values()):
        return "line break in a cell"
    if any(isinstance(v, str) and v[:1] in ("=", "+", "@") for v in r.values()):
        return "cell starts with a spreadsheet formula character"
    if table == "observations":
        if not _num(r["price_usd_per_gpu_hr"], *PRICE):
            return f"price {r['price_usd_per_gpu_hr']!r} out of range"
        if not _period(r["as_of"], empty_ok=r["source"] == "vast"):  # Vast listings carry no date
            return f"as_of {r['as_of']!r} is not a date or month"
    elif table == "silicondata_history":
        if not _DAY.fullmatch(r["date"]) or not _num(r["price"], *PRICE):
            return "bad date or price"
    elif table == "ornn_history":
        if not (_DAY.fullmatch(r["date"]) and _TS.fullmatch(r["timestamp"]) and r["gpu"]) or not _num(r["price"], *PRICE):
            return "bad gpu, timestamp or price"
    elif table == "kalshi_settlements":
        if not (_num(r["expiration_value"], 0, PRICE[1]) or (r["expiration_value"] == "" and r["bracket"])) \
                or not _period(r["period"]):
            return "bad expiration_value or period"
    elif table == "kalshi_quotes":
        if not all(_num(r[k], 0, 1, empty_ok=True) for k in ("yes_bid", "yes_ask", "last", "prob")):
            return "price or probability outside [0, 1]"
    elif table == "kalshi_brackets":
        if r["lo"] != "" and r["hi"] != "" and float(r["lo"]) >= float(r["hi"]):
            return "bracket lo >= hi"
    elif table == "vast_offers":
        if not _num(r["price_per_gpu_hr"], *PRICE) or not _num(r["num_gpus"], 1, 1024):
            return "bad price or GPU count"
    elif table == "getdeploying_weekly":
        if not _DAY.fullmatch(r["date"]) or not all(_num(r[k], *PRICE) for k in ("min_price", "median_price", "max_price")) \
                or not float(r["min_price"]) <= float(r["median_price"]) <= float(r["max_price"]):
            return "bad date or prices"
    elif table == "documents":
        if not _SHA.fullmatch(r["text_sha256"]):
            return "bad text_sha256"
    elif table == "announcements":
        if not (_DAY.fullmatch(r["announced"]) and _DAY.fullmatch(r["effective"])) \
                or r["restatement"] not in ("Yes", "No"):
            return "bad announcement dates or restatement flag"
    return None


def jumps(new: list[dict], previous: list[dict]) -> list[str]:
    """New observations more than JUMP away from the latest previous value of the same series."""
    last = {}
    for r in sorted(previous, key=lambda r: r["observed_at_utc"]):
        last[(r["source"], r["series_id"])] = float(r["price_usd_per_gpu_hr"])
    out = []
    for r in new:
        if r["source"] not in JUMP_SOURCES:
            continue
        before = last.get((r["source"], r["series_id"]))
        now = float(r["price_usd_per_gpu_hr"])
        if before and abs(now / before - 1) > JUMP:
            out.append(f"{r['series_id']} moved {now / before - 1:+.0%} ({before:.2f} -> {now:.2f}); recorded, check the page")
    return out
