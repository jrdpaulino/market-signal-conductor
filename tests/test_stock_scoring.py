import json
import os
import random
import tempfile
import unittest

from msc import alpaca
from msc import stock_scoring as S
from msc import weights as W
from msc.models import Observation
from msc.store import Store
from tests.test_core import make_prices

N_DAYS = 700
TICKERS = [f"T{i}" for i in range(12)]


class Mem:
    """Stock prices held in memory, same interface as DirStockPrices."""

    def __init__(self, data):
        self.data = data

    def get(self, t):
        return self.data.get(t)


def world(informative, seed=3):
    """Benchmark plus 12 stocks. Each stock has slow 'regimes' of out- or under-performance lasting 60 days.
    The signal's reading on a day is the sign of the coming regime when informative, else an unrelated coin."""
    rng = random.Random(seed)
    bench = make_prices(N_DAYS, seed=seed)
    stocks, obs = {}, []
    for t in TICKERS:
        tilt = [rng.choice((-1, 1)) for _ in range(N_DAYS // 60 + 2)]
        p, rows = 50.0, []
        for k, (d, o, c) in enumerate(bench):
            o_s = p
            p *= c / o * (1 + 0.004 * tilt[k // 60] + rng.gauss(0, 0.008))
            rows.append((d, o_s, p))
        stocks[t] = rows
        for k in range(0, N_DAYS - 25):
            d = bench[k][0]
            ahead = tilt[(k + 10) // 60]
            score = ahead * 0.8 if informative else rng.choice((-0.8, 0.8))
            obs.append(Observation("sig", d, "ticker", t, score, 1.0, {}))
    store = Store(os.path.join(tempfile.mkdtemp(), "s.sqlite"))
    store.put_obs(obs)
    return store, Mem(stocks), bench


class StockScoringTests(unittest.TestCase):
    def test_finds_a_real_link(self):
        store, stocks, bench = world(informative=True)
        rep = S.stock_report(store, stocks, bench, 20, k=60)
        r = rep["signals"]["sig"]
        self.assertGreater(r["corr"], 0.3)
        self.assertGreater(r["spread_pct"], 0)
        self.assertLess(r["p"], 0.05)
        self.assertEqual(r["tickers"], 12)
        self.assertEqual(rep["benchmark"], "benchmark price source")

    def test_noise_is_not_a_link(self):
        store, stocks, bench = world(informative=False)
        r = S.stock_report(store, stocks, bench, 20, k=60)["signals"]["sig"]
        self.assertLess(abs(r["corr"]), 0.1)
        self.assertGreater(r["p"], 0.05)

    def test_outcome_is_excess_return_from_next_open(self):
        bench = [("2024-01-01", 100, 100), ("2024-01-02", 100, 101), ("2024-01-03", 101, 102)]
        stock = [("2024-01-01", 10, 10), ("2024-01-02", 10, 11), ("2024-01-03", 11, 12)]
        store = Store(os.path.join(tempfile.mkdtemp(), "s.sqlite"))
        store.put_obs([Observation("sig", "2024-01-01", "ticker", "A", 0.5, 1.0, {}),
                       Observation("sig", "2024-01-02", "ticker", "A", 0.5, 0.0, {})])   # zero confidence: skipped
        pairs = S.ticker_pairs(store, "sig", Mem({"A": stock}), bench, 2)
        self.assertEqual(len(pairs), 1)
        self.assertAlmostEqual(pairs[0][3], (12 / 10 - 1) - (102 / 100 - 1))

    def test_missing_prices_are_reported_and_spy_preferred(self):
        store, stocks, bench = world(informative=True)
        del stocks.data["T0"]
        stocks.data["SPY"] = bench
        rep = S.stock_report(store, stocks, lambda: [], 20, k=0, split=2)
        self.assertEqual(rep["missing_prices"], ["T0"])
        self.assertEqual(rep["benchmark"], "SPY")
        self.assertEqual(len(rep["split"]["parts"]), 2)
        self.assertTrue(rep["split"]["same_sign_in_every_part"]["sig"])
        self.assertIn("sig: n", S.stock_summary(rep))

    def test_dir_prices_read_csv_files(self):
        folder = tempfile.mkdtemp()
        with open(os.path.join(folder, "BRK-B.csv"), "w") as f:
            f.write("Date,Open,Close\n2024-01-02,1,2\n2024-01-03,2,3\n")
        d = S.DirStockPrices(folder)
        self.assertEqual(d.get("BRK.B"), [("2024-01-02", 1.0, 2.0), ("2024-01-03", 2.0, 3.0)])
        self.assertIsNone(d.get("NOPE"))
        self.assertEqual(d.tickers(), ["BRK-B"])


class FakeAlpaca:
    def __init__(self):
        self.calls = []

    def get(self, url, params=None, headers=None):
        self.calls.append((url, dict(params), headers))
        if "BAD" in url:
            raise RuntimeError(f"HTTP 404 for {url} key {headers['APCA-API-KEY-ID']}")
        if params.get("page_token") is None:
            return json.dumps({"bars": [{"t": "2024-01-02T05:00:00Z", "o": 1, "c": 2}], "next_page_token": "p2"})
        return json.dumps({"bars": [{"t": "2024-01-03T05:00:00Z", "o": 2, "c": 3}], "next_page_token": None})


class AlpacaTests(unittest.TestCase):
    def test_pages_are_joined_and_adjusted_bars_requested(self):
        http = FakeAlpaca()
        rows = alpaca.fetch_bars(http, "BRK-B", "2024-01-01", "2024-02-01", "K", "S")
        self.assertEqual(rows, [("2024-01-02", 1.0, 2.0), ("2024-01-03", 2.0, 3.0)])
        url, params, headers = http.calls[0]
        self.assertIn("/BRK.B/bars", url)
        self.assertEqual(params["adjustment"], "all")
        self.assertEqual(headers["APCA-API-KEY-ID"], "K")

    def test_download_writes_csv_keeps_files_and_hides_keys(self):
        folder, logs = tempfile.mkdtemp(), []
        st = alpaca.download(FakeAlpaca(), ["AAA", "BAD"], folder, "2024-01-01", "2024-02-01", "SECRETKEY", "S",
                             log=logs.append)
        self.assertTrue(st["AAA"].startswith("2 days"))
        self.assertNotIn("SECRETKEY", st["BAD"] + " ".join(logs))
        self.assertEqual(S.DirStockPrices(folder).get("AAA")[-1], ("2024-01-03", 2.0, 3.0))
        again = alpaca.download(FakeAlpaca(), ["AAA"], folder, "2024-01-01", "2024-02-01", "K", "S", log=logs.append)
        self.assertEqual(again["AAA"], "kept")

    def test_keys_required(self):
        with self.assertRaises(SystemExit):
            alpaca.keys_from_env({})
        self.assertEqual(alpaca.keys_from_env({"ALPACA_KEY_ID": "a", "ALPACA_SECRET_KEY": "b"}), ("a", "b"))


class WeightDecayTests(unittest.TestCase):
    def test_no_link_fades_to_near_zero_but_untested_keeps_prior(self):
        rng = random.Random(1)
        x = [rng.gauss(0, 1) for _ in range(2500)]
        y = [rng.gauss(0, 1) for _ in range(2500)]
        w, n, corr = W.weight_from_pairs(x, y)
        self.assertLess(w, 0.08)
        self.assertEqual(W.weight_from_pairs(x[:5], y[:5])[0], W.PRIOR_WEIGHT)
        y_link = [a * 0.3 + b for a, b in zip(x, y)]
        self.assertGreater(W.weight_from_pairs(x, y_link)[0], 0.5)

    def test_early_evidence_still_near_prior(self):
        rng = random.Random(2)
        x = [rng.gauss(0, 1) for _ in range(20)]
        y = [rng.gauss(0, 1) for _ in range(20)]
        self.assertGreater(W.weight_from_pairs(x, y)[0] + 3 * abs(W.pearson(x, y)), 0.25)


if __name__ == "__main__":
    unittest.main()
