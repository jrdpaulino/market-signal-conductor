import json
import os
import tempfile
import unittest
from datetime import date, timedelta

from msc.config import Config
from msc.signals import journals_news, macro_fred, prediction_markets, sec_filings, social
from msc.signals.base import Context
from msc.signals.lexicon import is_speculative, polarity
from msc.store import Store
from tests.test_core import make_prices


class FakeHttp:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, params=None, headers=None, **kw):
        self.calls.append(url)
        for key, val in self.routes.items():
            if key in url:
                if isinstance(val, Exception):
                    raise val
                return val
        raise RuntimeError("no route for " + url)


def ctx_for(routes, day="2026-11-20", **cfg):
    base = dict(sec_user_agent="t@example.com", watchlist=("NVDA", "AAPL"), rss_feeds=("https://x/feed",),
                reddit_subs=("stocks",), pm_rules=({"match": "recession", "bullish_if": "no"},))
    base.update(cfg)
    store = Store(os.path.join(tempfile.mkdtemp(), "t.sqlite"))
    return Context(day, Config(**base), FakeHttp(routes), store, lambda: make_prices(400))


FORM4 = """<SEC-DOCUMENT><XML><ownershipDocument><issuer><issuerTradingSymbol>nvda</issuerTradingSymbol></issuer>
<reportingOwner><reportingOwnerId><rptOwnerCik>111</rptOwnerCik></reportingOwnerId>
<reportingOwnerRelationship><isOfficer>1</isOfficer></reportingOwnerRelationship></reportingOwner>
<nonDerivativeTable>
<nonDerivativeTransaction><transactionCoding><transactionCode>P</transactionCode></transactionCoding>
<transactionAmounts><transactionShares><value>2000</value></transactionShares>
<transactionPricePerShare><value>100</value></transactionPricePerShare></transactionAmounts></nonDerivativeTransaction>
<nonDerivativeTransaction><transactionCoding><transactionCode>S</transactionCode></transactionCoding>
<transactionAmounts><transactionShares><value>10</value></transactionShares>
<transactionPricePerShare><value>100</value></transactionPricePerShare></transactionAmounts></nonDerivativeTransaction>
<nonDerivativeTransaction><transactionCoding><transactionCode>A</transactionCode></transactionCoding>
<transactionAmounts><transactionShares><value>999</value></transactionShares>
<transactionPricePerShare><value>1</value></transactionPricePerShare></transactionAmounts></nonDerivativeTransaction>
</nonDerivativeTable></ownershipDocument></XML></SEC-DOCUMENT>"""

INDEX = """Form Type   Company Name   CIK   Date Filed   File Name
4           ACME CORP                                                     1234567     20261119    edgar/data/1234567/0001234567-26-000001.txt
4/A         OTHER CO                                                      7654321     20261119    edgar/data/7654321/0007654321-26-000002.txt
8-K         NOPE INC                                                      1111111     20261119    edgar/data/1111111/0001111111-26-000003.txt
"""


class LexiconTests(unittest.TestCase):
    def test_polarity_and_speculation(self):
        self.assertGreater(polarity("Stocks surge on record profit"), 0)
        self.assertLess(polarity("Recession fears trigger selloff"), 0)
        self.assertEqual(polarity("The committee met on Tuesday"), 0)
        self.assertTrue(is_speculative("Company reportedly considering a deal"))
        self.assertFalse(is_speculative("Company completes acquisition"))


