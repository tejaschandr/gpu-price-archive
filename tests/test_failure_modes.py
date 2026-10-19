"""Failure modes of collection, coverage and provenance: each test reproduces one and pins the fix."""

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from cim import anchor, quality, store, verify
from cim.sources import kalshi, ornn, silicondata, vast
from helpers import EMPTY_MARKETS, FakeNet, live, run_offline


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("VAST_API_KEY", None)

    def last_manifest(self):
        return store.read_manifests(self.root)[-1]


class TestKilledRun(_Base):
    def test_killed_run_does_not_poison_later_runs(self):
        """raw saved before the manifest was written left 'unexplained' files forever."""
        run_offline(self.root, start="2026-10-14T22:17:00Z")
        real, n = store.save_raw, {"calls": 0}

        def dies(*a, **kw):
            n["calls"] += 1
            if n["calls"] > 3:
                raise KeyboardInterrupt("runner killed")
            return real(*a, **kw)
        with mock.patch("cim.store.save_raw", dies), self.assertRaises(KeyboardInterrupt):
            run_offline(self.root, start="2026-10-15T22:17:00Z")
        self.assertFalse(any("not referenced" in i for i in verify.verify(self.root)))
        run_offline(self.root, start="2026-10-16T22:17:00Z")
        self.assertEqual(verify.verify(self.root), [])


class TestSilentFailures(_Base):
    def test_kalshi_blackout_is_degraded(self):
        """every series empty exited 0."""
        net = FakeNet(bodies={r.url: EMPTY_MARKETS for r in kalshi.requests()[0]})
        self.assertEqual(run_offline(self.root, net, argv=("--only", "kalshi")), 2)
        self.assertEqual(self.last_manifest()["degraded"], ["kalshi"])

    def test_problem_alone_is_degraded(self):
        """a new Ornn series was logged but the run was green."""
        page = live("ornn__home.html").replace(b"</body>", b"<p>OCPI-B300</p></body>")
        self.assertEqual(run_offline(self.root, FakeNet(bodies={ornn.PAGE_URL: page}), argv=("--only", "ornn")), 2)

    def test_skipped_source_is_a_note_not_a_failure(self):
        self.assertEqual(run_offline(self.root, argv=("--only", "vast")), 1)  # nothing fetched, nothing degraded
        e = self.last_manifest()["sources"]["vast"]
        self.assertEqual((e["notes"], e["problems"], self.last_manifest()["degraded"]),
                         (["skipped: VAST_API_KEY not set"], [], []))

    def test_vast_stopping_is_caught_by_freshness(self):
        """a revoked or removed key made Vast go quiet without any alert."""
        os.environ["VAST_API_KEY"] = "test-not-a-real-key"
        offers = json.dumps({"offers": [{"gpu_name": "H100 SXM", "num_gpus": 1, "dph_total": 2.0}]}).encode()
        run_offline(self.root, FakeNet(bodies={vast.URL: offers}), start="2026-10-14T22:17:00Z")
        os.environ.pop("VAST_API_KEY")
        run_offline(self.root, start="2026-10-15T22:17:00Z")
        late = verify.stale(self.root, datetime(2026, 10, 16, 5, 0, tzinfo=timezone.utc))
        self.assertEqual([s.split(":")[0] for s in late], ["vast"])

    def test_truncation_reported(self):
        """paging cursors and the Vast limit were ignored."""
        _, problems = kalshi.parse("KXH100MS", b'{"cursor": "abc", "markets": []}', "u")
        self.assertIn("more than one page", problems[0])
        offers = json.dumps({"offers": [{"gpu_name": "H100 SXM", "num_gpus": 1, "dph_total": 2.0}] * vast.LIMIT}).encode()
        self.assertIn("query limit", vast.parse("h100_offers", offers, "u")[1][0])

    def test_renamed_history_series_reported(self):
        """a renamed Silicon Data series would have silently started a new history."""
        body = live("silicondata__h100.html").replace(b'H100-hyperscaler', b'H100-Hyperscale')
        _, problems = silicondata.parse("h100", body, "u")
        self.assertIn("history series titles", problems[0])


