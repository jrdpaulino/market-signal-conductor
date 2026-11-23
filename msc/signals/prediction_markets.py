"""Prediction-market trends from two public venues, Polymarket and Kalshi (no keys needed).

Each rule in MSC_PM_RULES picks markets whose text matches a pattern and says which answer is good for stocks
(bullish_if "yes" or "no"). The reading is the TREND in the market-implied probability of the bullish outcome
over about a week, taken from this system's own daily snapshots, so it needs a week of history before it speaks.

History is kept per venue and the trends are averaged, so one venue being down for a day cannot fake a trend.
If one venue fails but the other works, the signal still reports and notes the failure.

Add another venue by subclassing Venue and listing it in VENUES. Both venues' field names come from their
public documentation and have not been run against the live services; values are parsed defensively.
"""
import json
import re
import statistics
from datetime import date, timedelta

from ..http import HttpError
from ..models import Observation
from .base import Signal, register

GAMMA = "https://gamma-api.polymarket.com/markets"
POLY_SEARCH = "https://gamma-api.polymarket.com/public-search"
KALSHI_BASES = ("https://external-api.kalshi.com/trade-api/v2", "https://api.elections.kalshi.com/trade-api/v2")
KALSHI_MAX_PAGES = 6
POLY_MAX_PAGES = 10


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ venues

def _polymarket_yes(market):
    prices, outcomes = market.get("outcomePrices"), market.get("outcomes")
    if isinstance(prices, str):
        prices = json.loads(prices)
    if isinstance(outcomes, str):
        outcomes = json.loads(outcomes)
    if not prices:
        return None
    idx = 0
    if outcomes:
        lowered = [str(o).lower() for o in outcomes]
        idx = lowered.index("yes") if "yes" in lowered else 0
    return float(prices[idx])


def normalize_polymarket(markets):
    out = []
    for m in markets:
        try:
            p = _polymarket_yes(m)
        except (ValueError, TypeError, IndexError):
            continue
        if p is not None and 0.0 <= p <= 1.0:
            out.append({"text": str(m.get("question", "")), "p_yes": p, "volume": _num(m.get("volume")) or 0.0})
    return out


def kalshi_yes_price(m):
    """Mid of best bid and ask when the spread is tight, else the last trade. Prices are dollar strings (0 to 1)."""
    bid, ask = _num(m.get("yes_bid_dollars")), _num(m.get("yes_ask_dollars"))
    if bid is not None and ask is not None and ask > 0 and 0 <= ask - bid <= 0.20:
        return (bid + ask) / 2
    last = _num(m.get("last_price_dollars"))
    return last if last is not None and last > 0 else None


def normalize_kalshi(markets):
    out = []
    for m in markets:
        p = kalshi_yes_price(m)
        if p is None or not 0.0 <= p <= 1.0:
            continue
        text = " ".join(str(m.get(k, "")) for k in ("title", "subtitle", "yes_sub_title", "event_ticker"))
        out.append({"text": text, "p_yes": p, "volume": _num(m.get("volume_fp")) or 0.0})
    return out


class Venue:
    name = ""
    extra_terms = ()                                  # extra search words (used by pm-debug)

    def fetch(self, ctx):
        """Return [{"text", "p_yes", "volume"}] for currently open markets."""
        raise NotImplementedError


class Polymarket(Venue):
    name = "polymarket"

    def fetch(self, ctx):
        markets, page = [], 100                      # the API serves about 100 per call: page through
        for i in range(POLY_MAX_PAGES):
            data = json.loads(ctx.http.get(GAMMA, params={"closed": "false", "active": "true", "limit": str(page),
                                                           "offset": str(i * page), "order": "volume",
                                                           "ascending": "false"}))
            rows = data.get("markets", []) if isinstance(data, dict) else data
            markets += rows
            if len(rows) < page:
                break
        found = normalize_polymarket(markets)
        for term in search_terms(ctx.cfg.pm_rules, self.extra_terms):   # the bulk list misses niche topics: search them
            try:
                data = json.loads(ctx.http.get(POLY_SEARCH, params={"q": term, "limit_per_type": "30"}))
                rows = [m for e in (data.get("events") or []) if isinstance(e, dict) for m in (e.get("markets") or [])]
                if rows and not any(r.get("closed") for r in rows[:1]):
                    found += normalize_polymarket([r for r in rows if not r.get("closed")])
            except Exception:                         # search is a bonus; never let it sink the venue
                continue
        return _dedupe(found)


