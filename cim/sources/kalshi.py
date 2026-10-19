"""Kalshi compute markets, all settling on Ornn: "Will the <GPU> price be above $K?" ladders.

Each strike's YES bid and ask bound the market-implied P(price > K). Across a ladder the sized
quotes bracket the implied median: above the highest strike bid over 0.5, at or below the lowest
strike offered at 0.5 or less. A point value (interpolated from mids) is published only when that
bracket is at most MAX_BRACKET wide and the point lies inside it.

Settled events carry expiration_value: Ornn's realized value for that event's period.
Public market-data API, no key.
"""

from __future__ import annotations

import json
import re

from ..model import Request, Rows

NAME = "kalshi"
# Point medians are rare, but the open books never all go empty: zero quotes means the API or the
# tickers changed, not that the market vanished.
EXPECTED_ROWS = {"kalshi_quotes": 1}
API = "https://api.elections.kalshi.com/trade-api/v2/markets"

# Compute series (GET /series, 2026-10-13). Suffix -> (term, description).
_TERMS = {
    "W": ("1w", "weekly price"), "WS": ("1w", "weekly price (older series)"),
    "MS": ("monthly-avg", "calendar-month mean of hourly values"), "MON": ("month-end", "month-end price"),
    "Q": ("quarterly", "quarterly directional price"), "MAX": ("yearly", "yearly directional price"),
}
SKUS = ("H100", "H200", "B200", "A100", "RTX5090")
SERIES = {f"KX{sku}{suf}": (sku, term, f"{sku} {desc}") for sku in SKUS for suf, (term, desc) in _TERMS.items()}
SERIES["KXCMECOMPUTE"] = ("", "event", "Will the CME offer compute futures before 2027?")  # yes/no market

MAX_SPREAD = 0.25   # widest two-sided book whose mid is used; empty books quote 0.00/1.00 (mid 0.5 by construction)
MAX_BRACKET = 0.25  # widest bracket with a published point (one strike step on 2026-10-13)
_TICKER = re.compile(r"[A-Z0-9.-]{1,64}")
_ISO_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
_PERIOD = re.compile(r"-(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d{2})?$")
_MON = "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split()


def period(event_ticker: str) -> str:
    """The period an event covers, from its ticker: -26OCT16 -> 2026-10-16, -26NOV -> Nov 2026.
    Not close_time: events close at midnight ET, which is the next day in UTC."""
    m = _PERIOD.search(event_ticker)
    if not m:
        return ""
    y, mon = 2000 + int(m[1]), _MON.index(m[2]) + 1
    return f"{y}-{mon:02d}-{int(m[3]):02d}" if m[3] else f"{m[2].title()} {y}"


def requests() -> tuple[list[Request], list[str]]:
    """Open books only; settled events are the kalshi_settled source."""
    return [Request(s, f"{API}?series_ticker={s}&status=open&limit=1000", "json",
                    headers={"Accept": "application/json"}) for s in SERIES], []


