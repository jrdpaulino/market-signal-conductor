import os
import unittest
from unittest import mock

from msc import version
from msc.backfill import signal_errors
from msc.execution.gate import running_base_brier, track_record
from msc.store import Store


class RunningBaseRateTests(unittest.TestCase):
    def test_uses_only_outcomes_already_known(self):
        # forecast on day 1 can't see its own outcome (exit day 5) -> says 0.5
        rows = [("d01", "d05", 1), ("d02", "d06", 1), ("d07", "d11", 0)]
        # d1: 0.5 -> .25; d2: 0.5 -> .25; d7: known outcomes d5,d6 = both 1 -> base 1.0 vs 0 -> 1.0
        self.assertAlmostEqual(running_base_brier(rows), (0.25 + 0.25 + 1.0) / 3)

    def test_track_record_reports_it_and_gate_benchmark_unchanged(self):
        fc = [(f"2026-01-{i:02d}", 20, 0.6, "x") for i in range(1, 11)]
        t = track_record(fc, lambda d, h: 1, exit_of=lambda d, h: "2026-02-01")
        self.assertAlmostEqual(t["brier_base"], 0.0)                 # hindsight base rate is perfect when all ups
        self.assertAlmostEqual(t["brier_running"], 0.25)             # nothing known yet at forecast time
        self.assertIn("brier_running", track_record(fc, lambda d, h: None))

    def test_without_exit_days_no_running_figure(self):
        self.assertIsNone(track_record([("d", 20, 0.5, "x")], lambda d, h: 1)["brier_running"])


class VersionAndErrorsTests(unittest.TestCase):
    def test_env_overrides(self):
        with mock.patch.dict(os.environ, {"MSC_COMMIT": "abc1234"}):
            self.assertEqual(version.commit_id(), "abc1234")

    def test_never_raises(self):
        with mock.patch.dict(os.environ, {"MSC_COMMIT": ""}), mock.patch("subprocess.run", side_effect=OSError):
            self.assertEqual(version.commit_id(), "unknown")

    def test_signal_errors_grouped(self):
        s = Store(":memory:")
        s.set_health("2026-01-01", "sig", "error", 0, "RuntimeError: need 250 observations, have 10")
        s.set_health("2026-01-02", "sig", "error", 0, "RuntimeError: need 250 observations, have 10")
        s.set_health("2026-01-03", "sig", "ok", 1)
        s.set_health("2026-01-04", "sig", "error", 0, "RuntimeError: need 250 observations, have 11")
        out = signal_errors(s)
        self.assertEqual(len(out["sig"]), 1)                       # the changing count is folded into one line
        self.assertEqual(out["sig"][0]["days"], 3)
        self.assertEqual((out["sig"][0]["first"], out["sig"][0]["last"]), ("2026-01-01", "2026-01-04"))


if __name__ == "__main__":
    unittest.main()


class PlaceboTests(unittest.TestCase):
    def _store(self, prices, horizon, skill):
        import random
        from msc import weights as W
        from msc.models import Observation
        rng = random.Random(5)
        days = [p[0] for p in prices]
        rets = W.forward_returns(prices, days, horizon)
        s = Store(":memory:")
        obs = []
        for d, r in zip(days, rets):
            real = 0.0 if r is None else max(-1, min(1, r * 15))
            obs.append(Observation("good", d, "market", "", max(-1, min(1, skill * real + rng.gauss(0, .4))), 1.0, {}))
            obs.append(Observation("noise", d, "market", "", rng.uniform(-1, 1), 1.0, {}))
        s.put_obs(obs)
        return s

    def test_real_signal_beats_placebos_and_noise_does_not(self):
        from msc import weights as W
        from tests.test_core import make_prices
        prices = make_prices(800, seed=3)
        s = self._store(prices, 20, skill=1.0)
        out = W.placebo_test(s, prices, 20, k=100)
        by = {r["signal"]: r for r in out["results"]}
        self.assertLess(by["good"]["p"], 0.05)
        self.assertGreater(by["noise"]["p"], 0.05)
        self.assertEqual(out["signals_tested"], 2)
        self.assertGreaterEqual(by["good"]["p_adjusted"], by["good"]["p"])

    def test_too_little_data_tests_nothing(self):
        from msc import weights as W
        from tests.test_core import make_prices
        prices = make_prices(200, seed=3)
        out = W.placebo_test(self._store(prices, 20, 1.0), prices, 20, k=10)
        self.assertEqual(out["signals_tested"], 0)

    def test_credit_needs_fewer_readings(self):
        from msc.signals.macro_fred import stress_score
        vals = [3.0 + 0.01 * (i % 7) for i in range(130)]
        stress_score(vals, 63, min_history=120)
        with self.assertRaises(RuntimeError):
            stress_score(vals[:100], 63, min_history=120)


class LogFileTests(unittest.TestCase):
    def test_write_log_names_file_with_commit_and_returns_path(self):
        import tempfile
        from msc.logs import write_log
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"MSC_LOG_DIR": d, "MSC_COMMIT": "abc1234"}):
            path = write_log("backfill", "hello")
            self.assertTrue(path.endswith("-abc1234.txt") and os.path.basename(path).startswith("backfill-"))
            self.assertEqual(open(path, encoding="utf-8").read(), "hello\n")

    def test_backfill_summary_is_one_line(self):
        from msc.__main__ import backfill_summary
        r = dict(commit="abc", scored_forecasts=5, hit_rate=0.6, brier=0.24, brier_running_base_rate=0.25,
                 brier_base_rate=0.23, gate_would_say="fail", placebo="off")
        self.assertNotIn("\n", backfill_summary("run", r))


class SpeedupEquivalenceTests(unittest.TestCase):
    def test_bulk_weights_match_the_one_day_at_a_time_calculation(self):
        import random
        from msc import weights as W
        from msc.models import Observation
        from tests.test_core import make_prices
        prices = make_prices(300, seed=4)
        rng = random.Random(2)
        s = Store(":memory:")
        s.put_obs([Observation("sig", p[0], "market", "", rng.uniform(-1, 1), 1.0, {}) for p in prices])
        today = prices[250][0]
        known = [p for p in prices if p[0] <= today]
        xs, ys = [], []
        for d, sc, _c in s.market_series("sig"):
            if d < today and (r := W.forward_return(known, d, 20)) is not None:
                xs.append(sc)
                ys.append(r)
        self.assertEqual(W.compute_weights(s, prices, 20, today)["sig"], W.weight_from_pairs(xs, ys))

    def test_fred_parse_cache_and_cutoff(self):
        from msc.signals.macro_fred import parse_fred_csv
        text = "DATE,X\n2026-01-01,1\n2026-01-02,2\n"
        self.assertEqual(parse_fred_csv(text), [("2026-01-01", 1.0), ("2026-01-02", 2.0)])
        self.assertIs(parse_fred_csv(text), parse_fred_csv(text))

    def test_fast_store_still_stores(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            s = Store(os.path.join(d, "x.sqlite"), fast=True)
            s.kv_set("k", 1)
            self.assertEqual(s.kv_get("k"), 1)
            s.close()
