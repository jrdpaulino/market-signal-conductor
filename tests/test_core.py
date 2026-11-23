import math
import os
import random
import tempfile
import unittest

from msc import weights as W
from msc.execution.gate import check_gate, track_record, wilson_lower
from msc.markov import REGIMES, RegimeModel, StockModel, cyclic_transitions, normalize, posterior
from msc.models import Observation
from msc.scheduler import is_due
from msc.store import Store
from datetime import datetime, timezone


def make_prices(n=400, drift=0.0004, seed=1):
    import datetime as dt
    rng, p, out, d = random.Random(seed), 100.0, [], dt.date(2024, 1, 1)
    while len(out) < n:
        if d.weekday() < 5:
            o = p
            p *= 1 + drift + rng.gauss(0, 0.01)
            out.append((d.isoformat(), o, p))
        d += dt.timedelta(days=1)
    return out


class MarkovTests(unittest.TestCase):
    def test_transition_rows_sum_to_one(self):
        for row in cyclic_transitions():
            self.assertAlmostEqual(sum(row), 1.0)
        m = StockModel()
        for r in REGIMES:
            for row in m.regime_matrix(r):
                self.assertAlmostEqual(sum(row), 1.0)
                self.assertTrue(all(x >= 0 for x in row))

    def test_filter_follows_the_evidence(self):
        model, belief = RegimeModel(), [0.25] * 4
        for _ in range(15):
            belief = model.step(belief, {"s": (-0.5, 1.0)})
        self.assertEqual(REGIMES[belief.index(max(belief))], "contraction")
        for _ in range(40):
            belief = model.step(belief, {"s": (0.5, 1.0)})
        self.assertEqual(REGIMES[belief.index(max(belief))], "expansion")
        self.assertAlmostEqual(sum(belief), 1.0)

    def test_zero_weight_signal_does_nothing(self):
        model = RegimeModel()
        start = [0.1, 0.2, 0.3, 0.4]
        self.assertEqual([round(x, 9) for x in model.step(start, {"s": (-0.9, 0.0)})],
                         [round(x, 9) for x in model.step(start, {})])

    def test_no_evidence_drifts_along_the_cycle(self):
        model, b = RegimeModel(), [1.0, 0, 0, 0]
        for _ in range(200):
            b = model.step(b, {})
        self.assertTrue(all(0.15 < x < 0.35 for x in b))          # forgets, tends toward uniform

    def test_forward_backward_gammas_are_distributions(self):
        model = RegimeModel()
        seq = [{"s": (random.Random(i).uniform(-1, 1), 1.0)} for i in range(30)]
        gammas, xis = model.smooth(seq)
        for g in gammas:
            self.assertAlmostEqual(sum(g), 1.0)
        for x in xis:
            self.assertAlmostEqual(sum(sum(r) for r in x), 1.0)

    def test_em_recovers_planted_regimes(self):
        rng = random.Random(3)
        mu = {"recovery": 0.3, "expansion": 0.5, "late_cycle": 0.0, "contraction": -0.5}
        truth, state, seq = [], 3, []
        for t in range(600):
            if rng.random() < 0.04:
                state = (state + 1) % 4
            truth.append(state)
            seq.append({"s": (rng.gauss(mu[REGIMES[state]], 0.3), 1.0)})
        model = RegimeModel()
        gammas = model.fit(seq, iters=10)
        right = sum(max(range(4), key=lambda i: g[i]) == s for g, s in zip(gammas, truth)) / len(truth)
        self.assertGreater(right, 0.55)                            # four overlapping states: well above 25% chance
        for row in model.A:
            self.assertAlmostEqual(sum(row), 1.0)
        self.assertLess(model.mu["s"][3], model.mu["s"][1])        # contraction reads lower than expansion

    def test_too_little_history_is_not_fitted(self):
        self.assertIsNone(RegimeModel().fit([{"s": (0.1, 1.0)}] * 5))

    def test_stock_chain_regime_tilt(self):
        m, strong = StockModel(), [0.0, 0.0, 1.0]
        a = m.step(strong, {}, [0, 0, 0, 1.0])                     # contraction
        b = m.step(strong, {}, [0, 1.0, 0, 0])                     # expansion
        self.assertGreater(a[0] + a[1], b[0] + b[1])
        up = m.step([1 / 3] * 3, {"x": (0.8, 1.0)}, [0.25] * 4)
        self.assertGreater(StockModel.tilt(up), 0)

    def test_posterior_stable_with_extreme_loglik(self):
        p = posterior([0.25] * 4, [-1e6, 0.0, -1e6, -1e6])
        self.assertAlmostEqual(p[1], 1.0)

    def test_params_roundtrip(self):
        m = RegimeModel()
        m.fit([{"s": (0.2, 1.0)}] * 20, iters=2)
        m2 = RegimeModel.from_dict(m.to_dict())
        self.assertEqual(m.mu, m2.mu)