def _f(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _day(x) -> str:
    return str(x)[:10] if _ISO_DAY.match(str(x or "")) else ""


def quotes(m: dict) -> tuple[float | None, float | None]:
    """(bid, ask). A side with size 0, a 0.00 bid or a 1.00 ask is a placeholder, so None."""
    bid, ask = _f(m.get("yes_bid_dollars")), _f(m.get("yes_ask_dollars"))
    if _f(m.get("yes_bid_size_fp")) == 0 or (bid is not None and bid <= 0):
        bid = None
    if _f(m.get("yes_ask_size_fp")) == 0 or (ask is not None and (ask <= 0 or ask >= 1)):
        ask = None
    return bid, ask


def market_prob(m: dict) -> tuple[float | None, float | None]:
    """(probability, spread): mid of a two-sided book no wider than MAX_SPREAD. Never a last trade."""
    bid, ask = quotes(m)
    if bid is not None and ask is not None and ask >= bid:
        spread = ask - bid
        return ((bid + ask) / 2 if spread <= MAX_SPREAD + 1e-9 else None), spread
    return None, None


def bracket(markets: list[dict]) -> tuple[float | None, float | None]:
    """(lo, hi] bounds on the implied median; None for an unbounded side; both None if quotes cross."""
    lo = hi = None
    for m in markets:
        k = _f(m.get("floor_strike"))
        bid, ask = quotes(m)
        if k is None:
            continue
        if bid is not None and bid > 0.5 and (lo is None or k > lo):
            lo = k
        if ask is not None and ask <= 0.5 and (hi is None or k < hi):
            hi = k
    if lo is not None and hi is not None and lo >= hi:
        return None, None
    return lo, hi


def monotone_survival(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Pool-adjacent-violators: force P(price > K) to be non-increasing in K."""
    pts = sorted(points)
    strikes = [k for k, _ in pts]
    blocks: list[list[float]] = []  # [sum_p, count, last_index]
    for i, (_, p) in enumerate(pts):
        blocks.append([p, 1, i])
        while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] < blocks[-1][0] / blocks[-1][1]:
            s, c, idx = blocks.pop()
            blocks[-1][0] += s
            blocks[-1][1] += c
            blocks[-1][2] = idx
    out, start = [], 0
    for s, c, end in blocks:
        out += [(strikes[j], s / c) for j in range(start, end + 1)]
        start = end + 1
    return out


def implied_median(points: list[tuple[float, float]]) -> float | None:
    """Linear interpolation where the survival curve crosses 0.5; None if it never does."""
    if len(points) < 2:
        return None
    curve = monotone_survival(points)
    for (k0, p0), (k1, p1) in zip(curve, curve[1:]):
        if p0 >= 0.5 >= p1 and p0 != p1:
            return k0 + (p0 - 0.5) * (k1 - k0) / (p0 - p1)
    return None


def parse_markets(markets: list[dict]) -> dict[str, dict]:
    """Strike ladders by event: {event_ticker: {points, close_time, markets}}."""
    events: dict[str, dict] = {}
    for m in markets:
        ev = str(m.get("event_ticker", ""))
        if m.get("strike_type") not in ("greater", "greater_or_equal") or not _TICKER.fullmatch(ev):
            continue
        e = events.setdefault(ev, {"points": [], "close_time": m.get("close_time", ""), "markets": []})
        e["markets"].append(m)
        k, (p, _) = _f(m.get("floor_strike")), market_prob(m)
        if k is not None and p is not None:
            e["points"].append((k, p))
    return events


def _markets(body: bytes) -> tuple[list[dict], list[str]]:
    d = json.loads(body)
    markets = d.get("markets")
    if not isinstance(markets, list):
        raise ValueError("response has no markets list")
    # Only the first page is fetched; a cursor means markets were left out.
    problems = ["more than one page of markets; only the first was fetched"] if d.get("cursor") else []
    return [m for m in markets if isinstance(m, dict)], problems


def _dollars(v) -> float | None:
    """expiration_value as a number: "2.83" or "$1.21". "Yes"/"No" (seen on a few events) is None."""
    return _f(str(v).strip().lstrip("$")) if v not in (None, "") else None


def settlements(series: str, markets: list[dict]) -> tuple[list[dict], list[str]]:
    """One row per settled event. expiration_value is Ornn's realized value; when Kalshi gives only
    Yes/No per strike, the value is bracketed by the strike results instead: (highest yes, lowest no]."""
    events: dict[str, list[dict]] = {}
    for m in markets:
        if _TICKER.fullmatch(str(m.get("event_ticker", ""))):
            events.setdefault(m["event_ticker"], []).append(m)
    rows, problems = [], []
    for ev, ms in sorted(events.items()):
        values = {round(v, 6) for v in (_dollars(m.get("expiration_value")) for m in ms) if v is not None}
        yes = [_f(m.get("floor_strike")) for m in ms if m.get("result") == "yes"]
        no = [_f(m.get("floor_strike")) for m in ms if m.get("result") == "no"]
        value, rng = "", ""
        if len(values) == 1:
            value = f"{values.pop():.4f}"
        elif not values and None not in yes + no and (yes or no):
            rng = f"({max(yes):.2f}, {min(no):.2f}]" if yes and no else f"({max(yes):.2f}, )" if yes else f"( , {min(no):.2f}]"
        else:
            problems.append(f"{ev}: settlement value unreadable ({sorted(str(m.get('expiration_value')) for m in ms)[:3]})")
            continue
        ts = next((str(m["settlement_ts"]) for m in ms if _day(m.get("settlement_ts"))), "")
        rows.append({"series": series, "event_ticker": ev, "period": period(ev), "expiration_value": value,
                     "bracket": rng, "settlement_ts": ts})
    return rows, problems


def parse(name: str, body: bytes, url: str) -> tuple[Rows, list[str]]:
    if name.endswith("_settled"):
        markets, problems = _markets(body)
        rows, more = settlements(name[: -len("_settled")], markets)
        return {"kalshi_settlements": rows}, problems + more
    markets, problems = _markets(body)
    quotes_rows = []
    for m in markets:
        if not _TICKER.fullmatch(str(m.get("ticker", ""))):
            continue
        p, _ = market_prob(m)
        quotes_rows.append({
            "series": name, "event_ticker": m.get("event_ticker"), "ticker": m.get("ticker"),
            "strike": m.get("floor_strike"), "yes_bid": m.get("yes_bid_dollars"), "yes_ask": m.get("yes_ask_dollars"),
            "yes_bid_size": m.get("yes_bid_size_fp"), "yes_ask_size": m.get("yes_ask_size_fp"),
            "last": m.get("last_price_dollars"), "prob": "" if p is None else f"{p:.4f}",
            "volume": m.get("volume_fp"), "open_interest": m.get("open_interest_fp"), "close_time": m.get("close_time"),
        })
    brackets = []
    for ev, e in parse_markets(markets).items():
        lo, hi = bracket(e["markets"])
        med = implied_median(e["points"])
        ok = (lo is not None and hi is not None and hi - lo <= MAX_BRACKET + 1e-9
              and med is not None and lo < med <= hi)
        brackets.append({"series": name, "event_ticker": ev, "period": period(ev),
                         "close_time": e["close_time"] if _ISO_DAY.match(str(e["close_time"] or "")) else "",
                         "lo": "" if lo is None else lo,
                         "hi": "" if hi is None else hi, "point": f"{med:.2f}" if ok else ""})
    # The implied median is computed here, not published: it lives in kalshi_brackets (where its
    # disappearance is recorded too), never in observations, which holds vendor-published values only.
    return {"kalshi_quotes": quotes_rows, "kalshi_brackets": brackets}, problems
