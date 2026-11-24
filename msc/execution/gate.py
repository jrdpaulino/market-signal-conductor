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


def running_base_brier(rows):
    """Brier score of a forecaster that only ever says "the up-rate so far". rows: [(day, exit_day, outcome)].
    At each forecast day the base rate uses only outcomes already known (exit_day <= day); with none known it
    says 0.5. This is the fair yardstick: it is what a naive person could actually have forecast at the time,
    unlike the full-sample base rate, which uses outcomes from the future."""
    rows = sorted(rows)
    pending = sorted((e, o) for _d, e, o in rows)
    k = ups = 0
    total = 0.0
    for d, _e, o in rows:
        while k < len(pending) and pending[k][0] <= d:
            ups += pending[k][1]
            k += 1
        base = ups / k if k else 0.5
        total += (base - o) ** 2
    return total / len(rows) if rows else None


def track_record(forecasts, outcome_of, exit_of=None):
    """forecasts: [(day, horizon, p_up, regime)]; outcome_of(day, horizon) -> 1, 0 or None (not yet known).
    exit_of(day, horizon) -> the date the outcome became known; when given, brier_running is also reported."""
    scored, run_rows = [], []
    for d, h, p, _r in forecasts:
        if (o := outcome_of(d, h)) is not None:
            scored.append((p, o))
            if exit_of is not None and (e := exit_of(d, h)) is not None:
                run_rows.append((d, e, o))
    n = len(scored)
    if n == 0:
        return dict(n=0, hits=0, hit_rate=None, brier=None, brier_base=None, brier_running=None)
    hits = sum((p >= 0.5) == bool(o) for p, o in scored)
    brier = sum((p - o) ** 2 for p, o in scored) / n
    base = sum(o for _p, o in scored) / n
    brier_base = sum((base - o) ** 2 for _p, o in scored) / n
    return dict(n=n, hits=hits, hit_rate=hits / n, brier=brier, brier_base=brier_base,
                brier_running=running_base_brier(run_rows) if run_rows else None)


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


def brier_decomposition(pairs, bins=10):
    """Why a Brier score is what it is. pairs: [(p_up, outcome 0/1)].

    brier ~ reliability - resolution + uncertainty (Murphy), with forecasts grouped into `bins` bins:
      uncertainty  how hard the question is (base rate x (1 - base rate)); nothing can beat it by luck alone
      reliability  calibration error: forecasts that say 55% when it happens 65% (lower is better)
      resolution   discrimination: do high forecasts really precede more ups than low ones (higher is better)
    bias is the mean forecast minus the base rate. brier_if_level_fixed is the Brier score after shifting every
    forecast by -bias (clipped to 0..1): how much of the shortfall was only the overall level being wrong."""
    n = len(pairs)
    if not n:
        return None
    base = sum(o for _p, o in pairs) / n
    mean_f = sum(p for p, _o in pairs) / n
    groups = {}
    for p, o in pairs:
        groups.setdefault(min(bins - 1, int(p * bins)), []).append((p, o))
    rel = sum(len(g) * (sum(p for p, _ in g) / len(g) - sum(o for _, o in g) / len(g)) ** 2 for g in groups.values()) / n
    res = sum(len(g) * (sum(o for _, o in g) / len(g) - base) ** 2 for g in groups.values()) / n
    shift = mean_f - base
    fixed = sum((min(1.0, max(0.0, p - shift)) - o) ** 2 for p, o in pairs) / n
    return dict(n=n, base_rate=round(base, 4), mean_forecast=round(mean_f, 4), bias=round(shift, 4),
                brier=round(sum((p - o) ** 2 for p, o in pairs) / n, 4), uncertainty=round(base * (1 - base), 4),
                reliability=round(rel, 4), resolution=round(res, 4), brier_if_level_fixed=round(fixed, 4),
                note="reliability = calibration error (lower is better); resolution = discrimination (higher is better)")
