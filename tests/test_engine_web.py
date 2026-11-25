import base64
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date, timedelta

from msc import web
from msc.config import Config
from msc.version import commit_id
from msc.engine import Conductor
from msc.models import Observation
from msc.signals.base import Signal, load_signals
from msc.store import Store
from tests.test_core import make_prices
from tests.test_signals import FakeHttp


class Fixed(Signal):
    name, description = "fixed", "test signal"

    def __init__(self, score):
        self.score = score

    def collect(self, ctx):
        return [Observation(self.name, ctx.day, "market", "", self.score, 1.0),
                Observation(self.name, ctx.day, "ticker", "AAPL", self.score, 1.0)]


class Broken(Signal):
    name, description = "broken", "always fails"

    def collect(self, ctx):
        raise RuntimeError("source down")


def conductor(signals, prices=None):
    cfg = Config(db_path=":memory:", watchlist=("AAPL",), dash_password="pw")
    store = Store(os.path.join(tempfile.mkdtemp(), "t.sqlite"))
    return Conductor(cfg, store, FakeHttp({}), signals, prices)


def day(i):
    import datetime
    return (datetime.date(2026, 1, 1) + datetime.timedelta(days=i)).isoformat()


class EngineTests(unittest.TestCase):
    def test_broken_signal_does_not_stop_the_run(self):
        c = conductor([Fixed(-0.6), Broken()])
        out = c.run_day(day(0))
        self.assertEqual(out["health"]["broken"][0], "error")
        self.assertEqual(out["health"]["fixed"][0], "ok")
        self.assertAlmostEqual(sum(out["belief"].values()), 1.0)
        self.assertEqual(c.store.latest_health()["broken"]["status"], "error")

    def test_bearish_evidence_moves_the_regime_and_stocks(self):
        c = conductor([Fixed(-0.7)])
        for i in range(25):
            out = c.run_day(day(i))
        self.assertEqual(out["regime"], "contraction")
        self.assertLess(out["p_up"], 0.5)
        self.assertLess(c.store.latest_stock_beliefs()["AAPL"][2], 0)

    def test_stocks_drift_back_when_signals_go_quiet(self):
        c = conductor([Fixed(0.8)])
        for i in range(10):
            c.run_day(day(i))
        hot = c.store.latest_stock_beliefs()["AAPL"][2]
        c.signals = []
        for i in range(10, 40):
            c.run_day(day(i))
        self.assertLess(abs(c.store.latest_stock_beliefs()["AAPL"][2]), abs(hot))

    def test_rerunning_a_day_is_idempotent(self):
        c = conductor([Fixed(0.3)])
        c.run_day(day(0))
        a = c.run_day(day(1))["belief"]
        b = c.run_day(day(1))["belief"]
        self.assertEqual(a, b)

    def test_learn_needs_history_then_saves_model(self):
        c = conductor([Fixed(0.3)])
        c.run_day(day(0))
        self.assertFalse(c.learn()["fitted"])
        for i in range(1, 30):
            c.run_day(day(i))
        self.assertTrue(c.learn()["fitted"])
        self.assertIn("fixed", c.load_model().mu)

    def test_state_payload_is_json_and_gate_is_closed(self):
        c = conductor([Fixed(0.3)], prices=lambda: make_prices(300))
        for i in range(5):
            c.run_day(day(i))
        s = json.loads(json.dumps(c.state()))
        self.assertFalse(s["gate"]["passed"])
        self.assertEqual(s["signals"][0]["name"], "fixed")

    def test_unknown_signal_name_is_rejected(self):
        with self.assertRaises(SystemExit):
            load_signals(Config(signals=("nope",)))

    def test_builtin_signals_register(self):
        names = {s.name for s in load_signals(Config())}
        self.assertEqual(names, {"sec_filings", "prediction_markets", "social", "journals_news", "price_anchor",
                                 "macro_curve", "macro_credit", "macro_credit_long", "macro_vix", "macro_labor",
                                 "macro_conditions", "anomaly_flow", "earnings_drift"})
        self.assertEqual([s.name for s in load_signals(Config())][-1], "anomaly_flow")   # runs after what it reads


