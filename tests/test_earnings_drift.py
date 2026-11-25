import json
import unittest
from datetime import date, timedelta

from msc.signals import earnings_drift as ED
from tests.test_signals import ctx_for

TICKERS = json.dumps({"0": {"cik_str": 1, "ticker": "AAA", "title": "A"},
                      "1": {"cik_str": 2, "ticker": "BBB", "title": "B"}})


def fact(start, end, val, filed, form="10-Q", accn="x"):
    return dict(start=start, end=end, val=val, filed=filed, form=form, accn=accn, fy=0, fp="Q")


def calendar_company(years, q_eps, split=None):
    """Calendar-year company. Each 10-Q carries its quarter, the same quarter a year earlier and a 6-month YTD
    figure (ignored); each 10-K carries this year and last year. Filed 35 days after a quarter, 60 after the year.
    q_eps(year, q) is EPS per original share; split=(iso_day, factor) divides every figure filed after that day."""
    def basis(filed):
        return split[1] if split and filed > split[0] else 1.0
    bounds = lambda y: ([date(y, 1, 1), date(y, 4, 1), date(y, 7, 1), date(y, 10, 1)],
                        [date(y, 3, 31), date(y, 6, 30), date(y, 9, 30), date(y, 12, 31)])
    facts = []
    for y in years:
        starts, ends = bounds(y)
        ps, pe = bounds(y - 1)
        for q in range(3):
            filed = (ends[q] + timedelta(35)).isoformat()
            b, a = basis(filed), f"{y}-q{q}"
            facts.append(fact(starts[q].isoformat(), ends[q].isoformat(), q_eps(y, q) / b, filed, accn=a))
            facts.append(fact(ps[q].isoformat(), pe[q].isoformat(), q_eps(y - 1, q) / b, filed, accn=a))
            if q:
                facts.append(fact(f"{y}-01-01", ends[q].isoformat(), 99.0, filed, accn=a))   # YTD noise, ignored
        filed = (ends[3] + timedelta(60)).isoformat()
        b, a = basis(filed), f"{y}-k"
        facts.append(fact(f"{y}-01-01", f"{y}-12-31", sum(q_eps(y, q) for q in range(4)) / b, filed, "10-K", a))
        facts.append(fact(f"{y - 1}-01-01", f"{y - 1}-12-31", sum(q_eps(y - 1, q) for q in range(4)) / b, filed, "10-K", a))
    return {"units": {"USD/shares": facts}}


def steady(y, q):
    return 1.0 + 0.1 * (y - 2015) + (0.01 if q % 2 else -0.01) * (y % 3)


class ParseTests(unittest.TestCase):
    def test_parse_skips_other_forms_units_and_bad_rows(self):
        doc = {"units": {"USD/shares": [fact("2020-01-01", "2020-03-31", 1.0, "2020-05-01"),
                                        fact("2020-01-01", "2020-03-31", 9.0, "2020-05-01", "8-K"),
                                        {"start": "2020-01-01", "end": "bad", "val": 1, "filed": "2020-05-01", "form": "10-Q"}],
                         "shares": [fact("2020-01-01", "2020-03-31", 5.0, "2020-05-01")]}}
        rows = ED.parse_concept(json.dumps(doc))
        self.assertEqual(rows, [("2020-01-01", "2020-03-31", 90, 1.0, "2020-05-01", "x")])

    def test_first_filing_wins_and_future_filings_are_invisible(self):
        rows = [("2020-01-01", "2020-03-31", 90, 1.0, "2020-05-01", "a"), ("2019-01-01", "2019-03-31", 89, 0.8, "2020-05-01", "a"),
                ("2020-01-01", "2020-03-31", 90, 5.0, "2020-09-01", "b"), ("2019-01-01", "2019-03-31", 89, 4.0, "2020-09-01", "b")]
        self.assertEqual(ED.filing_pairs(rows, "2022-01-01")["q"]["2020-03-31"][:2], (1.0, 0.8))   # amendment ignored
        self.assertEqual(ED.filing_pairs(rows, "2020-04-30")["q"], {})

    def test_q4_is_year_minus_three_quarters_and_known_when_10k_files(self):
        rows = ED.parse_concept(json.dumps(calendar_company([2019, 2020], lambda y, q: [1.0, 2.0, 3.0, 4.5][q] + (y - 2019))))
        qs = ED.quarters(rows, "2021-12-31")
        self.assertEqual(qs[-1][0], "2020-12-31")
        self.assertAlmostEqual(qs[-1][1], 5.5)
        self.assertAlmostEqual(qs[-1][2], 4.5)
        self.assertEqual(qs[-1][3], "2021-03-01")
        self.assertNotIn("2020-12-31", [q[0] for q in ED.quarters(rows, "2021-02-28")])


