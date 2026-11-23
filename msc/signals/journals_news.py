"""Financial journals and news speculation, from RSS feeds (no key).

Tone comes from the headline and summary. Speculation (could / may / reportedly / rumor ...) lowers confidence,
because a hedged report is weaker evidence than a stated fact. The share of speculative items is also reported,
since a spike in speculation is itself informative about market nerves.
"""
import re
import statistics
import xml.etree.ElementTree as ET

from ..models import Observation
from .base import Signal, register
from .lexicon import is_speculative, polarity

CASHTAG = re.compile(r"\$([A-Z]{1,5})\b")
TICKER = re.compile(r"\b([A-Z]{3,5})\b")


def parse_rss(text):
    root = ET.fromstring(text)
    items = []
    for it in root.iter("item"):
        title = it.findtext("title") or ""
        desc = re.sub(r"<[^>]+>", " ", it.findtext("description") or "")
        items.append(f"{title}. {desc}".strip())
    return items


@register
class JournalsNews(Signal):
    name = "journals_news"
    description = "Tone of financial news and journals, discounted by how speculative the reporting is."
    needs = ("MSC_RSS_FEEDS (optional)",)

    def fetch_items(self, ctx):
        items, errors = [], []
        for url in ctx.cfg.rss_feeds:
            try:
                items += parse_rss(ctx.http.get(url))
            except Exception as exc:
                errors.append(f"{url.split('/')[2]}: {str(exc)[:60]}")
        ctx.store.kv_set("news:feed_errors", errors)       # shown in the logs and kept for debugging
        if not items:
            raise RuntimeError(f"no news items retrieved ({len(errors)} feeds failed): " + " | ".join(errors))
        return items

    def collect(self, ctx):
        items = self.fetch_items(ctx)
        watch = set(ctx.cfg.watchlist)
        spec = [is_speculative(x) for x in items]
        spec_share = sum(spec) / len(items)
        per = {}
        for text, s in zip(items, spec):
            names = (set(CASHTAG.findall(text)) | set(TICKER.findall(text))) & watch
            for t in names:
                per.setdefault(t, []).append((polarity(text), s))
        out = []
        for t, rows in per.items():
            tone = statistics.fmean(p for p, _ in rows)
            sshare = sum(s for _, s in rows) / len(rows)
            out.append(Observation(self.name, ctx.day, "ticker", t, max(-1.0, min(1.0, 2 * tone)),
                                   min(1.0, len(rows) / 5) * (1 - 0.5 * sshare),
                                   {"items": len(rows), "speculative_share": round(sshare, 2)}))
        tone = statistics.fmean(polarity(x) for x in items)
        out.append(Observation(self.name, ctx.day, "market", "", max(-1.0, min(1.0, 2 * tone)),
                               min(1.0, len(items) / 30) * (1 - 0.5 * spec_share),
                               {"items": len(items), "speculative_share": round(spec_share, 2)}))
        return out