class SecTests(unittest.TestCase):
    def test_parse_index_keeps_only_plain_form_4(self):
        self.assertEqual(sec_filings.parse_index(INDEX), ["edgar/data/1234567/0001234567-26-000001.txt"])

    def test_parse_form4_buys_and_sales_only(self):
        txs = sec_filings.parse_form4(FORM4)
        self.assertEqual([(t["code"], t["ticker"], round(t["usd"])) for t in txs], [("P", "NVDA", 200000),
                                                                                    ("S", "NVDA", 1000)])
        self.assertTrue(txs[0]["officer"])
        self.assertEqual(sec_filings.parse_form4("garbage"), [])

    def test_10b5_1_buys_are_excluded_from_aggregate(self):
        planned = [dict(ticker="X", owner="1", code="P", usd=5e5, officer=False, planned=True)]
        self.assertEqual(sec_filings.day_aggregate(planned)["tickers"], {})

    def test_collect_scores_a_buying_cluster(self):
        routes = {"daily-index": INDEX, "edgar/data/1234567": FORM4}
        ctx = ctx_for(routes)
        obs = sec_filings.SecFilings().collect(ctx)
        nv = [o for o in obs if o.key == "NVDA"][0]
        self.assertGreater(nv.score, 0)
        market = [o for o in obs if o.scope == "market"][0]
        self.assertEqual(market.confidence, 0.0)                   # no baseline yet: stays silent

    def test_requires_contact_email(self):
        with self.assertRaises(RuntimeError):
            sec_filings.SecFilings().collect(ctx_for({}, sec_user_agent="nobody"))