class TestCoverage(_Base):
    def test_every_look_is_recorded(self):
        """a refused or failed day was indistinguishable from an unchanged one."""
        run_offline(self.root, start="2026-10-14T22:17:00Z", argv=("--only", "silicondata"))
        bad = live("silicondata__h100.html").replace(b"aria-pressed=", b"aria-selected=")
        net = FakeNet(bodies={silicondata.BASE.format(slug="h100"): bad}, fail={})
        run_offline(self.root, net, start="2026-10-15T22:17:00Z", argv=("--only", "silicondata"))
        run_offline(self.root, FakeNet(fail={"silicondata": 503}), start="2026-10-16T22:17:00Z", argv=("--only", "silicondata"))
        h100 = [(r["observed_at_utc"][:10], r["outcome"], r["changed"])
                for r in store.read_table(self.root, "coverage") if r["name"] == "h100"]
        self.assertEqual(h100, [("2026-10-14", "ok", "yes"), ("2026-10-15", "problem", "yes"), ("2026-10-16", "failed", "")])
        h200 = [(r["outcome"], r["changed"]) for r in store.read_table(self.root, "coverage") if r["name"] == "h200"]
        self.assertEqual(h200, [("ok", "yes"), ("ok", "no"), ("failed", "")])
        self.assertEqual(verify.verify(self.root), [])

    def test_unchanged_refused_page_is_not_a_confirmation(self):
        """a refused page whose bytes stayed the same showed as a clean 'unchanged'."""
        bad = live("silicondata__h100.html").replace(b"aria-pressed=", b"aria-selected=")
        for day in ("15", "16"):
            run_offline(self.root, FakeNet(bodies={silicondata.BASE.format(slug="h100"): bad}),
                        start=f"2026-10-{day}T22:17:00Z", argv=("--only", "silicondata"))
        h100 = [(r["outcome"], r["changed"], bool(r["problems"]))
                for r in store.read_table(self.root, "coverage") if r["name"] == "h100"]
        self.assertEqual(h100, [("problem", "yes", True), ("problem", "no", True)])


class TestProvability(_Base):
    def setUp(self):
        super().setUp()
        for day in ("14", "15", "16"):
            run_offline(self.root, start=f"2026-10-{day}T22:17:00Z", argv=("--only", "ornn"))

    def fake_proof(self, txt: Path, attested=False, digest=None) -> Path:
        """A structurally valid OpenTimestamps proof (no network): header, sha256 op, digest, a tag."""
        import hashlib
        tag = anchor._BITCOIN if attested else anchor._PENDING
        ots = txt.with_name(txt.name + ".ots")
        ots.write_bytes(anchor._MAGIC + b"\x01\x08" + (digest or hashlib.sha256(txt.read_bytes()).digest()) + b"\x00" + tag)
        return ots

    def test_chain_and_anchor_hold(self):
        a = anchor.anchor(self.root)
        self.assertEqual(a.read_text().split()[0], sorted((self.root / "runs").glob("*.json"))[-1].name)
        self.assertTrue(any("no timestamp proof" in i for i in verify.verify(self.root)))
        self.fake_proof(a)
        self.assertEqual(verify.verify(self.root), [])

    def test_proof_must_cover_its_anchor(self):
        ots = self.fake_proof(anchor.anchor(self.root), digest=b"\x00" * 32)
        self.assertTrue(any("does not commit to" in i for i in verify.verify(self.root)))
        ots.write_bytes(b"not a proof")
        self.assertTrue(any("not an OpenTimestamps" in i for i in verify.verify(self.root)))

    def test_pending_proofs_flagged_after_48h(self):
        a = anchor.anchor(self.root)
        ots = self.fake_proof(a)
        made = datetime.strptime(a.name.split(".")[0], "%Y-%m-%dT%H-%M-%SZ").replace(tzinfo=timezone.utc)
        from datetime import timedelta
        self.assertEqual(verify.pending_proofs(self.root, made + timedelta(hours=47)), [])
        self.assertEqual(len(verify.pending_proofs(self.root, made + timedelta(hours=49))), 1)
        self.fake_proof(a, attested=True)
        self.assertEqual(verify.pending_proofs(self.root, made + timedelta(hours=49)), [])
        self.assertEqual(anchor.proof(ots)[0], "complete")

    def test_editing_history_breaks_the_chain(self):
        """a backdated or edited manifest passed verify."""
        p = sorted((self.root / "runs").glob("*.json"))[1]
        m = json.loads(p.read_text())
        m["sources"]["ornn"]["problems"] = ["quietly edited"]
        p.write_text(json.dumps(m, indent=2, sort_keys=True) + "\n")
        self.assertTrue(any("chain broken" in i for i in verify.verify(self.root)))

    def test_inserted_backdated_run_breaks_the_chain(self):
        m = store.read_manifests(self.root)[0]
        m["started_at_utc"] = "2026-10-14T23:00:00Z"
        store.write_manifest(self.root, m)
        self.assertTrue(any("chain broken" in i for i in verify.verify(self.root)))

    def test_manifest_inserted_before_chain_is_caught(self):
        """A forged manifest dated before the chain started passed verify."""
        root = self.root / "legacy"
        run_offline(root, start="2026-10-14T22:17:00Z", argv=("--only", "ornn"))
        p = next((root / "runs").glob("*.json"))
        m = json.loads(p.read_text())
        del m["previous_manifest_sha256"]
        p.write_text(json.dumps(m))
        run_offline(root, start="2026-10-15T22:17:00Z", argv=("--only", "ornn"))
        forged = {**m, "started_at_utc": "2026-10-14T06:00:00Z"}
        (root / "runs" / "2026-10-14T06-00-00Z.json").write_text(json.dumps(forged))
        self.assertTrue(any("genesis list doesn't match" in i for i in verify.chain(root)))

    def test_missing_report_is_caught(self):
        (self.root / "divergence.csv").unlink()
        self.assertTrue(any("divergence.csv: differs" in i for i in verify.verify(self.root)))

    def test_anchored_manifest_cannot_change(self):
        anchor.anchor(self.root)
        p = sorted((self.root / "runs").glob("*.json"))[-1]
        p.write_text(p.read_text().replace('"ornn"', '"ornn" ', 1))
        self.assertTrue(any("no longer matches its anchored hash" in i for i in verify.verify(self.root)))

    def test_first_chained_run_covers_earlier_manifests(self):
        root = self.root / "legacy"
        run_offline(root, start="2026-10-14T22:17:00Z", argv=("--only", "ornn"))
        p = next((root / "runs").glob("*.json"))
        m = json.loads(p.read_text())
        del m["previous_manifest_sha256"]  # as written before the chain existed
        p.write_text(json.dumps(m))
        run_offline(root, start="2026-10-15T22:17:00Z", argv=("--only", "ornn"))
        self.assertIn(p.name, store.read_manifests(root)[-1]["genesis_manifests"])
        self.assertEqual(verify.chain(root), [])


