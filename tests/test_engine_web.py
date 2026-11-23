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
                                 "macro_curve", "macro_credit", "macro_vix", "anomaly_flow"})
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
        self.assertEqual(body, "ok last_run=never")
        self.c.run_day("2026-01-05")
        self.assertEqual(self.fetch("/healthz")[1], "ok last_run=2026-01-05")
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


class BackfillTests(unittest.TestCase):
    """Replay on synthetic FRED data: separate database, no look-ahead, honest report."""

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
                "BAMLH0A0HYM2": self._csv("BAMLH0A0HYM2", base=4), "VIXCLS": self._csv("VIXCLS", base=18, wobble=6)}

        class H:
            calls = 0

            def get(_s, url, params=None, **kw):
                H.calls += 1
                return data[params["id"]]
        return H

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
        self.assertEqual(set(res["final_weights"]) - {"price_anchor", "macro_curve", "macro_credit", "macro_vix"}, set())
        self.assertLess(H.calls, 12)                      # five FRED series fetched once each, not once per day
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