class SueTests(unittest.TestCase):
    def rows(self, jump=0.0):
        def eps(y, q):
            return steady(y, q) + (jump if (y, q) == (2020, 2) else 0.0)
        return ED.parse_concept(json.dumps(calendar_company(range(2015, 2021), eps)))

    def test_big_beat_scores_bullish_and_fades(self):
        day = "2020-11-05"                                    # Q3 2020 filed 2020-11-04
        score, conf, meta = ED.ticker_reading(self.rows(jump=1.0), day)
        self.assertGreater(score, 0.9)
        self.assertEqual(meta["quarter_end"], "2020-09-30")
        later = ED.ticker_reading(self.rows(jump=1.0), "2020-12-20")
        self.assertLess(later[1], conf)

    def test_miss_scores_bearish(self):
        score, _c, _m = ED.ticker_reading(self.rows(jump=-1.0), "2020-11-05")
        self.assertLess(score, -0.9)

    def test_no_reading_before_filing_or_after_window(self):
        r = ED.ticker_reading(self.rows(jump=1.0), "2020-11-03")    # Q3 not yet filed: the newest is Q2
        self.assertNotEqual(r and r[2]["quarter_end"], "2020-09-30")
        self.assertIsNone(ED.ticker_reading(self.rows(), "2021-02-25"))  # Q3 stale, Q4 not yet filed

    def test_split_does_not_fake_a_surprise(self):
        plain = ED.ticker_reading(self.rows(), "2020-11-05")
        rows = ED.parse_concept(json.dumps(calendar_company(range(2015, 2021), steady, split=("2020-06-01", 4.0))))
        split = ED.ticker_reading(rows, "2020-11-05")
        self.assertAlmostEqual(split[0], plain[0], places=6)
        self.assertAlmostEqual(split[2]["eps"] * 4, plain[2]["eps"], places=3)     # the split really is in the data
        # the split year's Q4 is skipped rather than built from quarters on two share bases
        self.assertNotIn("2020-12-31", [q[0] for q in ED.quarters(rows, "2021-03-05")])

    def test_too_little_history_is_silent(self):
        rows = ED.parse_concept(json.dumps(calendar_company([2020], steady)))     # three quarters: two of history
        self.assertIsNone(ED.ticker_reading(rows, "2020-11-05"))


class SignalTests(unittest.TestCase):
    def doc(self, jump):
        return json.dumps(calendar_company(range(2015, 2021),
                                           lambda y, q: steady(y, q) + (jump if (y, q) == (2020, 2) else 0)))

    def routes(self):
        return {"company_tickers.json": TICKERS,
                "CIK0000000001/us-gaap/EarningsPerShareDiluted": self.doc(1.0),
                "CIK0000000002/us-gaap/EarningsPerShareDiluted": RuntimeError("HTTP 404"),
                "CIK0000000002/us-gaap/EarningsPerShareBasic": self.doc(-1.0)}

    def test_collect_ticker_and_quiet_market_reading(self):
        ctx = ctx_for(self.routes(), day="2020-11-05", watchlist=("AAA", "BBB", "ZZZ"))
        obs = {(o.scope, o.key): o for o in ED.EarningsDrift().collect(ctx)}
        self.assertGreater(obs[("ticker", "AAA")].score, 0.9)
        self.assertLess(obs[("ticker", "BBB")].score, -0.9)          # fell back to the basic-EPS tag
        mkt = obs[("market", "")]
        self.assertEqual(mkt.confidence, 0.0)                         # below MIN_BREADTH
        self.assertEqual(mkt.meta["failed"], 1)                       # ZZZ is not in the ticker list

    def test_market_breadth_speaks_with_enough_companies(self):
        many = {str(i): {"cik_str": 10 + i, "ticker": f"T{i}", "title": "x"} for i in range(6)}
        routes = {"company_tickers.json": json.dumps(many), "us-gaap/EarningsPerShareDiluted": self.doc(1.0)}
        ctx = ctx_for(routes, day="2020-11-05", earnings_tickers=tuple(f"T{i}" for i in range(6)))
        mkt = [o for o in ED.EarningsDrift().collect(ctx) if o.scope == "market"][0]
        self.assertGreater(mkt.score, 0.9)
        self.assertEqual(mkt.confidence, 1.0)

    def test_needs_contact_email_and_raises_when_all_fail(self):
        with self.assertRaises(RuntimeError):
            ED.EarningsDrift().collect(ctx_for(self.routes(), sec_user_agent="nobody"))
        with self.assertRaises(RuntimeError):
            ED.EarningsDrift().collect(ctx_for({"company_tickers.json": TICKERS}, watchlist=("AAA",)))


if __name__ == "__main__":
    unittest.main()
