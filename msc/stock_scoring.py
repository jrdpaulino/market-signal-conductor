"""Per-stock scoring: do a signal's ticker readings line up with how that stock then did against the market?

Ticker-scope readings move the per-stock tilts, but until now nothing checked them against outcomes; only market
readings earned weights. This scores them, for the backfill and for any database that holds ticker readings:

  outcome  = the stock's return over the horizon minus the benchmark's over the same dates (excess return),
             entering at the next day's open, exactly like the market forecasts are scored
  link     = correlation between the reading's score and that excess return, pooled over every (day, ticker)
  spread   = mean excess return after bullish readings minus after bearish ones, in percent
  placebo  = the whole calendar is rotated by a random offset (at least min_shift trading days) against the
             outcomes, the same offset for every ticker, k times. That keeps each signal's slowness, each stock's
             streaks and the cross-section intact but breaks the timing. p = share of rotations with |corr| at
             least the real one (+1 smoothing); p_adjusted multiplies by the number of signals tested.

Readings with zero confidence are skipped. A reading that repeats for weeks (earnings_drift keeps one for about
a quarter) makes neighbouring outcomes overlap heavily, so n overstates the independent evidence; the placebo is
the number to read, not n. Prices must be split-adjusted, or a split looks like a crash.
"""
import os
import random
from bisect import bisect_right

from . import weights as W
from .prices import parse_price_csv

BENCHMARK_FILE = "SPY"      # SPY.csv in the price folder is preferred as the benchmark, so both sides use one feed


class DirStockPrices:
    """Daily prices from a folder of <TICKER>.csv files (Date, Close, optional Open), split-adjusted.
    `python -m msc stock-prices` fills such a folder from Alpaca; any source that exports Date/Close works."""

    def __init__(self, folder):
        self.folder, self._cache = folder, {}

    def path(self, ticker):
        return os.path.join(self.folder, f"{ticker.upper().replace('.', '-')}.csv")

    def get(self, ticker):
        if ticker not in self._cache:
            p = self.path(ticker)
            if not os.path.exists(p):
                self._cache[ticker] = None
            else:
                with open(p, encoding="utf-8-sig", newline="") as f:
                    self._cache[ticker] = parse_price_csv(f.read()) or None
        return self._cache[ticker]

    def tickers(self):
        if not os.path.isdir(self.folder):
            return []
        return sorted(f[:-4].upper() for f in os.listdir(self.folder) if f.lower().endswith(".csv"))


def _forward_by_day(prices, days, horizon):
    return dict(zip(days, W.forward_returns(prices, days, horizon, "direction")))


def ticker_pairs(store, signal, stock_prices, bench_prices, horizon, first=None, last=None):
    """[(day, ticker, score, excess_return)] for one signal's ticker readings with a known outcome."""
    rows = store._all("SELECT day, key, score, confidence FROM observations WHERE signal=? AND scope='ticker' "
                      "AND confidence > 0 ORDER BY day", (signal,))
    by_ticker = {}
    for d, t, s, _c in rows:
        if (first is None or d >= first) and (last is None or d <= last):
            by_ticker.setdefault(t, []).append((d, s))
    days_all = sorted({d for v in by_ticker.values() for d, _s in v})
    bench = _forward_by_day(bench_prices, days_all, horizon)
    out = []
    for t, readings in sorted(by_ticker.items()):
        px = stock_prices.get(t)
        if not px:
            continue
        fwd = _forward_by_day(px, [d for d, _s in readings], horizon)
        for d, s in readings:
            r, b = fwd.get(d), bench.get(d)
            if r is not None and b is not None:
                out.append((d, t, s, r - b))
    return out


def _rotated_corr(pairs, cal_index, outcome, off, n_cal):
    xs, ys = [], []
    for d, t, s, _e in pairs:
        y = outcome.get((cal_index[d] + off) % n_cal, {}).get(t)
        if y is not None:
            xs.append(s)
            ys.append(y)
    return W.pearson(xs, ys) if len(xs) >= 3 else float("nan")