class PredictionMarketTests(unittest.TestCase):
    POLY = [
        {"question": "Will the US enter a recession in 2026?", "outcomes": '["Yes","No"]',
         "outcomePrices": '["0.30","0.70"]', "volume": "1000"},
        {"question": "Will it rain?", "outcomePrices": '["0.5","0.5"]'},
    ]
    KALSHI = {"markets": [
        {"ticker": "K1", "event_ticker": "KXRECSS-26", "title": "Recession in 2026?", "yes_bid_dollars": "0.2200",
         "yes_ask_dollars": "0.2600", "last_price_dollars": "0.2000", "volume_fp": "5000.00"},
        {"ticker": "K2", "title": "Wide spread recession market", "yes_bid_dollars": "0.0100",
         "yes_ask_dollars": "0.9900", "last_price_dollars": "0.3000", "volume_fp": "10"},
        {"ticker": "K3", "title": "No price at all recession", "yes_bid_dollars": "0.0000",
         "yes_ask_dollars": "0.0000", "last_price_dollars": "0.0000"},
        {"ticker": "K4", "title": "Will it snow?", "yes_bid_dollars": "0.5", "yes_ask_dollars": "0.5"}], "cursor": ""}
    RULE = {"match": "recession", "bullish_if": "no"}

    def routes(self, **over):
        r = {"gamma-api": json.dumps(self.POLY), "kalshi.com": json.dumps(self.KALSHI)}
        r.update(over)
        return r

    def test_polymarket_normalize_and_direction(self):
        norm = prediction_markets.normalize_polymarket(self.POLY)
        self.assertEqual(prediction_markets.match_probabilities(norm, self.RULE), [0.7])
        self.assertEqual(prediction_markets.match_probabilities(norm, {"match": "recession", "bullish_if": "yes"}),
                         [0.3])

    def test_kalshi_prices_use_mid_when_tight_else_last_and_skip_empty(self):
        norm = prediction_markets.normalize_kalshi(self.KALSHI["markets"])
        by_text = {m["text"].split()[0]: m["p_yes"] for m in norm}
        self.assertAlmostEqual(norm[0]["p_yes"], 0.24)               # mid of 0.22 / 0.26
        self.assertAlmostEqual(norm[1]["p_yes"], 0.30)               # spread too wide: use last trade
        self.assertEqual(len(norm), 3)                               # the all-zero market is dropped
        probs = prediction_markets.match_probabilities(norm, self.RULE)
        self.assertEqual(len(probs), 2)

    def test_trend_needs_history_then_scores_it_per_venue(self):
        ctx = ctx_for(self.routes(), pm_venues=("polymarket", "kalshi"))
        sig = prediction_markets.PredictionMarkets()
        first = sig.collect(ctx)[0]
        self.assertEqual(first.confidence, 0.0)
        self.assertEqual(set(first.meta["levels"]), {"polymarket:0", "kalshi:0"})
        old = (date.fromisoformat(ctx.day) - timedelta(days=7)).isoformat()
        ctx.store.kv_set(f"pm:polymarket:0:{old}", 0.55)             # bullish odds were 55%, now 70%
        ctx.store.kv_set(f"pm:kalshi:0:{old}", 0.60)                 # and 60%, now 77%
        later = sig.collect(ctx)[0]
        self.assertGreater(later.score, 0)
        self.assertEqual(later.meta["series_with_history"], 2)

    def test_one_venue_down_still_reports(self):
        ctx = ctx_for(self.routes(**{"kalshi.com": RuntimeError("down")}))
        obs = prediction_markets.PredictionMarkets().collect(ctx)[0]
        self.assertIn("kalshi", obs.meta["venue_failures"])
        self.assertIn("polymarket:0", obs.meta["levels"])

    def test_all_venues_down_or_unmatched_is_an_error(self):
        with self.assertRaises(RuntimeError):
            prediction_markets.PredictionMarkets().collect(
                ctx_for(self.routes(**{"kalshi.com": RuntimeError("down"), "gamma-api": RuntimeError("down")})))
        with self.assertRaises(RuntimeError):
            prediction_markets.PredictionMarkets().collect(
                ctx_for({"gamma-api": json.dumps([{"question": "Will it rain?", "outcomePrices": "[0.5,0.5]"}]),
                         "kalshi.com": json.dumps({"markets": []})}))

    def test_kalshi_falls_back_to_the_second_hostname_and_paginates(self):
        from msc.http import HttpError

        class Http:
            def __init__(self):
                self.calls = []

            def get(self, url, params=None, **kw):
                self.calls.append((url, dict(params or {})))
                if "external-api" in url:
                    raise HttpError(0, "dns")
                cur = (params or {}).get("cursor")
                if not cur:
                    return json.dumps({"markets": [self_k[0]], "cursor": "p2"})
                return json.dumps({"markets": [self_k[1]], "cursor": ""})

        self_k = self.KALSHI["markets"][:2]
        ctx = ctx_for({})
        ctx.http = Http()
        norm = prediction_markets.Kalshi().fetch(ctx)
        self.assertEqual(len(norm), 2)
        self.assertTrue(any("api.elections" in u for u, _ in ctx.http.calls))
        self.assertTrue(any(p.get("cursor") == "p2" for _u, p in ctx.http.calls))
        self.assertTrue(any(p.get("series_ticker") == "KXRECSSNBER" for _u, p in ctx.http.calls))   # economy series asked for

    def test_rule_exclusions_and_search_terms(self):
        from msc.config import DEFAULT_PM_RULES
        mk = lambda t, p: {"text": t, "p_yes": p, "volume": 1.0}
        markets = [mk("Will no Fed rate cuts happen in 2026?", 0.98), mk("Will the Fed cut rates in December?", 0.4),
                   mk("Bank of Japan decreases interest rates after the December meeting?", 0.01),
                   mk("Will the Fed Pause-Hike-Hike in the next three decisions?", 0.5)]
        probs = prediction_markets.match_probabilities(markets, DEFAULT_PM_RULES[1])
        self.assertEqual(probs, [0.4])                                   # only the genuine Fed-cut market, right way round
        hike = [mk("Will the Fed increase interest rates by 25 bps after the December 2026 meeting?", 0.94),
                mk("Will the Federal Reserve Hike rates by 0bps at their December 2026 meeting?", 0.04),
                mk("Will no Fed rate hikes happen in 2026?", 0.1)]
        self.assertAlmostEqual(prediction_markets.match_probabilities(hike, DEFAULT_PM_RULES[2])[0], 0.06)
        rec = [mk("US recession by end of 2026?", 0.02), mk("UK Recession in 2026?", 0.5),
               mk("Will the Democrats sweep the House and Senate and the US enter a recession by 2027?", 0.38)]
        self.assertEqual(len(prediction_markets.match_probabilities(rec, DEFAULT_PM_RULES[0])), 1)
        terms = prediction_markets.search_terms(DEFAULT_PM_RULES, extra=("inflation",))
        self.assertEqual(terms[:3], ["inflation", "recession", "fed rate cut"])


