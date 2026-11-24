"""Settings, all from environment variables (a local .env file is read if present)."""
import json
import os
from dataclasses import dataclass, field

DEFAULT_WATCHLIST = "AAPL,MSFT,NVDA,AMZN,GOOGL,META,TSLA,JPM,XOM,UNH,JNJ,WMT,V,PG,HD,BAC,KO,PFE,INTC,AMD"
DEFAULT_FEEDS = ",".join([
    "https://www.federalreserve.gov/feeds/press_all.xml",
    "https://www.sec.gov/news/pressreleases.rss",
    "https://finance.yahoo.com/news/rssindex",
    "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "https://feeds.content.dowjones.io/public/rss/mw_topstories",
])
DEFAULT_PM_RULES = [
    # US recession odds: falling odds are good for stocks. Other countries and election-conditional markets are out.
    {"match": "recession", "bullish_if": "no",
     "exclude": r"\bUK\b|japan|canada|europe|germany|china|australia|democrat|republican|sweep|senate|house"},
    # Fed cuts: rising odds are good for stocks. "No cuts", hikes, pauses and other central banks are out.
    {"match": "(fed|fomc|federal reserve).{0,40}(cut|lower|decrease)|(cut|lower|decrease).{0,40}(fed|fomc|federal reserve)",
     "exclude": r"\bno\b|\bnot\b|hike|pause|increase|bank of|central bank|ecb|bps by 50|50\+|>25",
     "bullish_if": "yes", "search": "fed rate cut"},
    # Fed hikes: rising odds are bad for stocks. Only the plain 25bp-or-more hike markets, not "0bps" or "no change".
    {"match": "(fed|fomc|federal reserve).{0,40}(hike|increase)",
     "exclude": r"\bno\b|\bnot\b|0bps|bank of|central bank|ecb",
     "bullish_if": "no", "search": "fed rate hike"},
]
DEFAULT_KALSHI_SERIES = ("KXRECSSNBER", "KXFED", "KXFEDDECISION")


def load_dotenv(path=".env"):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8-sig") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _list(value):
    return [x.strip() for x in value.split(",") if x.strip()]


@dataclass(frozen=True)
class Config:
    db_path: str = "msc.sqlite"
    sec_user_agent: str = ""
    watchlist: tuple = ()
    signals: tuple = ()                 # empty = every registered signal
    extra_modules: tuple = ()           # extra Python modules that register more signals
    rss_feeds: tuple = ()
    reddit_subs: tuple = ("stocks", "investing", "wallstreetbets")
    pm_rules: tuple = ()
    pm_venues: tuple = ("polymarket", "kalshi")
    kalshi_series: tuple = DEFAULT_KALSHI_SERIES
    price_source: str = "fred"          # "fred" (S&P 500 closes, default), "stooq", or "file" (see price_file)
    price_file: str = ""                # CSV you downloaded (Date, Close, optional Open) when price_source is "file"
    price_symbol: str = "spy.us"        # Stooq symbol for the market anchor and for scoring
    target: str = "direction"          # what is forecast: direction, drawdown or vol_down (research targets, see weights.TARGETS)
    horizon: int = 20                   # trading days ahead that forecasts refer to
    calibrate: bool = True              # pull P(up) toward the track record (MSC_CALIBRATE=0 turns it off)
    run_hour_utc: int = 22
    run_minute_utc: int = 30
    port: int = 8080
    dash_user: str = "admin"
    dash_password: str = ""
    allow_open_dashboard: bool = False

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        g = env.get
        return cls(
            db_path=g("MSC_DB", "msc.sqlite"),
            sec_user_agent=g("SEC_USER_AGENT", ""),
            watchlist=tuple(x.upper() for x in _list(g("MSC_WATCHLIST", DEFAULT_WATCHLIST))),
            signals=tuple(_list(g("MSC_SIGNALS", ""))),
            extra_modules=tuple(_list(g("MSC_EXTRA_MODULES", ""))),
            rss_feeds=tuple(_list(g("MSC_RSS_FEEDS", DEFAULT_FEEDS))),
            reddit_subs=tuple(_list(g("MSC_REDDIT_SUBS", "stocks,investing,wallstreetbets"))),
            pm_rules=tuple(json.loads(g("MSC_PM_RULES", "")) if g("MSC_PM_RULES") else DEFAULT_PM_RULES),
            pm_venues=tuple(_list(g("MSC_PM_VENUES", "polymarket,kalshi"))),
            kalshi_series=tuple(_list(g("MSC_KALSHI_SERIES", ",".join(DEFAULT_KALSHI_SERIES)))),
            price_source=g("MSC_PRICE_SOURCE", "fred").lower(),
            price_file=g("MSC_PRICE_FILE", ""),
            price_symbol=g("MSC_PRICE_SYMBOL", "spy.us"),
            horizon=int(g("MSC_HORIZON", "20")),
            target=g("MSC_TARGET", "direction"),
            calibrate=g("MSC_CALIBRATE", "1") != "0",
            run_hour_utc=int(g("MSC_RUN_HOUR_UTC", "22")),
            run_minute_utc=int(g("MSC_RUN_MINUTE_UTC", "30")),
            port=int(g("PORT", g("MSC_PORT", "8080"))),
            dash_user=g("MSC_DASH_USER", "admin"),
            dash_password=g("MSC_DASH_PASSWORD", ""),
            allow_open_dashboard=g("MSC_ALLOW_OPEN", "") == "1",
        )
