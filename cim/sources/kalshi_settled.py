"""Kalshi settled events (once a day): expiration_value is Ornn's realized value for each
event's period. Same API and parser as kalshi; split out so intraday runs fetch only open books."""

from __future__ import annotations

import time

from ..model import Request
from .kalshi import API, SERIES, parse  # noqa: F401 - parse handles the "<series>_settled" names

NAME = "kalshi_settled"
EXPECTED_ROWS = {"kalshi_settlements": 1}  # weekly events settle every Friday, so 40 days always has some


# Only events closed in the last WINDOW_DAYS: a series' settled list grows ~15 markets a week and
# would pass the 1000-per-request limit within a year. Anything older is already in the table
# (change log). 40 days covers a monthly event plus late settlement.
WINDOW_DAYS = 40


def requests() -> tuple[list[Request], list[str]]:
    since = (int(time.time()) - WINDOW_DAYS * 86400) // 86400 * 86400  # whole UTC day: stable URLs
    return [Request(f"{s}_settled", f"{API}?series_ticker={s}&status=settled&limit=1000&min_close_ts={since}",
                    "json", headers={"Accept": "application/json"}) for s in SERIES], []
