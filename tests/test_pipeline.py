"""End-to-end, offline: collect -> raw + manifest -> rebuild -> verify -> reports.

These pin the archive's guarantees: every value traces to bytes; reruns never duplicate;
a revised value is a new row, never an edit; tables rebuild byte for byte; tampering is caught.
"""

import csv
import gzip
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from datetime import datetime, timezone

from cim import audit, build, report, run, store, verify
from cim.sources import docs, getdeploying, kalshi, ornn, silicondata
from helpers import DOC_HTML, FakeNet, live, ornn_history, run_offline


def rows(root, table):
    return store.read_table(root, table)


def tables_bytes(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(Path(root, "tables").rglob("*.csv"))}


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for k in ("VAST_API_KEY", "GITHUB_STEP_SUMMARY"):
            os.environ.pop(k, None)

    def manifests(self):
        return store.read_manifests(self.root)


class TestCollect(_Base):
    def test_full_run(self):
        net = FakeNet()
        self.assertEqual(run_offline(self.root, net), 0)
        [m] = self.manifests()
        expected = 1 + len(ornn.HISTORY_GPUS) + len(silicondata.PAGES) + 2 * len(kalshi.SERIES) + len(docs.PAGES) + len(getdeploying.SLUGS)  # vast skipped without a key
        self.assertEqual(len(m["fetches"]), expected)
        self.assertEqual(sum(net.calls.values()), expected)  # one request per page
        self.assertTrue(all(f.get("sha256") and f["status"] == 200 for f in m["fetches"]))
        self.assertEqual(m["degraded"], [])
        self.assertEqual(len(rows(self.root, "observations")), 11)  # ornn 5, silicondata 6 (SemiAnalysis retired)
        self.assertEqual(len(rows(self.root, "kalshi_settlements")), 2)
        self.assertEqual(verify.verify(self.root), [])
        self.assertIn("**+$0.32/GPU-hr** on 2026-10-13", (self.root / "LATEST.md").read_text())

    def test_every_row_traces_to_its_bytes(self):
        run_offline(self.root)
        fetches = {f["sha256"]: f for m in self.manifests() for f in m["fetches"] if f.get("sha256")}
        for r in rows(self.root, "observations"):
            f = fetches[r["raw_sha256"]]
            body = store.read_raw(self.root, f["path"], f["sha256"])
            self.assertIn(r, [{"observed_at_utc": f["fetched_at_utc"], **x, "raw_sha256": f["sha256"]}
                              for x in __import__("cim.sources", fromlist=["ALL"]).ALL[f["source"]]
                              .parse(f["name"], body, f["url"])[0]["observations"]])


