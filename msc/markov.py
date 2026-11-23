"""The Markov cycle core, in two layers.

Layer 1, RegimeModel: a hidden Markov chain over market-cycle states
    recovery -> expansion -> late_cycle -> contraction -> recovery ...
Each signal's market-level score is evidence about which state we are in. Each signal's influence is
tempered by a reliability weight in [0, 1] (see weights.py): the log-likelihood of its reading is multiplied
by that weight, so an unproven signal barely moves the belief and a proven one moves it fully.

Layer 2, StockModel: a 3-state chain (weak / neutral / strong) per ticker. Its transition matrix is a mixture
of regime-specific matrices, weighted by the layer-1 belief, so the market cycle tilts every stock's odds.

Starting values (state means, transition probabilities, P(up)) are ASSUMPTIONS, not findings. `fit` re-estimates
them from stored history with expectation-maximisation, anchored to the starting values by pseudo-counts.
"""
import math

REGIMES = ("recovery", "expansion", "late_cycle", "contraction")
STOCK_STATES = ("weak", "neutral", "strong")

DEFAULT_MU = {"recovery": 0.30, "expansion": 0.50, "late_cycle": 0.0, "contraction": -0.50}
DEFAULT_P_UP = {"recovery": 0.62, "expansion": 0.60, "late_cycle": 0.52, "contraction": 0.42}
SQRT_2PI_LOG = 0.9189385332046727


def logpdf(x, mu, sd):
    return -0.5 * ((x - mu) / sd) ** 2 - math.log(sd) - SQRT_2PI_LOG


def normalize(v):
    s = sum(v)
    return [x / s for x in v] if s > 0 else [1.0 / len(v)] * len(v)


def posterior(prior, loglik):
    """Bayes update in log space (stable for large likelihood gaps)."""
    top = max(loglik)
    return normalize([p * math.exp(ll - top) for p, ll in zip(prior, loglik)])


def predict(belief, A):
    n = len(belief)
    return [sum(belief[i] * A[i][j] for i in range(n)) for j in range(n)]


def cyclic_transitions(stay=0.94, forward=0.05, n=4):
    rest = (1.0 - stay - forward) / (n - 2)
    A = [[rest] * n for _ in range(n)]
    for i in range(n):
        A[i][i], A[i][(i + 1) % n] = stay, forward
    return A


