"""Download daily, split-adjusted stock prices from Alpaca's free market-data API into a folder of CSVs.

Used by `python -m msc stock-prices`, which fills the folder that per-stock scoring reads (msc/stock_scoring.py).
Needs a free Alpaca account: ALPACA_KEY_ID and ALPACA_SECRET_KEY. The keys are sent only to data.alpaca.markets
and never written anywhere.

Caveats:
  * The free feed is IEX, one exchange rather than the consolidated tape. Daily closes can differ slightly from
    the official close; fine for 20-day excess returns, not for anything intraday.
  * `adjustment=all` adjusts for splits and dividends, so returns are total returns and a split is not a crash.
  * Free history starts around 2016. Delisted or renamed tickers return nothing (survivorship).
  * The JSON layout ({"bars": [{"t", "o", "c", ...}], "next_page_token"}) matches the client in your earlier
    edgar-copytrader backtest; this module has not been run against the live API from the build workspace.
"""
import csv
import io
import json
import os

DATA_BASE = "https://data.alpaca.markets/v2/stocks/{symbol}/bars"


def keys_from_env(env=None):
    env = os.environ if env is None else env
    key, secret = env.get("ALPACA_KEY_ID", "").strip(), env.get("ALPACA_SECRET_KEY", "").strip()
    if not (key and secret):
        raise SystemExit("set ALPACA_KEY_ID and ALPACA_SECRET_KEY (a free Alpaca account) to download stock prices")
    return key, secret


def fetch_bars(http, symbol, start, end, key, secret, feed="iex"):
    """[(iso_day, open, close)] oldest first, split- and dividend-adjusted."""
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret, "Accept": "application/json"}
    out, token = {}, None
    while True:
        params = {"timeframe": "1Day", "start": start, "end": end, "adjustment": "all", "feed": feed, "limit": 10000}
        if token:
            params["page_token"] = token
        body = json.loads(http.get(DATA_BASE.format(symbol=symbol.upper().replace("-", ".")), params=params,
                                   headers=headers))
        for b in body.get("bars") or []:
            try:
                out[b["t"][:10]] = (b["t"][:10], float(b["o"]), float(b["c"]))
            except (KeyError, TypeError, ValueError):
                continue
        token = body.get("next_page_token")
        if not token:
            return sorted(out.values())


def to_csv(rows):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["Date", "Open", "Close"])
    for d, o, c in rows:
        w.writerow([d, f"{o:.6f}", f"{c:.6f}"])
    return buf.getvalue()


def download(http, tickers, folder, start, end, key, secret, refresh=False, log=print):
    """Write <folder>/<TICKER>.csv for each ticker. Existing files are kept unless refresh. Returns {ticker: status}."""
    os.makedirs(folder, exist_ok=True)
    status = {}
    for t in tickers:
        path = os.path.join(folder, f"{t.upper().replace('.', '-')}.csv")
        if os.path.exists(path) and not refresh:
            status[t] = "kept"
            continue
        try:
            rows = fetch_bars(http, t, start, end, key, secret)
        except Exception as exc:                       # one bad symbol must not stop the rest
            msg = str(exc).replace(key, "***").replace(secret, "***")
            status[t] = f"failed: {msg[:160]}"
            log(f"  {t}: {status[t]}")
            continue
        if not rows:
            status[t] = "no bars"
            log(f"  {t}: no bars (unknown or delisted symbol?)")
            continue
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(to_csv(rows))
        os.replace(tmp, path)
        status[t] = f"{len(rows)} days, {rows[0][0]} .. {rows[-1][0]}"
        log(f"  {t}: {status[t]}")
    return status
