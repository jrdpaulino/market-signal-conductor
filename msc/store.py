"""SQLite storage (one file; on a hosted volume it survives restarts)."""
import json
import sqlite3
import threading

from .models import Observation

SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    day TEXT, signal TEXT, scope TEXT, key TEXT, score REAL, confidence REAL, meta TEXT,
    PRIMARY KEY (day, signal, scope, key));
CREATE TABLE IF NOT EXISTS regime_beliefs (day TEXT PRIMARY KEY, belief TEXT, p_up REAL, regime TEXT);
CREATE TABLE IF NOT EXISTS stock_beliefs (day TEXT, ticker TEXT, belief TEXT, tilt REAL, PRIMARY KEY (day, ticker));
CREATE TABLE IF NOT EXISTS signal_health (day TEXT, signal TEXT, status TEXT, n_obs INTEGER, detail TEXT,
    PRIMARY KEY (day, signal));
CREATE TABLE IF NOT EXISTS signal_weights (day TEXT, signal TEXT, weight REAL, n INTEGER, corr REAL,
    PRIMARY KEY (day, signal));
CREATE TABLE IF NOT EXISTS forecasts (day TEXT PRIMARY KEY, horizon INTEGER, p_up REAL, regime TEXT);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.RLock()
        with self.lock:
            self.db.executescript(SCHEMA)
            self.db.commit()

    def _run(self, sql, args=(), many=False):
        with self.lock:
            cur = self.db.executemany(sql, args) if many else self.db.execute(sql, args)
            self.db.commit()
            return cur

    def _all(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    # ---- observations
    def put_obs(self, observations):
        self._run("INSERT OR REPLACE INTO observations VALUES (?,?,?,?,?,?,?)",
                  [(o.day, o.signal, o.scope, o.key, o.score, o.confidence, json.dumps(o.meta)) for o in observations],
                  many=True)

    def obs_on(self, day):
        rows = self._all("SELECT signal, scope, key, score, confidence, meta FROM observations WHERE day=?", (day,))
        return [Observation(s, day, sc, k, score, conf, json.loads(m or "{}")) for s, sc, k, score, conf, m in rows]

    def market_series(self, signal):
        """[(day, confidence-weighted mean score, mean confidence)] for a signal's market-scope readings."""
        rows = self._all("SELECT day, score, confidence FROM observations WHERE signal=? AND scope='market' "
                         "ORDER BY day", (signal,))
        out, by_day = [], {}
        for d, s, c in rows:
            by_day.setdefault(d, []).append((s, c))
        for d in sorted(by_day):
            tot = sum(c for _, c in by_day[d])
            if tot > 0:
                out.append((d, sum(s * c for s, c in by_day[d]) / tot, tot / len(by_day[d])))
        return out

    def obs_days(self):
        return [r[0] for r in self._all("SELECT DISTINCT day FROM observations ORDER BY day")]

    # ---- beliefs and forecasts
    def set_belief(self, day, belief, p_up, regime):
        self._run("INSERT OR REPLACE INTO regime_beliefs VALUES (?,?,?,?)", (day, json.dumps(belief), p_up, regime))

    def beliefs(self, limit=None):
        sql = "SELECT day, belief, p_up, regime FROM regime_beliefs ORDER BY day"
        rows = self._all(sql)
        rows = rows[-limit:] if limit else rows
        return [(d, json.loads(b), p, r) for d, b, p, r in rows]

    def last_belief(self, before=None):
        rows = self.beliefs()
        rows = [r for r in rows if before is None or r[0] < before]
        return rows[-1] if rows else None

    def set_stock_belief(self, day, ticker, belief, tilt):
        self._run("INSERT OR REPLACE INTO stock_beliefs VALUES (?,?,?,?)", (day, ticker, json.dumps(belief), tilt))

    def latest_stock_beliefs(self, before=None):
        """{ticker: (day, belief list, tilt)} using each ticker's newest row before `before`."""
        sql = "SELECT day, ticker, belief, tilt FROM stock_beliefs"
        args = ()
        if before:
            sql += " WHERE day < ?"
            args = (before,)
        out = {}
        for d, t, b, tilt in self._all(sql + " ORDER BY day", args):
            out[t] = (d, json.loads(b), tilt)
        return out

    def set_forecast(self, day, horizon, p_up, regime):
        self._run("INSERT OR REPLACE INTO forecasts VALUES (?,?,?,?)", (day, horizon, p_up, regime))

    def forecasts(self):
        return self._all("SELECT day, horizon, p_up, regime FROM forecasts ORDER BY day")

    # ---- health and weights
    def set_health(self, day, signal, status, n_obs, detail=""):
        self._run("INSERT OR REPLACE INTO signal_health VALUES (?,?,?,?,?)", (day, signal, status, n_obs, detail[:300]))

    def latest_health(self):
        out = {}
        for d, s, st, n, det in self._all("SELECT day, signal, status, n_obs, detail FROM signal_health ORDER BY day"):
            out[s] = dict(day=d, status=st, n_obs=n, detail=det)
        return out

    def set_weight(self, day, signal, weight, n, corr):
        self._run("INSERT OR REPLACE INTO signal_weights VALUES (?,?,?,?,?)", (day, signal, weight, n, corr))

    def latest_weights(self):
        out = {}
        for d, s, w, n, c in self._all("SELECT day, signal, weight, n, corr FROM signal_weights ORDER BY day"):
            out[s] = dict(day=d, weight=w, n=n, corr=c)
        return out

    def weight_history(self, days=120):
        """{signal: [[day, weight]]} for the last `days` calendar days that have weights."""
        rows = self._all("SELECT day, signal, weight FROM signal_weights WHERE day >= "
                         "(SELECT date(max(day), ?) FROM signal_weights) ORDER BY day", (f"-{int(days)} day",))
        out = {}
        for d, sig, w in rows:
            out.setdefault(sig, []).append([d, w])
        return out

    # ---- key/value scratch space for plugins and model parameters
    def kv_get(self, key, default=None):
        rows = self._all("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(rows[0][0]) if rows else default

    def kv_set(self, key, value):
        self._run("INSERT OR REPLACE INTO kv VALUES (?,?)", (key, json.dumps(value)))

    def kv_prefix(self, prefix):
        return {k: json.loads(v) for k, v in self._all("SELECT key, value FROM kv WHERE key LIKE ? ORDER BY key",
                                                        (prefix + "%",))}
