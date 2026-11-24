"""Forecast calibration: pull the model's raw P(up) toward what has actually happened, using only outcomes known so far.

Why: the backfill showed a model whose built-in odds sat below the real up-rate (mean forecast 55% against 64%) and
whose day-to-day swings carried almost no information.

How: on each day, look at every forecast whose outcome was already known and compare it with what happened. Then
  forecast = (mean raw forecast + w * (observed up-rate - mean raw forecast)) + s * (raw - mean raw forecast)
where w and s say how far to trust the track record. Outcomes of consecutive days overlap (each covers 20 days), so
n scored outcomes are worth only about n / horizon independent ones; both w and s are pooled by that effective size
(w = n_eff / (n_eff + 40)), so a young record barely moves the forecast and a long one moves it a lot. s also never
exceeds the observed slope of outcome on forecast, which is kept between 0 and 1: calibration can quiet a model's
swings but never amplify them, and a model that points the wrong way is flattened, not inverted. With fewer than 60
known outcomes nothing is changed.

The raw forecast is always kept (kv "praw:<day>"), the gate grades the calibrated one, and calibration never feeds
back into the Markov beliefs. It adds no information; it only keeps the forecast honest about how much it has.
The first version of this trusted the raw average of early outcomes and scored worse than no calibration at all on
simulated data; pooling by effective sample size is the fix, and the tests keep that honest.
"""
MIN_PAIRS = 60
POOL_K = 40            # a record worth 40 independent outcomes is trusted half-and-half with the model's own level
P_MIN, P_MAX = 0.05, 0.95


def fit_calibration(pairs, horizon=20):
    """pairs: [(raw_p_up, outcome 0/1)] known so far -> dict(active, n, base, mean_p, level_w, slope)."""
    n = len(pairs)
    if n < MIN_PAIRS:
        return dict(active=False, n=n, base=None, mean_p=None, level_w=None, slope=None)
    n_eff = n / max(1, horizon)
    w = n_eff / (n_eff + POOL_K)
    mean_p = sum(p for p, _o in pairs) / n
    base = sum(o for _p, o in pairs) / n
    var = sum((p - mean_p) ** 2 for p, _o in pairs) / n
    cov = sum((p - mean_p) * (o - base) for p, o in pairs) / n
    b = min(1.0, max(0.0, cov / var)) if var > 1e-9 else 0.0
    return dict(active=True, n=n, base=base, mean_p=mean_p, level_w=w, slope=b * w)


def apply_calibration(p, cal):
    if not cal or not cal.get("active"):
        return p
    level = cal["mean_p"] + cal["level_w"] * (cal["base"] - cal["mean_p"])
    return min(P_MAX, max(P_MIN, level + cal["slope"] * (p - cal["mean_p"])))
