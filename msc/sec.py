"""Shared bits for talking to SEC EDGAR: the required User-Agent and the ticker -> CIK map.

The SEC asks every automated client to send a User-Agent with a contact email and to stay under about ten
requests a second (msc.http.Http already spaces requests). Signals that read EDGAR use these helpers so the rule
lives in one place.
"""
from .company import TICKERS_URL, parse_ticker_list

_PARSED = {}        # text -> map. A backfill asks for the same ticker file every replayed day; parse it once.


def sec_headers(cfg):
    ua = cfg.sec_user_agent
    if "@" not in ua:
        raise RuntimeError("SEC_USER_AGENT must contain a contact email (the SEC requires it)")
    return {"User-Agent": ua}


def ticker_ciks(http, headers):
    """{TICKER: cik} from SEC's company_tickers.json. Tickers use '-' for share classes (BRK-B)."""
    text = http.get(TICKERS_URL, headers=headers)
    hit = _PARSED.get(text)
    if hit is None:
        hit = {t: cik for t, (cik, _name) in parse_ticker_list(text).items()}
        if len(_PARSED) >= 4:
            _PARSED.clear()
        _PARSED[text] = hit
    return hit
