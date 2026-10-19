"""Prove the archive is intact: python -m cim.verify [--data-dir data]

1. Every raw file decompresses and its content hash starts with the hash in its name.
2. Every manifest fetch with a sha256 points at a file with exactly that content.
3. Every raw file is referenced by some manifest (no unexplained bytes).
4. Every table row's raw_sha256 is a fetched snapshot.
5. Rebuilding the tables from raw reproduces the committed tables byte for byte, with no
   snapshot failing to parse and no row failing the validity rules (cim.quality).
6. The manifests form an unbroken hash chain (each names the previous one's sha256; the first
   chained one lists every earlier manifest), and every anchor still matches its manifest.
   Every proof file is an OpenTimestamps proof of exactly the anchor next to it.
7. With --fresh: every scheduled source succeeded recently (FRESH_HOURS), and no proof is still
   pending PROOF_HOURS after its anchor (Bitcoin attestation normally takes a few hours). The daily and the
   3-hourly runs each check the other, so a schedule that silently stops fails the next run.
Exit 1 with the list of failures if any check fails.
"""

from __future__ import annotations

import argparse
import filecmp
import gzip
import hashlib
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import anchor, build, report, store
from .model import TABLES

DERIVED = [*TABLES, "coverage"]


# Max hours since a source's last successful fetch. GitHub schedules are best-effort: on
# 2026-10-14 the 06:07 and 09:07 runs were dropped or ran hours late. Limits allow that slack:
# a daily run plus 6 h, and three missed 3-hourly Kalshi runs.
FRESH_HOURS = {"ornn": 30, "silicondata": 30, "semianalysis": 30, "kalshi_settled": 30, "kalshi": 10, "vast": 30,
               "docs": 30, "getdeploying": 30}


def chain(root: Path) -> list[str]:
    """Each manifest names its predecessor's hash; anchors still match the manifests they name."""
    issues, prev, started = [], "", False
    shas = {}
    for p in sorted((Path(root) / "runs").glob("*.json")):
        m = json.loads(p.read_text())
        if "previous_manifest_sha256" in m:
            if m["previous_manifest_sha256"] != prev:
                issues.append(f"runs/{p.name}: previous_manifest_sha256 does not match the run before it (chain broken)")
            if not started and "genesis_manifests" in m and set(m["genesis_manifests"]) != set(shas):
                issues.append(f"runs/{p.name}: genesis list doesn't match the manifests before the chain "
                              f"(unlisted: {sorted(set(shas) - set(m['genesis_manifests']))[:3]})")
            for name, sha in m.get("genesis_manifests", {}).items():
                if shas.get(name) != sha:
                    issues.append(f"runs/{p.name}: genesis entry {name} does not match that manifest")
            started = True
        elif started:
            issues.append(f"runs/{p.name}: missing previous_manifest_sha256 after the chain started")
        prev = shas[p.name] = hashlib.sha256(p.read_bytes()).hexdigest()
    for a in sorted((Path(root) / "anchors").glob("*.txt")):
        name, sha = a.read_text().split()
        if shas.get(name) != sha:
            issues.append(f"anchors/{a.name}: {name} no longer matches its anchored hash")
        ots = a.with_name(a.name + ".ots")
        if not ots.exists():
            issues.append(f"anchors/{a.name}: no timestamp proof")
        elif anchor.proof(ots)[1]:
            issues.append(f"anchors/{ots.name}: {anchor.proof(ots)[1]}")
    return issues


PROOF_HOURS = 48
# Days a daily index's own as_of may lag: fetches can succeed while the page stops updating.
# 4 covers a weekend plus a holiday (Ornn doesn't settle on market holidays).
VALUE_AGE_DAYS = {("ornn", "OCPI-H100"): 4, ("silicondata", "SDH100RT"): 4}


def stale_values(root: Path, now: datetime) -> list[str]:
    latest: dict[tuple, str] = {}
    for r in store.read_table(root, "observations"):
        k = (r["source"], r["series_id"])
        if k in VALUE_AGE_DAYS and r["as_of"] > latest.get(k, ""):
            latest[k] = r["as_of"]
    out = []
    for k, days in VALUE_AGE_DAYS.items():
        if k in latest and (now.date() - datetime.fromisoformat(latest[k]).date()).days > days:
            out.append(f"{k[1]}: newest published value is dated {latest[k]}, over {days} days old (page not updating?)")
    return out