class TestPointInTime(_Base):
    def test_identical_rerun_adds_nothing(self):
        run_offline(self.root, start="2026-10-13T21:37:00Z")
        data = lambda: {k: v for k, v in tables_bytes(self.root).items() if "coverage" not in k}
        before = data()
        raw_before = sorted((self.root / "raw").rglob("*.gz"))
        run_offline(self.root, start="2026-10-13T22:37:00Z")
        self.assertEqual(data(), before)  # no value rows added
        second = [r for r in rows(self.root, "coverage") if r["observed_at_utc"] >= "2026-10-13T22:37"]
        self.assertTrue(second and all(r["changed"] == "no" and r["outcome"] == "ok" for r in second))  # every look confirmed
        self.assertEqual(sorted((self.root / "raw").rglob("*.gz")), raw_before)  # same bytes, same files
        self.assertEqual(len(self.manifests()), 2)  # but the second look is on record

    def test_revision_is_a_new_row_not_an_edit(self):
        run_offline(self.root, start="2026-10-13T21:37:00Z")
        revised = live("ornn__home.html").replace(b"$3.17", b"$3.18")
        run_offline(self.root, FakeNet(bodies={ornn.PAGE_URL: revised}), start="2026-10-14T21:37:00Z")
        h100 = [r for r in rows(self.root, "observations") if r["series_id"] == "OCPI-H100"]
        self.assertEqual([(r["price_usd_per_gpu_hr"], r["as_of"], r["observed_at_utc"][:10]) for r in h100],
                         [("3.1700", "2026-10-13", "2026-10-13"), ("3.1800", "2026-10-13", "2026-10-14")])
        with open(self.root / "revisions.csv") as fh:
            rev = [r for r in csv.DictReader(fh) if r["series"] == "OCPI-H100"]
        self.assertEqual([r["value"] for r in rev], ["3.1700", "3.1800"])
        with open(self.root / "divergence.csv") as fh:  # the first-published value, flagged as revised later
            [d] = [r for r in csv.DictReader(fh) if r["date"] == "2026-10-13"]
        self.assertEqual((d["ornn_usd"], d["ornn_from"], d["revised_later"]), ("3.1700", "page", "yes"))

    def test_value_changing_back_is_recorded(self):
        # A -> B -> A: a "first time seen" rule would drop the return to A and show B forever.
        page = live("ornn__home.html")
        for day, price in (("13", b"$3.17"), ("14", b"$3.18"), ("15", b"$3.17")):
            run_offline(self.root, FakeNet(bodies={ornn.PAGE_URL: page.replace(b"$3.17", price)}),
                        start=f"2026-10-{day}T22:17:00Z", argv=("--only", "ornn"))
        h100 = [r["price_usd_per_gpu_hr"] for r in rows(self.root, "observations") if r["series_id"] == "OCPI-H100"]
        self.assertEqual(h100, ["3.1700", "3.1800", "3.1700"])

    def test_months_partition(self):
        run_offline(self.root, start="2026-10-31T21:37:00Z")
        run_offline(self.root, FakeNet(bodies={ornn.PAGE_URL: live("ornn__home.html").replace(b"$3.17", b"$3.20")}),
                    start="2026-11-01T21:37:00Z")
        self.assertEqual(sorted(p.name for p in (self.root / "tables" / "observations").glob("*.csv")),
                         ["2026-10.csv", "2026-11.csv"])
        self.assertEqual(verify.verify(self.root), [])


class TestRebuildAndVerify(_Base):
    def setUp(self):
        super().setUp()
        run_offline(self.root)

    def test_rebuild_is_byte_identical(self):
        before = tables_bytes(self.root)
        shutil.rmtree(self.root / "tables")
        self.assertEqual(build.build(self.root), [])
        self.assertEqual(tables_bytes(self.root), before)

    def test_edited_table_detected(self):
        p = next((self.root / "tables" / "observations").glob("*.csv"))
        p.write_text(p.read_text().replace("3.1700", "3.0000"))
        self.assertTrue(any("differs from a rebuild" in i for i in verify.verify(self.root)))

    def test_corrupted_raw_detected(self):
        p = next((self.root / "raw").rglob("ornn__*.gz"))
        p.write_bytes(gzip.compress(b"tampered", mtime=0))
        issues = verify.verify(self.root)
        self.assertTrue(any("does not match" in i for i in issues))

    def test_unexplained_raw_detected(self):
        store.save_raw(self.root, "2026-10-13", "ornn", "home", b"bytes no manifest mentions", "html")
        self.assertTrue(any("not referenced by any manifest" in i for i in verify.verify(self.root)))

    def test_manifest_hash_mismatch_detected(self):
        p = next((self.root / "runs").glob("*.json"))
        m = json.loads(p.read_text())
        m["fetches"][0]["sha256"] = "0" * 64
        p.write_text(json.dumps(m))
        self.assertTrue(any("does not match sha256" in i for i in verify.verify(self.root)))

    def test_row_pointing_at_unknown_snapshot_detected(self):
        p = next((self.root / "tables" / "observations").glob("*.csv"))
        lines = p.read_text().splitlines()
        lines[1] = lines[1][:-64] + "f" * 64
        p.write_text("\n".join(lines) + "\n")
        self.assertTrue(any("is not a fetched snapshot" in i for i in verify.verify(self.root)))

    def test_command_line_exit_codes(self):
        with mock.patch("sys.stderr"):
            self.assertEqual(verify.main(["--data-dir", str(self.root)]), 0)
            self.assertEqual(build.main(["--data-dir", str(self.root)]), 0)
            next((self.root / "tables" / "observations").glob("*.csv")).write_text("tampered\n")
            self.assertEqual(verify.main(["--data-dir", str(self.root)]), 1)

    def test_prefix_collision_gets_full_hash_name(self):
        path, digest = store.save_raw(self.root, "2026-10-13", "x", "y", b"abc", "html")
        (self.root / path).write_bytes(gzip.compress(b"other", mtime=0))
        path2, _ = store.save_raw(self.root, "2026-10-13", "x", "y", b"abc", "html")
        self.assertIn(digest, path2)
        self.assertEqual(store.read_raw(self.root, path2, digest), b"abc")

    def test_build_crash_still_writes_manifest(self):
        with mock.patch("cim.build.build", side_effect=RuntimeError("boom")), self.assertRaises(RuntimeError):
            run_offline(self.root, start="2026-10-14T21:37:00Z")
        self.assertEqual(self.manifests()[-1]["fatal"], "RuntimeError: boom")

    def test_parse_failure_does_not_block_other_snapshots(self):
        with mock.patch.object(ornn, "parse", side_effect=RuntimeError("boom")):
            problems = build.build(self.root)
        self.assertTrue(problems and "boom" in problems[0])
        self.assertEqual(len(rows(self.root, "observations")), 6)  # ornn's gone, silicondata's intact