class WebTests(unittest.TestCase):
    def setUp(self):
        self.c = conductor([Fixed(0.3)])
        self.cfg = Config(dash_password="secret", port=0)
        self.server = web.serve(self.c, self.cfg, host="127.0.0.1")
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def fetch(self, path, auth=None, method="GET"):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method)
        if auth:
            req.add_header("Authorization", "Basic " + base64.b64encode(auth.encode()).decode())
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def test_auth_required_except_health(self):
        code, body = self.fetch("/healthz")
        self.assertEqual(code, 200)
        self.assertEqual(body, f"ok last_run=never commit={commit_id()}")
        self.c.run_day("2026-01-05")
        self.assertEqual(self.fetch("/healthz")[1], f"ok last_run=2026-01-05 commit={commit_id()}")
        self.assertEqual(self.fetch("/")[0], 401)
        self.assertEqual(self.fetch("/api/state", "admin:wrong")[0], 401)
        self.assertEqual(self.fetch("/api/run", method="POST")[0], 401)

    def test_dashboard_and_run_now(self):
        code, body = self.fetch("/", "admin:secret")
        self.assertEqual(code, 200)
        self.assertIn("Market Signal Conductor", body)
        self.assertEqual(self.fetch("/api/run", "admin:secret", "POST")[0], 200)
        code, body = self.fetch("/api/state", "admin:secret")
        self.assertEqual(json.loads(body)["regime"] in ("recovery", "expansion", "late_cycle", "contraction"), True)

    def test_refuses_to_serve_open_without_password(self):
        with self.assertRaises(SystemExit):
            web.serve(self.c, Config(dash_password=""))


if __name__ == "__main__":
    unittest.main()


class PriceAndEvidenceTests(unittest.TestCase):
    def test_fred_prices_parse_and_refetch_daily(self):
        from datetime import date as real_date
        from unittest import mock
        from msc import prices as pm

        class Http:
            def __init__(self):
                self.n = 0

            def get(self, url, params=None, **kw):
                self.n += 1
                return "observation_date,SP500\n2026-11-18,6000.5\n2026-11-19,\n2026-11-20,6010.25\n"

        http = Http()
        fp = pm.FredPrices(http)
        self.assertEqual(fp(), [("2026-11-18", 6000.5, 6000.5), ("2026-11-20", 6010.25, 6010.25)])
        fp()
        self.assertEqual(http.n, 1)                                 # cached within the day

        class Tomorrow(real_date):
            @classmethod
            def today(cls):
                return real_date(2099, 1, 1)
        with mock.patch.object(pm, "date", Tomorrow):
            fp()
        self.assertEqual(http.n, 2)                                 # refetched when the day changes

    def test_stock_lists_only_show_tickers_with_recent_evidence(self):
        c = conductor([Fixed(0.8)])
        for i in range(3):
            c.run_day(day(i))
        c.signals = []                                              # AAPL keeps drifting but stops getting evidence
        for i in range(3, 50):
            c.run_day(day(i))
        t = c.state()["tilts"]
        self.assertEqual(t["strongest"] + t["weakest"], [])         # old evidence: not listed
        c.signals = [Fixed(0.8)]
        c.run_day(day(50))
        t = c.state()["tilts"]
        self.assertEqual([x["ticker"] for x in t["strongest"]], ["AAPL"])
        self.assertEqual(t["strongest"][0]["evidence"], ["fixed"])

    def test_error_messages_say_what_failed(self):
        from msc.signals import journals_news, social
        from tests.test_signals import ctx_for
        with self.assertRaises(RuntimeError) as cm:
            social.SocialChatter().collect(ctx_for({"reddit.com": RuntimeError("HTTP 403 for reddit")}))
        self.assertIn("r/stocks", str(cm.exception))
        self.assertIn("403", str(cm.exception))
        with self.assertRaises(RuntimeError) as cm:
            journals_news.JournalsNews().collect(ctx_for({"x/feed": RuntimeError("HTTP 404")}))
        self.assertIn("404", str(cm.exception))


