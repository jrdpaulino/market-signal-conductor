"""Macro cycle signals from FRED's free CSV downloads (no API key):

  macro_curve   yield-curve slope: 10y minus 2y (T10Y2Y) and 10y minus 3m (T10Y3M). Inversion is bearish.
  macro_credit  high-yield credit spread (BAMLH0A0HYM2). Wide or widening is bearish.
  macro_vix     VIX close (VIXCLS). High or rising is bearish.
  macro_labor   initial jobless claims (ICSA), 4-week average. High or rising is bearish.
  macro_conditions  Chicago Fed National Financial Conditions Index (NFCI). Tight (positive) or tightening is bearish.

They are three separate signals (one module, shared code) so each earns its own reliability weight: the curve
is slow and the other two are fast, and there is no reason to give them the same say.

Only observations dated on or before the run day are used, and a series whose latest value is more than a week
old is dropped as stale. The FRED CSV layout (date in the first column, "." or blank for missing) comes from
memory and has not been run against the live site; the parser is lenient. FRED may limit how much history it
serves for some ICE-sourced series, so these signals work with whatever history is returned (250 points minimum
for the spread and VIX).

The two weekly series are released about five days after the date they carry, so a reading is used only from five
days after its date (`lag_days`). Without that, a replayed day would see a number that was not yet public.

Direction is fixed in advance and the same for every stress-type signal here: stress is bearish. It is not adjusted
to fit any test; the reliability weights decide how far each signal is trusted, and a signal that points the wrong
way over the long run simply ends up at weight 0.

macro_conditions is excluded from the backfill: the Chicago Fed re-estimates the whole NFCI history every week, so
a replay would use revised numbers nobody had at the time.
"""
import csv
from bisect import bisect_right
import io
import math
import statistics
from datetime import date, timedelta

from ..models import Observation
from .base import Signal, register

FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv"
STALE_DAYS = 7
MIN_HISTORY = 250
CREDIT_MIN_HISTORY = 120  # FRED serves only about three years of this ICE series, so 250 left the first year dark
Z_WINDOW = 1260          # about five years of trading days


_PARSED = {}        # text -> rows. A backfill asks for the same CSV every replayed day; parse it once.


def parse_fred_csv(text):
    """[(iso_day, value)] oldest first. Missing values ('.', blank, 'NaN') are skipped."""
    hit = _PARSED.get(text)
    if hit is not None:
        return hit
    rows = _parse_fred_csv(text)
    if len(_PARSED) >= 16:
        _PARSED.clear()
    _PARSED[text] = rows
    return rows


def _parse_fred_csv(text):
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


def fetch_series(ctx, series_id, lag_days=0, stale_days=STALE_DAYS):
    """History available on ctx.day, or raise if the series is empty or stale.
    lag_days: a reading counts only once that many days have passed since its date (release delay)."""
    cutoff = (date.fromisoformat(ctx.day) - timedelta(days=lag_days)).isoformat()
    all_rows = parse_fred_csv(ctx.http.get(FRED_CSV, params={"id": series_id}))
    rows = all_rows[:bisect_right(all_rows, (cutoff, float("inf")))]      # readings dated on or before the cutoff
    if not rows:
        raise RuntimeError(f"FRED {series_id}: no usable data")
    age = (date.fromisoformat(ctx.day) - date.fromisoformat(rows[-1][0])).days
    if age > stale_days:
        raise RuntimeError(f"FRED {series_id}: latest value is {age} days old")
    return rows


def zscore(x, sample):
    sd = statistics.pstdev(sample)
    return (x - statistics.fmean(sample)) / sd if sd > 1e-12 else 0.0


def stress_score(values, change_lag, min_history=MIN_HISTORY):
    """Bearish-when-high score from a level series: average of the level's z-score and the z-score of its recent
    change, each against the series' own trailing history. Returns (score in [-1, 0.. +1], z_level, z_change)."""
    if len(values) < min_history:
        raise RuntimeError(f"need {min_history} observations, have {len(values)}")
    hist = values[-Z_WINDOW:]
    changes = [hist[i] - hist[i - change_lag] for i in range(change_lag, len(hist))]
    z_level = zscore(hist[-1], hist)
    z_change = zscore(changes[-1], changes)
    return -math.tanh((z_level + z_change) / 3.0), z_level, z_change


def robust_z(x, sample):
    """z-score from the median and the median absolute deviation, so one huge spike (2020 claims) does not
    flatten the scale for years afterwards."""
    med = statistics.median(sample)
    mad = statistics.median(abs(v - med) for v in sample) * 1.4826
    return (x - med) / mad if mad > 1e-12 else 0.0


WEEKLY_WINDOW = 260      # five years of weekly readings
WEEKLY_MIN = 130         # two and a half years
RELEASE_LAG_DAYS = 5     # ICSA and NFCI are published about five days after the week they describe
WEEKLY_STALE_DAYS = 16   # a missed release or two (holiday weeks) is fine; more than that is stale


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
        score, zl, zc = stress_score([v for _d, v in rows], change_lag=63, min_history=CREDIT_MIN_HISTORY)
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


@register
class MacroLabor(Signal):
    name = "macro_labor"
    description = "Initial jobless claims (4-week average): high or rising means a weakening labor market (FRED)."

    def collect(self, ctx):
        rows = fetch_series(ctx, "ICSA", RELEASE_LAG_DAYS, WEEKLY_STALE_DAYS)
        claims = [v for _d, v in rows]
        if len(claims) < WEEKLY_MIN + 4:
            raise RuntimeError(f"need {WEEKLY_MIN + 4} weekly readings, have {len(claims)}")
        avg4 = [statistics.fmean(claims[i - 3:i + 1]) for i in range(3, len(claims))][-WEEKLY_WINDOW:]
        changes = [avg4[i] - avg4[i - 13] for i in range(13, len(avg4))]            # change over about a quarter
        z_level, z_change = robust_z(avg4[-1], avg4), robust_z(changes[-1], changes)
        score = -math.tanh((z_level + z_change) / 3.0)
        return [Observation(self.name, ctx.day, "market", "", score, 1.0,
                            {"claims_4wk_avg": round(avg4[-1]), "week": rows[-1][0], "z_level": round(z_level, 2),
                             "z_change_13w": round(z_change, 2)})]


@register
class MacroConditions(Signal):
    name = "macro_conditions"
    description = "Chicago Fed financial conditions index: tight (positive) or tightening means stress (FRED)."

    def collect(self, ctx):
        rows = fetch_series(ctx, "NFCI", RELEASE_LAG_DAYS, WEEKLY_STALE_DAYS)
        vals = [v for _d, v in rows][-WEEKLY_WINDOW:]
        if len(vals) < WEEKLY_MIN:
            raise RuntimeError(f"need {WEEKLY_MIN} weekly readings, have {len(vals)}")
        changes = [vals[i] - vals[i - 13] for i in range(13, len(vals))]
        z_change = zscore(changes[-1], changes)
        score = -math.tanh((vals[-1] + z_change) / 3.0)      # NFCI is already scaled to mean 0, sd about 1
        return [Observation(self.name, ctx.day, "market", "", score, 1.0,
                            {"nfci": round(vals[-1], 3), "week": rows[-1][0], "z_change_13w": round(z_change, 2)})]
