"""Social media chatter from public Reddit RSS feeds (no key).

Per watchlist ticker (cashtags like $NVDA): average tone of posts mentioning it, trusted more as mentions grow.
Market level: average tone across all recent posts. Free social data is thin and noisy; treat this as the
weakest signal until its track record says otherwise. Higher-quality providers (paid APIs) plug in by
subclassing and overriding fetch_posts().
"""
import re
import statistics
import xml.etree.ElementTree as ET

from ..models import Observation
from .base import Signal, register
from .lexicon import polarity

ATOM = "{http://www.w3.org/2005/Atom}"
CASHTAG = re.compile(r"\$([A-Z]{1,5})\b")


def parse_atom(text):
    root = ET.fromstring(text)
    posts = []
    for entry in root.iter(ATOM + "entry"):
        title = entry.findtext(ATOM + "title") or ""
        body = re.sub(r"<[^>]+>", " ", entry.findtext(ATOM + "content") or "")
        posts.append(f"{title}. {body}")
    return posts


@register
class SocialChatter(Signal):
    name = "social"
    description = "Tone and volume of retail investor chatter (Reddit RSS by default)."
    needs = ("MSC_REDDIT_SUBS (optional)",)

    def fetch_posts(self, ctx):
        posts, errors = [], []
        headers = {"User-Agent": "python:market-signal-conductor:0.1 (personal research)"}
        for sub in ctx.cfg.reddit_subs:
            try:
                posts += parse_atom(ctx.http.get(f"https://www.reddit.com/r/{sub}/new/.rss",
                                                 params={"limit": "100"}, headers=headers))
            except Exception as exc:
                errors.append(f"r/{sub}: {str(exc)[:80]}")
        if not posts:
            raise RuntimeError("no social posts retrieved. " + " | ".join(errors))
        return posts

    def collect(self, ctx):
        posts = self.fetch_posts(ctx)
        watch = set(ctx.cfg.watchlist)
        per = {}
        for text in posts:
            tone = polarity(text)
            for tag in set(CASHTAG.findall(text)) & watch:
                per.setdefault(tag, []).append(tone)
        out = [Observation(self.name, ctx.day, "ticker", t, max(-1.0, min(1.0, 2 * statistics.fmean(v))),
                           min(1.0, len(v) / 10), {"mentions": len(v)}) for t, v in per.items()]
        tones = [polarity(p) for p in posts]
        out.append(Observation(self.name, ctx.day, "market", "", max(-1.0, min(1.0, 2 * statistics.fmean(tones))),
                               min(1.0, len(posts) / 100), {"posts": len(posts)}))
        return out
