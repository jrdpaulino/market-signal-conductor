"""Index strip for the dashboard: S&P 500, Nasdaq, Dow, Russell 2000 and the VIX. Delayed or end-of-day, never live.

Two free sources, no keys:
  * Stooq's quote CSV: intraday, delayed. The symbols and layout come from memory and have not been run against the
    live service from here, so every Stooq price is cross-checked against FRED's last close and thrown away if it is
    implausibly far from it (a wrong symbol or a changed page cannot reach the screen).
  * FRED's daily CSV: last close, one trading day behind. Always fetched: it is the fallback and the cross-check.

Each row says where it came from and when, so the board can never pass an end-of-day figure off as a live one.
The result is cached for a minute, so any number of open dashboards causes one refresh a minute. A failing source
only removes its rows; it never breaks the dashboard.
"""
import csv
import io
import logging
import threading
import time

from .signals.macro_fred import FRED_CSV, parse_fred_csv

log = logging.getLogger("msc.quotes")

STOOQ_QUOTE = "https://stooq.com/q/l/"
TTL_SECONDS = 60
# (name, Stooq symbol, FRED series, largest believable gap between Stooq and FRED's last close)
INDICES = (
    ("S&P 500", "^spx", "SP500", 0.06),
    ("Nasdaq", "^ndq", "NASDAQCOM", 0.06),
    ("Dow", "^dji", "DJIA", 0.06),
    ("Russell 2000", "^rut", "RU2000PR", 0.07),
    ("VIX", "^vix", "VIXCLS", 0.40),
)


def parse_stooq_quotes(text):
    """{symbol: (day, time, price)} from Stooq's quote CSV; unreadable rows ('N/D', pages that are not CSV) are skipped."""
    out = {}
    for row in csv.DictReader(io.StringIO(text)):
        try:
            sym = (row.get("Symbol") or "").strip().lower()
            price = float(row["Close"])
            day = (row.get("Date") or "").strip()
            if sym and price > 0 and len(day) == 10:
                out[sym] = (day, (row.get("Time") or "").strip(), price)
        except (KeyError, TypeError, ValueError):
            continue
    return out


def plausible(price, last_close, tolerance):
    return last_close > 0 and abs(price / last_close - 1) <= tolerance


class Quotes:
    def __init__(self, http, ttl=TTL_SECONDS, clock=time.monotonic):
        self.http, self.ttl, self.clock = http, ttl, clock
        self._at, self._payload, self._lock = None, None, threading.Lock()

    def get(self):
        with self._lock:
            if self._payload is None or self._at is None or self.clock() - self._at >= self.ttl:
                self._payload, self._at = self._refresh(), self.clock()
            return self._payload

    def _fred_history(self, errors):
        out = {}
        for _name, _sym, series, _tol in INDICES:
            try:
                rows = parse_fred_csv(self.http.get(FRED_CSV, params={"id": series}, retries=1, timeout=10))
                if rows:
                    out[series] = rows[-3:]
            except Exception as exc:
                errors.append(f"FRED {series}: {exc}")
        return out

    def _stooq(self, errors):
        try:
            text = self.http.get(STOOQ_QUOTE, params={"s": "+".join(s for _n, s, _f, _t in INDICES),
                                                      "f": "sd2t2ohlcv", "h": "", "e": "csv"}, retries=1, timeout=10)
            found = parse_stooq_quotes(text)
            if not found:
                errors.append("Stooq: no readable quotes (the service may now need a key)")
            return found
        except Exception as exc:
            errors.append(f"Stooq: {exc}")
            return {}

    def _refresh(self):
        errors = []
        eod, live = self._fred_history(errors), self._stooq(errors)
        rows = []
        for name, sym, series, tol in INDICES:
            hist, q = eod.get(series), live.get(sym)
            if not hist:
                continue                                    # nothing to cross-check against: show nothing
            last_day, last_close = hist[-1]
            if q and plausible(q[2], last_close, tol):
                price, asof, source = q[2], f"{q[0]} {q[1]}".strip(), "delayed (Stooq)"
                prev = last_close if last_day < q[0] else (hist[-2][1] if len(hist) > 1 else None)
            else:
                if q:
                    errors.append(f"Stooq {sym}: {q[2]} is too far from FRED's last close {last_close}; ignored")
                price, asof, source = last_close, f"{last_day} close", "end of day (FRED)"
                prev = hist[-2][1] if len(hist) > 1 else None
            change = None if not prev else price / prev - 1
            rows.append(dict(name=name, price=price, change=change, asof=asof, source=source))
        return dict(rows=rows, errors=errors, fetched_at=time.strftime("%H:%M:%S UTC", time.gmtime()))