class TestWritesAndInputs(_Base):
    def test_manifest_write_is_atomic(self):
        """A kill mid-write left truncated JSON that crashed every later run."""
        run_offline(self.root, start="2026-10-14T22:17:00Z", argv=("--only", "ornn"))
        p = next((self.root / "runs").glob("*.json"))
        before = p.read_text()
        real = Path.write_text

        def dies_halfway(self_, text, *a, **kw):  # the process is killed partway through a write
            real(self_, text[: len(text) // 2], *a, **kw)
            raise KeyboardInterrupt
        with mock.patch("pathlib.Path.write_text", dies_halfway), self.assertRaises(KeyboardInterrupt):
            store.write_manifest(self.root, {**json.loads(before), "fetches": []})
        self.assertEqual(p.read_text(), before)  # the old manifest survives intact
        self.assertEqual(run_offline(self.root, start="2026-10-15T22:17:00Z", argv=("--only", "ornn")), 0)

    def test_cents_rejected(self):
        """A switch to cents (317 for $3.17) passed the old $1,000 ceiling."""
        row = {"observed_at_utc": "2026-10-14T22:17:00Z", "raw_sha256": "a" * 64, "source": "ornn",
               "series_id": "OCPI-H100", "price_usd_per_gpu_hr": "317.0000", "as_of": "2026-10-13"}
        self.assertIn("out of range", quality.invalid("observations", row))

    def test_malformed_key_is_a_problem(self):
        os.environ["VAST_API_KEY"] = "bad key"
        self.assertEqual(run_offline(self.root, argv=("--only", "vast")), 1)  # nothing fetched
        self.assertEqual(self.last_manifest()["degraded"], ["vast"])
        self.assertIn("invalid format", self.last_manifest()["sources"]["vast"]["problems"][0])

    def test_local_collection_into_repo_data_refused(self):
        from cim import run
        repo_data = Path(run.__file__).resolve().parents[1] / "data"
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": ""}), mock.patch("sys.stderr"):
            self.assertEqual(run.main(["--data-dir", str(repo_data)]), 1)


class TestEvidence(_Base):
    def test_reports_are_verified(self):
        run_offline(self.root, argv=("--only", "ornn"))
        self.assertEqual(verify.verify(self.root), [])
        p = self.root / "LATEST.md"
        p.write_text(p.read_text().replace("3.17", "9.99"))
        self.assertTrue(any("LATEST.md: differs" in i for i in verify.verify(self.root)))

    def test_response_evidence_recorded(self):
        run_offline(self.root, argv=("--only", "ornn"))
        f = self.last_manifest()["fetches"][0]
        self.assertEqual(f["response"]["content-type"], "text/html")


class TestStaleValues(_Base):
    def test_page_that_stops_updating_is_flagged(self):
        """Fetches succeeding while the vendor's own date freezes passed every check."""
        run_offline(self.root, argv=("--only", "ornn"))  # as_of 2026-10-13
        self.assertEqual(verify.stale_values(self.root, datetime(2026, 10, 17, tzinfo=timezone.utc)), [])
        [msg] = verify.stale_values(self.root, datetime(2026, 10, 18, tzinfo=timezone.utc))
        self.assertIn("OCPI-H100", msg)


class TestStatus(_Base):
    def test_report_names_what_broke(self):
        from cim import status
        now = datetime(2026, 10, 14, 23, 0, tzinfo=timezone.utc)
        run_offline(self.root, start="2026-10-14T22:17:00Z", argv=("--only", "ornn"))
        self.assertEqual(status.report(self.root, now), [])
        run_offline(self.root, FakeNet(fail={"ornn": 403}), start="2026-10-14T22:30:00Z", argv=("--only", "ornn"))
        line, *more = status.report(self.root, now)
        self.assertIn("**ornn** degraded", line)
        self.assertIn("HTTP 403", line)
        self.assertTrue(all("audit:" in m for m in more))  # the audit sees the failed look too


class TestAudit(_Base):
    def test_clean_run_passes_and_reports_values(self):
        from cim import audit
        run_offline(self.root, start="2026-10-14T22:17:00Z")
        fail, info = audit.audit(self.root, 24, datetime(2026, 10, 14, 23, tzinfo=timezone.utc))
        self.assertEqual(fail, [])
        self.assertTrue(any(i.startswith("last 24h: 1 runs") for i in info))

    def test_cross_source_failures_are_caught(self):
        from cim import audit, status
        run_offline(self.root, start="2026-10-14T22:17:00Z")
        p = next((self.root / "tables" / "kalshi_settlements").glob("*.csv"))
        p.write_text(p.read_text().splitlines()[0] + "\n")  # settlements table loses its rows
        fail, _ = audit.audit(self.root, 24, datetime(2026, 10, 14, 23, tzinfo=timezone.utc))
        self.assertTrue(any("not in the table" in f for f in fail))
        self.assertTrue(any("audit:" in line for line in status.report(self.root, datetime(2026, 10, 14, 23, tzinfo=timezone.utc))))


class TestPrecision(_Base):
    def test_raw_filed_under_fetch_date(self):
        """a run crossing midnight filed later bytes under the start date."""
        run_offline(self.root, start="2026-10-14T23:59:57Z", argv=("--only", "silicondata"))
        days = {(f["fetched_at_utc"][:10], f["path"].split("/")[1]) for f in self.last_manifest()["fetches"]}
        self.assertTrue(all(a == b for a, b in days) and len(days) == 2)

    def test_jumps_only_for_indices(self):
        """thin Vast medians would have raised false alarms."""
        prev = [{"source": s, "series_id": "X", "price_usd_per_gpu_hr": "2.0", "observed_at_utc": "t"} for s in ("vast", "ornn")]
        new = [{"source": s, "series_id": "X", "price_usd_per_gpu_hr": "3.0"} for s in ("vast", "ornn")]
        self.assertEqual(len(quality.jumps(new, prev)), 1)

    def test_formula_cells_rejected(self):
        row = {"observed_at_utc": "2026-10-14T22:17:00Z", "raw_sha256": "a" * 64, "slug": "h100",
               "series": "=HYPERLINK(\"x\")", "date": "2026-10-14", "price": "2.85"}
        self.assertIn("formula", quality.invalid("silicondata_history", row))

    def test_method_change_at_same_price_is_recorded(self):
        """a switch to the meta-tag fallback at the same price left no trace."""
        run_offline(self.root, start="2026-10-14T22:17:00Z", argv=("--only", "ornn"))
        meta_only = (b'<html><head><meta name="description" content="H100 SXM at $3.17 per GPU-hour, settled '
                     b'October 13, 2026"/></head><body></body></html>')
        run_offline(self.root, FakeNet(bodies={ornn.PAGE_URL: meta_only}), start="2026-10-15T22:17:00Z", argv=("--only", "ornn"))
        h100 = [r["method"] for r in store.read_table(self.root, "observations") if r["series_id"] == "OCPI-H100"]
        self.assertEqual(h100, ["public page, latest daily settle", "public page meta description (card not found)"])


if __name__ == "__main__":
    unittest.main()