class CompanyTests(unittest.TestCase):
    TICKERS = '{"0":{"cik_str":320193,"ticker":"AAPL","title":"Apple Inc."}}'
    PROFILE = '{"name":"Apple Inc.","sicDescription":"ELECTRONIC COMPUTERS"}'

    def test_lookup_caches_and_tidies(self):
        from msc.company import Companies
        http = FakeHttp({"company_tickers": self.TICKERS, "submissions/CIK0000320193": self.PROFILE})
        store = Store(os.path.join(tempfile.mkdtemp(), "c.sqlite"))
        co = Companies(http, store)
        self.assertEqual(co.lookup(["AAPL"])["AAPL"], {"name": "Apple Inc.", "industry": "Electronic Computers"})
        n = len(http.calls)
        co.lookup(["AAPL"])
        self.assertEqual(len(http.calls), n)                      # second time comes from the cache

    def test_unknown_or_failing_lookup_is_blank_and_not_retried_today(self):
        from msc.company import Companies
        http = FakeHttp({"company_tickers": self.TICKERS})
        store = Store(os.path.join(tempfile.mkdtemp(), "c.sqlite"))
        co = Companies(http, store)
        self.assertEqual(co.lookup(["ZZZZ"])["ZZZZ"], {"name": "", "industry": ""})
        n = len(http.calls)
        co.lookup(["ZZZZ"])
        self.assertEqual(len(http.calls), n)

    def test_profile_failure_keeps_the_name(self):
        from msc.company import Companies
        http = FakeHttp({"company_tickers": self.TICKERS, "submissions": RuntimeError("down")})
        co = Companies(http, Store(os.path.join(tempfile.mkdtemp(), "c.sqlite")))
        self.assertEqual(co.lookup(["AAPL"])["AAPL"]["name"], "Apple Inc.")

    def test_state_includes_company_fields(self):
        c = conductor([Fixed(0.8)])
        c.http.routes.update({"company_tickers": self.TICKERS, "submissions/CIK0000320193": self.PROFILE})
        c.run_day(day(0))
        row = c.state()["tilts"]["strongest"][0]
        self.assertEqual((row["company"], row["industry"]), ("Apple Inc.", "Electronic Computers"))


class _FredFake:
    """Synthetic FRED CSV downloads for the backfill tests."""

    @staticmethod
    def _csv(series, n=1400, base=1.0, wobble=0.5):
        import math as _m
        d, rows = date(2019, 1, 1), ["observation_date," + series]
        while len(rows) <= n:
            if d.weekday() < 5:
                rows.append(f"{d.isoformat()},{base + wobble * _m.sin(len(rows) / 40):.4f}")
            d += timedelta(days=1)
        return "\n".join(rows)

    def _http(self):
        sp = self._csv("SP500", base=3000, wobble=300)
        data = {"SP500": sp, "T10Y2Y": self._csv("T10Y2Y"), "T10Y3M": self._csv("T10Y3M"),
                "BAMLH0A0HYM2": self._csv("BAMLH0A0HYM2", base=4), "BAA10Y": self._csv("BAA10Y", base=2.5, wobble=0.6),
                "VIXCLS": self._csv("VIXCLS", base=18, wobble=6),
                "ICSA": self._csv("ICSA", base=230000, wobble=30000)}

        class H:
            calls = 0

            def get(_s, url, params=None, **kw):
                H.calls += 1
                return data[params["id"]]
        return H



