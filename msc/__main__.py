"""Command line:  python -m msc serve | run-once | learn | signals | status | backfill | stock-prices | stock-score"""
import argparse
import json
import logging
import threading

from . import scheduler, web
from .config import Config, load_dotenv
from .engine import Conductor, today_utc
from .http import Http
from .logs import write_log
from .prices import FilePrices, FredPrices, StooqPrices
from .signals.base import load_signals
from .store import Store


def build(cfg):
    http = Http()
    if cfg.price_source == "file":
        if not cfg.price_file:
            raise SystemExit("MSC_PRICE_SOURCE=file needs MSC_PRICE_FILE=<path to a Date,Close CSV>")
        prices = FilePrices(cfg.price_file)
    elif cfg.price_source == "stooq":
        prices = StooqPrices(http, cfg.price_symbol)
    else:
        prices = FredPrices(http)
    return Conductor(cfg, Store(cfg.db_path), http, load_signals(cfg), prices)


def backfill_summary(name, r):
    pl = r.get("placebo")
    best = pl["results"][0] if isinstance(pl, dict) and pl.get("results") else None
    return (f"[{name}] commit {r.get('commit')}  target {r.get('target', 'direction')} {r.get('horizon', '')}d  forecasts {r['scored_forecasts']}  hit {r['hit_rate']:.3f}  "
            f"brier {r['brier']:.4f}  running base {r['brier_running_base_rate']:.4f}  hindsight base {r['brier_base_rate']:.4f}  "
            f"gate {r['gate_would_say']}"
            + (f"  best placebo p {best['p']} ({best['signal']})" if best else ""))


