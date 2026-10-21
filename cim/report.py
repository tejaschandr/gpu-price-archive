"""Reports derived from the tables, rebuilt every run:

- divergence.csv: per date, Ornn's H100 settle and Silicon Data's H100 (Neo-Cloud) value for that same
  date, and the signed gap. Values come from the vendor's page when it was seen; earlier dates are
  filled from Ornn's settlement-source history, Kalshi's weekly settlements (Ornn's 4 PM ET value on
  the event date) and Silicon Data's own 30-day history. Each value says where it came from and when
  it was first seen.
- revisions.csv: every vendor-published value that changed after it was first seen.
- LATEST.md: the latest values, for reading.
"""

from __future__ import annotations

import csv
import re
import unicodedata
from datetime import date
from pathlib import Path

from . import store
from .sources import vast

INDEX = {"ornn": "OCPI-H100", "silicondata": "SDH100RT"}
# Max age, in days past the end of the value's own period, for LATEST.md's "current" status.
MAX_AGE_DAYS = {"ornn": 3, "silicondata": 3}  # 3 days covers a weekend
FILES = ("divergence.csv", "revisions.csv", "LATEST.md")


def status(src: str, row: dict, day: str) -> str:
    """'' if current, else 'stale' or 'unknown_as_of'."""
    try:
        published = date.fromisoformat(row["as_of"])
    except ValueError:
        return "unknown_as_of"
    return "stale" if (date.fromisoformat(day) - published).days > MAX_AGE_DAYS[src] else ""


def md(text) -> str:
    """Escape a scraped value for a markdown table cell: no extra rows, links or HTML."""
    text = " ".join(str(text).split())
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")[:80]
    return re.sub(r"([\\`*_\[\]()<>!|#])", r"\\\1", text)


def _first(rows: list[dict], key, value: str) -> dict[str, tuple[float, str]]:
    """{date: (value, first_seen)} keeping each date's first-seen row (point in time)."""
    out: dict[str, tuple[float, str]] = {}
    for r in sorted(rows, key=lambda r: r["observed_at_utc"]):
        d = key(r)
        if d and d not in out:
            out[d] = (float(r[value]), r["observed_at_utc"])
    return out


def divergence(root: Path) -> list[dict]:
    obs = store.read_table(root, "observations")
    ornn = {d: v + ("page",) for d, v in _first([r for r in obs if r["series_id"] == "OCPI-H100"],
                                                 lambda r: r["as_of"], "price_usd_per_gpu_hr").items()}
    for d, v in _first([r for r in store.read_table(root, "ornn_history") if r["gpu"] == "H100 SXM"],
                       lambda r: r["date"], "price").items():
        ornn.setdefault(d, v + ("settlement source history",))
    for d, v in _first([r for r in store.read_table(root, "kalshi_settlements")
                        if r["series"] in ("KXH100W", "KXH100WS") and r["expiration_value"]],
                       lambda r: r["period"], "expiration_value").items():
        ornn.setdefault(d, v + ("kalshi weekly settlement",))
    sd = {d: v + ("page",) for d, v in _first([r for r in obs if r["series_id"] == "SDH100RT"],
                                              lambda r: r["as_of"], "price_usd_per_gpu_hr").items()}
    for d, v in _first([r for r in store.read_table(root, "silicondata_history")
                        if r["slug"] == "h100" and r["series"] == "H100(SDH100RT)"],
                       lambda r: r["date"], "price").items():
        sd.setdefault(d, v + ("page history",))
    revised = {(r["series"], r["date"]) for r in revisions(root)}
    out = []
    for d in sorted(set(ornn) | set(sd)):
        o, s = ornn.get(d), sd.get(d)
        out.append({"date": d,
                    "ornn_usd": f"{o[0]:.4f}" if o else "", "ornn_from": o[2] if o else "",
                    "ornn_first_seen_utc": o[1] if o else "",
                    "silicondata_usd": f"{s[0]:.4f}" if s else "", "silicondata_from": s[2] if s else "",
                    "silicondata_first_seen_utc": s[1] if s else "",
                    "ornn_minus_silicondata": f"{o[0] - s[0]:+.4f}" if o and s else "",
                    "revised_later": "yes" if {("OCPI-H100", d), ("SDH100RT", d), ("H100(SDH100RT)", d)} & revised
                    or any(s == "H100 SXM" and t[:10] == d for s, t in revised) else ""})
    return out