class BackfillTests(_FredFake, unittest.TestCase):
    """Replay on synthetic FRED data: separate database, no look-ahead, honest report."""

    def test_replay_is_separate_cached_and_reported(self):
        from msc.backfill import backfill_path, run_backfill
        from msc.prices import FredPrices
        H = self._http()
        http = H()
        cfg = Config(db_path=os.path.join(tempfile.mkdtemp(), "live.sqlite"))
        prices = FredPrices(http)
        out = backfill_path(cfg)
        self.assertTrue(out.endswith("live.backfill.sqlite"))
        all_days = [d for d, _o, _c in prices()]
        res = run_backfill(cfg, http, prices, all_days[600], all_days[-1], out, step=7)
        self.assertGreater(res["days_replayed"], 50)
        self.assertGreater(res["scored_forecasts"], 20)
        self.assertEqual(set(res["final_weights"]) - {"price_anchor", "macro_curve", "macro_credit", "macro_credit_long", "macro_vix", "macro_labor"}, set())
        self.assertLess(H.calls, 14)                      # six FRED series (seven files) fetched once each, not once per day
        self.assertFalse(os.path.exists(cfg.db_path))     # the live database is untouched
        self.assertIn("never counts toward the live gate", res["note"])
        with self.assertRaises(ValueError):
            run_backfill(cfg, http, prices, all_days[600], all_days[-1], cfg.db_path)

    def test_weights_ignore_prices_after_the_evaluation_day(self):
        from msc import weights as W
        store = Store(os.path.join(tempfile.mkdtemp(), "t.sqlite"))
        prices = make_prices(300)
        for k in range(100):
            store.put_obs([Observation("sig", prices[k][0], "market", "", 0.3 if k % 2 else -0.3)])
        early = W.compute_weights(store, prices, 20, prices[60][0])      # only ~39 outcomes were known by day 60
        late = W.compute_weights(store, prices, 20, prices[299][0])
        self.assertLess(early["sig"][1], late["sig"][1])
        self.assertEqual(early["sig"][1], W.compute_weights(store, prices[:61], 20, prices[60][0])["sig"][1])


class OverlapTests(unittest.TestCase):
    def test_overlap_finds_signals_that_move_together(self):
        from msc import weights as W
        store = Store(os.path.join(tempfile.mkdtemp(), "t.sqlite"))
        prices = make_prices(80)
        for k, p in enumerate(prices):
            x = (k % 7 - 3) / 3
            store.put_obs([Observation("a", p[0], "market", "", x), Observation("b", p[0], "market", "", 0.9 * x + 0.01),
                           Observation("c", p[0], "market", "", ((k * 5) % 11 - 5) / 5)])
        rows = W.signal_overlap(store)
        self.assertEqual((rows[0]["a"], rows[0]["b"]), ("a", "b"))
        self.assertGreater(rows[0]["corr"], 0.95)
        self.assertEqual(W.signal_overlap(store, min_days=500), [])


