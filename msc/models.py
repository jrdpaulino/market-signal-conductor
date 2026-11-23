"""Shared data types."""
from dataclasses import dataclass, field


@dataclass
class Observation:
    """One reading from one signal.

    score:      direction and strength in [-1, 1] (negative = bearish, positive = bullish)
    confidence: how much to trust this reading, in [0, 1] (data volume, freshness, hedging...)
    scope:      "market" (key == "") or "ticker" (key == the symbol)
    """
    signal: str
    day: str
    scope: str
    key: str
    score: float
    confidence: float = 1.0
    meta: dict = field(default_factory=dict)

    def __post_init__(self):
        self.score = max(-1.0, min(1.0, float(self.score)))
        self.confidence = max(0.0, min(1.0, float(self.confidence)))
        if self.scope not in ("market", "ticker"):
            raise ValueError(f"bad scope {self.scope!r}")
