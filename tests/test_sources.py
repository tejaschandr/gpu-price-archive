"""Parsers against synthetic pages in each source's layout, and the page changes each must refuse.

A parser that can't be sure
must return nothing and say why: a wrong value is worse than a missing one.
"""

import json
from pathlib import Path
import time
import unittest
from unittest import mock

from cim.sources import docs, getdeploying, kalshi, ornn, semianalysis, silicondata, vast
from helpers import DOC_HTML, DOCS_MD, GD_H100, live, ornn_history


def sd(slug, body=None):
    return silicondata.parse_page(body if body is not None else live(f"silicondata__{slug}.html"),
                                  silicondata.PAGES[slug][2])


def hyperscaler_headline(body: bytes) -> bytes:
    """The H100 page with the rendered headline showing the Hyperscaler value (7.15)."""
    k = body.index(b"As of Oct 13, 2026</p>")
    return body[:k] + body[k:].replace(b">2.85<", b">7.15<", 1)


class TestOrnn(unittest.TestCase):
    BODY = live("ornn__home.html")

    def test_values(self):
        rows, problems = ornn.parse("home", self.BODY, ornn.PAGE_URL)
        got = {r["series_id"]: (r["price_usd_per_gpu_hr"], r["as_of"]) for r in rows["observations"]}
        self.assertEqual(got, {"OCPI-H100": ("3.1700", "2026-10-13"), "OCPI-H200": ("5.5100", "2026-10-13"),
                               "OCPI-B200": ("7.8300", "2026-10-13"), "OCPI-A100": ("0.9600", "2026-10-13"),
                               "OCPI-RTX6K": ("1.4000", "2026-10-13")})
        self.assertEqual(problems, [])

    def test_layout_2026_10_19_still_parses(self):
        rows, problems = ornn.parse("home", live("ornn__home_20261019.html"), ornn.PAGE_URL)
        self.assertEqual(len(rows["observations"]), 5)
        self.assertEqual(problems, [])

    def test_undated_rows_need_one_updated_date(self):
        two = self.BODY.replace(b"</body>", b"<p>Updated October 12, 2026 at 20:00 UTC</p></body>")
        rows, problems = ornn.parse("home", two, ornn.PAGE_URL)
        self.assertEqual([r["method"] for r in rows["observations"]], ["public page meta description (card not found)"])

    def test_layout_before_2026_10_19_still_parses(self):
        """Archived snapshots must rebuild unchanged: the 4-GPU page without 7-day changes."""
        rows, problems = ornn.parse("home", live("ornn__home_20261013.html"), ornn.PAGE_URL)
        self.assertEqual({r["series_id"]: r["price_usd_per_gpu_hr"] for r in rows["observations"]},
                         {"OCPI-H100": "3.1700", "OCPI-H200": "5.5100", "OCPI-B200": "7.8300", "OCPI-A100": "0.9600"})
        self.assertEqual(problems, [])

    def test_conflicting_copies_drop_the_gpu(self):
        body = self.BODY.replace(b'">$5.51</td>', b'">$5.61</td>')
        rows, problems = ornn.parse("home", body, ornn.PAGE_URL)
        self.assertNotIn("OCPI-H200", {r["series_id"] for r in rows["observations"]})
        self.assertIn("H200", problems[0])

    def test_new_series_reported(self):
        body = self.BODY.replace(b"</body>", b"<p>OCPI-B300 OCPI-H100-PCIE</p></body>")
        _, problems = ornn.parse("home", body, ornn.PAGE_URL)
        self.assertIn("OCPI-B300, OCPI-H100-PCIE", problems[-1])

    def test_meta_fallback_is_labelled(self):
        body = (b'<html><head><meta name="description" content="H100 SXM at $3.17 per GPU-hour, settled '
                b'October 13, 2026 on the Ornn Compute Price Index"/></head><body></body></html>')
        rows, problems = ornn.parse("home", body, ornn.PAGE_URL)
        self.assertEqual(rows["observations"][0]["method"], "public page meta description (card not found)")
        self.assertIn("fallback", problems[0])



