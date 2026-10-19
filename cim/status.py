"""Health report: python -m cim.status [--data-dir data]

Prints a markdown report of everything wrong right now: the latest run's degraded sources with
their errors and problems, plus every cim.verify --fresh issue. Exit 1 if anything is wrong.
CI posts this as a GitHub issue, so a failure says what broke, not only that something did.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import audit, store, verify


def report(root: Path, now: datetime) -> list[str]:
    lines = []
    ms = store.read_manifests(root)
    if ms:
        m = ms[-1]
        for name in m.get("degraded", []):
            e = m["sources"].get(name, {})
            detail = "; ".join(e.get("errors", []) + e.get("jumps", []) + e.get("problems", [])) or "see build problems"
            lines.append(f"- **{name}** degraded in run {m['started_at_utc']}: {detail[:500]}")
        for p in m.get("build_problems", [])[:10]:
            lines.append(f"- build: {p[:300]}")
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