def revisions(root: Path) -> list[dict]:
    """Vendor-published values seen with more than one value for the same series and date."""
    groups: dict[tuple, list[tuple[str, dict]]] = {}
    for r in store.read_table(root, "observations"):
        groups.setdefault(("observations", r["source"], r["series_id"], r["as_of"]), []).append(
            (r["price_usd_per_gpu_hr"], r))
    for r in store.read_table(root, "silicondata_history"):
        groups.setdefault(("silicondata_history", "silicondata", r["series"], r["date"], r["slug"]), []).append(
            (r["price"], r))
    for r in store.read_table(root, "ornn_history"):
        groups.setdefault(("ornn_history", "ornn", r["gpu"], r["timestamp"]), []).append((r["price"], r))
    for r in store.read_table(root, "kalshi_settlements"):
        groups.setdefault(("kalshi_settlements", "kalshi", r["event_ticker"], r["period"]), []).append(
            (r["expiration_value"] or r["bracket"], r))
    out = []
    for k, rs in sorted(groups.items()):
        if len({v for v, _ in rs}) > 1:
            out += [{"table": k[0], "source": k[1], "series": k[2], "date": k[3], "value": v,
                     "first_seen_utc": r["observed_at_utc"], "raw_sha256": r["raw_sha256"]} for v, r in rs]
    return out


def _write(path: Path, cols: list[str], rows: list[dict]) -> None:
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def build(root: str | Path, out: str | Path | None = None) -> None:
    """Write the reports to out (default: root), from the tables under root."""
    root, out = Path(root), Path(out or root)
    div = divergence(root)
    _write(out / "divergence.csv", ["date", "ornn_usd", "ornn_from", "ornn_first_seen_utc", "silicondata_usd",
                                    "silicondata_from", "silicondata_first_seen_utc", "ornn_minus_silicondata",
                                    "revised_later"], div)
    _write(out / "revisions.csv", ["table", "source", "series", "date", "value", "first_seen_utc", "raw_sha256"],
           revisions(root))

    obs = store.read_table(root, "observations")
    if not obs:
        (out / "LATEST.md").write_text("# Latest H100 readings\n\nNo observations yet.\n")
        return
    day = max(r["observed_at_utc"] for r in obs)[:10]
    lines = ["# Latest H100 readings", "", f"As known at {max(r['observed_at_utc'] for r in obs)} (UTC). "
             "`as_of` is the source's own date.", "",
             "| Source | Series | USD/GPU-hr | as_of | Status | Notes |", "|---|---|---|---|---|---|"]
    for src, sid in INDEX.items():
        rs = sorted((r for r in obs if r["series_id"] == sid), key=lambda r: r["observed_at_utc"])
        if rs:
            r = rs[-1]
            lines.append(f"| {src} | {sid} | {float(r['price_usd_per_gpu_hr']):.2f} | {md(r['as_of'])} "
                         f"| {status(src, r, day) or 'current'} | {md(r['notes'])} |")
    offers = store.read_table(root, "vast_offers")
    if offers:
        t = max(r["observed_at_utc"] for r in offers)
        for ff, (n, p25, p50, p75) in vast.medians([r for r in offers if r["observed_at_utc"] == t]).items():
            lines.append(f"| vast | H100 {ff} median listed | {p50:.2f} | {t[:16]}Z | context "
                         f"| {n} machines, p25 {p25:.2f}, p75 {p75:.2f} |")
    paired = [r for r in div if r["ornn_minus_silicondata"]]
    if paired:
        g = paired[-1]
        gap = float(g["ornn_minus_silicondata"])
        lines += ["", f"Ornn − Silicon Data: **{'+' if gap >= 0 else '−'}${abs(gap):.2f}/GPU-hr** on {g['date']} "
                  f"(latest date both published); {len(paired)} paired dates in `divergence.csv`."]
    (out / "LATEST.md").write_text("\n".join(lines) + "\n")