class TestOrnnHistory(unittest.TestCase):
    def test_points(self):
        rows, problems = ornn.parse("history_h100", ornn_history("H100 SXM", 3.17, days=3), "u")
        self.assertEqual([(r["gpu"], r["date"], r["price"]) for r in rows["ornn_history"]],
                         [("H100 SXM", d, "3.1700") for d in ("2026-10-11", "2026-10-12", "2026-10-13")])
        self.assertEqual(problems, [])

    def test_refuses_failures_and_odd_points(self):
        for body in (b'{"success": false, "data": []}', b'{"success": true, "data": []}',
                     b'{"success": true, "data": [{"timestamp": "2026-10-13", "index_value": 3.17}]}',
                     b'{"success": true, "data": [{"timestamp": "2026-10-13T20:00:00Z", "index_value": "3.17"}]}'):
            rows, problems = ornn.parse("history_h100", body, "u")
            self.assertEqual((rows["ornn_history"], len(problems)), ([], 1), body)


class TestSiliconData(unittest.TestCase):
    EXPECTED = {"h100": (2.85, 2.53, "neocloud"), "a100": (1.52, 1.29, "neocloud"), "h200": (3.38, 3.10, ""),
                "b200": (5.95, 5.45, ""), "b300": (6.65, 6.95, ""), "mi300x": (2.64, 2.12, "")}

    def test_values(self):
        for slug, (price, faq, segment) in self.EXPECTED.items():
            with self.subTest(slug):
                self.assertEqual(sd(slug), {"as_of": "2026-10-13", "price": price, "faq_price": faq, "segment": segment})

    def test_faq_disagreement_recorded_in_notes(self):
        rows, _ = silicondata.parse("h100", live("silicondata__h100.html"), "u")
        self.assertEqual(rows["observations"][0]["notes"], "page_faq_states=2.53")

    def test_refuses_layout_changes(self):
        h100 = live("silicondata__h100.html")
        hyper = hyperscaler_headline(h100)
        pressed = h100.replace(b'aria-pressed="true"', b'aria-pressed="X"', 1) \
                      .replace(b'aria-pressed="false"', b'aria-pressed="true"', 1).replace(b'aria-pressed="X"', b'aria-pressed="false"', 1)
        i = h100.index(b'aria-pressed="true"')
        pair = h100[h100.rindex(b"<button", 0, i):h100.index(b"</button>", h100.index(b"Hyperscaler", i)) + 9]
        toggle = b'<button aria-pressed="true">Neo-Cloud</button><button aria-pressed="false">Hyperscaler</button>'
        cases = {
            "hyperscaler tab pressed": ("h100", pressed),
            "toggle markup changed to aria-selected": ("h100", hyper.replace(b"aria-pressed=", b"aria-selected=")),
            "toggle labels wrapped": ("h100", hyper.replace(b">Neo-Cloud<", b"><span>Neo-Cloud</span><")),
            "toggle removed": ("h100", hyper.replace(b">Neo-Cloud<", b"><").replace(b">Hyperscaler<", b"><")),
            "stale toggle copy elsewhere": ("h100", hyper.replace(pair, pair.replace(b"aria-pressed", b"aria-selected"), 1)
                                            .replace(b"<body", b"<noscript>" + pair + b"</noscript><body", 1)),
            "single-reading page gains a toggle": ("h200", live("silicondata__h200.html").replace(b"<body", toggle + b"<body", 1)),
            "two headlines": ("h200", live("silicondata__h200.html").replace(
                b"</body>", b"<p>As of Oct 13, 2026</p><p>9.99</p><p>USD / GPU-hour</p></body>", 1)),
        }
        for name, (slug, body) in cases.items():
            with self.subTest(name):
                self.assertIsNone(sd(slug, body))
                rows, problems = silicondata.parse(slug, body, "u")
                self.assertNotIn("observations", rows)
                self.assertIn("does not match expected layout", problems[0])

    def test_history(self):
        by = {}
        for t, d, p in silicondata.parse_history(live("silicondata__h100.html")):
            by.setdefault(t, {})[d] = p
        self.assertEqual({k: len(v) for k, v in by.items()}, {"H100(SDH100RT)": 30, "H100-hyperscaler": 30})
        self.assertEqual((by["H100(SDH100RT)"]["2026-09-14"], by["H100(SDH100RT)"]["2026-10-13"],
                          by["H100-hyperscaler"]["2026-09-14"]), (2.66, 2.85, 7.17))

    def test_regexes_linear_on_hostile_input(self):
        for body in ('aria-pressed="true">' * 100_000, 'aria-pressed="true"' * 100_000,
                     '"title":"x"' + " " * 1990 + '"points":[' + "{" * 50_000):
            t = time.perf_counter()
            silicondata.parse_page(body.encode(), True)
            silicondata.parse_history(body.encode())
            self.assertLess(time.perf_counter() - t, 1.0)