class BackfillExtrasTests(_FredFake, unittest.TestCase):
    def _setup(self):
        from msc.prices import FredPrices
        http = self._http()()
        cfg = Config(db_path=os.path.join(tempfile.mkdtemp(), "live.sqlite"))
        prices = FredPrices(http)
        return cfg, http, prices, [d for d, _o, _c in prices()]

    def test_walk_forward_refit_runs_and_reports_the_breakdown(self):
        from msc.backfill import backfill_path, run_backfill
        cfg, http, prices, days = self._setup()
        res = run_backfill(cfg, http, prices, days[600], days[-1], backfill_path(cfg), step=7, refit_every=10, refit_after=40)
        self.assertGreater(res["walk_forward_refit"]["refits_done"], 3)
        b = res["brier_breakdown"]
        self.assertAlmostEqual(b["brier"], b["reliability"] - b["resolution"] + b["uncertainty"], delta=0.02)
        self.assertEqual(res["brier_breakdown"]["n"], res["brier_breakdown_raw"]["n"])      # calibrated and raw side by side

    def test_rerun_starts_clean_and_foreign_databases_are_refused(self):
        from msc.backfill import backfill_path, run_backfill
        cfg, http, prices, days = self._setup()
        out = backfill_path(cfg)
        a = run_backfill(cfg, http, prices, days[600], days[-1], out, step=9)
        b = run_backfill(cfg, http, prices, days[600], days[-1], out, step=9)       # same result: no stale model carried over
        self.assertEqual((a["brier"], a["hit_rate"]), (b["brier"], b["hit_rate"]))
        foreign = os.path.join(tempfile.mkdtemp(), "mine.sqlite")
        s = Store(foreign)
        s.put_obs([Observation("x", "2026-01-02", "market", "", 0.1)])
        s.db.close()
        with self.assertRaises(ValueError):
            run_backfill(cfg, http, prices, days[600], days[-1], foreign, step=9)
        self.assertTrue(os.path.exists(foreign))
        legacy = os.path.join(tempfile.mkdtemp(), "live.backfill.sqlite")           # made before the marker existed
        s = Store(legacy)
        s.put_obs([Observation("x", "2026-01-02", "market", "", 0.1)])
        s.db.close()
        run_backfill(cfg, http, prices, days[600], days[-1], legacy, step=9)         # replaced, not refused

    def test_learn_as_of_ignores_outcomes_after_that_day(self):
        from msc.engine import Conductor
        cfg = Config(db_path=os.path.join(tempfile.mkdtemp(), "t.sqlite"))
        prices = make_prices(200)
        store = Store(os.path.join(tempfile.mkdtemp(), "t.sqlite"))
        for k in range(120):
            store.put_obs([Observation("s", prices[k][0], "market", "", ((k % 9) - 4) / 4)])
        c = Conductor(cfg, store, None, [], lambda: prices)
        c.learn(as_of=prices[130][0])
        fit_asof = store.kv_get("model")["p_up"]
        store2 = Store(os.path.join(tempfile.mkdtemp(), "t2.sqlite"))
        for k in range(120):
            store2.put_obs([Observation("s", prices[k][0], "market", "", ((k % 9) - 4) / 4)])
        c2 = Conductor(cfg, store2, None, [], lambda: prices[:131])                # the future was never available
        c2.learn()
        self.assertEqual(fit_asof, store2.kv_get("model")["p_up"])


class BrierBreakdownTests(unittest.TestCase):
    def test_calibrated_but_uninformative_forecast(self):
        from msc.execution.gate import brier_decomposition
        pairs = [(0.6, 1)] * 60 + [(0.6, 0)] * 40
        b = brier_decomposition(pairs)
        self.assertAlmostEqual(b["reliability"], 0.0, places=6)
        self.assertAlmostEqual(b["resolution"], 0.0, places=6)
        self.assertAlmostEqual(b["brier"], b["uncertainty"], places=3)

    def test_bias_is_found_and_fixing_the_level_helps(self):
        from msc.execution.gate import brier_decomposition
        pairs = [(0.45, 1)] * 65 + [(0.45, 0)] * 35                                # says 45% when it is 65%
        b = brier_decomposition(pairs)
        self.assertAlmostEqual(b["bias"], -0.20, places=3)
        self.assertLess(b["brier_if_level_fixed"], b["brier"])
        self.assertGreater(b["reliability"], 0.03)

    def test_discrimination_shows_up_as_resolution(self):
        from msc.execution.gate import brier_decomposition
        pairs = [(0.2, 0)] * 40 + [(0.2, 1)] * 10 + [(0.8, 1)] * 40 + [(0.8, 0)] * 10
        self.assertAlmostEqual(brier_decomposition(pairs)["resolution"], 0.09, places=6)   # outcome rates 0.2 / 0.8 around 0.5


