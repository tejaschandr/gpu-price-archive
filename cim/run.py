"""Collect: python -m cim.run [--data-dir data] [--only ornn,kalshi]

Fetches every source's requests once, saves the exact bytes, and records each fetch in the run's
manifest as it happens. Then rebuilds the tables and reports from all raw snapshots
(cim.build, cim.report). Each source is isolated: one failing never blocks the others.

Exit: 2 when a human must look now (a published index moved more than 30%, a row failed the
validity rules, or a source's pages downloaded but gave no rows at all for a table it must fill:
the API or tickers changed); 1 nothing was fetched; else 0. Fetch failures, refused pages and short sources are
warnings ("degraded" in the manifest and job summary): missing data alerts through cim.verify
--fresh once a page has gone unread for its freshness limit.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, build, http, quality, report, store
from .sources import ALL


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def scrub(text: str) -> str:
    """Remove API key values from anything written to the committed manifest."""
    for k, v in os.environ.items():
        if k.endswith("_API_KEY") and v.strip():
            text = text.replace(v.strip(), "***")
    return text


def code_commit() -> str:
    if os.environ.get("GITHUB_SHA"):
        return os.environ["GITHUB_SHA"]
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                              timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def previous_manifest_sha256(root: Path) -> str:
    """Hash of the latest manifest file: each run commits to the whole history before it."""
    files = sorted((Path(root) / "runs").glob("*.json"))
    return hashlib.sha256(files[-1].read_bytes()).hexdigest() if files else ""


def collect_source(root: Path, manifest: dict, name: str, previous: list[dict]) -> None:
    mod = ALL[name]
    entry = manifest["sources"][name] = {"rows": {}, "observations": 0, "notes": [], "problems": [],
                                         "errors": [], "jumps": [], "requests": 0}
    try:
        reqs, notes = mod.requests()
    except Exception as e:  # noqa: BLE001
        entry["errors"].append(scrub(f"{type(e).__name__}: {e}"))
        return
    # "skipped: ..." is expected (e.g. no key configured); anything else, like a malformed key, is a problem.
    entry["notes"] += [m for m in notes if m.startswith("skipped:")]
    entry["problems"] += [scrub(m) for m in notes if not m.startswith("skipped:")]
    entry["requests"] = len(reqs)
    retries = getattr(mod, "RETRIES", 0)
    for r in reqs:
        for attempt in range(1 + retries):
            body = fetch(root, manifest, entry, name, r)
            if body is None:
                break
            try:
                rows, problems = mod.parse(r.name, body, r.url)
            except Exception as e:  # noqa: BLE001 - saved bytes stay; the parse failure is reported
                entry["problems"].append(scrub(f"{r.name}: parse failed: {type(e).__name__}: {e}"))
                entry["trace"] = scrub(traceback.format_exc(limit=3))
                break
            if problems and attempt < retries:
                # The vendor serves inconsistent page variants; a refused page is asked for again. Every
                # attempt stays in the manifest and in coverage.
                entry["notes"].append(scrub(f"{r.name}: fetched again after: {problems[-1]}")[:300])
                time.sleep(getattr(mod, "RETRY_WAIT", 5))
                continue
            for t, rs in rows.items():
                entry["rows"][t] = entry["rows"].get(t, 0) + len(rs)
            entry["observations"] += len(rows.get("observations", []))
            entry["problems"] += [scrub(p) for p in problems]
            entry["jumps"] += quality.jumps(rows.get("observations", []), previous)
            break


def fetch(root: Path, manifest: dict, entry: dict, name: str, r) -> bytes | None:
    """One request, saved and recorded in the manifest as it happens; None if it failed."""
    f = {"source": name, "name": r.name, "url": r.url, "fetched_at_utc": utcnow()}
    manifest["fetches"].append(f)
    try:
        status, body, evidence = http.get(r.url, headers=r.headers, data=r.data, method=r.method)
        # When the bytes arrived, not when the request was queued: pacing and retries can add
        # a minute, and a point-in-time stamp must never be earlier than the data.
        f["fetched_at_utc"] = utcnow()
        f["status"], f["response"] = status, evidence
        if r.transform:
            body = r.transform(body)
        f["path"], f["sha256"] = store.save_raw(root, f["fetched_at_utc"][:10], name, r.name, body, r.ext)
        f["bytes"] = len(body)
        return body
    except Exception as e:  # noqa: BLE001 - isolate every request
        f["error"] = scrub(f"{type(e).__name__}: {e}")
        entry["errors"].append(f"{r.name}: {f['error']}")
        return None
    finally:
        # After every fetch, so a killed run still leaves a manifest for every byte it saved.
        store.write_manifest(root, manifest)

def degraded(name: str, e: dict) -> bool:
    short = e["requests"] and any(e["rows"].get(t, 0) < n for t, n in ALL[name].EXPECTED_ROWS.items())
    return bool(e["errors"] or e["problems"] or e["jumps"] or short)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--only", default="", help="comma-separated source names")
    ap.add_argument("--allow-local", action="store_true", help="collect into the repo's data/ outside CI")
    args = ap.parse_args(argv)
    root = Path(args.data_dir)
    if (root.resolve() == (Path(__file__).resolve().parents[1] / "data") and not os.environ.get("GITHUB_ACTIONS")
            and not args.allow_local):
        # A local run committed next to CI's forks the manifest chain. Collect locally elsewhere.
        print("refusing to collect into the repo's data/ outside CI (it would fork the manifest chain); "
              "use --data-dir /tmp/cim, or --allow-local if you mean it", file=sys.stderr)
        return 1
    wanted = [s for s in args.only.split(",") if s] or list(ALL)

    manifest = {"started_at_utc": utcnow(), "version": __version__, "code_commit": code_commit(),
                "previous_manifest_sha256": previous_manifest_sha256(root), "fetches": [], "sources": {}}
    earlier = sorted((root / "runs").glob("*.json"))
    if earlier and not any("previous_manifest_sha256" in p.read_text() for p in earlier):
        # The first chained run: commit to every manifest that predates the chain.
        manifest["genesis_manifests"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in earlier}
    store.write_manifest(root, manifest)
    previous = store.read_table(root, "observations")
    for name in wanted:
        collect_source(root, manifest, name, previous)
    manifest["degraded"] = [n for n, e in manifest["sources"].items() if degraded(n, e)]
    store.write_manifest(root, manifest)
    try:
        manifest["build_problems"] = build.build(root)
        if manifest["build_problems"]:
            manifest["degraded"].append("build")
        report.build(root)
    except Exception as e:
        manifest["fatal"] = scrub(f"{type(e).__name__}: {e}")
        raise
    finally:
        store.write_manifest(root, manifest)
        summary(manifest)
    if not any(f.get("sha256") for f in manifest["fetches"]):
        return 1
    return 2 if alerts(manifest) else 0


def alerts(m: dict) -> list[str]:
    """What needs a human now. Everything else in "degraded" is a warning."""
    out = [f"{n}: {j}" for n, e in m["sources"].items() for j in e["jumps"]]
    out += [f"build: {p}" for p in m.get("build_problems", [])]
    for n, e in m["sources"].items():
        fetched = any(f.get("sha256") for f in m["fetches"] if f["source"] == n)
        empty = [t for t in ALL[n].EXPECTED_ROWS if fetched and e["requests"] and e["rows"].get(t, 0) == 0]
        if empty:
            out.append(f"{n}: pages downloaded but no {', '.join(empty)} rows (API or layout changed?)")
    return out


def summary(m: dict) -> None:
    """Per-source counts to stderr and, in GitHub Actions, the job summary."""
    lines = ["| source | fetched | failed | rows | problems / errors |", "|---|---|---|---|---|"]
    for n, e in m["sources"].items():
        fs = [f for f in m["fetches"] if f["source"] == n]
        ok = sum(1 for f in fs if f.get("sha256"))
        rows = ", ".join(f"{t} {c}" for t, c in sorted(e["rows"].items())) or "0"
        issues = "; ".join(e["errors"] + e["jumps"] + e["problems"] + e["notes"])[:400].replace("|", "/") or "-"
        lines.append(f"| {n}{' ⚠' if n in m['degraded'] else ''} | {ok} | {len(fs) - ok} | {rows} | {issues} |")
    text = "\n".join(lines) + "\n"
    print(text, file=sys.stderr)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write(text)


if __name__ == "__main__":
    sys.exit(main())