class TestFreshness(_Base):
    def test_overdue_sources_flagged(self):
        run_offline(self.root, start="2026-10-13T22:17:00Z")
        self.assertEqual(verify.stale(self.root, datetime(2026, 10, 14, 4, 0, tzinfo=timezone.utc)), [])
        later = verify.stale(self.root, datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc))
        self.assertEqual([s.split(":")[0] for s in later], ["kalshi"])  # 3-hourly source, 10h limit
        much_later = verify.stale(self.root, datetime(2026, 10, 15, 6, 0, tzinfo=timezone.utc))
        self.assertEqual(sorted(s.split(":")[0] for s in much_later),
                         ["docs", "getdeploying", "kalshi", "kalshi_settled", "ornn", "silicondata"])

    def test_kalshi_only_run_keeps_daily_sources_fresh_check(self):
        run_offline(self.root, start="2026-10-13T22:17:00Z")
        run_offline(self.root, start="2026-10-14T23:07:00Z", argv=("--only", "kalshi"))
        stale = verify.stale(self.root, datetime(2026, 10, 15, 5, 0, tzinfo=timezone.utc))
        self.assertEqual(sorted(s.split(":")[0] for s in stale),
                         ["docs", "getdeploying", "kalshi_settled", "ornn", "silicondata"])  # the daily run stopped


