"""Macro cycle signals from FRED's free CSV downloads (no API key):

  macro_curve   yield-curve slope: 10y minus 2y (T10Y2Y) and 10y minus 3m (T10Y3M). Inversion is bearish.
  macro_credit  high-yield credit spread (BAMLH0A0HYM2). Wide or widening is bearish.
  macro_vix     VIX close (VIXCLS). High or rising is bearish.

They are three separate signals (one module, shared code) so each earns its own reliability weight: the curve
is slow and the other two are fast, and there is no reason to give them the same say.

Only observations dated on or before the run day are used, and a series whose latest value is more than a week
old is dropped as stale. The FRED CSV layout (date in the first column, "." or blank for missing) comes from
memory and has not been run against the live site; the parser is lenient. FRED may limit how much history it
serves for some ICE-sourced series, so these signals work with whatever history is returned (250 points minimum
for the spread and VIX).
"""
import csv
import io
import math
import statistics
from datetime import date, timedelta

from ..models import Observation
from .base import Signal, register

FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv"
STALE_DAYS = 7
MIN_HISTORY = 250
Z_WINDOW = 1260          # about five years of trading days


def parse_fred_csv(text):
    """[(iso_day, value)] oldest first. Missing values ('.', blank, 'NaN') are skipped."""
    rows = []
    reader = csv.reader(io.StringIO(text))
    next(reader, None)                                   # header: "DATE,<id>" or "observation_date,<id>"
    for r in reader:
        if len(r) < 2:
            continue
        try:
            date.fromisoformat(r[0].strip())
            v = float(r[1])
        except ValueError:
            continue
        if not math.isnan(v):
            rows.append((r[0].strip(), v))
    return sorted(rows)


def fetch_series(ctx, series_id):
    """History up to and including ctx.day, or raise if the series is empty or stale."""
    rows = [(d, v) for d, v in parse_fred_csv(ctx.http.get(FRED_CSV, params={"id": series_id})) if d <= ctx.day]
    if not rows:
        raise RuntimeError(f"FRED {series_id}: no usable data")
    age = (date.fromisoformat(ctx.day) - date.fromisoformat(rows[-1][0])).days
    if age > STALE_DAYS:
        raise RuntimeError(f"FRED {series_id}: latest value is {age} days old")
    return rows


def zscore(x, sample):
    sd = statistics.pstdev(sample)
    return (x - statistics.fmean(sample)) / sd if sd > 1e-12 else 0.0


def stress_score(values, change_lag):
    """Bearish-when-high score from a level series: average of the level's z-score and the z-score of its recent
    change, each against the series' own trailing history. Returns (score in [-1, 0.. +1], z_level, z_change)."""
    if len(values) < MIN_HISTORY:
        raise RuntimeError(f"need {MIN_HISTORY} observations, have {len(values)}")
    hist = values[-Z_WINDOW:]
    changes = [hist[i] - hist[i - change_lag] for i in range(change_lag, len(hist))]
    z_level = zscore(hist[-1], hist)
    z_change = zscore(changes[-1], changes)
    return -math.tanh((z_level + z_change) / 3.0), z_level, z_change


@register
class MacroCurve(Signal):
    name = "macro_curve"
    description = "Yield-curve slope (10y-2y, 10y-3m): inversion warns of contraction (FRED)."

    SERIES = ("T10Y2Y", "T10Y3M")

    def collect(self, ctx):
        levels, failures = {}, []
        for sid in self.SERIES:
            try:
                levels[sid] = fetch_series(ctx, sid)[-1][1]
            except Exception as exc:
                failures.append(str(exc))
        if not levels:
            raise RuntimeError("; ".join(failures))
        score = statistics.fmean(math.tanh(v / 1.0) for v in levels.values())
        return [Observation(self.name, ctx.day, "market", "", score, len(levels) / len(self.SERIES),
                            {"levels_pct": {k: round(v, 2) for k, v in levels.items()}, "failures": failures})]


@register
class MacroCredit(Signal):
    name = "macro_credit"
    description = "High-yield credit spread: wide or widening means stress (FRED)."

    def collect(self, ctx):
        rows = fetch_series(ctx, "BAMLH0A0HYM2")
        score, zl, zc = stress_score([v for _d, v in rows], change_lag=63)
        return [Observation(self.name, ctx.day, "market", "", score, 1.0,
                            {"spread_pct": rows[-1][1], "z_level": round(zl, 2), "z_change_3m": round(zc, 2)})]


@register
class MacroVix(Signal):
    name = "macro_vix"
    description = "VIX: high or rising means fear (FRED)."

    def collect(self, ctx):
        rows = fetch_series(ctx, "VIXCLS")
        score, zl, zc = stress_score([v for _d, v in rows], change_lag=21)
        return [Observation(self.name, ctx.day, "market", "", score, 1.0,
                            {"vix": rows[-1][1], "z_level": round(zl, 2), "z_change_1m": round(zc, 2)})]
