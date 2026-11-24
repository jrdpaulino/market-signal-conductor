"""Stronger checks of the Markov core: exact answers on tiny problems, recovery of planted structure, and skill on a
simulated market where a regime really does drive returns. These test the machinery, not whether any real signal
works."""
import itertools
import math
import random
import unittest

from msc.markov import (REGIMES, DEFAULT_MU, RegimeModel, StockModel, cyclic_transitions, logpdf, normalize)


def sample_chain(A, T, rng, start=0):
    s, out = start, []
    for _ in range(T):
        s = rng.choices(range(len(A)), weights=A[s])[0]
        out.append(s)
    return out


class ExactnessTests(unittest.TestCase):
    def setUp(self):
        self.rng = random.Random(5)
        self.m = RegimeModel()
        self.obs = [{"s": (self.rng.uniform(-1, 1), 1.0)} for _ in range(5)]

    def test_filter_equals_brute_force_enumeration(self):
        """The day-by-day filter must equal the exact posterior over all 4^6 state paths."""
        T, A = len(self.obs), self.m.A
        belief = [0.25] * 4
        for t in range(T):
            belief = self.m.step(belief, self.obs[t])
            exact = [0.0] * 4
            for path in itertools.product(range(4), repeat=t + 2):      # s_0 (before day 1) .. s_{t+1}
                p = 0.25
                for a, b in zip(path, path[1:]):
                    p *= A[a][b]
                for k in range(t + 1):
                    p *= math.exp(self.m.loglik(self.obs[k])[path[k + 1]])
                exact[path[-1]] += p
            for got, want in zip(belief, normalize(exact)):
                self.assertAlmostEqual(got, want, places=9)

    def test_smoother_equals_brute_force_marginals(self):
        T, A, pi = len(self.obs), self.m.A, self.m.pi
        gammas, xis = self.m.smooth(self.obs)
        exact = [[0.0] * 4 for _ in range(T)]
        for path in itertools.product(range(4), repeat=T):
            p = pi[path[0]]
            for a, b in zip(path, path[1:]):
                p *= A[a][b]
            for k in range(T):
                p *= math.exp(self.m.loglik(self.obs[k])[path[k]])
            for k in range(T):
                exact[k][path[k]] += p
        for k in range(T):
            for got, want in zip(gammas[k], normalize(exact[k])):
                self.assertAlmostEqual(got, want, places=9)
        for x in xis:                                                   # pair marginals are proper distributions
            self.assertAlmostEqual(sum(sum(r) for r in x), 1.0, places=9)

    def test_weight_is_exactly_a_tempering_exponent(self):
        x = 0.4
        full = self.m.loglik({"s": (x, 1.0)})
        half = self.m.loglik({"s": (x, 0.5)})
        for a, b in zip(full, half):
            self.assertAlmostEqual(b, 0.5 * a, places=12)
        b0 = [0.1, 0.2, 0.3, 0.4]
        self.assertEqual(self.m.step(b0, {"s": (0.9, 0.0)}), self.m.step(b0, {}))   # weight 0 == no evidence

    def test_cycle_matrix_is_doubly_stochastic_so_no_evidence_goes_to_uniform(self):
        A = cyclic_transitions()
        for j in range(4):
            self.assertAlmostEqual(sum(A[i][j] for i in range(4)), 1.0, places=12)
        b = [1.0, 0.0, 0.0, 0.0]
        for _ in range(2000):
            b = self.m.step(b, {})
        for v in b:
            self.assertAlmostEqual(v, 0.25, places=3)

    def test_forecast_is_a_mix_of_the_regime_probabilities(self):
        for b in ([1, 0, 0, 0], [0.25] * 4, [0, 0, 0, 1], [0.1, 0.2, 0.3, 0.4]):
            p = self.m.forecast_p_up(b)
            self.assertGreaterEqual(p, min(self.m.p_up) - 1e-12)
            self.assertLessEqual(p, max(self.m.p_up) + 1e-12)

    def test_stock_chain_rows_are_distributions_for_any_regime_mix(self):
        sm = StockModel()
        for mix in ([1, 0, 0, 0], [0, 0, 0, 1], [0.25] * 4):
            for row in sm.effective_matrix(mix):
                self.assertAlmostEqual(sum(row), 1.0, places=12)
                self.assertTrue(all(v >= 0 for v in row))


class RecoveryTests(unittest.TestCase):
    MUS = ((0.35, 0.55, 0.05, -0.55), (0.25, 0.45, -0.05, -0.45))      # same ordering the model's priors assume

    def simulate(self, T=3000, seed=11, mus=MUS, sd=0.4):
        rng = random.Random(seed)
        states = sample_chain(cyclic_transitions(), T, rng)
        seq = [{f"s{k}": (rng.gauss(mu[s], sd), 1.0) for k, mu in enumerate(mus)} for s in states]
        return states, seq

    def test_filter_finds_the_hidden_state_far_better_than_chance(self):
        states, seq = self.simulate()
        m, b, hits = RegimeModel(), [0.25] * 4, 0
        for s, obs in zip(states, seq):
            b = m.step(b, obs)
            hits += max(range(4), key=lambda i: b[i]) == s
        self.assertGreater(hits / len(states), 0.5)          # chance is 25%

    def test_em_recovers_means_ordering_and_persistence(self):
        states, seq = self.simulate()
        m = RegimeModel()
        m.fit(seq)
        for k, truth in enumerate(self.MUS):
            got = m.mu[f"s{k}"]
            self.assertEqual(sorted(range(4), key=lambda i: got[i]), sorted(range(4), key=lambda i: truth[i]))
            for g, t in zip(got, truth):
                self.assertLess(abs(g - t), 0.2)
        self.assertTrue(all(m.A[i][i] > 0.8 for i in range(4)))   # stays in a regime for a long time

    def test_pipeline_has_skill_when_a_regime_really_drives_returns(self):
        """Plant regimes; make 20-day returns depend on the regime; the forecast must beat the base rate OUT of sample.
        If this failed, the machinery would be broken. (It says nothing about real signals.)"""
        rng = random.Random(21)
        T, h = 3600, 20
        drift = {0: 0.0010, 1: 0.0006, 2: 0.0000, 3: -0.0014}      # daily mean return by regime
        states = sample_chain(cyclic_transitions(), T, rng)
        ret = [rng.gauss(drift[s], 0.009) for s in states]
        seq = [{f"s{k}": (rng.gauss(DEFAULT_MU[REGIMES[s]], 0.45), 0.9) for k in range(3)} for s in states]
        m, b, pairs = RegimeModel(), [0.25] * 4, []
        for t in range(T - h):
            b = m.step(b, seq[t])
            fwd = sum(ret[t + 1:t + 1 + h])
            pairs.append((m.forecast_p_up(b), int(fwd > 0)))
        test = pairs[T // 2:]                                       # second half only
        base = sum(o for _p, o in test) / len(test)
        brier = sum((p - o) ** 2 for p, o in test) / len(test)
        brier_base = sum((base - o) ** 2 for _p, o in test) / len(test)
        self.assertLess(brier, brier_base)


if __name__ == "__main__":
    unittest.main()