class TestSemiAnalysis(unittest.TestCase):
    BODY = live("semianalysis__index.html").decode()
    CELL = '<span class="font-semibold">$<!-- -->2.82</span>'

    def test_values(self):
        rows, problems = semianalysis.parse("index", self.BODY.encode(), semianalysis.URL)
        got = {r["series_id"]: (r["price_usd_per_gpu_hr"], r["as_of"]) for r in rows["observations"]}
        self.assertEqual(got, {"SA-H100-COMPOSITE": ("2.8200", "Apr 2026"), "SA-B200-COMPOSITE": ("3.6800", "Apr 2026")})
        self.assertEqual(problems, [])

    def test_renamed_column_2026_10_16(self):
        new = live("semianalysis__index_20261016.html")
        rows, problems = semianalysis.parse("index", new, semianalysis.URL)
        self.assertEqual(problems, [])
        # H100's price lost its period label: undated, so not recorded (and not a problem)
        self.assertEqual({r["series_id"]: (r["price_usd_per_gpu_hr"], r["as_of"]) for r in rows["observations"]},
                         {"SA-B200-COMPOSITE": ("3.6800", "Apr 2026")})
        ranged = new.replace(b"$<!-- -->3.68", b"$3.40\xe2\x80\x93$3.95", 1)
        self.assertTrue(any(p.startswith("B200:") for p in semianalysis.parse_page(ranged)[1]))

    def test_cell_must_be_one_price(self):
        start = self.BODY.index("$<!-- -->2.82")
        cell_open = self.BODY.rindex("<td", 0, start)
        cell = self.BODY[cell_open:self.BODY.index("</td>", start) + 5]
        for repl in ("<td><svg></svg></td>", "<td></td>", "<td>$2.10-2.70</td>"):
            with self.subTest(repl):
                found, problems = semianalysis.parse_page(self.BODY.replace(cell, repl, 1).encode())
                self.assertNotIn("H100", {r["sku"] for r in found})
                self.assertTrue(any(p.startswith("H100:") for p in problems))

    def test_ambiguous_table_or_column(self):
        t = self.BODY[self.BODY.index("<table"):self.BODY.index("</table>") + 8]
        for body in (self.BODY.replace(">SKU</th>", ">SKU</th><th>Prior Spot-Contract Composite Index</th>", 1),
                     self.BODY.replace(t, t + t, 1)):
            found, problems = semianalysis.parse_page(body.encode())
            self.assertEqual(found, [])
            self.assertIn("no single table", problems[0])