def main(argv=None):
    load_dotenv()
    ap = argparse.ArgumentParser(prog="msc", description="Market Signal Conductor")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="web dashboard + daily scheduler (what the host runs)")
    r = sub.add_parser("run-once", help="collect signals and update the model for one day")
    r.add_argument("--day", help="ISO date, default today (UTC)")
    sub.add_parser("learn", help="re-fit the Markov model from stored history")
    sub.add_parser("signals", help="list registered signals")
    sub.add_parser("overlap", help="which signals move together (the model treats them as independent)")
    sub.add_parser("status", help="print the current state as JSON")
    b = sub.add_parser("backfill", help="replay FRED macro + S&P 500 signals over past years into a separate database")
    b.add_argument("--start", help="ISO date, default: 5 years before --end")
    b.add_argument("--end", help="ISO date, default: today")
    b.add_argument("--step", type=int, default=1, help="replay every Nth trading day (default 1)")
    b.add_argument("--db", help="output database, default: <MSC_DB>.backfill.sqlite")
    b.add_argument("--refit-every", type=int, default=0, help="walk-forward re-fit of the model every N replayed days, after the first 120 (0 = never)")
    b.add_argument("--target", default=None, help="what to forecast: direction (default), drawdown or vol_down")
    b.add_argument("--horizon", type=int, default=None, help="trading days ahead (default: MSC_HORIZON, 20)")
    b.add_argument("--signals", default=None, help="comma list of signals to replay (default: the backfillable set; earnings_drift is opt-in)")
    b.add_argument("--split", type=int, default=0, help="also score N consecutive slices of the history separately (2 = halves)")
    b.add_argument("--placebo", type=int, default=200, help="time-rotated placebo copies of each signal to compare against (0 = skip)")
    b.add_argument("--compare", action="store_true", help="run twice (no re-fit, then re-fit every 63 days) and compare")
    sp = sub.add_parser("stock-prices", help="download daily split-adjusted stock prices from Alpaca for per-stock scoring")
    sp.add_argument("--dir", default=None, help="folder for <TICKER>.csv files (default: MSC_STOCK_PRICE_DIR, stock_prices)")
    sp.add_argument("--tickers", default=None, help="comma list (default: MSC_EARNINGS_TICKERS or the watchlist); SPY is always added")
    sp.add_argument("--start", default="2016-01-01", help="ISO date, default 2016-01-01 (about where Alpaca's free history starts)")
    sp.add_argument("--end", default=None, help="ISO date, default today")
    sp.add_argument("--refresh", action="store_true", help="download again even if a file exists")
    ss = sub.add_parser("stock-score", help="score the per-stock readings in a database against each stock's excess return")
    ss.add_argument("--db", default=None, help="database to score (default: the backfill database)")
    ss.add_argument("--dir", default=None, help="price folder (default: MSC_STOCK_PRICE_DIR, stock_prices)")
    ss.add_argument("--horizon", type=int, default=None, help="trading days ahead (default: MSC_HORIZON, 20)")
    ss.add_argument("--split", type=int, default=0, help="also score N consecutive slices separately (2 = halves)")
    ss.add_argument("--placebo", type=int, default=200, help="calendar-rotated placebo copies (0 = skip)")
    d = sub.add_parser("pm-debug", help="show what each prediction-market venue returns for a search pattern")
    d.add_argument("pattern", nargs="?", default="recession|rate cut|fed")
    a = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = Config.from_env()
    cond = build(cfg)

    if a.cmd == "signals":
        for s in cond.signals:
            print(f"{s.name:<20} {s.description}  needs: {', '.join(s.needs) or '-'}")
    elif a.cmd == "run-once":
        print(json.dumps(cond.run_day(a.day), indent=2, default=str))
    elif a.cmd == "learn":
        print(json.dumps(cond.learn(), indent=2))
    elif a.cmd == "overlap":
        from .weights import signal_overlap
        rows = signal_overlap(cond.store)
        print(json.dumps(rows, indent=2) if rows else "not enough overlapping days yet (need 30)")
    elif a.cmd == "backfill":
        from datetime import date as _d
        from dataclasses import replace
        from .backfill import backfill_path, run_backfill
        from .weights import TARGETS
        names = [n.strip() for n in a.signals.split(",") if n.strip()] if a.signals else None
        if names:
            from .backfill import BACKFILL_SIGNALS, OPT_IN_BACKFILL
            allowed = BACKFILL_SIGNALS + OPT_IN_BACKFILL
            bad = [n for n in names if n not in allowed]
            if bad:
                ap.error(f"cannot replay {bad}; backfillable signals: {', '.join(allowed)}")
        if a.target is not None and a.target not in TARGETS:
            ap.error(f"--target must be one of {', '.join(TARGETS)}")
        if a.target is not None or a.horizon is not None:      # research variants only change this replay, never the live setting
            cfg = replace(cfg, target=a.target or cfg.target, horizon=a.horizon or cfg.horizon)
            cond = build(cfg)
        end = a.end or today_utc()
        start = a.start or _d.fromisoformat(end).replace(year=_d.fromisoformat(end).year - 5).isoformat()
        from .backfill import BACKFILL_SIGNALS, preflight_message
        from .signals.base import REGISTRY, preflight_errors
        problems = preflight_errors([REGISTRY[n]() for n in (names or BACKFILL_SIGNALS)], cfg)
        if problems:
            raise SystemExit(preflight_message(problems))
        out = a.db or backfill_path(cfg)
        prog = lambda i, n, d: print(f"  {i}/{n} days, at {d}")
        step = max(1, a.step)
        if a.compare:
            print(f"replaying {start} .. {end}, twice: plain and with a walk-forward re-fit every 63 days")
            plain = run_backfill(cfg, cond.http, cond.prices, start, end, out, step, prog, placebo=a.placebo, signal_names=names, split=a.split)
            refit = run_backfill(cfg, cond.http, cond.prices, start, end, backfill_path(cfg, "backfill.refit"), step, prog,
                                 refit_every=63, placebo=a.placebo, signal_names=names, split=a.split)
            keys = ("walk_forward_refit", "scored_forecasts", "hit_rate", "brier", "brier_base_rate", "brier_running_base_rate",
                    "beats_running_base_rate", "beats_base_rate", "brier_breakdown", "brier_breakdown_raw")
            full = dict(compare={"plain": {k: plain[k] for k in keys}, "walk_forward_refit": {k: refit[k] for k in keys}},
                        plain=plain, walk_forward_refit=refit)
            path = write_log("backfill", json.dumps(full, indent=2, default=str))
            for name, r in (("plain", plain), ("re-fit", refit)):
                print(backfill_summary(name, r))
        else:
            print(f"replaying {start} .. {end} into {out} (the live database is not touched)")
            r = run_backfill(cfg, cond.http, cond.prices, start, end, out, step, prog,
                             refit_every=max(0, a.refit_every), placebo=a.placebo, signal_names=names, split=a.split)
            path = write_log("backfill", json.dumps(r, indent=2, default=str))
            print(backfill_summary("run", r))
            if isinstance(r.get("stock_scoring"), dict):
                from .stock_scoring import stock_summary
                print(stock_summary(r["stock_scoring"]))
        print("full report:", path)
    elif a.cmd == "stock-prices":
        from . import alpaca
        key, secret = alpaca.keys_from_env()
        tickers = [t.strip().upper() for t in a.tickers.split(",") if t.strip()] if a.tickers else \
            list(cfg.earnings_tickers or cfg.watchlist)
        if "SPY" not in tickers:
            tickers.append("SPY")                      # the benchmark, from the same feed as the stocks
        folder = a.dir or cfg.stock_price_dir
        print(f"downloading {len(tickers)} tickers into {folder} (existing files kept; --refresh to replace)")
        status = alpaca.download(Http(min_interval=0.35), tickers, folder, a.start, a.end or today_utc(), key, secret,
                                 refresh=a.refresh)
        bad = [t for t, s in status.items() if not (s == "kept" or s[0].isdigit())]
        print(f"done: {len(status) - len(bad)} ok" + (f", no data for {', '.join(bad)}" if bad else ""))
    elif a.cmd == "stock-score":
        import os
        from .backfill import backfill_path
        from .stock_scoring import DirStockPrices, stock_report, stock_summary
        db = a.db or backfill_path(cfg)
        if not os.path.exists(db):
            raise SystemExit(f"no database at {db}; run a backfill first or pass --db")
        folder = a.dir or cfg.stock_price_dir
        stocks = DirStockPrices(folder)
        if not stocks.tickers():
            raise SystemExit(f"no price files in {folder}; run python -m msc stock-prices first")
        store = Store(db)
        try:
            rep = stock_report(store, stocks, cond.prices, a.horizon or cfg.horizon, a.placebo, a.split)
        finally:
            store.close()
        rep.update(database=db, price_folder=folder)
        path = write_log("stock-score", json.dumps(rep, indent=2, default=str))
        print(stock_summary(rep))
        print("full report:", path)
    elif a.cmd == "pm-debug":
        import re
        from .signals.base import Context
        from .signals.prediction_markets import PredictionMarkets
        ctx = Context(today_utc(), cfg, cond.http, cond.store, None)
        pat = re.compile(a.pattern, re.I)
        for venue in PredictionMarkets().venues(cfg):
            venue.extra_terms = [t for t in re.split(r'[|]', a.pattern) if re.fullmatch(r'[A-Za-z ]{3,30}', t.strip())]
            try:
                markets = venue.fetch(ctx)
            except Exception as exc:
                print(f"{venue.name}: FAILED {exc}")
                continue
            hits = [m for m in markets if pat.search(m["text"])]
            print(f"{venue.name}: {len(markets)} markets, {len(hits)} match {a.pattern!r}")
            for m in sorted(hits, key=lambda m: -m["volume"])[:10]:
                print(f"   p_yes={m['p_yes']:.2f}  vol={m['volume']:.0f}  {m['text'][:110]}")
            if not hits:
                print("   top by volume instead:")
                for m in sorted(markets, key=lambda m: -m["volume"])[:8]:
                    print(f"   p_yes={m['p_yes']:.2f}  {m['text'][:110]}")
                econ = re.compile(r"recess|\bfed\b|fomc|interest rate|rate cut|inflation|\bcpi\b|\bgdp\b|unemployment|s&p|nasdaq", re.I)
                near = [m for m in markets if econ.search(m["text"])]
                print(f"   economy-related titles: {len(near)}")
                for m in sorted(near, key=lambda m: -m["volume"])[:12]:
                    print(f"   p_yes={m['p_yes']:.2f}  {m['text'][:110]}")
    elif a.cmd == "status":
        print(json.dumps(cond.state(), indent=2, default=str))
    elif a.cmd == "serve":
        lock = threading.Lock()
        server = web.serve(cond, cfg, lock)
        stop = scheduler.start(cond, cfg, lock)
        logging.getLogger("msc").info("serving on port %s; daily run at %02d:%02d UTC", cfg.port,
                                      cfg.run_hour_utc, cfg.run_minute_utc)
        try:
            server.serve_forever()
        except KeyboardInterrupt:                      # Fly stops the app with SIGINT: exit quietly, not with a traceback
            logging.getLogger("msc").info("shutting down")
        finally:
            stop.set()
            server.server_close()


if __name__ == "__main__":
    main()