def score_signal(pairs, k=200, min_shift=130, seed=0):
    """Link, spread and placebo for one signal's [(day, ticker, score, excess)] pairs."""
    n = len(pairs)
    if n < 3:
        return dict(n=n, error="too few readings with a known outcome")
    xs, ys = [p[2] for p in pairs], [p[3] for p in pairs]
    corr = W.pearson(xs, ys)
    up = [e for _d, _t, s, e in pairs if s > 0]
    down = [e for _d, _t, s, e in pairs if s < 0]
    mean = lambda v: sum(v) / len(v) if v else None
    res = dict(n=n, tickers=len({p[1] for p in pairs}), days=len({p[0] for p in pairs}),
               corr=None if corr != corr else round(corr, 3),
               mean_excess_after_bullish_pct=None if not up else round(100 * mean(up), 2),
               mean_excess_after_bearish_pct=None if not down else round(100 * mean(down), 2),
               n_bullish=len(up), n_bearish=len(down))
    if up and down:
        res["spread_pct"] = round(100 * (mean(up) - mean(down)), 2)
    cal = sorted({p[0] for p in pairs})
    n_cal = len(cal)
    if k and corr == corr and n_cal >= 2 * min_shift:
        cal_index = {d: i for i, d in enumerate(cal)}
        outcome = {}
        for d, t, _s, e in pairs:
            outcome.setdefault(cal_index[d], {})[t] = e
        rng, hits = random.Random(seed), 0
        for _ in range(k):
            c = _rotated_corr(pairs, cal_index, outcome, rng.randint(min_shift, n_cal - min_shift), n_cal)
            hits += c == c and abs(c) >= abs(corr)
        res["p"] = round((hits + 1) / (k + 1), 3)
    elif k:
        res["p"] = None
        res["placebo_note"] = f"needs at least {2 * min_shift} distinct days, has {n_cal}"
    return res


def ticker_signals(store):
    return [r[0] for r in store._all("SELECT DISTINCT signal FROM observations WHERE scope='ticker' AND confidence > 0")]


def benchmark_prices(stock_prices, fallback):
    """SPY from the price folder when present (same feed and adjustment as the stocks), else the fallback."""
    spy = stock_prices.get(BENCHMARK_FILE)
    return (spy, BENCHMARK_FILE) if spy else (fallback() if callable(fallback) else fallback, "benchmark price source")


def stock_report(store, stock_prices, fallback_bench, horizon, k=200, split=0):
    """{signals: {name: result}, benchmark, missing_prices, split?} for every signal with ticker readings."""
    bench, bench_name = benchmark_prices(stock_prices, fallback_bench)
    if not bench:
        return dict(error="no benchmark prices")
    sigs = sorted(ticker_signals(store))
    out, missing = {}, set()
    for sig in sigs:
        tickers = {r[0] for r in store._all("SELECT DISTINCT key FROM observations WHERE signal=? AND scope='ticker'", (sig,))}
        missing |= {t for t in tickers if not stock_prices.get(t)}
        out[sig] = score_signal(ticker_pairs(store, sig, stock_prices, bench, horizon), k)
    tested = [s for s in out.values() if s.get("p") is not None]
    for s in tested:
        s["p_adjusted"] = round(min(1.0, s["p"] * len(tested)), 3)
    rep = dict(horizon=horizon, outcome="stock return minus benchmark return over the horizon, from the next open",
               benchmark=bench_name, signals=out, missing_prices=sorted(missing),
               note="Readings that repeat for weeks overlap heavily, so n overstates the evidence: read the placebo p. "
                    "Survivorship: only tickers you chose (usually today's names) are in the test.")
    if split and split > 1:
        rep["split"] = split_parts(store, stock_prices, bench, horizon, sigs, split, k)
    return rep


def split_parts(store, stock_prices, bench, horizon, sigs, parts, k):
    rows = store._all("SELECT DISTINCT day FROM observations WHERE scope='ticker' AND confidence > 0 ORDER BY day")
    days = [r[0] for r in rows]
    if len(days) < parts * 40:
        return dict(error="too few days to split")
    size = len(days) // parts
    out, signs = [], {}
    for i in range(parts):
        first, last = days[i * size], days[-1] if i == parts - 1 else days[(i + 1) * size - 1]
        part = dict(part=i + 1, first_day=first, last_day=last, signals={})
        for sig in sigs:
            r = score_signal(ticker_pairs(store, sig, stock_prices, bench, horizon, first, last), k,
                             min_shift=min(130, max(20, size // 4)))
            part["signals"][sig] = {key: r.get(key) for key in ("n", "corr", "spread_pct", "p")}
            if r.get("corr") is not None:
                signs.setdefault(sig, []).append(r["corr"])
        out.append(part)
    same = {s: len(c) == parts and (all(x > 0 for x in c) or all(x < 0 for x in c)) for s, c in signs.items()}
    return dict(parts=out, same_sign_in_every_part=same)


def stock_summary(rep):
    if "error" in rep:
        return f"[stocks] {rep['error']}"
    bits = []
    for sig, r in rep["signals"].items():
        if r.get("corr") is None:
            bits.append(f"{sig}: {r.get('error', 'no usable readings')}")
        else:
            bits.append(f"{sig}: n {r['n']} over {r['tickers']} stocks, corr {r['corr']:+.3f}, "
                        f"spread {r.get('spread_pct', 'n/a')}%, placebo p {r.get('p')}")
    miss = f"  (no prices for {', '.join(rep['missing_prices'])})" if rep["missing_prices"] else ""
    return "[stocks] " + "; ".join(bits) + miss
