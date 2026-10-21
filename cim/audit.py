"""Cross-source data audit: python -m cim.audit [--data-dir data] [--hours 24]

Checks verify doesn't: sources against each other and against their own raw bytes. Prints a short
report; exit 1 if any check fails. Big moves and the latest values are listed for a human, not failed.
"""

from __future__ import annotations

import argparse
import glob
import gzip
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import store

SD_HISTORY = {"SDH100RT": "H100(SDH100RT)", "SDA100RT": "A100(SDA100RT)", "SDB200RT": "B200(SDB200RT)",
              "SD-H200": "H200", "SD-B300": "B300", "SD-MI300X": "MI300X"}
HEADLINE = ("OCPI-H100", "OCPI-H200", "OCPI-B200", "OCPI-A100", "SDH100RT")


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def audit(root: Path, hours: int, now: datetime) -> tuple[list[str], list[str]]:
    """(failures, info lines)."""
    root, fail, info = Path(root), [], []
    since = (now - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M")
    ms = [m for m in store.read_manifests(root) if m["started_at_utc"] >= since]
    failed = sum(1 for m in ms for f in m["fetches"] if f.get("error"))
    info.append(f"last {hours}h: {len(ms)} runs, {sum(len(m['fetches']) for m in ms)} fetches, {failed} failed")
    # Fail only while a page's latest look is bad: a vendor serving a broken page once (a deploy in
    # progress) is recorded in coverage and listed here, but recovered on the next look.
    recent = [r for r in store.read_table(root, "coverage") if r["observed_at_utc"] >= since]
    latest = {(r["source"], r["name"]): r for r in recent}
    bad = [r for r in latest.values() if r["outcome"] != "ok"]
    if bad:  # a warning: verify --fresh alerts once a page stays unread past its limit
        info.append(f"WARN {len(bad)} pages not ok on their latest look, e.g. {bad[0]['name']} {bad[0]['outcome']} at {bad[0]['observed_at_utc']}")
    earlier = [r for r in recent if r["outcome"] != "ok" and latest[(r["source"], r["name"])]["outcome"] == "ok"]
    if earlier:
        info.append(f"{len(earlier)} earlier page looks not ok, since read ok (e.g. {earlier[0]['name']} at {earlier[0]['observed_at_utc']})")
    starts = sorted(m["started_at_utc"] for m in ms if "kalshi" in m["sources"])
    t = [datetime.fromisoformat(s.replace("Z", "+00:00")) for s in starts]
    if len(t) > 1:
        info.append(f"Kalshi largest gap {max((b - a).total_seconds() for a, b in zip(t, t[1:])) / 3600:.1f}h")

    raw = set()
    for p in glob.glob(str(root / "raw" / "*" / "kalshi*_settled__*.json.gz")):
        raw |= {m["event_ticker"] for m in json.loads(gzip.decompress(Path(p).read_bytes())).get("markets", [])}
    missing = raw - {r["event_ticker"] for r in store.read_table(root, "kalshi_settlements")}
    if missing:
        fail.append(f"settled events in raw but not in the table: {sorted(missing)[:5]}")

    obs = store.read_table(root, "observations")
    hist = {(r["series"], r["date"]): r["price"] for r in store.read_table(root, "silicondata_history")}
    mism = [(r["series_id"], r["as_of"]) for r in obs if r["source"] == "silicondata"
            and hist.get((SD_HISTORY.get(r["series_id"], ""), r["as_of"])) not in (None, r["price_usd_per_gpu_hr"])]
    if mism:
        fail.append(f"Silicon Data headline differs from its own history: {mism[:5]}")
    # The page against Kalshi's settlement source, same GPU and date.
    from .sources.ornn import DASHBOARD_NAME, GPUS
    dash = {(r["gpu"], r["date"]): r["price"] for r in store.read_table(root, "ornn_history")}
    sid_gpu = {sid: DASHBOARD_NAME.get(g, g) for g, (_, _, sid) in GPUS.items()}
    off = [(r["series_id"], r["as_of"]) for r in obs if r["source"] == "ornn"
           and dash.get((sid_gpu.get(r["series_id"]), r["as_of"])) not in (None, r["price_usd_per_gpu_hr"])]
    if off:
        fail.append(f"Ornn page differs from its settlement source (dashboard): {off[:5]}")
    future = [(r["series_id"], r["as_of"]) for r in obs if len(r["as_of"]) == 10 and r["as_of"] > r["observed_at_utc"][:10]]
    if future:
        fail.append(f"values dated after they were seen: {future[:5]}")
    crossed = [r["ticker"] for r in store.read_table(root, "kalshi_quotes")
               if _f(r["yes_bid"]) and _f(r["yes_ask"]) and _f(r["yes_bid_size"]) and _f(r["yes_ask_size"])
               and _f(r["yes_bid"]) > _f(r["yes_ask"])]
    if crossed:
        fail.append(f"crossed Kalshi books: {crossed[:5]}")
    revs = max(0, len((root / "revisions.csv").read_text().splitlines()) - 1) if (root / "revisions.csv").exists() else 0
    info.append(f"vendor revisions: {revs}")
    # Rows appear only on change, so once a table has history, any recent row is a new methodology,
    # terms text or announcement.
    docs = []
    for t, key in (("documents", "name"), ("announcements", "label")):
        rows = store.read_table(root, t)
        if any(r["observed_at_utc"] < since for r in rows):
            docs += [r[key] for r in rows if r["observed_at_utc"] >= since]
    if docs:
        info.append(f"CHECK vendor docs changed: {sorted(set(docs))}")

    for sid in HEADLINE:
        rs = sorted((r for r in obs if r["series_id"] == sid and len(r["as_of"]) == 10), key=lambda r: r["as_of"])
        if len(rs) >= 2:
            a, b = float(rs[-2]["price_usd_per_gpu_hr"]), float(rs[-1]["price_usd_per_gpu_hr"])
            info.append(f"{sid}: {b:.2f} on {rs[-1]['as_of']} ({b / a - 1:+.1%} vs {rs[-2]['as_of']})")
    div = [line.split(",") for line in (root / "divergence.csv").read_text().splitlines()[1:]] if (root / "divergence.csv").exists() else []
    paired = [d for d in div if d[7]]
    if paired:
        info.append(f"Ornn - Silicon Data: {paired[-1][7]} on {paired[-1][0]} ({len(paired)} paired dates)")
    return fail, info


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--hours", type=int, default=24)
    a = ap.parse_args(argv)
    fail, info = audit(Path(a.data_dir), a.hours, datetime.now(timezone.utc))
    print(*info, sep="\n")
    print("audit: " + ("FAILED\n  " + "\n  ".join(fail) if fail else "ok"))
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