class TestKalshi(unittest.TestCase):
    MS = live("kalshi__KXH100MS.json")

    def test_live_ladder(self):
        rows, _ = kalshi.parse("KXH100MS", self.MS, "u")
        br = {r["event_ticker"]: (r["lo"], r["hi"], r["point"]) for r in rows["kalshi_brackets"]}
        self.assertEqual(br["KXH100MS-26OCT"], (2.75, 3.0, ""))
        self.assertEqual(br["KXH100MS-26NOV"], (2.75, 3.25, ""))
        self.assertEqual(br["KXH100MS-26DEC"], (3.0, "", ""))
        self.assertEqual(len(rows["kalshi_quotes"]), 15)
        self.assertNotIn("observations", rows)  # computed medians never enter observations
        self.assertTrue(all(r["point"] == "" for r in rows["kalshi_brackets"]))  # none within $0.25

    def test_settlement_formats(self):
        """"$1.21" values and Yes/No-only events used to be dropped without a word."""
        mk = lambda ev, k, v, res: {"event_ticker": ev, "floor_strike": k, "expiration_value": v, "result": res,
                                    "settlement_ts": "2026-08-28T21:35:00Z"}
        markets = [mk("KXA100WS-26AUG28", 1.0, "$1.21", "yes"), mk("KXA100WS-26AUG28", 1.5, "$1.21", "no"),
                   mk("KXB200W-26AUG28", 6.80, "Yes", "yes"), mk("KXB200W-26AUG28", 6.95, "No", "no"),
                   mk("KXH100W-26AUG28", 3.0, "garbage", "")]
        rows, problems = kalshi.settlements("X", markets)
        got = {r["event_ticker"]: (r["expiration_value"], r["bracket"]) for r in rows}
        self.assertEqual(got, {"KXA100WS-26AUG28": ("1.2100", ""), "KXB200W-26AUG28": ("", "(6.80, 6.95]")})
        self.assertIn("KXH100W-26AUG28", problems[0])

    def test_settlements(self):
        rows, _ = kalshi.parse("KXH100MS_settled", live("kalshi__KXH100MS_settled.json"), "u")
        self.assertEqual({r["event_ticker"]: r["expiration_value"] for r in rows["kalshi_settlements"]},
                         {"KXH100MS-26AUG": "2.7700", "KXH100MS-26SEP": "2.8300"})

    def ladder(self, quotes, ev="KXH100WS-26OCT16"):
        return json.dumps({"markets": [
            {"event_ticker": ev, "ticker": f"{ev}-{k}", "strike_type": "greater", "floor_strike": k,
             "close_time": "2026-10-16T20:00:00Z", "yes_bid_dollars": b, "yes_ask_dollars": a} for k, b, a in quotes]}).encode()

    def test_point_published_inside_one_step_bracket(self):
        rows, _ = kalshi.parse("KXH100WS", self.ladder([(3.0, "0.70", "0.74"), (3.25, "0.30", "0.34")]), "u")
        [b] = rows["kalshi_brackets"]
        self.assertEqual((b["point"], b["lo"], b["hi"], b["period"]), ("3.14", 3.0, 3.25, "2026-10-16"))

    def test_point_at_open_lower_bound_not_published(self):
        rows, _ = kalshi.parse("KXH100WS", self.ladder([(3.0, "0.49", "0.51"), (3.25, "0.30", "0.34")]), "u")
        self.assertEqual(rows["kalshi_brackets"][0]["point"], "")

    def test_point_inside_bracket_for_any_spread_cutoff(self):
        for cutoff in [x / 20 for x in range(2, 21)]:
            with self.subTest(cutoff=cutoff), mock.patch.object(kalshi, "MAX_SPREAD", cutoff):
                rows, _ = kalshi.parse("KXH100MS", self.MS, "u")
            for b in rows["kalshi_brackets"]:
                if b["point"]:
                    self.assertTrue(b["lo"] < float(b["point"]) <= b["hi"])

    def test_placeholders_and_last_trades_are_not_probabilities(self):
        self.assertEqual(kalshi.market_prob({"yes_bid_dollars": "0.95", "yes_bid_size_fp": "211",
                                             "yes_ask_dollars": "1.00", "yes_ask_size_fp": "0"}), (None, None))
        self.assertEqual(kalshi.market_prob({"yes_bid_dollars": "0.95", "yes_ask_dollars": "1.00"}), (None, None))
        self.assertIsNone(kalshi.market_prob({"yes_bid_dollars": "0", "yes_ask_dollars": "0", "last_price_dollars": "0.4"})[0])
        self.assertEqual(kalshi.market_prob({"yes_bid_dollars": "0.6", "yes_ask_dollars": "0.5"}), (None, None))

    def test_only_ticker_shaped_names_and_strike_ladders(self):
        good = {"event_ticker": "KXH100WS-26OCT16", "strike_type": "greater", "floor_strike": 3.0,
                "yes_bid_dollars": "0.6", "yes_ask_dollars": "0.7"}
        bad = [dict(good, event_ticker=e) for e in ("KXH100WS-https://evil.example", "KX‮EVIL", "kx lower", "X" * 65)]
        bad.append(dict(good, strike_type=None))
        self.assertEqual(set(kalshi.parse_markets([good] + bad)), {"KXH100WS-26OCT16"})


class TestVast(unittest.TestCase):
    OFFER = {"id": 1, "machine_id": 9, "host_id": 7, "public_ipaddr": "1.2.3.4", "gpu_name": "H100 SXM",
             "num_gpus": 8, "dph_total": 16.0, "reliability": 0.9973412, "geolocation": "Austin, Texas, US",
             "dlperf": 123.4567, "verification": {"host_id": 777}}

    def test_redaction(self):
        [o] = vast.redact({"offers": [self.OFFER], "extra": "1.2.3.4"})["offers"]
        self.assertEqual(o, {"gpu_name": "H100 SXM", "num_gpus": 8, "dph_total": 16.0, "reliability": 1.0,
                             "geolocation": "US"})
        for payload in ({}, {"bundles": [self.OFFER]}, {"offers": {"1": self.OFFER}}, {"offers": [None]}, []):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                vast.redact(payload)

    def test_rows_and_quantiles(self):
        sxm = [dict(self.OFFER, num_gpus=1, dph_total=p, gpu_mem_bw=2400 + p)
               for p in (2.0, 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.8)]
        bundles = [dict(sxm[0], num_gpus=n, dph_total=2.0 * n) for n in (2, 4, 8)]  # same machine, bigger bundles
        offers = sxm + bundles + [dict(self.OFFER, gpu_name="H100 PCIE", num_gpus=2, dph_total=3.6),
                                             dict(self.OFFER, gpu_name="RTX 4090")]
        rows, _ = vast.parse("h100_offers", json.dumps({"offers": offers}).encode(), "u")
        self.assertEqual(len(rows["vast_offers"]), 12)  # every listing kept as published
        self.assertNotIn("observations", rows)  # medians are computed by the report, not stored
        # One machine counts once (its 2-, 4-, 8-GPU bundles don't drag the median down);
        # PCIe has 1 offer, below MIN_OFFERS, so it gets no median.
        [(ff, (n, p25, p50, p75))] = vast.medians(rows["vast_offers"]).items()
        self.assertEqual((ff, n, round(p50, 4)), ("SXM", 8, 2.35))
        self.assertEqual(vast.parse("h100_offers", b'{"offers": []}', "u")[1][0][:20], "no H100 offers retur")


