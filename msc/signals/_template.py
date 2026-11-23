"""Template for a new signal. Copy this file, rename the class, and add the module to MSC_EXTRA_MODULES
(or import it from builtin.py). Nothing else in the system needs to change.

Rules every signal follows:
  * Use only information that was public on ctx.day (no hindsight).
  * score in [-1, 1]; confidence in [0, 1] (low when data is thin, stale or hedged).
  * Return market-scope readings (key "") and/or ticker-scope readings (key = symbol).
  * Raise an exception if the source is down: the dashboard shows the signal as unhealthy and the model
    carries on without it.
  * Keep your own history in ctx.store.kv_get / kv_set if you need trends.

A paid feed slots in the same way: read its key from the environment, call it through ctx.http, and map its
output to Observations. A second prediction-market venue (Kalshi) or a premium sentiment API are the obvious
first additions.
"""
import os

from ..models import Observation
from .base import Signal, register


# @register    # uncomment to activate
class ExampleSignal(Signal):
    name = "example"
    description = "Describe what this measures in one line."
    needs = ("EXAMPLE_API_KEY",)

    def collect(self, ctx):
        key = os.environ.get("EXAMPLE_API_KEY")
        if not key:
            raise RuntimeError("EXAMPLE_API_KEY is not set")
        # data = ctx.http.get("https://api.example.com/v1/thing", headers={"Authorization": f"Bearer {key}"})
        return [Observation(self.name, ctx.day, "market", "", 0.0, 0.0, {"note": "template"})]