class DashboardExtrasTests(unittest.TestCase):
    def setUp(self):
        from msc.engine import Conductor
        from msc.signals.base import Signal
        outer = self

        class Up(Signal):
            name, description, needs = "up", "x", ()

            def collect(self, ctx):
                return [Observation("up", ctx.day, "market", "", 0.6, 0.8)]

        class Quiet(Signal):
            name, description, needs = "quiet", "x", ()

            def collect(self, ctx):
                return []
        self.prices = make_prices(120)
        cfg = Config(db_path=os.path.join(tempfile.mkdtemp(), "d.sqlite"))
        self.c = Conductor(cfg, Store(cfg.db_path), None, [Up(), Quiet()], lambda: self.prices)
        for d, _o, _c in self.prices[40:80]:
            self.c.run_day(d)

    def test_state_reports_influence_and_weight_history(self):
        s = self.c.state()
        by = {x["name"]: x for x in s["influence"]}
        self.assertAlmostEqual(by["up"]["share"], 1.0)
        self.assertGreater(by["up"]["pull"], 0)
        self.assertIsNone(by["quiet"]["score"])                 # no reading today: no say
        self.assertEqual(by["quiet"]["share"], 0.0)
        self.assertGreater(len(s["weight_history"]["up"]), 5)
        json.dumps(s)

    def test_page_has_day_night_theme_and_influence_card(self):
        from msc.web import PAGE
        self.assertIn('data-theme', PAGE)
        self.assertIn('getHours()', PAGE)
        self.assertIn('Signal Weights', PAGE)
        self.assertNotIn('prefers-color-scheme', PAGE)          # theme follows the clock, not the OS


class QuotesTests(unittest.TestCase):
    STOOQ_OK = ("Symbol,Date,Time,Open,High,Low,Close,Volume\n^SPX,2026-11-23,16:05:00,6000,6050,5990,6040.5,0\n"
                "^NDQ,2026-11-23,16:05:00,20000,20100,19900,20050,0\n^DJI,2026-11-23,16:05:00,N/D,N/D,N/D,N/D,N/D\n"
                "^VIX,2026-11-23,16:05:00,15,17,14,16.5,0\n")

    @staticmethod
    def _fred(last_day="2026-11-20"):
        base = {"SP500": 6000.0, "NASDAQCOM": 20000.0, "DJIA": 44000.0, "VIXCLS": 16.0}
        return {k: f"observation_date,{k}\n2026-11-18,{v}\n2026-11-19,{v}\n{last_day},{v}\n" for k, v in base.items()}

    def _http(self, stooq, fred=None, fail_fred=()):
        fred = self._fred() if fred is None else fred
        calls = []

        class H:
            def get(_s, url, params=None, **kw):
                calls.append((url, params))
                if "stooq" in url:
                    if isinstance(stooq, Exception):
                        raise stooq
                    return stooq
                sid = params["id"]
                if sid in fail_fred or sid not in fred:
                    raise RuntimeError("down")
                return fred[sid]
        return H(), calls

    def test_parse_skips_unreadable_rows(self):
        from msc.quotes import parse_stooq_quotes
        got = parse_stooq_quotes(self.STOOQ_OK)
        self.assertEqual(set(got), {"^spx", "^ndq", "^vix"})            # the N/D row is dropped
        self.assertEqual(parse_stooq_quotes("<html>please get an apikey</html>"), {})

    def test_delayed_quote_is_used_and_labelled_when_it_checks_out(self):
        from msc.quotes import Quotes
        http, _ = self._http(self.STOOQ_OK)
        rows = {r["name"]: r for r in Quotes(http).get()["rows"]}
        self.assertEqual(rows["S&P 500"]["source"], "delayed (Stooq)")
        self.assertAlmostEqual(rows["S&P 500"]["change"], 6040.5 / 6000 - 1)   # against FRED's last close
        self.assertEqual(rows["Dow"]["source"], "end of day (FRED)")           # its Stooq row was N/D
        self.assertNotIn("Russell 2000", rows)                                 # nothing to cross-check: not shown

    def test_implausible_quote_falls_back_to_end_of_day(self):
        from msc.quotes import Quotes
        bad = "Symbol,Date,Time,Open,High,Low,Close,Volume\n^SPX,2026-11-23,16:05:00,1,1,1,3000,0\n"   # wrong symbol or garbage
        http, _ = self._http(bad)
        payload = Quotes(http).get()
        spx = [r for r in payload["rows"] if r["name"] == "S&P 500"][0]
        self.assertEqual((spx["price"], spx["source"]), (6000.0, "end of day (FRED)"))
        self.assertTrue(any("too far" in e for e in payload["errors"]))

    def test_both_sources_down_gives_an_empty_strip_not_an_error(self):
        from msc.quotes import Quotes
        http, _ = self._http(RuntimeError("blocked"), fred={})
        payload = Quotes(http).get()
        self.assertEqual(payload["rows"], [])
        self.assertTrue(payload["errors"])

    def test_cached_for_ttl_then_refreshed(self):
        from msc.quotes import Quotes
        now = [0.0]
        http, calls = self._http(self.STOOQ_OK)
        q = Quotes(http, ttl=60, clock=lambda: now[0])
        q.get()
        n = len(calls)
        now[0] = 30
        q.get()
        self.assertEqual(len(calls), n)                                        # served from cache
        now[0] = 61
        q.get()
        self.assertGreater(len(calls), n)

    def test_endpoint_needs_login_and_returns_rows(self):
        c = conductor([Fixed(0.3)])
        c.quotes.http = self._http(self.STOOQ_OK)[0]
        cfg = Config(dash_password="secret", port=0)
        server = web.serve(c, cfg, host="127.0.0.1")
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}/api/quotes"
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(url)
            self.assertEqual(cm.exception.code, 401)
            req = urllib.request.Request(url, headers={"Authorization": "Basic " + base64.b64encode(b"admin:secret").decode()})
            body = json.loads(urllib.request.urlopen(req).read())
            self.assertTrue(body["rows"])
        finally:
            server.shutdown()
            server.server_close()