class TestKalshiSettledWindow(unittest.TestCase):
    def test_settled_requests_are_bounded_to_a_recent_window(self):
        """A series' settled list grows weekly; unbounded it would pass the 1000-market page limit."""
        import time
        from cim.sources import kalshi_settled
        with mock.patch("time.time", return_value=1792108800.0):  # 2026-10-15T00:00Z
            [r, *_] = kalshi_settled.requests()[0]
        self.assertIn(f"min_close_ts={1792108800 - 40 * 86400}", r.url)


class TestDocs(unittest.TestCase):
    MD = DOCS_MD.read_bytes()

    def test_silicondata_announcements(self):
        rows, problems = docs.parse("silicondata_announcements", self.MD, docs.PAGES["silicondata_announcements"])
        got = {r["label"]: (r["announced"], r["effective"], r["restatement"]) for r in rows["announcements"]}
        self.assertEqual(got["251203-1"], ("2025-12-03", "2025-12-04", "Yes"))  # the H100/A100 restatement
        self.assertEqual(got["260911-1"], ("2026-09-11", "2026-09-14", "No"))
        self.assertEqual((len(rows["documents"]), problems), (1, []))

    def test_announcement_format_change_is_not_a_revision(self):
        """2026-10-20: the page dropped its <Update> blocks for bold labels; same entries, same hashes."""
        new = live("docs__silicondata_announcements_20261020.md")
        url = docs.PAGES["silicondata_announcements"]
        key = lambda body: [{k: r[k] for k in ("label", "announced", "effective", "type", "restatement", "body_sha256")}
                            for r in docs.parse("silicondata_announcements", body, url)[0]["announcements"]]
        self.assertEqual(key(new), key(self.MD))
        self.assertEqual(len(key(new)), 2)

    def test_text_version_ignores_markup_and_whitespace(self):
        a = docs.parse("ornn_terms", DOC_HTML, "u")[0]["documents"][0]["text_sha256"]
        b = docs.parse("ornn_terms", DOC_HTML.replace(b"<p>", b'<p class="x">\n  '), "u")[0]["documents"][0]["text_sha256"]
        c = docs.parse("ornn_terms", DOC_HTML.replace(b"apply", b"applied"), "u")[0]["documents"][0]["text_sha256"]
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_refuses_shells_and_empty_announcements(self):
        self.assertEqual(docs.parse("ornn_terms", b"<p>Checking your browser</p>", "u")[0], {})
        rows, problems = docs.parse("silicondata_announcements", b"# Notes\n" + b"text " * 200,
                                    docs.PAGES["silicondata_announcements"])
        self.assertEqual((rows["announcements"], len(problems)), ([], 1))


class TestGetDeploying(unittest.TestCase):
    BODY = GD_H100.read_bytes()

    def test_rows(self):
        rows, problems = getdeploying.parse("nvidia-h100", self.BODY, "u")
        got = {(r["date"], r["billing_type"], r["reservation_months"]): r["median_price"] for r in rows["getdeploying_weekly"]}
        self.assertEqual(got[("2025-10-20", "ON_DEMAND", "")], "2.98")  # first data row of the 2026-10-19 file
        self.assertEqual((len(got), problems), (533, []))

    def test_refuses_changed_header_or_wrong_gpu(self):
        self.assertEqual(getdeploying.parse("nvidia-h100", self.BODY.replace(b"median_price,", b"p50,", 1), "u")[0],
                         {"getdeploying_weekly": []})
        self.assertEqual(getdeploying.parse("nvidia-h200", self.BODY, "u")[0], {"getdeploying_weekly": []})


if __name__ == "__main__":
    unittest.main()
