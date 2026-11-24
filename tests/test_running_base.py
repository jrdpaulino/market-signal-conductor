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
        s.set_health("2026-01-05", "other", "error", 0, "RuntimeError: FRED BAMLH0A0HYM2: no usable data")
        out = signal_errors(s)
        self.assertEqual(out["other"][0]["error"], "RuntimeError: FRED BAMLH0A0HYM2: no usable data")   # series id not mangled
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


class TargetTests(unittest.TestCase):
    def _prices(self, closes):
        import datetime as dt
        d, out = dt.date(2026, 1, 1), []
        for c in closes:
            while d.weekday() >= 5:
                d += dt.timedelta(days=1)
            out.append((d.isoformat(), c, c))
            d += dt.timedelta(days=1)
        return out

    def test_direction_unchanged(self):
        from msc import weights as W
        pr = self._prices([100, 100, 101, 102, 103, 104, 105])
        self.assertAlmostEqual(W.forward_return(pr, pr[0][0], 3), 102 / 100 - 1)           # next open (100) to the close three days on (102)
        self.assertEqual(W.forward_returns(pr, [pr[0][0]], 3), W.forward_returns(pr, [pr[0][0]], 3, "direction"))

    def test_drawdown_flags_a_fall_of_more_than_the_threshold(self):
        from msc import weights as W
        calm = self._prices([100, 100.5, 101, 100.8, 101.2, 101.5])
        rough = self._prices([100, 99, 96, 97, 98, 99])             # closes 4% below the start
        self.assertEqual(W.forward_return(calm, calm[0][0], 4, "drawdown"), 1.0)
        self.assertEqual(W.forward_return(rough, rough[0][0], 4, "drawdown"), -1.0)
        self.assertIsNone(W.forward_return(calm, calm[-1][0], 4, "drawdown"))      # outcome not known yet

    def test_vol_down_compares_next_window_with_last_window(self):
        from msc import weights as W
        wild_then_calm = self._prices([100, 105, 99, 106, 98, 100, 100.1, 100.2, 100.1, 100.2, 100.15])
        calm_then_wild = self._prices([100, 100.1, 100.2, 100.1, 100.2, 100.15, 106, 98, 107, 97, 105])
        self.assertEqual(W.forward_return(wild_then_calm, wild_then_calm[5][0], 5, "vol_down"), 1.0)
        self.assertEqual(W.forward_return(calm_then_wild, calm_then_wild[5][0], 5, "vol_down"), -1.0)
        self.assertIsNone(W.forward_return(wild_then_calm, wild_then_calm[2][0], 5, "vol_down"))   # not a full window of history

    def test_vol_down_does_not_use_prices_past_the_horizon(self):
        from msc import weights as W
        pr = self._prices([100, 101, 99, 102, 98, 101, 100.5, 100.4, 100.6, 100.5, 100.55])
        a = W.forward_return(pr, pr[5][0], 5, "vol_down")
        b = W.forward_return(pr[:11], pr[5][0], 5, "vol_down")
        self.assertEqual(a, b)

    def test_unknown_target_is_rejected(self):
        from msc import weights as W
        with self.assertRaises(ValueError):
            W.forward_returns(self._prices([1, 2, 3]), ["2026-01-01"], 1, "moon")

    def test_backfill_runs_on_a_research_target(self):
        from dataclasses import replace
        from msc.backfill import backfill_path, run_backfill
        from tests.test_engine_web import BackfillExtrasTests
        t = BackfillExtrasTests("test_walk_forward_refit_runs_and_reports_the_breakdown")
        cfg, http, prices, days = t._setup()
        res = run_backfill(replace(cfg, target="drawdown"), http, prices, days[600], days[-1], backfill_path(cfg), step=9,
                           placebo=20)
        self.assertEqual(res["target"], "drawdown")
        self.assertGreater(res["scored_forecasts"], 20)
        self.assertIn("falls more than", res["target_means"])


class SplitAndLongCreditTests(unittest.TestCase):
    def _run(self, **kw):
        from msc.backfill import backfill_path, run_backfill
        from tests.test_engine_web import BackfillExtrasTests
        t = BackfillExtrasTests("test_walk_forward_refit_runs_and_reports_the_breakdown")
        cfg, http, prices, days = t._setup()
        return run_backfill(cfg, http, prices, days[300], days[-1], backfill_path(cfg), step=3, placebo=20, **kw)

    def test_split_reports_each_slice_and_sign_consistency(self):
        res = self._run(split=2)
        sp = res["split"]
        self.assertEqual(len(sp["parts"]), 2)
        self.assertLess(sp["parts"][0]["last_day"], sp["parts"][1]["first_day"])
        self.assertEqual(sum(p["scored_forecasts"] for p in sp["parts"]), res["scored_forecasts"])
        self.assertIn("same_sign_in_every_part", sp)

    def test_split_off_by_default_and_too_few_forecasts_is_reported(self):
        self.assertEqual(self._run()["split"], "off")
        self.assertIn("error", self._run(split=40)["split"])

    def test_signals_option_limits_the_replay(self):
        res = self._run(signal_names=["price_anchor"])
        self.assertEqual(list(res["signal_days"]), ["price_anchor"])

    def test_long_credit_signal_is_registered_and_backfillable(self):
        from msc.backfill import BACKFILL_SIGNALS
        from msc.signals.base import REGISTRY
        self.assertIn("macro_credit_long", REGISTRY)
        self.assertIn("macro_credit_long", BACKFILL_SIGNALS)


class FilePriceTests(unittest.TestCase):
    def test_parses_stooq_and_yahoo_style_files(self):
        from msc.prices import parse_price_csv
        stooq = "Date,Open,High,Low,Close,Volume\n2026-01-02,100,101,99,100.5,1\n2026-01-05,100.5,102,100,101,1\n"
        self.assertEqual(parse_price_csv(stooq), [("2026-01-02", 100.0, 100.5), ("2026-01-05", 100.5, 101.0)])
        close_only = "Date,Close\n2026-01-05,10\n2026-01-02,9\nnot,a row\n2026-01-06,\n"
        self.assertEqual(parse_price_csv(close_only), [("2026-01-02", 9.0, 9.0), ("2026-01-05", 10.0, 10.0)])
        fred = "observation_date,Close\n2026-01-02,7\n"
        self.assertEqual(parse_price_csv(fred), [("2026-01-02", 7.0, 7.0)])

    def test_bad_header_is_explained(self):
        from msc.prices import parse_price_csv
        with self.assertRaises(ValueError):
            parse_price_csv("a,b\n1,2\n")

    def test_file_source_reads_and_rereads_on_change(self):
        import tempfile, time
        from msc.prices import FilePrices
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "px.csv")
            open(p, "w").write("Date,Close\n2026-01-02,1\n")
            fp = FilePrices(p)
            self.assertEqual(len(fp()), 1)
            open(p, "w").write("Date,Close\n2026-01-02,1\n2026-01-05,2\n")
            os.utime(p, (time.time() + 5, time.time() + 5))
            self.assertEqual(len(fp()), 2)

    def test_empty_price_source_gives_a_clear_error(self):
        from msc.backfill import backfill_path, run_backfill
        from tests.test_engine_web import BackfillExtrasTests
        t = BackfillExtrasTests("test_walk_forward_refit_runs_and_reports_the_breakdown")
        cfg, http, prices, days = t._setup()
        with self.assertRaises(RuntimeError) as cm:
            run_backfill(cfg, http, lambda: [], "2020-01-01", "2021-01-01", backfill_path(cfg))
        self.assertIn("MSC_PRICE_FILE", str(cm.exception))