class SocialAndNewsTests(unittest.TestCase):
    ATOM = """<feed xmlns="http://www.w3.org/2005/Atom">
    <entry><title>$NVDA breakout, calls printing</title><content>&lt;p&gt;bullish rally&lt;/p&gt;</content></entry>
    <entry><title>$NVDA surge continues</title><content>strong</content></entry>
    <entry><title>market crash fears, recession</title><content>selloff</content></entry></feed>"""
    RSS = """<rss><channel>
    <item><title>NVDA beats estimates, record profit</title><description>Shares surge</description></item>
    <item><title>Fed may cut rates, sources say</title><description>speculation grows</description></item>
    </channel></rss>"""

    def test_social_per_ticker_and_market(self):
        obs = social.SocialChatter().collect(ctx_for({"reddit.com": self.ATOM}))
        nv = [o for o in obs if o.key == "NVDA"][0]
        self.assertGreater(nv.score, 0)
        self.assertEqual(nv.meta["mentions"], 2)
        self.assertEqual([o for o in obs if o.scope == "market"][0].meta["posts"], 3)

    def test_social_no_posts_is_an_error(self):
        with self.assertRaises(RuntimeError):
            social.SocialChatter().collect(ctx_for({"reddit.com": RuntimeError("down")}))

    def test_news_discounts_speculation(self):
        obs = journals_news.JournalsNews().collect(ctx_for({"x/feed": self.RSS}))
        market = [o for o in obs if o.scope == "market"][0]
        self.assertEqual(market.meta["speculative_share"], 0.5)
        self.assertLess(market.confidence, min(1.0, 2 / 30))       # hedged items shrink confidence
        self.assertTrue(any(o.key == "NVDA" and o.score > 0 for o in obs))

    def test_news_all_feeds_down_is_an_error(self):
        with self.assertRaises(RuntimeError):
            journals_news.JournalsNews().collect(ctx_for({"x/feed": RuntimeError("down")}))




class MacroTests(unittest.TestCase):
    @staticmethod
    def csv(values, start=date(2021, 1, 4), header="observation_date,X", missing_every=0):
        lines, d, i = [header], start, 0
        for v in values:
            while d.weekday() >= 5:
                d += timedelta(days=1)
            lines.append(f"{d.isoformat()},{'.' if missing_every and i % missing_every == 0 else v}")
            d += timedelta(days=1)
            i += 1
        return "\n".join(lines)

    def test_parser_handles_either_header_and_missing_values(self):
        for header in ("DATE,X", "observation_date,X"):
            rows = macro_fred.parse_fred_csv(f"{header}\n2026-11-02,1.5\n2026-11-03,.\n2026-11-04,\n2026-11-05,2.0\nbad,1")
            self.assertEqual(rows, [("2026-11-02", 1.5), ("2026-11-05", 2.0)])

    def test_curve_inversion_is_bearish_and_steep_is_bullish(self):
        inv = ctx_for({"fredgraph": "DATE,X\n2026-11-19,-0.80"})
        self.assertLess(macro_fred.MacroCurve().collect(inv)[0].score, -0.5)
        steep = ctx_for({"fredgraph": "DATE,X\n2026-11-19,1.20"})
        self.assertGreater(macro_fred.MacroCurve().collect(steep)[0].score, 0.5)

    def test_curve_survives_one_series_failing_with_lower_confidence(self):
        class Http:
            def get(self, url, params=None, **kw):
                if params["id"] == "T10Y3M":
                    raise RuntimeError("down")
                return "DATE,X\n2026-11-19,0.5"
        ctx = ctx_for({})
        ctx.http = Http()
        obs = macro_fred.MacroCurve().collect(ctx)[0]
        self.assertEqual(obs.confidence, 0.5)
        self.assertEqual(len(obs.meta["failures"]), 1)

    def test_vix_spike_is_bearish_calm_is_bullish(self):
        calm = [14 + 0.1 * (i % 5) for i in range(600)]
        spike = calm[:-5] + [30, 32, 35, 38, 40]
        quiet = calm[:-1] + [12]
        ctx_q = ctx_for({"fredgraph": self.csv(quiet)})
        ctx_q.day = self.last_day(quiet)
        self.assertGreater(macro_fred.MacroVix().collect(ctx_q)[0].score, -0.2)
        ctx = ctx_for({"fredgraph": self.csv(spike)})
        ctx.day = self.last_day(spike)
        self.assertLess(macro_fred.MacroVix().collect(ctx)[0].score, -0.5)

    @staticmethod
    def last_day(values, start=date(2021, 1, 4)):
        d, n = start, 0
        while n < len(values) - 1:
            d += timedelta(days=1)
            if d.weekday() < 5:
                n += 1
        return d.isoformat()

    def test_stale_series_is_rejected_and_future_rows_ignored(self):
        with self.assertRaises(RuntimeError):
            macro_fred.MacroCredit().collect(ctx_for({"fredgraph": "DATE,X\n2026-10-01,3.0"}))
        ctx = ctx_for({"fredgraph": "DATE,X\n2026-11-19,1.0\n2026-11-25,-9.0"})
        self.assertGreater(macro_fred.MacroCurve().collect(ctx)[0].score, 0)   # the future row is ignored

    def test_too_little_history_is_an_error(self):
        ctx = ctx_for({"fredgraph": "DATE,X\n2026-11-19,3.0"})
        with self.assertRaises(RuntimeError):
            macro_fred.MacroCredit().collect(ctx)


