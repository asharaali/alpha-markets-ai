"""Every open Kalshi event, for the Markets page (public data, no key)."""
from __future__ import annotations

from typing import Any, Dict, List

from app.config import settings
from app.core.http import make_client
from app.data.browse_market import BrowseMarket, to_price


def parse_events(payload: Dict[str, Any]) -> List[BrowseMarket]:
    out: List[BrowseMarket] = []
    for event in payload.get("events") or []:
        series = str(event.get("series_ticker") or event.get("event_ticker") or "")
        for m in event.get("markets") or []:
            bid, ask = to_price(m.get("yes_bid_dollars")), to_price(m.get("yes_ask_dollars"))
            last = to_price(m.get("last_price_dollars"))
            if bid is None and ask is None:
                continue                      # no book: nothing tradable to show
            title = m.get("title") or event.get("title") or m.get("ticker")
            try:
                volume = float(m.get("volume_fp") or 0) or None
            except (TypeError, ValueError):
                volume = None
            out.append(BrowseMarket(
                venue="kalshi", id=str(m.get("ticker")), title=str(title),
                category=str(event.get("category") or "Other"), yes_bid=bid, yes_ask=ask, last=last,
                volume=volume, closes=m.get("close_time"),
                url=f"https://kalshi.com/markets/{series.lower()}"))
    return out


async def fetch(limit: int = 200) -> List[BrowseMarket]:
    async with make_client(timeout=20) as client:
        r = await client.get(f"{settings.KALSHI_BASE}/events",
                             params={"status": "open", "with_nested_markets": "true", "limit": limit})
        r.raise_for_status()
        return parse_events(r.json())
