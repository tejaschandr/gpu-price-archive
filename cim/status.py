"""Health report: python -m cim.status [--data-dir data]

Prints a markdown report of everything wrong right now: >30% moves and rejected rows in the latest
run, every cim.verify --fresh issue (including pages unread past their limit) and audit failures.
Warnings (a failed fetch, a refused page) are in the job summary, not here. Exit 1 if anything is wrong.
CI posts this as a GitHub issue, so a failure says what broke, not only that something did.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import audit, run, store, verify


def report(root: Path, now: datetime) -> list[str]:
    lines = []
    ms = store.read_manifests(root)
    if ms:
        m = ms[-1]
        # Warnings (failed fetches, refused pages) don't alert; missing data does, via verify.stale.
        lines += [f"- run {m['started_at_utc']}: {a[:400]}" for a in run.alerts(m)[:10]]
        if m.get("fatal"):
            lines.append(f"- run {m['started_at_utc']} crashed: {m['fatal']}")
    issues = verify.verify(root) + verify.stale(root, now) + verify.pending_proofs(root, now) + verify.stale_values(root, now)
    lines += [f"- {i}" for i in issues]
    lines += [f"- audit: {f}" for f in audit.audit(root, 24, now)[0]]
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data")
    lines = report(Path(ap.parse_args(argv).data_dir), datetime.now(timezone.utc))
    if lines:
        print("\n".join(["Problems found:", "", *lines, "", "What to do: see RUNBOOK.md."]))
        return 1
    print("healthy")
    return 0


if __name__ == "__main__":
    sys.exit(main())