def pending_proofs(root: Path, now: datetime) -> list[str]:
    out = []
    for ots in sorted((Path(root) / "anchors").glob("*.ots")):
        status, _ = anchor.proof(ots)
        made = datetime.strptime(ots.name.split(".")[0], "%Y-%m-%dT%H-%M-%SZ").replace(tzinfo=timezone.utc)
        if status == "pending" and (now - made).total_seconds() > PROOF_HOURS * 3600:
            out.append(f"anchors/{ots.name}: timestamp proof still pending after {PROOF_HOURS}h (upgrade not running?)")
    return out


def stale(root: Path, now: datetime) -> list[str]:
    """Sources overdue. A source never attempted (e.g. Vast without a key) is not monitored; one
    that was attempted and then stopped succeeding is."""
    last: dict[str, str] = {}
    attempted: set[str] = set()
    for m in store.read_manifests(root):
        for f in m["fetches"]:
            group = "kalshi_settled" if f["name"].endswith("_settled") else f["source"]
            attempted.add(group)
            if f.get("sha256"):
                last[group] = max(last.get(group, ""), f["fetched_at_utc"])
    out = []
    for src, hours in FRESH_HOURS.items():
        if src not in attempted:
            continue
        if src not in last:
            out.append(f"{src}: never fetched successfully")
            continue
        age = (now - datetime.fromisoformat(last[src].replace("Z", "+00:00"))).total_seconds() / 3600
        if age > hours:
            out.append(f"{src}: last successful fetch {last[src]} is {age:.0f}h old (limit {hours}h)")
    return out


def verify(root: Path) -> list[str]:
    root = Path(root)
    issues: list[str] = []
    fetched: dict[str, str] = {}  # path -> sha256
    for m in store.read_manifests(root):
        for f in m["fetches"]:
            if not f.get("sha256"):
                continue
            fetched[f["path"]] = f["sha256"]
            try:
                store.read_raw(root, f["path"], f["sha256"])
            except (OSError, EOFError, ValueError) as e:
                issues.append(f"manifest {m['started_at_utc']}: {f['path']}: {e}")
    for p in sorted((root / "raw").rglob("*.gz")):
        rel = str(p.relative_to(root))
        try:
            digest = hashlib.sha256(gzip.decompress(p.read_bytes())).hexdigest()
        except (OSError, EOFError) as e:
            issues.append(f"{rel}: unreadable: {e}")
            continue
        if not digest.startswith(p.name.split("__")[-1].split(".")[0]):
            issues.append(f"{rel}: content hash {digest[:16]} does not match its name")
        if rel not in fetched:
            issues.append(f"{rel}: not referenced by any manifest")
    issues += chain(root)
    shas = set(fetched.values())
    for t in TABLES:
        for i, r in enumerate(store.read_table(root, t)):
            if r["raw_sha256"] not in shas:
                issues.append(f"tables/{t} row {i}: raw_sha256 {r['raw_sha256'][:16]} is not a fetched snapshot")
    with tempfile.TemporaryDirectory() as tmp:
        try:
            issues += [f"build: {p}" for p in build.build(root, Path(tmp))]
        except (OSError, EOFError, ValueError) as e:
            return issues + [f"rebuild from raw failed: {e}"]
        for t in DERIVED:
            mine, theirs = Path(tmp) / "tables" / t, root / "tables" / t
            names = {p.name for p in mine.glob("*.csv")} | {p.name for p in theirs.glob("*.csv")}
            for n in sorted(names):
                if not ((mine / n).exists() and (theirs / n).exists() and filecmp.cmp(mine / n, theirs / n, shallow=False)):
                    issues.append(f"tables/{t}/{n}: differs from a rebuild from raw")
        report.build(root, Path(tmp))
        for n in report.FILES:
            if not (root / n).exists() or not filecmp.cmp(Path(tmp) / n, root / n, shallow=False):
                issues.append(f"{n}: differs from a rebuild from the tables")
    return issues


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--fresh", action="store_true", help="also fail if a scheduled source is overdue")
    args = ap.parse_args(argv)
    issues = verify(Path(args.data_dir))
    if args.fresh:
        now = datetime.now(timezone.utc)
        issues += stale(Path(args.data_dir), now) + pending_proofs(Path(args.data_dir), now) \
            + stale_values(Path(args.data_dir), now)
    for i in issues:
        print(i, file=sys.stderr)
    print(f"verify: {'FAILED, ' + str(len(issues)) + ' issue(s)' if issues else 'ok'}", file=sys.stderr)
    return 1 if issues else 0


if __name__ == "__main__":
    sys.exit(main())