class CalibrationTests(unittest.TestCase):
    def test_uninformative_forecasts_are_flattened(self):
        from msc.calibration import apply_calibration, fit_calibration
        import random
        rng = random.Random(1)
        pairs = [(rng.choice([0.42, 0.52, 0.60, 0.62]), int(rng.random() < 0.65)) for _ in range(1500)]
        cal = fit_calibration(pairs)
        self.assertTrue(cal["active"])
        self.assertLess(cal["slope"], 0.25)                                 # the swings carry no information
        spread_before = 0.62 - 0.42
        self.assertLess(apply_calibration(0.62, cal) - apply_calibration(0.42, cal), 0.25 * spread_before + 1e-9)

    def test_level_moves_toward_the_observed_up_rate_in_proportion_to_the_record(self):
        from msc.calibration import apply_calibration, fit_calibration
        short = fit_calibration([(0.5, 1)] * 40 + [(0.5, 0)] * 20)           # 60 overlapping outcomes: worth ~3 independent
        long = fit_calibration([(0.5, 1)] * 1400 + [(0.5, 0)] * 700)          # ~100 independent outcomes, up-rate 67%
        self.assertLess(short["level_w"], 0.1)
        self.assertGreater(long["level_w"], 0.6)
        self.assertLess(abs(apply_calibration(0.5, short) - 0.5), 0.02)       # a young record barely moves the forecast
        self.assertGreater(apply_calibration(0.5, long), 0.62)                # a long one moves most of the way to 0.67

    def test_informative_forecasts_keep_spread_but_are_never_amplified(self):
        from msc.calibration import apply_calibration, fit_calibration
        import random
        rng = random.Random(2)
        pairs = []
        for _ in range(3000):
            p = rng.choice([0.3, 0.5, 0.7])
            pairs.append((p, int(rng.random() < p)))                         # perfectly calibrated
        cal = fit_calibration(pairs)
        self.assertGreater(cal["slope"], 0.6)
        self.assertLessEqual(cal["slope"], 1.0)
        self.assertGreater(apply_calibration(0.7, cal), apply_calibration(0.3, cal) + 0.2)
        self.assertLess(apply_calibration(0.7, cal) - apply_calibration(0.3, cal), 0.4 + 1e-9)    # never wider than the raw spread

    def test_backwards_forecasts_are_flattened_not_inverted(self):
        from msc.calibration import apply_calibration, fit_calibration
        pairs = [(0.7, 0)] * 40 + [(0.7, 1)] * 20 + [(0.3, 1)] * 40 + [(0.3, 0)] * 20       # high p -> fewer ups
        cal = fit_calibration(pairs)
        self.assertEqual(cal["slope"], 0.0)
        self.assertAlmostEqual(apply_calibration(0.7, cal), apply_calibration(0.3, cal))

    def test_nothing_changes_until_enough_outcomes_and_output_is_bounded(self):
        from msc.calibration import apply_calibration, fit_calibration
        cal = fit_calibration([(0.6, 1)] * 59)
        self.assertFalse(cal["active"])
        self.assertEqual(apply_calibration(0.61, cal), 0.61)
        extreme = fit_calibration([(0.5, 1)] * 5000 + [(0.51, 1)] * 5000)
        self.assertLessEqual(apply_calibration(0.5, extreme), 0.95)

    def _conductor(self, calibrate=True):
        from msc.signals.base import Signal
        prices = make_prices(260)

        class Up(Signal):
            name, description, needs = "up", "x", ()

            def collect(self, ctx):
                k = [d for d, _o, _c in prices].index(ctx.day)
                return [Observation("up", ctx.day, "market", "", ((k % 11) - 5) / 5, 0.9)]
        cfg = Config(db_path=os.path.join(tempfile.mkdtemp(), "c.sqlite"), calibrate=calibrate)
        return Conductor(cfg, Store(cfg.db_path), None, [Up()], lambda: prices), prices

    def test_run_day_keeps_the_raw_forecast_and_calibrates_once_there_is_a_record(self):
        c, prices = self._conductor()
        results = [c.run_day(d) for d, _o, _c in prices[30:200]]
        early, late = results[5], results[-1]
        self.assertEqual(early["p_up"], early["p_raw"])                      # no outcomes known yet
        self.assertTrue(c.store.kv_get("calib")["active"])
        self.assertNotEqual(late["p_up"], late["p_raw"])
        self.assertEqual(c.store.kv_get(f"praw:{late['day']}"), late["p_raw"])
        stored = dict((d, p) for d, _h, p, _r in c.store.forecasts())
        self.assertEqual(stored[late["day"]], late["p_up"])                  # the gate grades the calibrated number
        self.assertEqual(c.state()["calibration"]["active"], True)

    def test_switch_off_leaves_the_raw_forecast(self):
        c, prices = self._conductor(calibrate=False)
        for d, _o, _c in prices[30:200]:
            r = c.run_day(d)
            self.assertEqual(r["p_up"], r["p_raw"])

    def test_calibration_never_sees_outcomes_after_its_day(self):
        c, prices = self._conductor()
        for d, _o, _c in prices[30:200]:
            c.run_day(d)
        day = prices[150][0]
        seen = c.calibration_on(day)
        c2, _ = self._conductor()
        c2.prices = lambda: prices[:151]                                     # the future was never available
        for d, _o, _c in prices[30:151]:
            c2.run_day(d)
        self.assertEqual(seen["n"], c2.calibration_on(day)["n"])

    def test_scored_forecasts_can_return_the_raw_numbers(self):
        c, prices = self._conductor()
        for d, _o, _c in prices[30:200]:
            c.run_day(d)
        cal, raw = c.scored_forecasts(), c.scored_forecasts(raw=True)
        self.assertEqual(len(cal), len(raw))
        self.assertNotEqual([p for p, _ in cal], [p for p, _ in raw])
        self.assertEqual([o for _, o in cal], [o for _, o in raw])
