"""The conductor: collect signals, update the Markov layers, record forecasts, score them later."""
from datetime import datetime, timezone

from . import weights as W
from .calibration import apply_calibration, fit_calibration
from .company import Companies
from .execution.gate import check_gate, track_record
from .quotes import Quotes
from .markov import REGIMES, STOCK_STATES, RegimeModel, StockModel, logpdf
from .signals.base import Context

UNIFORM = [0.25] * 4


def _days_before(iso_day, n):
    from datetime import date, timedelta
    return (date.fromisoformat(iso_day) - timedelta(days=n)).isoformat()


def today_utc():
    return datetime.now(timezone.utc).date().isoformat()


class Conductor:
    def __init__(self, cfg, store, http, signals, prices=None):
        self.cfg, self.store, self.http, self.signals, self.prices = cfg, store, http, signals, prices
        self.stock_model = StockModel()
        self.companies = Companies(http, store, cfg.sec_user_agent)
        self.quotes = Quotes(http)

    # ---------------------------------------------------------------- model parameters
    def load_model(self):
        saved = self.store.kv_get("model")
        return RegimeModel.from_dict(saved) if saved else RegimeModel()

    # ---------------------------------------------------------------- one day
    def run_day(self, day=None):
        day = day or today_utc()
        ctx = Context(day, self.cfg, self.http, self.store, self.prices)
        health = {}
        for sig in self.signals:
            try:
                obs = [o for o in sig.collect(ctx) if o.day == day]
                self.store.put_obs(obs)
                self.store.set_health(day, sig.name, "ok", len(obs))
                health[sig.name] = ("ok", len(obs))
            except Exception as exc:                        # one broken source must not stop the rest
                self.store.set_health(day, sig.name, "error", 0, f"{type(exc).__name__}: {exc}")
                health[sig.name] = ("error", str(exc))

        weights = self.update_weights(day)
        model = self.load_model()
        obs_today = self.store.obs_on(day)

        prev = self.store.last_belief(before=day)
        belief = prev[1] if prev else UNIFORM
        market = self._by_signal([o for o in obs_today if o.scope == "market"], weights)
        belief = model.step(belief, market)
        regime = REGIMES[max(range(4), key=lambda i: belief[i])]
        p_raw = model.forecast_p_up(belief)
        cal = self.calibration_on(day) if self.cfg.calibrate else fit_calibration([], self.cfg.horizon)
        p_up = apply_calibration(p_raw, cal)
        self.store.kv_set(f"praw:{day}", p_raw)              # the model's own number, kept next to the calibrated one
        self.store.kv_set("calib", dict(cal, day=day))
        self.store.set_belief(day, belief, p_up, regime)
        self.store.set_forecast(day, self.cfg.horizon, p_up, regime)

        tickers = self._step_stocks(day, obs_today, weights, belief)
        return dict(day=day, regime=regime, belief=dict(zip(REGIMES, belief)), p_up=p_up, p_raw=p_raw, health=health,
                    weights={k: v[0] for k, v in weights.items()}, tickers_updated=tickers)

    @staticmethod
    def _by_signal(observations, weights):
        """{signal: (confidence-weighted mean score, reliability weight x mean confidence)}."""
        groups = {}
        for o in observations:
            groups.setdefault(o.signal, []).append(o)
        out = {}
        for sig, rows in groups.items():
            tot = sum(o.confidence for o in rows)
            if tot <= 0:
                continue
            score = sum(o.score * o.confidence for o in rows) / tot
            out[sig] = (score, weights.get(sig, (W.PRIOR_WEIGHT,))[0] * tot / len(rows))
        return out

    def _step_stocks(self, day, obs_today, weights, regime_belief):
        by_ticker = {}
        for o in obs_today:
            if o.scope == "ticker":
                by_ticker.setdefault(o.key, []).append(o)
        existing = self.store.latest_stock_beliefs(before=day)
        touched = 0
        for t in sorted(set(by_ticker) | set(existing)):
            belief = existing[t][1] if t in existing else [1 / 3] * 3
            obs = self._by_signal(by_ticker.get(t, []), weights)
            if obs:
                self.store.kv_set(f"ev:{t}", {"day": day, "signals": sorted(obs)})
            new = self.stock_model.step(belief, obs, regime_belief)
            self.store.set_stock_belief(day, t, new, StockModel.tilt(new))
            touched += 1
        return touched

    # ---------------------------------------------------------------- weights and track record
    def price_history(self):
        try:
            return self.prices() if self.prices else []
        except Exception:
            return []

    def update_weights(self, day):
        prices = self.price_history()
        computed = W.compute_weights(self.store, prices, self.cfg.horizon, day)
        for sig, (w, n, corr) in computed.items():
            self.store.set_weight(day, sig, w, n, corr if corr == corr else None)
        return computed

    def outcome_of(self, day, horizon):
        r = W.forward_return(self.price_history(), day, horizon)
        return None if r is None else int(r > 0)

    def track(self):
        prices = self.price_history()
        return track_record(self.store.forecasts(), lambda d, h: (
            None if (r := W.forward_return(prices, d, h)) is None else int(r > 0)))

    def scored_forecasts(self, raw=False):
        """[(p_up, outcome)] for every forecast whose outcome is now known. raw=True gives the model's own
        number before calibration (older forecasts, made before calibration existed, are already raw)."""
        prices = self.price_history()
        rows = self.store.forecasts()
        raws = self.store.kv_prefix("praw:") if raw else {}
        out = []
        for (d, _h, p, _r), r in zip(rows, W.forward_returns(prices, [x[0] for x in rows], self.cfg.horizon)):
            if r is not None:
                out.append((raws.get(f"praw:{d}", p) if raw else p, int(r > 0)))
        return out

    def calibration_on(self, day):
        """Calibration fitted only on forecasts whose outcome was known by `day`."""
        prices = [p for p in self.price_history() if p[0] <= day]
        rows = [x for x in self.store.forecasts() if x[0] < day]
        raws = self.store.kv_prefix("praw:")
        pairs = [(raws.get(f"praw:{d}", p), int(r > 0))
                 for (d, _h, p, _r), r in zip(rows, W.forward_returns(prices, [x[0] for x in rows], self.cfg.horizon))
                 if r is not None]
        return fit_calibration(pairs, self.cfg.horizon)

    # ---------------------------------------------------------------- learning from history
    def learn(self, as_of=None):
        """Re-fit the regime model's transitions, signal means and P(up) from everything stored.

        `as_of` (used by the backfill's walk-forward refit) hides prices after that day, so an outcome counts only
        once it was known by then. Live runs leave it unset: prices already end today."""
        weights = {s: (v["weight"],) for s, v in self.store.latest_weights().items()}
        days = self.store.obs_days()
        seq = []
        for d in days:
            obs = self._by_signal([o for o in self.store.obs_on(d) if o.scope == "market"], weights)
            if obs:
                seq.append((d, obs))
        model = self.load_model()
        gammas = model.fit([o for _d, o in seq])
        if gammas is None:
            return dict(fitted=False, reason=f"only {len(seq)} days of market readings, need 10")
        ups, gs = [], []
        prices = [p for p in self.price_history() if as_of is None or p[0] <= as_of]
        for (d, _o), g in zip(seq, gammas):
            r = W.forward_return(prices, d, self.cfg.horizon)
            if r is not None:
                ups.append(int(r > 0))
                gs.append(g)
        if gs:
            model.fit_p_up(gs, ups)
        self.store.kv_set("model", model.to_dict())
        return dict(fitted=True, days=len(seq), outcomes=len(gs))

    def influence(self, day):
        """Per signal, how much it counts on `day`: trust (weight), its share of the total say (weight x confidence),
        and the pull it exerts: how far it separates the most and least likely regimes, and toward which."""
        if not day:
            return []
        model, weights = self.load_model(), self.store.latest_weights()
        w = {s: (v["weight"],) for s, v in weights.items()}
        market = self._by_signal([o for o in self.store.obs_on(day) if o.scope == "market"], w)
        total = sum(v[1] for v in market.values())
        out = []
        for sig in self.signals:
            weight = weights.get(sig.name, {}).get("weight", W.PRIOR_WEIGHT)
            if sig.name in market:
                x, eff = market[sig.name]
                mu, sd = model.emission(sig.name)
                ll = [eff * logpdf(x, mu[i], sd[i]) for i in range(4)]
                top = max(range(4), key=lambda i: ll[i])
                out.append(dict(name=sig.name, weight=weight, effective=eff, share=eff / total if total else 0.0,
                                score=x, pull=max(ll) - min(ll), leans=REGIMES[top]))
            else:
                out.append(dict(name=sig.name, weight=weight, effective=0.0, share=0.0, score=None, pull=0.0, leans=""))
        return out

    # ---------------------------------------------------------------- dashboard payload
    def state(self):
        beliefs = self.store.beliefs()
        latest = beliefs[-1] if beliefs else None
        stocks = self.store.latest_stock_beliefs()
        evidence = {k[3:]: v for k, v in self.store.kv_prefix("ev:").items()}
        cutoff = latest[0] if latest else ""
        recent = {t for t, e in evidence.items() if cutoff and e["day"] >= _days_before(cutoff, 30)}
        ranked = sorted(((t, v[2], v[1], v[0]) for t, v in stocks.items() if t in recent), key=lambda x: -x[1])
        strong = [x for x in ranked if x[1] > 0][:10]
        weak = [x for x in ranked[::-1] if x[1] < 0][:10]
        info = self.companies.lookup([x[0] for x in strong + weak])
        track = self.track()
        passed, reasons = check_gate(track) if track["n"] is not None else (False, [])
        weights, health = self.store.latest_weights(), self.store.latest_health()
        return dict(
            as_of=latest[0] if latest else None,
            regime=latest[3] if latest else None,
            belief=dict(zip(REGIMES, latest[1])) if latest else {},
            p_up=latest[2] if latest else None,
            calibration=self.store.kv_get("calib"),
            horizon=self.cfg.horizon,
            history=[dict(day=d, p_up=p, regime=r, belief=dict(zip(REGIMES, b))) for d, b, p, r in
                     [(x[0], x[1], x[2], x[3]) for x in beliefs[-120:]]],
            signals=[dict(name=s.name, description=s.description, needs=list(s.needs),
                          weight=weights.get(s.name, {}).get("weight", W.PRIOR_WEIGHT),
                          pairs=weights.get(s.name, {}).get("n", 0),
                          corr=weights.get(s.name, {}).get("corr"),
                          health=health.get(s.name, {"status": "never run", "detail": "", "n_obs": 0}))
                     for s in self.signals],
            tilts=dict(
                strongest=[dict(ticker=t, tilt=tl, p_strong=b[2], p_weak=b[0], day=d, evidence=evidence[t]["signals"],
                                company=info[t]["name"], industry=info[t]["industry"]) for t, tl, b, d in strong],
                weakest=[dict(ticker=t, tilt=tl, p_strong=b[2], p_weak=b[0], day=d, evidence=evidence[t]["signals"],
                              company=info[t]["name"], industry=info[t]["industry"]) for t, tl, b, d in weak],
                states=list(STOCK_STATES)),
            influence=self.influence(latest[0] if latest else None), weight_history=self.store.weight_history(),
            track=track, gate=dict(passed=passed, reasons=reasons))
