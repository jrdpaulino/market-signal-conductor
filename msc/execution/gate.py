"""The edge gate: live trading code may be added only after this says yes, and the gate itself places no orders.

It looks at the model's own scored forecasts (made before the outcome was known) and requires:
  * enough scored forecasts,
  * a better Brier score than always predicting the base rate,
  * a hit rate whose 95% lower bound is above 50%.
"""
import math

MIN_FORECASTS = 60


def wilson_lower(hits, n, z=1.96):
    if n == 0:
        return 0.0
    p = hits / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - margin) / denom


def track_record(forecasts, outcome_of):
    """forecasts: [(day, horizon, p_up, regime)]; outcome_of(day, horizon) -> 1, 0 or None (not yet known)."""
    scored = [(p, o) for d, h, p, _r in forecasts if (o := outcome_of(d, h)) is not None]
    n = len(scored)
    if n == 0:
        return dict(n=0, hits=0, hit_rate=None, brier=None, brier_base=None)
    hits = sum((p >= 0.5) == bool(o) for p, o in scored)
    brier = sum((p - o) ** 2 for p, o in scored) / n
    base = sum(o for _p, o in scored) / n
    brier_base = sum((base - o) ** 2 for _p, o in scored) / n
    return dict(n=n, hits=hits, hit_rate=hits / n, brier=brier, brier_base=brier_base)


def check_gate(track):
    reasons = []
    if track["n"] < MIN_FORECASTS:
        reasons.append(f"only {track['n']} scored forecasts, need {MIN_FORECASTS}")
    else:
        if track["brier"] >= track["brier_base"]:
            reasons.append("Brier score is no better than always guessing the base rate")
        if wilson_lower(track["hits"], track["n"]) <= 0.5:
            reasons.append("hit rate is not distinguishable from a coin flip")
    return (not reasons), reasons
