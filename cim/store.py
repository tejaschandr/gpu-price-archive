"""Storage layout under data/. Raw bytes are the source of truth; tables are derived.

raw/<date>/<source>__<name>__<sha16>.<ext>.gz   exact bytes fetched, content-addressed, never overwritten
runs/<started_at>.json                          manifest: every fetch with url, time, status, sha256, error
tables/<table>/<YYYY-MM>.csv                    rebuilt from raw + manifests by cim.build; never hand-edited
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import os
from pathlib import Path


def save_raw(root: Path, day: str, source: str, name: str, body: bytes, ext: str) -> tuple[str, str]:
    """Write a gzip snapshot; return (path relative to root, sha256 of the uncompressed body)."""
    digest = hashlib.sha256(body).hexdigest()
    d = Path(root) / "raw" / day
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{source}__{name}__{digest[:16]}.{ext}.gz"
    on_disk = _gz_sha256(path)
    if on_disk == digest:
        return str(path.relative_to(root)), digest
    if on_disk is not None:  # readable but different bytes: a 64-bit prefix collision
        path = d / f"{source}__{name}__{digest}.{ext}.gz"
    # Temp file + rename: a killed run never leaves a truncated snapshot. mtime=0: deterministic gzip.
    tmp = path.with_suffix(".tmp")
    with open(tmp, "wb") as fh, gzip.GzipFile(fileobj=fh, mode="wb", mtime=0) as gz:
        gz.write(body)
    os.replace(tmp, path)
    return str(path.relative_to(root)), digest


def read_raw(root: Path, path: str, sha256: str) -> bytes:
    """The snapshot's bytes. Raises if they don't hash to sha256."""
    body = gzip.decompress((Path(root) / path).read_bytes())
    if hashlib.sha256(body).hexdigest() != sha256:
        raise ValueError(f"{path}: content does not match sha256 {sha256}")
    return body


def _gz_sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(gzip.decompress(path.read_bytes())).hexdigest()
    except (OSError, EOFError):
        return None


def write_manifest(root: Path, manifest: dict) -> Path:
    d = Path(root) / "runs"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{manifest['started_at_utc'].replace(':', '-')}.json"
    write_atomic(path, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return path


def write_atomic(path: Path, text: str) -> None:
    """Temp file + rename: a kill mid-write leaves the old file or the new one, never half of one.
    (The manifest is rewritten after every fetch; a truncated one would break every later run.)"""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def read_manifests(root: Path) -> list[dict]:
    """All run manifests, oldest first."""
    ms = [json.loads(p.read_text()) for p in (Path(root) / "runs").glob("*.json")]
    return sorted(ms, key=lambda m: m["started_at_utc"])


def write_table(root: Path, table: str, columns: list[str], rows: list[dict]) -> None:
    """Replace the table with rows, one CSV per month of observed_at_utc. Partitions that no
    longer have rows are removed, so the directory is exactly what the rows say."""
    d = Path(root) / "tables" / table
    parts: dict[str, list[dict]] = {}
    for r in rows:
        parts.setdefault(r["observed_at_utc"][:7], []).append(r)
    d.mkdir(parents=True, exist_ok=True)
    for old in d.glob("*.csv"):
        if old.stem not in parts:
            old.unlink()
    for month, rs in parts.items():
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=columns, lineterminator="\n")
        w.writeheader()
        w.writerows(rs)
        path = d / f"{month}.csv"
        if not path.exists() or path.read_text(encoding="utf-8") != buf.getvalue():
            path.write_text(buf.getvalue(), encoding="utf-8")


def read_table(root: Path, table: str) -> list[dict]:
    rows: list[dict] = []
    for p in sorted((Path(root) / "tables" / table).glob("*.csv")):
        with open(p, newline="", encoding="utf-8") as fh:
            rows += list(csv.DictReader(fh))
    return rows
