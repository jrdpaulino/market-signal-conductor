"""Reliability weights: how much each signal has earned the right to move the model.

For each signal we pair its past market-level scores with what the benchmark actually did afterwards, using
only outcomes already known on the evaluation day (no peeking). The weight rises with a positive correlation,
falls with a negative one, and sits at a low starting value until there is enough evidence.
"""
import math
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


def forward_return(prices, day, horizon):
    """Return from the next open after `day` to the close `horizon` days later. None if not yet known.

    prices: list of (iso_day, open, close), oldest first."""
    days = [p[0] for p in prices]
    i = bisect_right(days, day) - 1            # last trading day on or before `day`
    if i < 0 or i + horizon >= len(prices):
        return None
    entry, exit_ = prices[i + 1][1], prices[i + horizon][2]
    return exit_ / entry - 1 if entry and entry > 0 else None


def weight_from_pairs(scores, returns):
    n = len(scores)
    corr = pearson(scores, returns) if n >= MIN_PAIRS else float("nan")
    if corr != corr:
        return PRIOR_WEIGHT, n, float("nan")
    shrunk = corr * n / (n + SHRINK_N)
    return max(0.0, min(1.0, PRIOR_WEIGHT + SLOPE * shrunk)), n, corr


def compute_weights(store, prices, horizon, today):
    """{signal: (weight, n_pairs, corr)} from outcomes known as of `today`."""
    out = {}
    if prices:
        prices = [p for p in prices if p[0] <= today]        # an outcome is known only once its last price is in the past
    for sig in {o for o in _signals(store)}:
        xs, ys = [], []
        for day, score, _conf in store.market_series(sig):
            if day >= today:
                continue
            r = forward_return(prices, day, horizon) if prices else None
            if r is not None:
                xs.append(score)
                ys.append(r)
        out[sig] = weight_from_pairs(xs, ys)
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
