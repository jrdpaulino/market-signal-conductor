"""Reliability weights: how much each signal has earned the right to move the model.

For each signal we pair its past market-level scores with what the benchmark actually did afterwards, using
only outcomes already known on the evaluation day (no peeking). The weight rises with a positive correlation,
falls with a negative one, and sits at a low starting value until there is enough evidence.
"""
import math
import random
from bisect import bisect_right

PRIOR_WEIGHT = 0.30      # an unproven signal gets this much say
MIN_PAIRS = 12           # below this many known outcomes, stay at the prior
SHRINK_N = 30.0          # correlation is shrunk by n / (n + SHRINK_N)
SLOPE = 3.0              # how fast the weight moves with (shrunk) correlation


def pearson(x, y):
    n = len(x)
    if n < 3:
        return float("nan")
    mx, my = sum(x) / n, sum(y) / n
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    if sxx <= 0 or syy <= 0:
        return float("nan")
    return sum((a - mx) * (b - my) for a, b in zip(x, y)) / math.sqrt(sxx * syy)


TARGETS = ("direction", "drawdown", "vol_down")
DRAWDOWN_PCT = 0.03       # fixed in advance on purpose: a threshold tuned after seeing results would flatter the model
TARGET_HELP = {
    "direction": "market is up over the horizon (the default)",
    "drawdown": f"no close falls more than {DRAWDOWN_PCT:.0%} below today's close during the horizon",
    "vol_down": "realized volatility over the next horizon is lower than over the last horizon (volatility falls)",
}


def forward_return(prices, day, horizon, target="direction"):
    """The outcome value for `day`, None if not yet known. Positive means the good outcome for the target.

    direction: return from the next open after `day` to the close `horizon` days later.
    drawdown / vol_down: +1 or -1 (see TARGET_HELP), so the same correlation, Brier and calibration code works.
    prices: list of (iso_day, open, close), oldest first."""
    return forward_returns(prices, [day], horizon, target)[0]


def _daily_log_returns(prices):
    return [0.0] + [math.log(prices[k][2] / prices[k - 1][2]) if prices[k][2] > 0 and prices[k - 1][2] > 0 else 0.0
                    for k in range(1, len(prices))]


def forward_returns(prices, query_days, horizon, target="direction"):
    """[outcome value for d in query_days], building the day index once. See forward_return."""
    if target not in TARGETS:
        raise ValueError(f"unknown target {target!r}; choose from {', '.join(TARGETS)}")
    days = [p[0] for p in prices]
    if target == "vol_down":
        lr = _daily_log_returns(prices)
        c1, c2 = [0.0], [0.0]
        for x in lr:
            c1.append(c1[-1] + x)
            c2.append(c2[-1] + x * x)

        def rv(end):                       # std of the `horizon` daily returns ending at index `end`
            n = horizon
            m = (c1[end + 1] - c1[end + 1 - n]) / n
            return math.sqrt(max(0.0, (c2[end + 1] - c2[end + 1 - n]) / n - m * m))
    out = []
    for day in query_days:
        i = bisect_right(days, day) - 1
        if i < 0 or i + horizon >= len(prices):
            out.append(None)
            continue
        if target == "direction":
            entry, exit_ = prices[i + 1][1], prices[i + horizon][2]
            out.append(exit_ / entry - 1 if entry and entry > 0 else None)
        elif target == "drawdown":
            base = prices[i][2]
            if not base or base <= 0:
                out.append(None)
                continue
            worst = min(prices[k][2] for k in range(i + 1, i + horizon + 1)) / base - 1
            out.append(1.0 if worst > -DRAWDOWN_PCT else -1.0)
        else:                              # vol_down: needs one full horizon of history before `day`
            if i < horizon:
                out.append(None)
                continue
            out.append(1.0 if rv(i + horizon) < rv(i) else -1.0)
    return out


def forward_exit_days(prices, query_days, horizon):
    """For each query day, the date on which its forward return becomes known (None if not yet)."""
    days = [p[0] for p in prices]
    out = []
    for day in query_days:
        i = bisect_right(days, day) - 1
        out.append(days[i + horizon] if i >= 0 and i + horizon < len(prices) else None)
    return out