class WeeklyMacroTests(unittest.TestCase):
    @staticmethod
    def weekly_csv(values, last_date, header="observation_date,X"):
        n = len(values)
        lines = [header] + [f"{(last_date - timedelta(weeks=n - 1 - i)).isoformat()},{v}" for i, v in enumerate(values)]
        return "\n".join(lines)

    CALM = [220000 + 4000 * ((i * 7) % 5 - 2) for i in range(300)]

    def ctx(self, values, last_date=date(2026, 11, 14), day="2026-11-24"):
        c = ctx_for({"fredgraph": self.weekly_csv(values, last_date)})
        c.day = day
        return c

    def test_claims_spike_is_bearish_and_falling_claims_are_bullish(self):
        spike = self.CALM[:-6] + [250000, 270000, 300000, 330000, 350000, 380000]
        self.assertLess(macro_fred.MacroLabor().collect(self.ctx(spike))[0].score, -0.5)
        falling = self.CALM[:-6] + [212000, 208000, 205000, 202000, 200000, 198000]
        self.assertGreater(macro_fred.MacroLabor().collect(self.ctx(falling))[0].score, 0.3)

    def test_a_huge_old_spike_does_not_blind_the_signal(self):
        covid = self.CALM[:100] + [6000000, 4000000, 2000000, 900000] + self.CALM[104:]       # one 2020-style spike
        rise = covid[:-6] + [250000, 270000, 300000, 330000, 350000, 380000]
        self.assertLess(macro_fred.MacroLabor().collect(self.ctx(rise))[0].score, -0.5)

    def test_a_reading_is_not_used_before_it_was_released(self):
        vals = self.CALM[:-1] + [900000]                         # a shock dated Saturday 2026-11-21
        c = self.ctx(vals, last_date=date(2026, 11, 21), day="2026-11-24")                  # released Thursday the 26th
        self.assertGreater(macro_fred.MacroLabor().collect(c)[0].score, -0.3)               # not yet visible
        c = self.ctx(vals, last_date=date(2026, 11, 21), day="2026-11-27")
        self.assertLess(macro_fred.MacroLabor().collect(c)[0].score, -0.5)                  # visible after release

    def test_stale_or_short_weekly_history_is_an_error(self):
        with self.assertRaises(RuntimeError):
            macro_fred.MacroLabor().collect(self.ctx(self.CALM, last_date=date(2026, 10, 3)))   # 7+ weeks old
        with self.assertRaises(RuntimeError):
            macro_fred.MacroLabor().collect(self.ctx(self.CALM[:50]))

    def test_conditions_tight_is_bearish_loose_is_bullish(self):
        base = [-0.45 + 0.02 * ((i * 3) % 5 - 2) for i in range(300)]
        tight = base[:-8] + [-0.2, 0.0, 0.2, 0.4, 0.7, 1.0, 1.3, 1.6]
        loose = base[:-1] + [-0.75]
        self.assertLess(macro_fred.MacroConditions().collect(self.ctx(tight))[0].score, -0.4)
        self.assertGreater(macro_fred.MacroConditions().collect(self.ctx(loose))[0].score, 0.3)


