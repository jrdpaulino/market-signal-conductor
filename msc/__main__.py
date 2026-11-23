"""Command line:  python -m msc serve | run-once | learn | signals | status"""
import argparse
import json
import logging
import threading

from . import scheduler, web
from .config import Config, load_dotenv
from .engine import Conductor
from .http import Http
from .prices import FredPrices, StooqPrices
from .signals.base import load_signals
from .store import Store


def build(cfg):
    http = Http()
    prices = StooqPrices(http, cfg.price_symbol) if cfg.price_source == "stooq" else FredPrices(http)
    return Conductor(cfg, Store(cfg.db_path), http, load_signals(cfg), prices)


def main(argv=None):
    load_dotenv()
    ap = argparse.ArgumentParser(prog="msc", description="Market Signal Conductor")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="web dashboard + daily scheduler (what the host runs)")
    r = sub.add_parser("run-once", help="collect signals and update the model for one day")
    r.add_argument("--day", help="ISO date, default today (UTC)")
    sub.add_parser("learn", help="re-fit the Markov model from stored history")
    sub.add_parser("signals", help="list registered signals")
    sub.add_parser("status", help="print the current state as JSON")
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
    elif a.cmd == "pm-debug":
        import re
        from .engine import today_utc
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
        finally:
            stop.set()


if __name__ == "__main__":
    main()