def weight_from_pairs(scores, returns):
    n = len(scores)
    corr = pearson(scores, returns) if n >= MIN_PAIRS else float("nan")
    if corr != corr:
        return PRIOR_WEIGHT, n, float("nan")
    shrunk = corr * n / (n + SHRINK_N)
    return max(0.0, min(1.0, PRIOR_WEIGHT + SLOPE * shrunk)), n, corr


def compute_weights(store, prices, horizon, today, target="direction"):
    """{signal: (weight, n_pairs, corr)} from outcomes known as of `today`."""
    out = {}
    if prices:
        prices = [p for p in prices if p[0] <= today]        # an outcome is known only once its last price is in the past
    for sig in {o for o in _signals(store)}:
        series = [(day, score) for day, score, _conf in store.market_series(sig) if day < today]
        rets = forward_returns(prices, [d for d, _s in series], horizon, target) if prices else []   # one pass, not one per day
        pairs = [(sc, r) for (_d, sc), r in zip(series, rets) if r is not None]
        out[sig] = weight_from_pairs([sc for sc, _r in pairs], [r for _sc, r in pairs])
    return out


def _signals(store):
    return [r[0] for r in store._all("SELECT DISTINCT signal FROM observations WHERE scope='market'")]


def signal_overlap(store, min_days=30):
    """How closely the signals' daily market scores move together: [{a, b, n, corr}], strongest first.

    The model multiplies signals as if independent, so two signals with high overlap partly count the same
    evidence twice. This is a reading for the operator, not an input to the model."""
    series = {}
    for sig in sorted(_signals(store)):
        series[sig] = {d: s for d, s, c in store.market_series(sig) if c > 0}
    out, names = [], sorted(series)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            days = sorted(set(series[a]) & set(series[b]))
            if len(days) < min_days:
                continue
            corr = pearson([series[a][d] for d in days], [series[b][d] for d in days])
            if corr == corr:
                out.append(dict(a=a, b=b, n=len(days), corr=round(corr, 3)))
    return sorted(out, key=lambda r: -abs(r["corr"]))


def placebo_test(store, prices, horizon, k=200, min_shift=130, seed=0, target="direction"):
    """Is a signal's link to forward returns more than luck? For each signal, its daily score series is rotated
    in time by a random amount (at least min_shift days) against the returns, k times. A rotation keeps the
    signal's own slowness and the returns' own streaks but breaks any real timing, so those correlations show what
    noise would have looked like. p = share of rotations whose |corr| is at least the real |corr| (+1 smoothing).
    p_adjusted multiplies by the number of signals tested, because the best of many always looks good by luck."""
    rng = random.Random(seed)
    rows, usable = [], []
    for sig in sorted(_signals(store)):
        series = [(d, s) for d, s, c in store.market_series(sig) if c > 0]
        rets = forward_returns(prices, [d for d, _ in series], horizon, target)
        pairs = [(s, r) for (_d, s), r in zip(series, rets) if r is not None]
        if len(pairs) >= 2 * min_shift:
            usable.append((sig, [s for s, _ in pairs], [r for _, r in pairs]))
    for sig, x, y in usable:
        real = pearson(x, y)
        if real != real:
            continue
        n, hits = len(x), 0
        for _ in range(k):
            off = rng.randint(min_shift, n - min_shift)
            c = pearson(x[off:] + x[:off], y)
            hits += c == c and abs(c) >= abs(real)
        p = (hits + 1) / (k + 1)
        rows.append(dict(signal=sig, n=n, corr=round(real, 3), p=round(p, 3),
                         p_adjusted=round(min(1.0, p * len(usable)), 3)))
    return dict(rotations=k, signals_tested=len(usable), results=sorted(rows, key=lambda r: r["p"]),
                note="Small p means the link is unlikely to be luck. Overlapping 20-day returns make p optimistic; "
                     "a negative corr with small p means the signal points the wrong way, still not usable as is.")
