"""Anchor the archive: python -m cim.anchor [--data-dir data]

Writes anchors/<run>.txt naming the latest manifest and its sha256. Through the chain of
previous_manifest_sha256 links, that one hash commits to every earlier manifest, and through them
to the hash of every raw file. CI then timestamps the anchor with OpenTimestamps (anchors in
Bitcoin, free): proof that the archive up to that run existed at that time, which can't be
backdated. Only the hash leaves the repo. Check a proof with `ots verify anchors/<run>.txt.ots`.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

from .store import write_atomic

# OpenTimestamps proof format: header, version, the file-hash op (0x08 = sha256), the 32-byte file
# digest, then the attestation tree. Read with the stdlib so checks don't depend on the client.
_MAGIC = b"\x00OpenTimestamps\x00\x00Proof\x00\xbf\x89\xe2\xe8\x84\xe8\x92\x94"
_BITCOIN = bytes.fromhex("0588960d73d71901")   # BitcoinBlockHeaderAttestation tag
_PENDING = bytes.fromhex("83dfe30d2ef90c8e")   # PendingAttestation tag (a calendar's promise)


def proof(ots: Path) -> tuple[str, str]:
    """(status, problem) for a proof file: status is complete (Bitcoin-attested) or pending.
    problem is set if the file isn't a proof of exactly the anchor it sits next to."""
    b = ots.read_bytes()
    target = ots.with_suffix("")  # x.txt.ots -> x.txt
    if not b.startswith(_MAGIC) or b[len(_MAGIC) + 1:len(_MAGIC) + 2] != b"\x08":
        return "", "not an OpenTimestamps sha256 proof"
    if not target.exists() or b[len(_MAGIC) + 2:len(_MAGIC) + 34] != hashlib.sha256(target.read_bytes()).digest():
        return "", f"does not commit to {target.name}"
    return ("complete" if _BITCOIN in b else "pending"), ""


def anchor(root: Path) -> Path | None:
    runs = sorted((Path(root) / "runs").glob("*.json"))
    if not runs:
        return None
    latest = runs[-1]
    d = Path(root) / "anchors"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{latest.stem}.txt"
    write_atomic(path, f"{latest.name} {hashlib.sha256(latest.read_bytes()).hexdigest()}\n")
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--pending", action="store_true", help="list proofs not yet Bitcoin-attested, and exit")
    args = ap.parse_args(argv)
    if args.pending:
        for ots in sorted((Path(args.data_dir) / "anchors").glob("*.ots")):
            if proof(ots)[0] == "pending":
                print(ots)
        return 0
    path = anchor(Path(args.data_dir))
    print(path or "no manifests to anchor")
    return 0 if path else 1


if __name__ == "__main__":
    sys.exit(main())
