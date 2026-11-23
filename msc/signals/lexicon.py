"""A small finance word list for the free text signals. Crude on purpose: it is a placeholder for a better
model (a paid sentiment feed, or an LLM scorer) and the plugin interface lets you swap that in."""
import re

POSITIVE = {"beat", "beats", "surge", "surges", "rally", "rallies", "soar", "soars", "gain", "gains", "upgrade",
            "upgraded", "bullish", "record", "growth", "strong", "rebound", "outperform", "buy", "calls", "moon",
            "breakout", "profit", "raises", "boost", "optimism", "cut rates", "easing"}
NEGATIVE = {"miss", "misses", "plunge", "plunges", "slump", "slumps", "fall", "falls", "drop", "drops", "downgrade",
            "downgraded", "bearish", "recession", "weak", "selloff", "crash", "default", "lawsuit", "probe", "puts",
            "layoffs", "warning", "warns", "fear", "inflation", "tariff", "bankruptcy", "fraud", "dump"}
HEDGES = {"could", "may", "might", "reportedly", "rumor", "rumors", "rumored", "speculation", "speculate",
          "expected", "expects", "considering", "weighs", "eyes", "plans", "said to", "sources say", "possible",
          "potential", "likely", "unconfirmed", "if"}
WORD = re.compile(r"[a-z']+")


def words(text):
    return WORD.findall((text or "").lower())


def sentiment(text):
    """(positive hits, negative hits)."""
    ws = words(text)
    pos = sum(w in POSITIVE for w in ws)
    neg = sum(w in NEGATIVE for w in ws)
    return pos, neg


def polarity(text):
    """Score in (-1, 1): (pos - neg) / (pos + neg + 2). The +2 keeps one stray word from reading as certainty."""
    pos, neg = sentiment(text)
    return (pos - neg) / (pos + neg + 2.0)


def is_speculative(text):
    ws = set(words(text))
    low = (text or "").lower()
    return bool(ws & HEDGES) or "said to" in low or "sources say" in low
