"""Earnings drift: stocks tend to keep moving for weeks in the direction of an earnings surprise.

The surprise is a seasonal-random-walk "standardized unexpected earnings" (SUE): this quarter's diluted EPS against
the same quarter a year earlier, standardized by how that change varied over the previous eight quarters. No
analyst forecasts are needed, which is what makes it free and replayable.

Stock splits: the year-ago figure is the comparative printed in the SAME filing (already on the current share
basis), and the change is measured as growth relative to the year-ago figure, so the eight-quarter history stays
comparable across a split. Comparing against the year-ago figure as first reported would turn every split
(AAPL 2020, NVDA 2021 and 2024, AMZN and GOOGL 2022, ...) into a huge fake miss.

Data: SEC's XBRL company-concept API (one JSON file per company and tag), free with the usual SEC User-Agent.

Point in time, so a replayed day sees only what was public:
  * Every fact carries the date the filing that reported it was `filed`. Only facts filed on or before the run
    day count.
  * Each quarter is taken from the first filing that reported it, so a later restatement never leaks backwards.
  * Q4 is not reported on its own; it is the 10-K annual figure minus the three 10-Q quarters, in both the current
    and year-ago columns (an approximation for EPS, because share counts change during the year). It counts as
    known once all four are filed, and is skipped in a year with a split or restatement (SPLIT_TOLERANCE).

Two kinds of reading:
  * Ticker: tanh(SUE / 2), for about a quarter (DRIFT_DAYS) after the filing, with confidence fading linearly
    over that window and lower when the surprise history is short.
  * Market: the average ticker score across the universe that has a fresh report, i.e. earnings-surprise
    breadth. It is silent (confidence 0) until at least MIN_BREADTH companies have reported. Direction is fixed in
    advance (good surprises = bullish); research on aggregate earnings (Kothari, Lewellen and Warner, 2006) found
    the opposite sign for the market as a whole, so if that holds here the reliability weight will go to 0.

Caveats to read results with:
  * The 10-Q / 10-K filing usually comes days to weeks after the earnings press release (an 8-K that carries no
    XBRL), so this enters late: it can catch the drift, not the announcement jump.
  * The documented effect is concentrated in small, thinly covered stocks. The default universe (MSC_WATCHLIST)
    is mega-caps, where it is weakest. MSC_EARNINGS_TICKERS widens it.
  * A backfill over today's watchlist has survivorship bias: those names are today's winners.
  * XBRL starts around 2009-2011, and nine quarters are needed before the first SUE.
  * The JSON layout (units -> list of {start, end, val, form, filed}) is written from documentation and memory
    and has not been run against the live API from here; the parser skips anything it does not recognise.
"""
import json
import math
import statistics
from datetime import date

from ..models import Observation
from ..sec import sec_headers, ticker_ciks
from .base import Signal, register

CONCEPT_URL = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/us-gaap/{tag}.json"
TAGS = ("EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted", "EarningsPerShareBasic")   # first tag with data wins
FORMS = ("10-Q", "10-K", "10-Q/A", "10-K/A")
DRIFT_DAYS = 91          # calendar days a surprise keeps a ticker reading (about 63 trading days)
SUE_HISTORY = 8          # past seasonal differences used for the scale
SUE_MIN_HISTORY = 4
MIN_SD = 0.02            # in growth units; stops a company with very steady EPS from producing a huge SUE
EPS_FLOOR = 0.10         # dollars per share; growth is measured against at least this, so a near-zero base can't explode
SPLIT_TOLERANCE = 0.2    # 10-K prior-year EPS this far from its first report means a split or restatement
MIN_BREADTH = 5          # companies with a fresh report before the market reading speaks

_PARSED = {}             # text -> facts; the backfill reuses one download for every replayed day


def parse_concept(text):
    """[(start, end, days, val, filed, accn)] for 10-Q / 10-K facts in a USD-per-share unit."""
    hit = _PARSED.get(text)
    if hit is not None:
        return hit
    rows = []
    for unit, facts in (json.loads(text).get("units") or {}).items():
        if not unit.upper().startswith("USD"):
            continue
        for f in facts or ():
            if f.get("form") not in FORMS:
                continue
            try:
                start, end = date.fromisoformat(f["start"]), date.fromisoformat(f["end"])
                filed = date.fromisoformat(f["filed"])
                val = float(f["val"])
            except (KeyError, TypeError, ValueError):
                continue
            if math.isfinite(val):
                rows.append((start.isoformat(), end.isoformat(), (end - start).days, val, filed.isoformat(),
                             str(f.get("accn") or filed.isoformat())))
    if len(_PARSED) >= 64:
        _PARSED.clear()
    _PARSED[text] = rows
    return rows


def _is_quarter(days):
    return 80 <= days <= 100


def _is_year(days):
    return 350 <= days <= 380


def _year_apart(a, b):
    return 345 <= (date.fromisoformat(a) - date.fromisoformat(b)).days <= 385


def filing_pairs(rows, day):
    """Per filing known on `day`: its own period and the same period a year earlier AS REPORTED IN THAT FILING.
    Both numbers in a pair sit on the same share basis, so a stock split between them cannot fake a surprise.
    {"q": {end: (cur, prior, filed, start)}, "y": {end: (cur, prior, filed, start)}}, first filing per period wins."""
    by_accn = {}
    for start, end, days, val, filed, accn in rows:
        if filed <= day:
            by_accn.setdefault(accn, []).append((start, end, days, val, filed))
    out = {"q": {}, "y": {}}
    for facts in by_accn.values():
        for kind, ok in (("q", _is_quarter), ("y", _is_year)):
            mine = sorted((f for f in facts if ok(f[2])), key=lambda f: f[1])
            if not mine:
                continue
            own = mine[-1]                                           # the newest period is the one being reported
            prior = [f for f in mine if _year_apart(own[1], f[1])]
            if not prior:
                continue
            cur = out[kind].get(own[1])
            if cur is None or own[4] < cur[2]:
                out[kind][own[1]] = (own[3], prior[-1][3], own[4], own[0])
    return out


