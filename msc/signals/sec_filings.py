"""SEC filings: insider open-market purchases and sales from Form 4.

Per ticker: a cluster of insider buying scores bullish (more insiders and more dollars = stronger).
Market level: the share of insider dollars that are purchases over the last 30 days, compared with its own
history. The market reading stays silent (confidence 0) until 60 days of history exist, so it never leans on a
baseline we made up.

Sales are counted only for the market-level share; they are mostly pre-planned and noisy, so they are not scored
per ticker.
"""
import re
import statistics
import xml.etree.ElementTree as ET
from datetime import date, timedelta

from ..models import Observation
from ..sec import sec_headers
from .base import Signal, register

BASE = "https://www.sec.gov/Archives/"
INDEX_LINE = re.compile(r"^4\s+(.*?)\s+(\d{3,10})\s+(\d{8})\s+(edgar/data/\S+)\s*$")
MAX_FILINGS_PER_RUN = 300
BACKFILL_DAYS = 5


def index_url(day):
    q = (day.month - 1) // 3 + 1
    return f"{BASE}edgar/daily-index/{day.year}/QTR{q}/form.{day:%Y%m%d}.idx"


def parse_index(text):
    """Form 4 filing paths listed in a daily form index."""
    return [m.group(4) for line in text.splitlines() if (m := INDEX_LINE.match(line))]


def parse_form4(text):
    """List of dicts {ticker, owner, code, usd, officer, planned} for open-market buys (P) and sales (S)."""
    start = text.find("<ownershipDocument")
    end = text.find("</ownershipDocument>")
    if start < 0 or end < 0:
        return []
    root = ET.fromstring(text[start:end + len("</ownershipDocument>")])
    ticker = (root.findtext("issuer/issuerTradingSymbol") or "").strip().upper()
    owner = (root.findtext("reportingOwner/reportingOwnerId/rptOwnerCik") or "").strip()
    rel = root.find("reportingOwner/reportingOwnerRelationship")
    officer = bool(rel is not None and (rel.findtext("isOfficer") or "").strip() in ("1", "true"))
    planned = (root.findtext("aff10b5One") or "").strip() in ("1", "true") or "10b5-1" in text
    out = []
    for tx in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        code = (tx.findtext("transactionCoding/transactionCode") or "").strip()
        if code not in ("P", "S"):
            continue
        try:
            shares = float(tx.findtext("transactionAmounts/transactionShares/value") or 0)
            price = float(tx.findtext("transactionAmounts/transactionPricePerShare/value") or 0)
        except ValueError:
            continue
        if ticker and shares > 0 and price > 0:
            out.append(dict(ticker=ticker, owner=owner, code=code, usd=shares * price, officer=officer,
                            planned=planned))
    return out


def day_aggregate(transactions):
    """Collapse one day's transactions into {buy_usd, sell_usd, tickers: {T: {buy, insiders}}}."""
    agg = {"buy_usd": 0.0, "sell_usd": 0.0, "tickers": {}}
    for t in transactions:
        if t["code"] == "S":
            agg["sell_usd"] += t["usd"]
        elif not t["planned"]:
            agg["buy_usd"] += t["usd"]
            rec = agg["tickers"].setdefault(t["ticker"], {"buy": 0.0, "insiders": []})
            rec["buy"] += t["usd"]
            if t["owner"] not in rec["insiders"]:
                rec["insiders"].append(t["owner"])
    return agg


@register
class SecFilings(Signal):
    name = "sec_filings"
    description = "Insider open-market buying clusters (Form 4) per ticker, and market-wide insider buy share."
    needs = ("SEC_USER_AGENT",)

    def preflight(self, cfg):
        sec_headers(cfg)

    def collect(self, ctx):
        headers = sec_headers(ctx.cfg)
        today = date.fromisoformat(ctx.day)
        budget = MAX_FILINGS_PER_RUN
        for back in range(BACKFILL_DAYS, -1, -1):
            d = today - timedelta(days=back)
            if d.weekday() >= 5 or ctx.store.kv_get(f"sec:day:{d.isoformat()}") is not None:
                continue
            try:
                paths = parse_index(ctx.http.get(index_url(d), headers=headers))
            except Exception:
                continue                                  # holiday, or not published yet: try again next run
            txs = []
            for path in paths[:budget]:
                try:
                    txs += parse_form4(ctx.http.get(BASE + path, headers=headers))
                except Exception:
                    continue
            budget = max(0, budget - len(paths))
            ctx.store.kv_set(f"sec:day:{d.isoformat()}", day_aggregate(txs))
        return self._observations(ctx, today)

    def _observations(self, ctx, today):
        days = ctx.store.kv_prefix("sec:day:")
        by_day = {k.rsplit(":", 1)[1]: v for k, v in days.items()}
        out = []
        recent = [by_day[d] for d in by_day if today - timedelta(days=14) <= date.fromisoformat(d) <= today]
        merged = {}
        for agg in recent:
            for t, rec in agg["tickers"].items():
                m = merged.setdefault(t, {"buy": 0.0, "insiders": set()})
                m["buy"] += rec["buy"]
                m["insiders"].update(rec["insiders"])
        for t, m in merged.items():
            n = len(m["insiders"])
            score = min(1.0, m["buy"] / 250_000) * min(1.0, 0.4 + 0.3 * n)
            out.append(Observation(self.name, ctx.day, "ticker", t, score, min(1.0, 0.4 + 0.2 * n),
                                   {"buy_usd": round(m["buy"]), "insiders": n}))
        out += self._market(ctx, today, by_day)
        return out

    def _market(self, ctx, today, by_day):
        def share(end):
            win = [v for d, v in by_day.items() if end - timedelta(days=30) < date.fromisoformat(d) <= end]
            b, s = sum(v["buy_usd"] for v in win), sum(v["sell_usd"] for v in win)
            return b / (b + s) if b + s > 0 else None

        ends = sorted(date.fromisoformat(d) for d in by_day)
        if len(ends) < 60:
            return [Observation(self.name, ctx.day, "market", "", 0.0, 0.0, {"history_days": len(ends)})]
        history = [x for x in (share(e) for e in ends[:-1]) if x is not None]
        now = share(today)
        if now is None or len(history) < 30:
            return []
        sd = statistics.pstdev(history) or 1e-9
        z = (now - statistics.fmean(history)) / sd
        return [Observation(self.name, ctx.day, "market", "", max(-1.0, min(1.0, z / 2)), 1.0,
                            {"buy_share_30d": round(now, 4), "z": round(z, 2)})]