class TestFailures(_Base):
    def test_one_source_down_others_recorded(self):
        for code in (403, 429, 500):
            with self.subTest(code=code):
                self.setUp()
                net = FakeNet(fail={"getdeploying": code})
                self.assertEqual(run_offline(self.root, net), 0)  # a warning; freshness alerts if it lasts
                [m] = self.manifests()
                self.assertEqual(m["degraded"], ["getdeploying"])
                fs = [f for f in m["fetches"] if f["source"] == "getdeploying"]
                self.assertTrue(all("sha256" not in f and f"HTTP {code}" in f["error"] for f in fs))
                self.assertEqual(net.calls["getdeploying"], len(getdeploying.SLUGS))  # HTTP errors are never retried
                self.assertEqual(len(rows(self.root, "observations")), 11)
                self.assertEqual(verify.verify(self.root), [])

    def test_everything_down(self):
        self.assertEqual(run_offline(self.root, FakeNet(fail={s: 503 for s in ("ornn", "silicondata", "kalshi", "docs", "getdeploying")})), 1)
        self.assertIn("No observations yet.", (self.root / "LATEST.md").read_text())

    def test_no_response_is_retried_with_backoff(self):
        def down(req, timeout=None):
            raise urllib.error.URLError("no route")
        sleeps = []
        self.assertEqual(run_offline(self.root, down, argv=("--only", "ornn"), sleeps=sleeps), 1)
        self.assertEqual(sleeps, [1, 2] * (1 + len(ornn.HISTORY_GPUS)))  # 3 attempts per request, no sleep after the last

    def test_unparseable_kalshi_body_is_kept(self):
        net = FakeNet(bodies={r.url: b"<html>maintenance</html>" for r in kalshi.requests()[0]})
        run_offline(self.root, net, argv=("--only", "kalshi"))
        [m] = self.manifests()
        self.assertTrue(all(f.get("sha256") for f in m["fetches"]))
        self.assertEqual(len(m["sources"]["kalshi"]["problems"]), len(kalshi.SERIES))

    def test_job_summary(self):
        os.environ["GITHUB_STEP_SUMMARY"] = str(self.root / "summary.md")
        run_offline(self.root, FakeNet(fail={"ornn": 403}))
        self.assertIn(f"| ornn ⚠ | 0 | {1 + len(ornn.HISTORY_GPUS)} | 0 |", (self.root / "summary.md").read_text())


class TestSecrets(_Base):
    def test_malformed_key_never_sent_or_logged(self):
        for bad in ("SECRETAAAA\n1111BBBB", "short", "has space in it 123"):
            with self.subTest(bad=bad):
                self.setUp()
                os.environ["VAST_API_KEY"] = bad
                net = FakeNet()
                run_offline(self.root, net, argv=("--only", "vast"))
                self.assertEqual(net.calls["vast"], 0)
                text = next((self.root / "runs").glob("*.json")).read_text()
                self.assertIn("invalid format", text)
                self.assertNotIn("SECRET", text)

    def test_key_values_scrubbed(self):
        os.environ["ORNN_API_KEY"] = "  K3Y-VALUE-0123456789 \n"
        self.assertEqual(run.scrub("Bearer K3Y-VALUE-0123456789 failed"), "Bearer *** failed")

    def test_vast_bytes_redacted_before_saving(self):
        os.environ["VAST_API_KEY"] = "test-not-a-real-key"
        offers = {"offers": [{"gpu_name": "H100 SXM", "num_gpus": 1, "dph_total": 2.0,
                              "public_ipaddr": "1.2.3.4", "machine_id": 9}]}
        net = FakeNet(bodies={"https://console.vast.ai/api/v0/bundles/": json.dumps(offers).encode()})
        self.assertEqual(run_offline(self.root, net, argv=("--only", "vast")), 0)
        for p in (self.root / "raw").rglob("*.gz"):
            self.assertNotIn(b"1.2.3.4", gzip.decompress(p.read_bytes()))
        self.assertEqual(len(rows(self.root, "vast_offers")), 1)
        self.assertNotIn("Authorization", next((self.root / "runs").glob("*.json")).read_text())


class TestReport(unittest.TestCase):
    def test_stale_rules(self):
        cases = [("silicondata", "2026-10-31", "2026-11-01", ""), ("silicondata", "2026-10-01", "2026-10-30", "stale"),
                 ("ornn", "", "2026-10-15", "unknown_as_of"), ("ornn", "Apr 2026", "2026-10-15", "unknown_as_of"),
                 ("ornn", "2026-10-09", "2026-10-12", "")]
        for src, as_of, day, want in cases:
            with self.subTest(src=src, as_of=as_of, day=day):
                self.assertEqual(report.status(src, {"as_of": as_of}, day), want)

    def test_markdown_escaped(self):
        self.assertEqual(report.md("a|b [x](y) <img>"), r"a\|b \[x\]\(y\) \<img\>")
        self.assertEqual(report.md("a‮b​c"), "abc")
        self.assertEqual(len(report.md("y" * 500)), 80)