if __name__ == "__main__":
    unittest.main()


class AnomalyFlowTests(unittest.TestCase):
    def _ctx(self, prices, day="2026-11-20"):
        from msc.signals import anomaly_flow  # noqa: F401
        c = ctx_for({}, day=day)
        c.prices = lambda: prices
        return c

    def _calm_prices(self, n=90, last_move=0.0, end="2026-11-20"):
        import random
        rng, p, out, d = random.Random(3), 100.0, [], date.fromisoformat(end)
        days = []
        while len(days) < n:
            if d.weekday() < 5:
                days.append(d)
            d -= timedelta(days=1)
        for i, d in enumerate(reversed(days)):
            p *= 1 + (last_move if i == n - 1 else rng.gauss(0, 0.005))
            out.append((d.isoformat(), p, p))
        return out

    def test_quiet_day_is_not_flagged(self):
        from msc.signals.anomaly_flow import AnomalyFlow
        ctx = self._ctx(self._calm_prices(last_move=0.001))
        obs = AnomalyFlow().collect(ctx)[0]
        self.assertEqual((obs.score, obs.confidence), (0.0, 0.0))

    def test_big_benchmark_gap_is_flagged_with_capped_confidence(self):
        from msc.signals.anomaly_flow import AnomalyFlow
        ctx = self._ctx(self._calm_prices(last_move=0.05))
        obs = AnomalyFlow().collect(ctx)[0]
        self.assertGreater(obs.score, 0)
        self.assertLessEqual(obs.confidence, 0.5)
        self.assertEqual(obs.meta["events"][0]["kind"], "benchmark_price")
        self.assertIsNotNone(ctx.store.kv_get("anomaly:2026-11-20"))   # logged for later review

    def test_prediction_market_jump_needs_history_and_size(self):
        from msc.signals.anomaly_flow import AnomalyFlow, pm_flags
        ctx = self._ctx(self._calm_prices(last_move=0.0))
        end = date(2026, 11, 20)
        for i in range(9, 0, -1):                                   # nine calm days, then a 15-point drop
            ctx.store.kv_set(f"pm:polymarket:0:{(end - timedelta(days=i)).isoformat()}", 0.50 + 0.003 * (i % 2))
        ctx.store.kv_set("pm:polymarket:0:2026-11-20", 0.35)
        flags = pm_flags(ctx.store, "2026-11-20")
        self.assertEqual(len(flags), 1)
        self.assertLess(AnomalyFlow().collect(ctx)[0].score, 0)

    def test_thin_pm_history_never_flags(self):
        from msc.signals.anomaly_flow import pm_flags
        ctx = self._ctx(self._calm_prices())
        ctx.store.kv_set("pm:polymarket:0:2026-11-19", 0.50)
        ctx.store.kv_set("pm:polymarket:0:2026-11-20", 0.20)
        self.assertEqual(pm_flags(ctx.store, "2026-11-20"), [])

    def test_stale_prices_are_not_todays_move(self):
        from msc.signals.anomaly_flow import price_flag
        prices = self._calm_prices(last_move=0.08, end="2026-11-10")
        self.assertIsNone(price_flag(prices, "2026-11-20"))
