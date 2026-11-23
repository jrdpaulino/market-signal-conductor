"""Backfill: replay the signals that can be rebuilt honestly from past data, to see how the model would have done.

Only signals whose sources keep full history are replayed: the FRED macro series (yield curve, credit spread, VIX)
and the S&P 500 trend. They already use only data dated on or before each day, so replaying a past day sees what
was public then. The others are left out rather than faked: prediction markets, news, social and anomaly flags keep no
rewindable history. Insider filings (Form 4) could be replayed honestly by filing date, but a full replay is
thousands of filings a day, so it is shelved until a bulk source is checked.

Safeguards:
  * Everything goes into a SEPARATE database. The live track record, the weights and the trading gate never see it.
  * Walk-forward only: each day's reliability weights use outcomes known by that day (weights.compute_weights).
  * The model is NOT re-fit on the replayed data (that would grade it on data it just learned from). Its prior
    settings were chosen before this test and nothing was tuned on it.
  * The result is information for the person reading it. It never counts toward the gate.

Caveats to read the output with: macro series were first published on their dates and are not revised (the three
used here), but a few years of replay is one market history, not many; overlapping 20-day windows are not
independent, so the effective sample is far smaller than the day count.
"""
import os
from datetime import date

from . import weights as W
from .engine import Conductor
from .execution.gate import check_gate
from .signals import macro_fred, price_anchor  # noqa: F401  (registers them)
from .signals.base import REGISTRY
from .store import Store

OVERLAP_FLAG = 0.6
BACKFILL_SIGNALS = ("price_anchor", "macro_curve", "macro_credit", "macro_vix")


class CachingHttp:
    """Wraps an Http so the same FRED CSV is downloaded once, not once per replayed day."""

    def __init__(self, http):
        self.http, self.cache = http, {}

    def get(self, url, params=None, **kw):
        key = (url, tuple(sorted((params or {}).items())))
        if key not in self.cache:
            self.cache[key] = self.http.get(url, params=params, **kw)
        return self.cache[key]


def backfill_path(cfg):
    base, ext = os.path.splitext(cfg.db_path)
    return f"{base}.backfill{ext or '.sqlite'}"


def replay_days(prices, start, end, step):
    days = [d for d, _o, _c in prices if start <= d <= end]
    return days[::step]


def run_backfill(cfg, http, prices, start, end, db_path, step=1, progress=None):
    """Replay [start, end] into db_path and return a report dict. `prices` is the full price history callable."""
    if os.path.abspath(db_path) == os.path.abspath(cfg.db_path):
        raise ValueError("backfill must use its own database, never the live one")
    store = Store(db_path)
    cond = Conductor(cfg, store, CachingHttp(http), [REGISTRY[n]() for n in BACKFILL_SIGNALS], prices)
    days = replay_days(prices(), start, end, step)
    if not days:
        raise RuntimeError(f"no trading days between {start} and {end} in the price history")
    for i, day in enumerate(days):
        cond.run_day(day)
        if progress and (i + 1) % 100 == 0:
            progress(i + 1, len(days), day)
    return report(cond, days, step)


def report(cond, days, step):
    track = cond.track()
    passed, reasons = check_gate(track) if track["n"] else (False, ["no scored forecasts"])
    health = {}
    for row in cond.store._all("SELECT signal, status, COUNT(*) FROM signal_health GROUP BY signal, status"):
        health.setdefault(row[0], {})[row[1]] = row[2]
    return dict(
        first_day=days[0], last_day=days[-1], days_replayed=len(days), step_days=step, horizon=cond.cfg.horizon,
        scored_forecasts=track["n"], hit_rate=track["hit_rate"], brier=track["brier"],
        brier_base_rate=track["brier_base"],
        beats_base_rate=(track["brier"] is not None and track["brier"] < track["brier_base"]),
        gate_would_say=("pass" if passed else "fail"), gate_reasons=reasons,
        final_weights={k: dict(weight=round(v["weight"], 3), n=v["n"],
                               corr=None if v["corr"] is None else round(v["corr"], 3))
                       for k, v in cond.store.latest_weights().items()},
        signal_days=health,
        signal_overlap=[dict(r, note="moves closely with the other: partly double-counted") if abs(r["corr"]) >= OVERLAP_FLAG else r
                        for r in W.signal_overlap(cond.store)],
        note="Information only: replayed data is kept apart and never counts toward the live gate.",
    )
