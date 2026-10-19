"""Property tests for kalshi.monotone_survival and kalshi.implied_median.

Stdlib only: inputs come from a seeded random.Random, so every run checks the same cases
and a failure names its seed and case number. Run: python -m unittest discover -s tests -v
"""

import random
import unittest

from cim.sources import kalshi

SEED = 20261013
CASES = 1000


def random_ladder(rng: random.Random) -> list[tuple[float, float]]:
    """A Kalshi-like ladder: $0.25 strikes, cent probabilities, sometimes noisy or duplicated."""
    n = rng.randint(1, 15)
    start = rng.choice([1.0, 1.5, 2.0, 2.5])
    strikes = [round(start + 0.25 * i, 2) for i in range(n)]
    if rng.random() < 0.3:  # duplicate strikes (two quotes for one level)
        strikes += rng.choices(strikes, k=rng.randint(1, 3))
    kind = rng.random()
    pts = []
    for k in strikes:
        if kind < 0.4:  # arbitrary, often non-monotone
            p = round(rng.random(), 2)
        else:  # roughly decreasing with noise, like a real book
            p = min(1.0, max(0.0, round(1.0 - (k - start) / (0.25 * n) + rng.uniform(-0.2, 0.2), 2)))
        if rng.random() < 0.05:
            p = rng.choice([0.0, 0.5, 1.0])  # boundary values
        pts.append((k, p))
    return pts


class TestKalshiProperties(unittest.TestCase):
    def cases(self):
        rng = random.Random(SEED)
        for i in range(CASES):
            yield i, random_ladder(rng), rng

    def test_curve_monotone_non_increasing(self):
        for i, pts, _ in self.cases():
            probs = [p for _, p in kalshi.monotone_survival(pts)]
            with self.subTest(case=i, pts=pts):
                self.assertTrue(all(a >= b for a, b in zip(probs, probs[1:])))

    def test_curve_same_length_and_strikes(self):
        for i, pts, _ in self.cases():
            curve = kalshi.monotone_survival(pts)
            with self.subTest(case=i, pts=pts):
                self.assertEqual(len(curve), len(pts))
                self.assertEqual([k for k, _ in curve], sorted(k for k, _ in pts))

    def test_curve_preserves_total_probability_mass(self):
        # PAV replaces each block by its mean, so the sum of probabilities is unchanged.
        for i, pts, _ in self.cases():
            curve = kalshi.monotone_survival(pts)
            with self.subTest(case=i, pts=pts):
                self.assertAlmostEqual(sum(p for _, p in curve), sum(p for _, p in pts), places=9)

    def test_curve_leaves_monotone_input_alone(self):
        for i, pts, _ in self.cases():
            pts = list(dict(pts).items())  # unique strikes; duplicates are always pooled (see below)
            mono = sorted(pts)
            ps = sorted((p for _, p in mono), reverse=True)
            mono = [(k, p) for (k, _), p in zip(mono, ps)]  # strikes ascending, probs descending
            with self.subTest(case=i, pts=mono):
                self.assertEqual(kalshi.monotone_survival(mono), mono)

    def test_duplicate_strikes_pooled_to_one_value(self):
        for i, pts, _ in self.cases():
            by_k: dict[float, set] = {}
            for k, p in kalshi.monotone_survival(pts):
                by_k.setdefault(k, set()).add(p)
            with self.subTest(case=i, pts=pts):
                self.assertTrue(all(len(v) == 1 for v in by_k.values()))

    def test_median_within_strike_range(self):
        hits = 0
        for i, pts, _ in self.cases():
            med = kalshi.implied_median(pts)
            if med is None:
                continue
            hits += 1
            ks = [k for k, _ in pts]
            with self.subTest(case=i, pts=pts, med=med):
                self.assertGreaterEqual(med, min(ks))
                self.assertLessEqual(med, max(ks))
        self.assertGreater(hits, CASES // 4)  # the generator must actually exercise the crossing

    def test_median_is_where_curve_crosses_half(self):
        for i, pts, _ in self.cases():
            med = kalshi.implied_median(pts)
            if med is None:
                continue
            curve = kalshi.monotone_survival(pts)
            with self.subTest(case=i, pts=pts, med=med):
                # every strike below the median has P >= 0.5, every strike above has P <= 0.5
                self.assertTrue(all(p >= 0.5 for k, p in curve if k < med - 1e-12))
                self.assertTrue(all(p <= 0.5 for k, p in curve if k > med + 1e-12))

    def test_shuffle_invariant(self):
        for i, pts, rng in self.cases():
            shuffled = pts[:]
            rng.shuffle(shuffled)
            with self.subTest(case=i, pts=pts):
                self.assertEqual(kalshi.monotone_survival(shuffled), kalshi.monotone_survival(pts))
                self.assertEqual(kalshi.implied_median(shuffled), kalshi.implied_median(pts))

    def test_none_when_curve_never_crosses(self):
        rng = random.Random(SEED + 1)
        for i in range(500):
            n = rng.randint(2, 10)
            above = rng.random() < 0.5
            pts = [(1 + 0.25 * j, round(rng.uniform(0.51, 1.0) if above else rng.uniform(0.0, 0.49), 2))
                   for j in range(n)]
            with self.subTest(case=i, pts=pts):
                self.assertIsNone(kalshi.implied_median(pts))

    def test_edge_inputs(self):
        self.assertEqual(kalshi.monotone_survival([]), [])
        self.assertIsNone(kalshi.implied_median([]))
        self.assertIsNone(kalshi.implied_median([(3.0, 0.5)]))
        # all quotes at one strike: no interval to interpolate over
        self.assertIsNone(kalshi.implied_median([(3.0, 0.9), (3.0, 0.1)]))
        # exact 0.5 on a strike returns that strike
        self.assertEqual(kalshi.implied_median([(2.0, 0.8), (3.0, 0.5), (4.0, 0.1)]), 3.0)


if __name__ == "__main__":
    unittest.main()
