"""Market price trend. Not one of the four headline signals: it anchors the hidden states to what the market
is actually doing, so the cycle labels mean something."""
import math

from ..models import Observation
from .base import Signal, register


@register
class PriceAnchor(Signal):
    name = "price_anchor"
    description = "Benchmark trend: distance above its 200-day average, 3-month momentum, drawdown from 1-year high."
    needs = ("price history (Stooq by default)",)

    def collect(self, ctx):
        prices = ctx.prices() if ctx.prices else []
        closes = [c for d, _o, c in prices if d <= ctx.day]
        if len(closes) < 252:
            raise RuntimeError(f"need 252 days of benchmark prices, have {len(closes)}")
        last = closes[-1]
        trend = last / (sum(closes[-200:]) / 200) - 1
        mom = last / closes[-64] - 1
        dd = last / max(closes[-252:]) - 1
        score = math.tanh(5 * trend + 3 * mom + 2 * dd)
        return [Observation(self.name, ctx.day, "market", "", score, 1.0,
                            {"trend200": round(trend, 4), "mom63": round(mom, 4), "drawdown": round(dd, 4)})]
