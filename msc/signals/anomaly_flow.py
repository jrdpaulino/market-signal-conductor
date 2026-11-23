"""Unusual-activity flags from free data: a sudden jump in prediction-market odds, or an outsized benchmark move.

What it is: a statistical "something unusual happened" detector. A reading is flagged only when today's change is
large compared with the series' own recent behaviour (a z-score), not by a fixed rule of thumb.

What it is not: proof that anyone knew anything. Most flags will be news, noise or coincidence. The signal starts at
the same 0.30 prior weight as every other signal and earns influence only if flagged days turn out to precede
market moves. Every flagged event is also written to the database (kv keys "anomaly:<day>") so the track record can
be reviewed later. Reading is capped at low confidence on purpose.

Components (both use only data public on the day):
  * prediction markets: day-over-day change in the median bullish probability the prediction_markets signal stores
  * benchmark price: the latest daily return against the previous 60 daily returns

Not covered, by design: single options trades, which need a paid feed. A paid options-flow source would be one more
component here: read its key from the environment, compute a z-score, add it to `parts`.
"""
import math
import statistics
from datetime import date

from ..models import Observation
from .base import Signal, register

PM_MIN_CHANGES = 5        # daily changes of history needed before a prediction-market flag can fire
PM_MIN_JUMP = 0.05        # and the jump must be at least 5 percentage points
Z_FLAG = 2.5
PRICE_LOOKBACK = 60
PRICE_Z_FLAG = 3.0
MAX_CONFIDENCE = 0.5


def _pm_series(store, day):
    """{(venue, rule): [(day, level)]} from the levels the prediction_markets signal keeps in kv."""
    series = {}
    for key, level in store.kv_prefix("pm:").items():
        parts = key.split(":")
        if len(parts) == 4 and parts[3] <= day:
            series.setdefault((parts[1], parts[2]), []).append((parts[3], level))
    return {k: sorted(v) for k, v in series.items()}


def pm_flags(store, day):
    """Prediction-market series whose newest daily change is unusually large: [(name, change, z)]."""
    today = date.fromisoformat(day)
    out = []
    for (venue, rule), points in _pm_series(store, day).items():
        if not points or points[-1][0] != day:
            continue
        changes = []
        for (d0, v0), (d1, v1) in zip(points, points[1:]):
            if 0 < (date.fromisoformat(d1) - date.fromisoformat(d0)).days <= 4:   # skip gaps from missed runs
                changes.append(v1 - v0)
        if len(changes) < PM_MIN_CHANGES + 1 or (today - date.fromisoformat(points[-2][0])).days > 4:
            continue
        newest, history = changes[-1], changes[:-1]
        sd = max(statistics.pstdev(history), 0.01)
        z = newest / sd
        if abs(newest) >= PM_MIN_JUMP and abs(z) >= Z_FLAG:
            out.append((f"{venue}:{rule}", newest, z))
    return out


def price_flag(prices, day):
    """Latest benchmark return against the previous 60: (return, z) when unusually large, else None."""
    closes = [(d, c) for d, _o, c in prices if d <= day]
    if len(closes) < PRICE_LOOKBACK + 2:
        return None
    if (date.fromisoformat(day) - date.fromisoformat(closes[-1][0])).days > 4:   # stale data is not today's move
        return None
    rets = [b[1] / a[1] - 1 for a, b in zip(closes[-PRICE_LOOKBACK - 2:], closes[-PRICE_LOOKBACK - 1:])]
    newest, history = rets[-1], rets[:-1]
    sd = max(statistics.pstdev(history), 1e-4)
    z = newest / sd
    return (newest, z) if abs(z) >= PRICE_Z_FLAG else None


@register
class AnomalyFlow(Signal):
    name = "anomaly_flow"
    description = "Flags unusual jumps in prediction-market odds or benchmark price. Low confidence; earns weight only from outcomes."
    needs = ("prediction_markets history (optional)", "price history")
    runs_last = True          # reads the levels prediction_markets stores earlier the same day

    def collect(self, ctx):
        prices = ctx.prices() if ctx.prices else []
        pm = pm_flags(ctx.store, ctx.day)
        px = price_flag(prices, ctx.day)
        if not prices and not _pm_series(ctx.store, ctx.day):
            raise RuntimeError("no price history and no prediction-market history to check")
        parts, events = [], []
        for name, change, z in pm:
            parts.append(math.tanh(z / 4))
            events.append({"kind": "prediction_market", "series": name, "change": round(change, 4), "z": round(z, 2)})
        if px:
            parts.append(math.tanh(px[1] / 4))
            events.append({"kind": "benchmark_price", "return": round(px[0], 4), "z": round(px[1], 2)})
        if not parts:
            return [Observation(self.name, ctx.day, "market", "", 0.0, 0.0, {"flagged": 0})]
        ctx.store.kv_set(f"anomaly:{ctx.day}", events)             # review log: what was flagged, to score later
        return [Observation(self.name, ctx.day, "market", "", statistics.fmean(parts),
                            min(MAX_CONFIDENCE, 0.25 * len(parts)), {"flagged": len(parts), "events": events})]
