"""Rebuild every table from raw snapshots and manifests: python -m cim.build [--data-dir data]

Deterministic: the same raw files and manifests always give byte-identical tables. Fetches are
parsed in time order, skipping a fetch identical to the previous one of the same page, and each
row is stamped with its fetch time. Keyed tables
get a row only when an entity's value differs from its previous row (see cim.model), so reruns
never duplicate and every revision is a new row with a later observed_at_utc, never an edit.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import quality, store
from .model import COVERAGE, TABLES
from .sources import ALL

# Full rebuild every run: about a second per month of data. Rebuild only the current month once
# it takes minutes.


def all_fetches(root: Path) -> list[tuple[dict, bool]]:
    """Every fetch in time order, with whether its bytes changed since the previous fetch of the
    same page. An unchanged fetch isn't parsed again (its rows would all be repeats). Not a global
    "seen before" set: a page that changes and then reverts must be parsed again."""
    last: dict[tuple, str] = {}
    out = []
    for m in store.read_manifests(root):
        for f in sorted(m["fetches"], key=lambda f: f["fetched_at_utc"]):
            page = (f["source"], f["name"])
            changed = bool(f.get("sha256")) and last.get(page) != f["sha256"]
            if f.get("sha256"):
                last[page] = f["sha256"]
            out.append((f, changed))
    return out


def derive(root: Path) -> tuple[dict[str, list[dict]], list[dict], list[str]]:
    tables: dict[str, list[dict]] = {t: [] for t in TABLES}
    last: dict[str, dict] = {t: {} for t in TABLES}
    coverage: list[dict] = []
    parsed: dict[tuple, dict] = {}  # page -> outcome/rows/problems of its last parsed snapshot
    problems = []
    for f, changed in all_fetches(root):
        cov = {"observed_at_utc": f["fetched_at_utc"], "source": f["source"], "name": f["name"],
               "changed": "", "http_status": f.get("status", ""), "rows": "", "problems": "", "error": "",
               "raw_sha256": f.get("sha256", "")}
        coverage.append(cov)
        page = (f["source"], f["name"])
        if not f.get("sha256"):
            cov.update(outcome="failed", error=" ".join(str(f.get("error", "")).split())[:300])
            continue
        cov["changed"] = "yes" if changed else "no"
        if not changed:
            cov.update(parsed[page])  # same bytes: same result as when they were parsed
            continue
        body = store.read_raw(root, f["path"], f["sha256"])  # a hash mismatch is fatal, by design
        try:
            rows, parse_problems = ALL[f["source"]].parse(f["name"], body, f["url"])
        except Exception as e:  # noqa: BLE001 - one bad snapshot never blocks the rest
            problems.append(f"{f['path']}: {type(e).__name__}: {e}")
            cov.update(outcome="parse_failed", error=f"{type(e).__name__}: {e}"[:300])
            parsed[page] = {k: cov[k] for k in ("outcome", "rows", "problems", "error")}
            continue
        cov.update(outcome="problem" if parse_problems else "ok", rows=sum(len(rs) for rs in rows.values()),
                   problems=" ".join("; ".join(parse_problems).split())[:300])
        parsed[page] = {k: cov[k] for k in ("outcome", "rows", "problems", "error")}
        for table, rs in rows.items():
            _, entity, value = TABLES[table]
            for r in rs:
                row = {"observed_at_utc": f["fetched_at_utc"], **r, "raw_sha256": f["sha256"]}
                why = quality.invalid(table, row)
                if why:
                    problems.append(f"{f['path']}: {table} row rejected: {why}: {row}")
                    continue
                if entity:
                    k, v = tuple(str(row[c]) for c in entity), tuple(str(row[c]) for c in value)
                    if last[table].get(k) == v:
                        continue
                    last[table][k] = v
                tables[table].append(row)
    return tables, coverage, problems


def build(root: Path, out: Path | None = None) -> list[str]:
    """Write tables and coverage under (out or root)/tables. Returns per-snapshot failures."""
    tables, coverage, problems = derive(Path(root))
    for t, rows in tables.items():
        store.write_table(Path(out or root), t, TABLES[t][0], rows)
    store.write_table(Path(out or root), "coverage", COVERAGE, coverage)
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data")
    problems = build(Path(ap.parse_args(argv).data_dir))
    for p in problems:
        print(p, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