def first_reported_year(rows, day):
    """{end: value} for each fiscal year as first reported in its own 10-K (filed on or before `day`)."""
    out = {}
    for start, end, days, val, filed, _accn in rows:
        if filed <= day and _is_year(days) and (end not in out or filed < out[end][1]):
            out[end] = (val, filed)
    return {e: v for e, (v, _f) in out.items()}


def quarters(rows, day):
    """[(end, cur, prior, known_on)] oldest first. Q1-Q3 straight from 10-Qs; Q4 = the 10-K year minus the three
    quarters, for both the current and the prior-year column. Q4 is skipped when the 10-K's prior-year figure differs
    from what the previous 10-K first reported by more than SPLIT_TOLERANCE: a split or restatement happened, so the
    10-Q quarters may sit on a different share basis from the 10-K."""
    pairs = filing_pairs(rows, day)
    q = dict(pairs["q"])
    firsts = first_reported_year(rows, day)
    for end, (cur, prior, filed, start) in pairs["y"].items():
        if end in q:
            continue
        inside = [v for e, v in q.items() if start <= v[3] and e < end]
        if len(inside) != 3:
            continue
        prev_first = [v for e, v in firsts.items() if _year_apart(end, e)]
        if not prev_first or abs(prior - prev_first[0]) > SPLIT_TOLERANCE * max(abs(prev_first[0]), EPS_FLOOR):
            continue
        known = max([filed] + [v[2] for v in inside])
        q[end] = (cur - sum(v[0] for v in inside), prior - sum(v[1] for v in inside), known, start)
    return sorted((end, v[0], v[1], v[2]) for end, v in q.items())


def growth(cur, prior):
    """EPS change against the same quarter a year earlier, relative to that quarter's size. Scale-free, so the
    surprise history stays comparable across a split."""
    return (cur - prior) / max(abs(prior), EPS_FLOOR)


def sue_latest(qs):
    """(sue, quarter, n_history) for the newest quarter, or None with too little history."""
    if not qs:
        return None
    hist = [growth(c, p) for _e, c, p, _k in qs[:-1]][-SUE_HISTORY:]
    if len(hist) < SUE_MIN_HISTORY:
        return None
    sd = max(statistics.pstdev(hist), MIN_SD)
    return growth(qs[-1][1], qs[-1][2]) / sd, qs[-1], len(hist)


def ticker_reading(rows, day):
    """(score, confidence, meta) for one company on `day`, or None if it has no fresh report."""
    hit = sue_latest(quarters(rows, day))
    if hit is None:
        return None
    sue, (end, cur, prior, known), n_hist = hit
    age = (date.fromisoformat(day) - date.fromisoformat(known)).days
    if age > DRIFT_DAYS:
        return None
    conf = (1 - age / DRIFT_DAYS) * min(1.0, n_hist / SUE_HISTORY)
    return math.tanh(sue / 2), conf, dict(sue=round(sue, 2), quarter_end=end, eps=round(cur, 4),
                                          eps_year_ago=round(prior, 4), known_on=known, days_since=age)


@register
class EarningsDrift(Signal):
    name = "earnings_drift"
    description = "Post-earnings drift: EPS vs the same quarter last year, scaled by its own history (SEC XBRL)."
    needs = ("SEC_USER_AGENT",)

    def preflight(self, cfg):
        sec_headers(cfg)

    def universe(self, cfg):
        return tuple(getattr(cfg, "earnings_tickers", ()) or cfg.watchlist)

    def _facts(self, ctx, cik, headers):
        last = None
        for tag in TAGS:
            try:
                rows = parse_concept(ctx.http.get(CONCEPT_URL.format(cik=cik, tag=tag), headers=headers))
            except Exception as exc:                       # 404: the company does not use this tag
                last = exc
                continue
            if rows:
                return rows
        raise RuntimeError(f"no EPS facts ({last})" if last else "no EPS facts")

    def collect(self, ctx):
        headers = sec_headers(ctx.cfg)
        ciks = ticker_ciks(ctx.http, headers)
        tickers = self.universe(ctx.cfg)
        out, failures, scores = [], {}, []
        for t in tickers:
            cik = ciks.get(t.upper().replace(".", "-"))
            if cik is None:
                failures[t] = "not in SEC ticker list"
                continue
            try:
                rows = self._facts(ctx, cik, headers)
            except Exception as exc:
                failures[t] = str(exc)[:120]
                continue
            r = ticker_reading(rows, ctx.day)
            if r is None:
                continue
            score, conf, meta = r
            out.append(Observation(self.name, ctx.day, "ticker", t, score, conf, meta))
            scores.append(score)
        if failures and len(failures) == len(tickers):
            raise RuntimeError(f"every company failed, e.g. {next(iter(failures.items()))}")
        meta = dict(reporting=len(scores), universe=len(tickers), failed=len(failures))
        if len(scores) < MIN_BREADTH:
            out.append(Observation(self.name, ctx.day, "market", "", 0.0, 0.0, meta))
        else:
            mean = statistics.fmean(scores)
            meta.update(breadth=round(mean, 3), positive_share=round(sum(s > 0 for s in scores) / len(scores), 3))
            conf = min(1.0, 2 * len(scores) / len(tickers))       # full say once half the universe has a fresh report
            out.append(Observation(self.name, ctx.day, "market", "", mean, conf, meta))
        return out
