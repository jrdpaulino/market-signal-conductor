"""Backfill: replay the signals that can be rebuilt honestly from past data, to see how the model would have done.

Only signals whose sources keep full history are replayed: the FRED macro series (yield curve, credit spread, VIX)
and the S&P 500 trend (plus weekly jobless claims, with their release delay applied; the revised financial
conditions index is left out). They already use only data dated on or before each day, so replaying a past day sees what
was public then. The others are left out rather than faked: prediction markets, news, social and anomaly flags keep no
rewindable history. Insider filings (Form 4) could be replayed honestly by filing date, but a full replay is
thousands of filings a day, so it is shelved until a bulk source is checked.

Walk-forward refit (optional): every `refit_every` replayed days the model is re-fitted with `Conductor.learn(as_of=day)`,
which sees only data and outcomes known by that day, then used for the days that follow. That is the honest version
of running `learn` on this history. The report always includes a Brier decomposition (calibration versus
discrimination).

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
from .version import commit_id
from .execution.gate import brier_decomposition, check_gate, track_record
from .signals import macro_fred, price_anchor  # noqa: F401  (registers them)
from .signals.base import REGISTRY
from .store import Store

OVERLAP_FLAG = 0.6
BACKFILL_SIGNALS = ("price_anchor", "macro_curve", "macro_credit", "macro_credit_long", "macro_vix", "macro_labor")


class CachingHttp:
    """Wraps an Http so the same FRED CSV is downloaded once, not once per replayed day."""

    def __init__(self, http):
        self.http, self.cache = http, {}

    def get(self, url, params=None, **kw):
        key = (url, tuple(sorted((params or {}).items())))
        if key not in self.cache:
            self.cache[key] = self.http.get(url, params=params, **kw)
        return self.cache[key]


def backfill_path(cfg, tag="backfill"):
    base, ext = os.path.splitext(cfg.db_path)
    return f"{base}.{tag}{ext or '.sqlite'}"


def replay_days(prices, start, end, step):
    days = [d for d, _o, _c in prices if start <= d <= end]
    return days[::step]


def fresh_store(cfg, db_path):
    """A new empty database for the replay. An old backfill database is replaced; anything else is refused."""
    if os.path.abspath(db_path) == os.path.abspath(cfg.db_path):
        raise ValueError("backfill must use its own database, never the live one")
    if os.path.exists(db_path):
        old = Store(db_path)
        # marked by this version, or named like the default backfill files (made before the marker existed)
        is_backfill = old.kv_get("backfill_db") is not None or ".backfill" in os.path.basename(db_path)
        has_data = bool(old.obs_days())
        old.db.close()
        if has_data and not is_backfill:
            raise ValueError(f"{db_path} holds data that is not a backfill database; choose another --db")
        os.remove(db_path)                                  # start clean: a stale fitted model would leak into the replay
    store = Store(db_path, fast=True)
    store.kv_set("backfill_db", 1)
    return store


def run_backfill(cfg, http, prices, start, end, db_path, step=1, progress=None, refit_every=0, refit_after=120, placebo=200, signal_names=None, split=0):
    """Replay [start, end] into db_path and return a report dict. `prices` is the full price history callable.
    refit_every > 0: walk-forward re-fit of the model every that many replayed days, once refit_after have passed."""
    store = fresh_store(cfg, db_path)
    cond = Conductor(cfg, store, CachingHttp(http), [REGISTRY[n]() for n in (signal_names or BACKFILL_SIGNALS)], prices)
    days = replay_days(prices(), start, end, step)
    if not days:
        raise RuntimeError(f"no trading days between {start} and {end} in the price history")
    refits = 0
    for i, day in enumerate(days):
        cond.run_day(day)
        if refit_every and i + 1 >= refit_after and (i + 1 - refit_after) % refit_every == 0:
            if cond.learn(as_of=day).get("fitted"):
                refits += 1
        if progress and (i + 1) % 100 == 0:
            progress(i + 1, len(days), day)
    try:
        return report(cond, days, step, refit_every, refits, placebo, split)
    finally:
        store.close()          # Windows cannot delete or replace a SQLite file that is still open


def signal_errors(store):
    """Why signals failed: per signal and error text, how many days, first and last day. Counts inside the
    text ("have 17") are folded into "N" so a warm-up period is one line, not one line per day. A message that
    only ever appears in one form (for example one with a series id) is shown exactly as it was."""
    import re
    out = {}
    for sig, det, day in store._all(
            "SELECT signal, detail, day FROM signal_health WHERE status='error' ORDER BY signal, day"):
        det = det or ""
        key = re.sub(r"\b(need|have) \d+", r"\1 N", det)
        row = out.setdefault(sig, {}).setdefault(key, dict(error=det, days=0, first=day, last=day, _forms=set()))
        row["days"] += 1
        row["last"] = day
        row["_forms"].add(det)
    result = {}
    for sig, rows in out.items():
        result[sig] = []
        for key, row in sorted(rows.items(), key=lambda kv: kv[1]["first"]):
            forms = row.pop("_forms")
            row["error"] = key if len(forms) > 1 else next(iter(forms))
            result[sig].append(row)
    return result


def split_report(cond, parts, placebo_k=200):
    """The same questions asked separately of consecutive slices of the replay. A real effect shows up in every
    slice with the same sign; one that exists in a single slice (often one bear market) is probably an episode.
    Each slice is scored on its own: hit rate, Brier against the running base rate, and for every signal its link
    to the target with a placebo p-value computed inside that slice."""
    prices = cond.price_history()
    tgt, hz = cond.cfg.target, cond.cfg.horizon
    rows = cond.store.forecasts()
    known = [r for r, v in zip(rows, W.forward_returns(prices, [x[0] for x in rows], hz, tgt)) if v is not None]
    if len(known) < parts * 40:
        return dict(error=f"only {len(known)} scored forecasts, too few for {parts} parts")
    size = len(known) // parts
    out, signs = [], {}
    for i in range(parts):
        chunk = known[i * size:(i + 1) * size if i < parts - 1 else len(known)]
        first, last = chunk[0][0], chunk[-1][0]
        track = track_record(chunk, lambda d, h: (None if (r := W.forward_return(prices, d, h, tgt)) is None else int(r > 0)),
                             exit_of=lambda d, h: W.forward_exit_days(prices, [d], h)[0])
        pl = W.placebo_test(cond.store, prices, hz, placebo_k, min_shift=min(130, max(20, size // 4)), target=tgt,
                            first=first, last=last) if placebo_k else None
        sigs = [dict(signal=r["signal"], n=r["n"], corr=r["corr"], p=r["p"]) for r in (pl or {}).get("results", [])]
        for r in sigs:
            signs.setdefault(r["signal"], []).append(r["corr"])
        out.append(dict(part=i + 1, first_day=first, last_day=last, scored_forecasts=track["n"],
                        hit_rate=track["hit_rate"], brier=track["brier"], brier_running_base_rate=track["brier_running"],
                        beats_running_base_rate=(track["brier"] is not None and track["brier_running"] is not None
                                                 and track["brier"] < track["brier_running"]),
                        signals=sigs))
    same = {sig: (len(c) == parts and (all(x > 0 for x in c) or all(x < 0 for x in c))) for sig, c in signs.items()}
    return dict(parts=out, same_sign_in_every_part=same,
                note="A real effect keeps its sign and a small p in every part. One strong part and weak others "
                     "points to a single episode.")


def report(cond, days, step, refit_every=0, refits=0, placebo=200, split=0):
    track = cond.track()
    passed, reasons = check_gate(track) if track["n"] else (False, ["no scored forecasts"])
    health = {}
    for row in cond.store._all("SELECT signal, status, COUNT(*) FROM signal_health GROUP BY signal, status"):
        health.setdefault(row[0], {})[row[1]] = row[2]
    return dict(
        first_day=days[0], last_day=days[-1], days_replayed=len(days), step_days=step, horizon=cond.cfg.horizon, target=cond.cfg.target,
        target_means=W.TARGET_HELP[cond.cfg.target],
        walk_forward_refit=dict(every_days=refit_every, refits_done=refits) if refit_every else "off",
        scored_forecasts=track["n"], hit_rate=track["hit_rate"], brier=track["brier"],
        brier_base_rate=track["brier_base"], brier_running_base_rate=track.get("brier_running"),
        beats_running_base_rate=(track["brier"] is not None and track.get("brier_running") is not None
                                 and track["brier"] < track["brier_running"]),
        beats_base_rate=(track["brier"] is not None and track["brier"] < track["brier_base"]),
        gate_would_say=("pass" if passed else "fail"), gate_reasons=reasons,
        final_weights={k: dict(weight=round(v["weight"], 3), n=v["n"],
                               corr=None if v["corr"] is None else round(v["corr"], 3))
                       for k, v in cond.store.latest_weights().items()},
        brier_breakdown=brier_decomposition(cond.scored_forecasts()),
        brier_breakdown_raw=brier_decomposition(cond.scored_forecasts(raw=True)),
        calibration_now=cond.store.kv_get("calib"),
        placebo=W.placebo_test(cond.store, cond.price_history(), cond.cfg.horizon, placebo, target=cond.cfg.target) if placebo else "off",
        split=split_report(cond, split, placebo) if split and split > 1 else "off",
        signal_days=health, signal_errors=signal_errors(cond.store), commit=commit_id(),
        signal_overlap=[dict(r, note="moves closely with the other: partly double-counted") if abs(r["corr"]) >= OVERLAP_FLAG else r
                        for r in W.signal_overlap(cond.store)],
        note="Information only: replayed data is kept apart and never counts toward the live gate.",
    )
