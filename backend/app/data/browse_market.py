"""One open market on any venue, in the shape the Markets page lists."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Literal, Optional


def to_price(value: Any) -> Optional[float]:
    """Venue price strings → float in (0, 1); zero/absent/garbage → None."""
    try:
        p = float(value)
    except (TypeError, ValueError):
        return None
    return p if 0.0 < p < 1.0 else None


@dataclass
class BrowseMarket:
    venue: Literal["kalshi", "polymarket"]
    id: str
    title: str
    category: str
    yes_bid: Optional[float]
    yes_ask: Optional[float]
    last: Optional[float]
    volume: Optional[float]
    closes: Optional[str]
    url: str

    @property
    def mid(self) -> Optional[float]:
        if self.yes_bid is not None and self.yes_ask is not None:
            return round((self.yes_bid + self.yes_ask) / 2, 4)
        return self.last if self.last is not None else (self.yes_ask or self.yes_bid)

    def to_dict(self) -> Dict[str, Any]:
        return {**asdict(self), "mid": self.mid}