class RegimeModel:
    def __init__(self, A=None, mu=None, sd=None, p_up=None):
        self.A = A or cyclic_transitions()
        self.mu = mu or {}                 # signal -> [mean score per regime]
        self.sd = sd or {}
        self.p_up = p_up or [DEFAULT_P_UP[r] for r in REGIMES]
        self.pi = [0.25] * 4

    def emission(self, signal):
        return (self.mu.get(signal) or [DEFAULT_MU[r] for r in REGIMES],
                self.sd.get(signal) or [0.45] * 4)

    def loglik(self, obs):
        """obs: {signal: (score, weight)} -> log-likelihood of the readings under each regime."""
        ll = [0.0] * 4
        for sig, (x, w) in obs.items():
            mu, sd = self.emission(sig)
            for s in range(4):
                ll[s] += w * logpdf(x, mu[s], sd[s])
        return ll

    def step(self, belief, obs):
        """One day: let the chain move, then absorb the readings."""
        pred = predict(belief, self.A)
        return posterior(pred, self.loglik(obs)) if obs else pred

    def forecast_p_up(self, belief):
        nxt = predict(belief, self.A)
        return sum(b * p for b, p in zip(nxt, self.p_up))

    # ---------------------------------------------------------------- learning
    def smooth(self, seq):
        """Forward-backward over a history. seq: list of obs dicts. Returns (gammas, xis)."""
        T = len(seq)
        lik = []
        for obs in seq:
            ll = self.loglik(obs)
            top = max(ll)
            lik.append([math.exp(x - top) for x in ll])
        alpha, scale = [], []
        for t in range(T):
            a = [self.pi[s] * lik[t][s] for s in range(4)] if t == 0 else \
                [sum(alpha[t - 1][i] * self.A[i][j] for i in range(4)) * lik[t][j] for j in range(4)]
            c = sum(a) or 1e-300
            alpha.append([x / c for x in a])
            scale.append(c)
        beta = [[1.0] * 4 for _ in range(T)]
        for t in range(T - 2, -1, -1):
            b = [sum(self.A[i][j] * lik[t + 1][j] * beta[t + 1][j] for j in range(4)) for i in range(4)]
            c = sum(b) or 1e-300
            beta[t] = [x / c for x in b]
        gammas = [normalize([alpha[t][s] * beta[t][s] for s in range(4)]) for t in range(T)]
        xis = []
        for t in range(T - 1):
            m = [[alpha[t][i] * self.A[i][j] * lik[t + 1][j] * beta[t + 1][j] for j in range(4)] for i in range(4)]
            tot = sum(sum(r) for r in m) or 1e-300
            xis.append([[x / tot for x in r] for r in m])
        return gammas, xis

    def fit(self, seq, weights=None, iters=15, emission_prior=20.0, transition_prior=60.0, sd_floor=0.2):
        """MAP expectation-maximisation. `seq` = list of {signal: (score, weight)}. Returns the final gammas."""
        if len(seq) < 10:
            return None
        base_A = [row[:] for row in cyclic_transitions()]
        signals = sorted({s for obs in seq for s in obs})
        gammas = None
        for _ in range(iters):
            gammas, xis = self.smooth(seq)
            for i in range(4):
                row = [transition_prior * base_A[i][j] + sum(x[i][j] for x in xis) for j in range(4)]
                self.A[i] = normalize(row)
            for sig in signals:
                mu0, sd0 = [DEFAULT_MU[r] for r in REGIMES], [0.45] * 4
                new_mu, new_sd = [], []
                for s in range(4):
                    num = den = 0.0
                    pts = [(g[s] * obs[sig][1], obs[sig][0]) for g, obs in zip(gammas, seq) if sig in obs]
                    for w, x in pts:
                        num, den = num + w * x, den + w
                    m = (num + emission_prior * mu0[s]) / (den + emission_prior)
                    var = (sum(w * (x - m) ** 2 for w, x in pts) + emission_prior * sd0[s] ** 2) / (den + emission_prior)
                    new_mu.append(m)
                    new_sd.append(max(sd_floor, math.sqrt(var)))
                self.mu[sig], self.sd[sig] = new_mu, new_sd
        return gammas

    def fit_p_up(self, gammas, ups, prior=20.0):
        """P(market up over the horizon | regime), from smoothed regime beliefs and realised outcomes."""
        for s in range(4):
            num = sum(g[s] * u for g, u in zip(gammas, ups))
            den = sum(g[s] for g in gammas)
            self.p_up[s] = (num + prior * DEFAULT_P_UP[REGIMES[s]]) / (den + prior)

    def to_dict(self):
        return {"A": self.A, "mu": self.mu, "sd": self.sd, "p_up": self.p_up}

    @classmethod
    def from_dict(cls, d):
        return cls(d.get("A"), d.get("mu"), d.get("sd"), d.get("p_up"))


class StockModel:
    """Weak / neutral / strong per ticker, tilted by the market regime."""
    MU = [-0.5, 0.0, 0.5]
    SD = 0.5
    STAY = 0.80
    REGIME_DRIFT = {"recovery": 0.04, "expansion": 0.03, "late_cycle": 0.0, "contraction": -0.05}

    def regime_matrix(self, regime):
        b = self.REGIME_DRIFT[regime]
        up = [0.10 + b, 0.10 + b, 0.0]            # chance of moving one step up, from weak / neutral / strong
        down = [0.0, 0.10 - b, 0.10 - b]
        A = []
        for i in range(3):
            row = [0.0] * 3
            if i < 2:
                row[i + 1] = max(0.01, up[i])
            if i > 0:
                row[i - 1] = max(0.01, down[i])
            row[i] = 1.0 - sum(row)
            A.append(row)
        return A

    def effective_matrix(self, regime_belief):
        mats = [self.regime_matrix(r) for r in REGIMES]
        return [[sum(b * m[i][j] for b, m in zip(regime_belief, mats)) for j in range(3)] for i in range(3)]

    def step(self, belief, obs, regime_belief):
        """obs: {signal: (score, weight)} (empty = no news today, belief just drifts)."""
        pred = predict(belief, self.effective_matrix(regime_belief))
        if not obs:
            return pred
        ll = [sum(w * logpdf(x, self.MU[s], self.SD) for x, w in obs.values()) for s in range(3)]
        return posterior(pred, ll)

    @staticmethod
    def tilt(belief):
        return belief[2] - belief[0]
