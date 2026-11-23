"""Daily benchmark prices from Stooq's free CSV endpoint (no key). Pluggable: anything returning
[(iso_day, open, close)] oldest-first works, so Alpaca or a paid feed can replace this later."""
import csv
import io
from datetime import date

STOOQ = "https://stooq.com/q/d/l/"


class StooqPrices:
    def __init__(self, http, symbol="spy.us"):
        self.http, self.symbol, self._cache, self._day = http, symbol, None, None

    def __call__(self):
        if self._cache is None or self._day != date.today():       # refetch once per day in a long-running server
            self._day = date.today()
            text = self.http.get(STOOQ, params={"s": self.symbol, "i": "d"})
            self._cache = parse_stooq(text)
        return self._cache


def parse_stooq(text):
    rows = []
    for r in csv.DictReader(io.StringIO(text)):
        try:
            rows.append((r["Date"], float(r["Open"]), float(r["Close"])))
        except (KeyError, ValueError, TypeError):
            continue                      # header-only or "No data" responses end up here
    return sorted(rows)


class FredPrices:
    """Daily S&P 500 index closes from FRED (series SP500, free CSV). FRED serves close only, so the entry price
    used when scoring a forecast is the next day's close rather than its open: a small, harmless difference
    for 20-day outcomes. FRED keeps about ten years of this series, plenty for the signals and scoring."""

    def __init__(self, http, series="SP500"):
        self.http, self.series, self._cache, self._day = http, series, None, None

    def __call__(self):
        if self._cache is None or self._day != date.today():       # refetch once per day in a long-running server
            self._day = date.today()
            from .signals.macro_fred import FRED_CSV, parse_fred_csv
            rows = parse_fred_csv(self.http.get(FRED_CSV, params={"id": self.series}))
            self._cache = [(d, v, v) for d, v in rows]
        return self._cache
