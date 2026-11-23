"""Company name and industry for a ticker, from SEC EDGAR (free, no key).

Names come from SEC's ticker list and the industry is the company's SIC description from its filing profile.
Results are cached in the database, so each ticker is looked up once. A failed lookup is not retried until the
next day, and a failure never breaks the dashboard: the columns are simply blank.
"""
import json
import logging
from datetime import date

log = logging.getLogger("msc.company")

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
PROFILE_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
MAX_NEW_PER_CALL = 25


def _norm(ticker):
    return ticker.upper().replace(".", "-")


def parse_ticker_list(text):
    """SEC's company_tickers.json -> {TICKER: (cik, name)}."""
    out = {}
    for row in json.loads(text).values():
        try:
            out[_norm(row["ticker"])] = (int(row["cik_str"]), str(row["title"]))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def tidy(text):
    """SEC writes names and industries in capitals ('SERVICES-PREPACKAGED SOFTWARE'); make them readable."""
    text = (text or "").strip()
    return text.title() if text.isupper() else text


class Companies:
    def __init__(self, http, store, user_agent=""):
        self.http, self.store = http, store
        self.headers = {"User-Agent": user_agent} if user_agent else None
        self._map, self._map_day = None, None

    def _ticker_map(self):
        if self._map is None or self._map_day != date.today():
            self._map = parse_ticker_list(self.http.get(TICKERS_URL, headers=self.headers, retries=2, timeout=15))
            self._map_day = date.today()
        return self._map

    def _fetch(self, ticker):
        cik, name = self._ticker_map()[ticker]
        industry = ""
        try:
            prof = json.loads(self.http.get(PROFILE_URL.format(cik=cik), headers=self.headers, retries=2, timeout=15))
            industry = tidy(prof.get("sicDescription", ""))
            name = prof.get("name") or name
        except Exception as exc:                       # keep the name even if the profile call fails
            log.warning("profile lookup failed for %s: %s", ticker, exc)
        return {"name": tidy(name), "industry": industry}

    def lookup(self, tickers):
        """{ticker: {"name", "industry"}} for the given tickers; blanks where unknown."""
        out, fresh, today = {}, 0, date.today().isoformat()
        for t in tickers:
            key = "co:" + _norm(t)
            cached = self.store.kv_get(key)
            if cached and cached.get("name"):
                out[t] = {"name": cached["name"], "industry": cached.get("industry", "")}
                continue
            out[t] = {"name": "", "industry": ""}
            if (cached and cached.get("failed") == today) or fresh >= MAX_NEW_PER_CALL:
                continue
            fresh += 1
            try:
                info = self._fetch(_norm(t))
            except Exception as exc:
                log.warning("company lookup failed for %s: %s", t, exc)
                self.store.kv_set(key, {"failed": today})
                continue
            self.store.kv_set(key, info)
            out[t] = info
        return out