class Kalshi(Venue):
    name = "kalshi"

    def fetch(self, ctx):
        last_error = None
        for base in KALSHI_BASES:                    # the docs show two hostnames; use whichever answers
            try:
                markets, cursor = [], ""
                for _ in range(KALSHI_MAX_PAGES):
                    params = {"status": "open", "limit": "1000", "mve_filter": "exclude"}
                    if cursor:
                        params["cursor"] = cursor
                    body = json.loads(ctx.http.get(base + "/markets", params=params))
                    markets += body.get("markets", [])
                    cursor = body.get("cursor") or ""
                    if not cursor:
                        break
                for series in ctx.cfg.kalshi_series:  # economy series by ticker: the bulk list is mostly sports
                    try:
                        body = json.loads(ctx.http.get(base + "/markets", params={
                            "series_ticker": series, "status": "open", "limit": "200"}))
                        markets += body.get("markets", [])
                    except Exception:
                        continue
                return _dedupe(normalize_kalshi(markets))
            except HttpError as exc:
                last_error = exc
        raise last_error or RuntimeError("Kalshi unreachable")


VENUES = (Polymarket, Kalshi)


# ------------------------------------------------------------------ matching and the signal

def match_probabilities(markets, rule):
    """Bullish-outcome probability per matching market, highest-volume first (max 10).
    A rule may carry "exclude", a pattern for wordings that flip or muddy the meaning ("no rate cuts")."""
    pat = re.compile(rule["match"], re.I)
    skip = re.compile(rule["exclude"], re.I) if rule.get("exclude") else None
    yes_is_bullish = rule.get("bullish_if", "yes") == "yes"
    hits = [(m["volume"], m["p_yes"] if yes_is_bullish else 1.0 - m["p_yes"]) for m in markets
            if pat.search(m["text"]) and not (skip and skip.search(m["text"]))]
    hits.sort(reverse=True)
    return [p for _v, p in hits[:10]]


def search_terms(rules, extra=()):
    """Plain words to search each venue for, taken from the rules' own wording plus any extra terms."""
    terms = list(extra)
    for r in rules:
        terms += [t.strip() for t in re.split(r"[|]", r.get("search") or r["match"]) if re.fullmatch(r"[A-Za-z ]{3,30}", t.strip())]
    seen, out = set(), []
    for t in terms:
        if t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out[:8]


def _dedupe(markets):
    seen, out = set(), []
    for m in markets:
        if m["text"] not in seen:
            seen.add(m["text"])
            out.append(m)
    return out


@register
class PredictionMarkets(Signal):
    name = "prediction_markets"
    description = "Week-over-week trend in market-implied odds of stock-friendly outcomes (Polymarket and Kalshi)."
    needs = ("MSC_PM_RULES (optional)", "MSC_PM_VENUES (optional)")

    def venues(self, cfg):
        wanted = set(cfg.pm_venues)
        return [v() for v in VENUES if v.name in wanted]

    def collect(self, ctx):
        today = date.fromisoformat(ctx.day)
        trends, levels, failures, counts = [], {}, {}, {}
        for venue in self.venues(ctx.cfg):
            try:
                markets = venue.fetch(ctx)
            except Exception as exc:
                failures[venue.name] = f"{type(exc).__name__}: {exc}"
                continue
            counts[venue.name] = len(markets)
            for i, rule in enumerate(ctx.cfg.pm_rules):
                probs = match_probabilities(markets, rule)
                if not probs:
                    continue
                now = statistics.median(probs)
                ctx.store.kv_set(f"pm:{venue.name}:{i}:{ctx.day}", now)
                levels[f"{venue.name}:{i}"] = round(now, 3)
                for lag in range(7, 15):
                    prev = ctx.store.kv_get(f"pm:{venue.name}:{i}:{(today - timedelta(days=lag)).isoformat()}")
                    if prev is not None:
                        trends.append(now - prev)
                        break
        if not levels:
            raise RuntimeError(f"no prediction markets matched the rules (markets fetched: {counts}"
                               + (f"; venue failures: {failures}" if failures else "") + ")")
        meta = {"levels": levels, "venue_failures": failures}
        if not trends:
            return [Observation(self.name, ctx.day, "market", "", 0.0, 0.0, dict(meta, note="collecting history"))]
        trend = statistics.fmean(trends)
        return [Observation(self.name, ctx.day, "market", "", max(-1.0, min(1.0, 5 * trend)),
                            min(1.0, len(trends) / 2), dict(meta, trend=round(trend, 4), series_with_history=len(trends)))]