class WeightTests(unittest.TestCase):
    def test_pearson(self):
        self.assertAlmostEqual(W.pearson([1, 2, 3, 4], [2, 4, 6, 8]), 1.0)
        self.assertTrue(math.isnan(W.pearson([1, 1, 1], [1, 2, 3])))

    def test_unproven_signal_stays_at_prior(self):
        w, n, corr = W.weight_from_pairs([0.1] * 5, [0.01] * 5)
        self.assertEqual(w, W.PRIOR_WEIGHT)

    def test_predictive_signal_gains_and_backwards_signal_loses(self):
        rng = random.Random(2)
        ys = [rng.gauss(0, 0.05) for _ in range(120)]
        good = [y * 10 + rng.gauss(0, 0.1) for y in ys]
        bad = [-y * 10 + rng.gauss(0, 0.1) for y in ys]
        self.assertGreater(W.weight_from_pairs(good, ys)[0], 0.7)
        self.assertEqual(W.weight_from_pairs(bad, ys)[0], 0.0)

    def test_forward_return_is_unknown_until_it_happens(self):
        prices = make_prices(100)
        self.assertIsNone(W.forward_return(prices, prices[90][0], 20))
        self.assertIsNotNone(W.forward_return(prices, prices[10][0], 20))
        i, h = 10, 20
        self.assertAlmostEqual(W.forward_return(prices, prices[i][0], h), prices[i + h][2] / prices[i + 1][1] - 1)

    def test_compute_weights_uses_only_known_outcomes(self):
        store = Store(os.path.join(tempfile.mkdtemp(), "t.sqlite"))
        prices = make_prices(200)
        for k in range(100):
            store.put_obs([Observation("sig", prices[k][0], "market", "", 0.3)])
        early = W.compute_weights(store, prices[:110], 20, prices[109][0])["sig"]
        self.assertEqual(early[1], 90)                              # the last 10 readings had no known outcome yet


class GateTests(unittest.TestCase):
    def test_gate_closed_until_enough_good_forecasts(self):
        ok, why = check_gate(track_record([], lambda d, h: None))
        self.assertFalse(ok)
        # forecasts that vary (80% up on even days, 20% on odd) and are right about 85% of the time
        forecasts = [(f"d{i}", 20, 0.8 if i % 2 == 0 else 0.2, "x") for i in range(100)]
        skilled = lambda d, h: (1 if int(d[1:]) % 2 == 0 else 0) if int(d[1:]) % 7 else (0 if int(d[1:]) % 2 == 0 else 1)
        self.assertTrue(check_gate(track_record(forecasts, skilled))[0])
        # a constant 70% forecast cannot beat the base rate, however often it is "right"
        constant = [(f"d{i}", 20, 0.7, "x") for i in range(100)]
        self.assertFalse(check_gate(track_record(constant, lambda d, h: 1 if int(d[1:]) % 10 else 0))[0])
        # forecasts with no relationship to outcomes
        rng = random.Random(1)
        noise = [(f"d{i}", 20, rng.choice([0.3, 0.7]), "x") for i in range(100)]
        self.assertFalse(check_gate(track_record(noise, lambda d, h: random.Random(int(d[1:]) + 99).randint(0, 1)))[0])

    def test_wilson(self):
        self.assertLess(wilson_lower(55, 100), 0.55)
        self.assertEqual(wilson_lower(0, 0), 0.0)


class SchedulerTests(unittest.TestCase):
    def test_due_once_per_day_after_run_time(self):
        t = lambda h, m: datetime(2026, 11, 23, h, m, tzinfo=timezone.utc)
        self.assertFalse(is_due(t(22, 29), 22, 30, None))
        self.assertTrue(is_due(t(22, 30), 22, 30, None))
        self.assertTrue(is_due(t(23, 59), 22, 30, "2026-11-22"))      # restart catch-up
        self.assertFalse(is_due(t(23, 59), 22, 30, "2026-11-23"))


class StoreTests(unittest.TestCase):
    def test_obs_roundtrip_and_kv(self):
        s = Store(os.path.join(tempfile.mkdtemp(), "t.sqlite"))
        s.put_obs([Observation("a", "2026-01-02", "ticker", "AAPL", 0.5, 0.8, {"x": 1})])
        got = s.obs_on("2026-01-02")[0]
        self.assertEqual((got.key, got.score, got.meta), ("AAPL", 0.5, {"x": 1}))
        s.kv_set("pm:0:2026-01-01", 0.4)
        s.kv_set("pm:0:2026-01-02", 0.5)
        self.assertEqual(s.kv_prefix("pm:0:"), {"pm:0:2026-01-01": 0.4, "pm:0:2026-01-02": 0.5})

    def test_observation_clamps(self):
        o = Observation("a", "d", "market", "", 7, 3)
        self.assertEqual((o.score, o.confidence), (1.0, 1.0))
        with self.assertRaises(ValueError):
            Observation("a", "d", "weird", "", 0)


if __name__ == "__main__":
    unittest.main()