if __name__ == "__main__":
    unittest.main()


class TestDocsAudit(_Base):
    def test_changed_terms_reported_once_history_exists(self):
        run_offline(self.root, start="2026-10-13T21:37:00Z")
        _, info = audit.audit(self.root, 24, datetime(2026, 10, 13, 22, tzinfo=timezone.utc))
        self.assertFalse([i for i in info if "vendor docs" in i])  # first capture is not a change
        changed = FakeNet({docs.PAGES["vast_terms"]: DOC_HTML.replace(b"apply", b"changed")})
        run_offline(self.root, changed, start="2026-10-15T21:37:00Z")
        _, info = audit.audit(self.root, 24, datetime(2026, 10, 15, 22, tzinfo=timezone.utc))
        self.assertIn("CHECK vendor docs changed: ['vast_terms']", info)


class TestSettlementSourceAudit(_Base):
    def test_page_vs_dashboard(self):
        now = datetime(2026, 10, 13, 22, tzinfo=timezone.utc)
        run_offline(self.root)
        self.assertEqual(audit.audit(self.root, 24, now)[0], [])
        url = ornn.HISTORY_URL.format(gpu="H100%20SXM")
        run_offline(self.root, FakeNet({url: ornn_history("H100 SXM", 3.18)}), start="2026-10-13T21:47:00Z")
        self.assertIn("Ornn page differs from its settlement source (dashboard): [('OCPI-H100', '2026-10-13')]",
                      audit.audit(self.root, 24, now)[0])


class TestAuditPageLooks(_Base):
    def test_bad_latest_look_is_a_warning_until_stale(self):
        now = datetime(2026, 10, 14, 23, tzinfo=timezone.utc)
        bad = FakeNet({silicondata.BASE.format(slug="h100"): live("silicondata__h100.html").replace(b"aria-pressed=", b"aria-selected=")})
        run_offline(self.root, bad, start="2026-10-14T21:37:00Z")
        fail, info = audit.audit(self.root, 24, now)
        self.assertEqual(fail, [])
        self.assertTrue(any(i.startswith("WARN 1 pages not ok on their latest look") for i in info))
        self.assertEqual(verify.stale(self.root, now), [])
        run_offline(self.root, bad, start="2026-10-16T03:37:00Z")  # still refused 30 h later: data missing
        stale = verify.stale(self.root, datetime(2026, 10, 16, 4, tzinfo=timezone.utc))
        self.assertEqual([s.split(":")[0] for s in stale], ["silicondata/h100"])
        run_offline(self.root, start="2026-10-16T04:37:00Z")  # read again: fresh, and the bad looks are history
        later = datetime(2026, 10, 16, 5, tzinfo=timezone.utc)
        self.assertEqual(verify.stale(self.root, later), [])
        fail, info = audit.audit(self.root, 24, later)
        self.assertEqual(fail, [])
        n = 1 + silicondata.RETRIES
        self.assertTrue(any(i.startswith(f"{n} earlier page looks not ok, since read ok (e.g. h100") for i in info))


class TestRefetch(_Base):
    def test_refused_page_is_fetched_again(self):
        url = silicondata.BASE.format(slug="h200")
        bad = live("silicondata__h200.html").replace(b"As of", b"Updated", 1)  # no headline
        net = FakeNet({url: [bad, bad, live("silicondata__h200.html")]})
        self.assertEqual(run_offline(self.root, net, argv=("--only", "silicondata")), 0)
        [m] = self.manifests()
        self.assertEqual(len([f for f in m["fetches"] if f["name"] == "h200"]), 3)
        self.assertEqual(len([n for n in m["sources"]["silicondata"]["notes"] if n.startswith("h200: fetched again")]), 2)
        looks = [r["outcome"] for r in rows(self.root, "coverage") if r["name"] == "h200"]
        self.assertEqual(looks, ["problem", "problem", "ok"])
        self.assertIn("SD-H200", {r["series_id"] for r in rows(self.root, "observations")})
