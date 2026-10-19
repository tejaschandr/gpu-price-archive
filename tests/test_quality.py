"""Validity rules, jump alerts and Kalshi period labels."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cim import build, quality, store, verify
from cim.sources import kalshi, ornn, vast
from helpers import FakeNet, live, run_offline

ROW = {"observed_at_utc": "2026-10-14T22:17:00Z", "raw_sha256": "a" * 64}


class TestRules(unittest.TestCase):
    def obs(self, **kw):
        return {**ROW, "source": "ornn", "series_id": "OCPI-H100", "price_usd_per_gpu_hr": "3.1700",
                "as_of": "2026-10-13", **kw}

    def test_observations(self):
        self.assertIsNone(quality.invalid("observations", self.obs()))
        self.assertIsNone(quality.invalid("observations", self.obs(as_of="Apr 2026")))
        for bad in (self.obs(price_usd_per_gpu_hr="285000"), self.obs(price_usd_per_gpu_hr="0"),
                    self.obs(price_usd_per_gpu_hr="n/a"), self.obs(as_of="Oct 13"), self.obs(as_of=""),
                    self.obs(observed_at_utc="yesterday"), self.obs(raw_sha256="abc"), self.obs(notes="a\nb")):
            with self.subTest(bad=bad):
                self.assertIsNotNone(quality.invalid("observations", bad))
        self.assertIsNone(quality.invalid("observations", self.obs(source="vast", as_of="")))

    def test_other_tables(self):
        self.assertIsNotNone(quality.invalid("kalshi_quotes", {**ROW, "yes_bid": "1.5", "yes_ask": "", "last": "", "prob": ""}))
        self.assertIsNotNone(quality.invalid("kalshi_brackets", {**ROW, "lo": "3.0", "hi": "2.75"}))
        self.assertIsNotNone(quality.invalid("kalshi_settlements", {**ROW, "expiration_value": "2.8", "period": "26OCT"}))
        self.assertIsNotNone(quality.invalid("silicondata_history", {**ROW, "date": "2026-10-1", "price": "2.85"}))
        self.assertIsNotNone(quality.invalid("vast_offers", {**ROW, "price_per_gpu_hr": "2.0", "num_gpus": "0"}))

    def test_jumps(self):
        prev = [{**self.obs(), "observed_at_utc": "2026-10-13T22:17:00Z"}]
        self.assertEqual(quality.jumps([self.obs(price_usd_per_gpu_hr="3.9000")], prev), [])  # +23%
        [msg] = quality.jumps([self.obs(price_usd_per_gpu_hr="4.5000")], prev)              # +42%
        self.assertIn("OCPI-H100 moved +42%", msg)

    def test_kalshi_period_from_ticker(self):
        cases = {"KXH100MS-26NOV": "Nov 2026", "KXH100W-26OCT16": "2026-10-16", "KXA100MAX-26DEC31": "2026-12-31",
                 "KXCMECOMPUTE": "", "KXH100Q-26Q4": ""}
        for t, want in cases.items():
            self.assertEqual(kalshi.period(t), want)


class TestPipelineQuality(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("VAST_API_KEY", None)

    def test_jump_is_recorded_but_flagged(self):
        run_offline(self.root, start="2026-10-13T22:17:00Z", argv=("--only", "ornn"))
        spiked = live("ornn__home.html").replace(b"$3.17", b"$4.50")
        rc = run_offline(self.root, FakeNet(bodies={ornn.PAGE_URL: spiked}), start="2026-10-14T22:17:00Z", argv=("--only", "ornn"))
        self.assertEqual(rc, 2)
        m = store.read_manifests(self.root)[-1]
        self.assertEqual(m["degraded"], ["ornn"])
        self.assertIn("OCPI-H100 moved +42%", m["sources"]["ornn"]["jumps"][0])
        self.assertIn("4.5000", [r["price_usd_per_gpu_hr"] for r in store.read_table(self.root, "observations")])

    def test_invalid_row_never_enters_a_table(self):
        run_offline(self.root, argv=("--only", "ornn"))
        with mock.patch.object(quality, "PRICE", (0.01, 1.0)):  # every Ornn price above 1.00 is now "invalid"
            problems = build.build(self.root)
            self.assertTrue(problems and "row rejected" in problems[0])
            self.assertEqual({r["series_id"] for r in store.read_table(self.root, "observations")}, {"OCPI-A100"})
            self.assertTrue(any(i.startswith("build:") for i in verify.verify(self.root)))


class TestVastLive(unittest.TestCase):
    def test_live_response(self):
        body = live("vast__h100_offers.json")
        rows, problems = vast.parse("h100_offers", body, vast.URL)
        self.assertEqual((len(rows["vast_offers"]), problems), (14, []))
        # 11 SXM listings are 10 machines (one listed twice); PCIe has 3, below MIN_OFFERS
        m = vast.medians(rows["vast_offers"])
        self.assertEqual({ff: (n, round(p50, 4)) for ff, (n, _, p50, _) in m.items()}, {"SXM": (10, 2.1375)})
        self.assertTrue(all(set(o) <= vast.KEEP for o in json.loads(body)["offers"]))


if __name__ == "__main__":
    unittest.main()
