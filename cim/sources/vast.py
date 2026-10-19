"""Vast.ai marketplace: listed H100 offers with the specs that make an "H100-hour" non-fungible.

Requires VAST_API_KEY; skipped without it. The response is redacted to an allowlist before
anything is saved, and kept values are coarsened so a row can't single out a host machine.
Price per GPU = dph_total / num_gpus (what dph_total includes is undocumented).
"""

from __future__ import annotations

import json
import statistics

from .. import http
from ..model import Request, Rows

NAME = "vast"
EXPECTED_ROWS = {"vast_offers": 1}  # checked only when a request was made (i.e. a key is set)
LIMIT = 1000
MIN_OFFERS = 8  # distinct machines before a form factor's median is shown (5 let one host set p75)
_BUNDLE = {"num_gpus", "dph_total", "price_per_gpu_hr", "observed_at_utc", "raw_sha256"}  # what differs between bundles of one machine
URL = "https://console.vast.ai/api/v0/bundles/"
GPU_NAMES = ["H100 SXM", "H100 PCIE", "H100 NVL"]  # unverified against a live response

KEEP = {"gpu_name", "num_gpus", "dph_total", "gpu_max_power", "pci_gen", "pcie_bw", "bw_nvlink",
        "gpu_mem_bw", "reliability", "geolocation", "verification", "hosting_type"}
_SCALAR = (str, int, float, bool, type(None))


def requests() -> tuple[list[Request], list[str]]:
    key, err = http.api_key("VAST_API_KEY")
    if not key:
        return [], [err or "skipped: VAST_API_KEY not set"]
    query = {"gpu_name": {"in": GPU_NAMES}, "rentable": {"eq": True}, "rented": {"eq": False},
             "verified": {"eq": True}, "type": "ondemand", "limit": LIMIT}
    return [Request("h100_offers", URL, "json", method="POST", data=json.dumps(query).encode(),
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                             "Accept": "application/json"},
                    transform=lambda raw: json.dumps(redact(json.loads(raw)), sort_keys=True).encode())], []


def redact(payload) -> dict:
    """Only allowlisted scalar offer fields, coarsened. Any other shape raises: nothing unredacted is saved."""
    offers = payload.get("offers") if isinstance(payload, dict) else None
    if not isinstance(offers, list) or not all(isinstance(o, dict) for o in offers):
        raise ValueError("unexpected Vast payload shape; nothing archived")
    return {"offers": [{k: _coarsen(k, v) for k, v in o.items() if k in KEEP and isinstance(v, _SCALAR)}
                       for o in offers]}


def _coarsen(k: str, v):
    if k == "reliability" and isinstance(v, (int, float)) and not isinstance(v, bool):
        return round(v, 2)
    if k == "geolocation" and isinstance(v, str):
        return v.rsplit(",", 1)[-1].strip()  # country only
    return v


def form_factor(gpu_name: str) -> str:
    n = gpu_name.upper()
    return "SXM" if "SXM" in n else "NVL" if "NVL" in n else "PCIe" if "PCIE" in n else ""


def offer_rows(offers: list[dict]) -> list[dict]:
    rows = []
    for o in offers:
        name, n, dph = str(o.get("gpu_name", "")), o.get("num_gpus") or 0, o.get("dph_total")
        if "H100" not in name.upper() or not n or dph is None:
            continue
        rows.append({"gpu_name": name, "form_factor": form_factor(name), "num_gpus": n,
                     "price_per_gpu_hr": round(float(dph) / n, 6), "dph_total": dph,
                     "gpu_max_power_w": o.get("gpu_max_power"), "pci_gen": o.get("pci_gen"),
                     "pcie_bw_gbs": o.get("pcie_bw"), "bw_nvlink_gbs": o.get("bw_nvlink"),
                     "gpu_mem_bw_gbs": o.get("gpu_mem_bw"), "reliability": o.get("reliability"),
                     "geolocation": o.get("geolocation"), "verification": o.get("verification"),
                     "datacenter_hosted": o.get("hosting_type")})
    return rows


def medians(rows: list[dict]) -> dict[str, tuple[int, float, float, float]]:
    """{form_factor: (machines, p25, p50, p75)} of price per GPU, for form factors with MIN_OFFERS+
    distinct machines. A host lists one machine as 1-, 2-, 4-GPU bundles that share every measured
    spec; they count once (on 2026-10-15 one machine's three $9.20 bundles made the SXM median 5x)."""
    by_ff: dict[str, dict[tuple, float]] = {}
    for r in rows:
        machine = tuple(sorted((k, str(v)) for k, v in r.items() if k not in _BUNDLE))
        by_ff.setdefault(r["form_factor"] or "unknown", {}).setdefault(machine, float(r["price_per_gpu_hr"]))
    out = {}
    for ff, distinct in sorted(by_ff.items()):
        if len(distinct) >= MIN_OFFERS:
            q = statistics.quantiles(sorted(distinct.values()), n=4, method="inclusive")
            out[ff] = (len(distinct), q[0], q[1], q[2])
    return out


def parse(name: str, body: bytes, url: str) -> tuple[Rows, list[str]]:
    offers = json.loads(body).get("offers", [])
    rows = offer_rows(offers)
    if not rows:
        return {}, ["no H100 offers returned; check GPU_NAMES against Vast's gpu_name strings"]
    problems = [f"{len(offers)} offers = the query limit; results may be truncated"] if len(offers) >= LIMIT else []
    return {"vast_offers": rows}, problems
