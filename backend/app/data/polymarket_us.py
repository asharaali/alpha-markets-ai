"""Polymarket US (the CFTC-regulated exchange) public market data — read-only.

Uses the public gateway (no key). The payload's `outcomePrices` ordering contradicted its own
YES side on 2026-11-23, so prices come from bestBidQuote/bestAskQuote (the YES book), with the
YES side's price as a fallback.
"""
from __future__ import annotations

from typing import Any, Dict, List

from app.core.http import make_client
from app.data.browse_market import BrowseMarket, to_price

GATEWAY = "https://gateway.polymarket.us/v1"


def _quote(q: Any):
    return to_price(q.get("value")) if isinstance(q, dict) else None


def parse_markets(payload: Dict[str, Any]) -> List[BrowseMarket]:
    out: List[BrowseMarket] = []
    for m in payload.get("markets") or [] if isinstance(payload, dict) else []:
        try:
            bid, ask = _quote(m.get("bestBidQuote")), _quote(m.get("bestAskQuote"))
            yes_side = next((s for s in m.get("marketSides") or []
                             if str(s.get("description", "")).lower() == "yes" or s.get("long") is True), None)
            last = to_price(yes_side.get("price")) if yes_side else None
            if bid is None and ask is None and last is None:
                continue
            question, title = m.get("question") or "", m.get("title") or ""
            name = f"{question} — {title}" if title and title not in question else (question or title)
            out.append(BrowseMarket(
                venue="polymarket", id=str(m["id"]), title=name,
                category=str(m.get("category") or "other").title(), yes_bid=bid, yes_ask=ask, last=last,
                volume=None, closes=m.get("endDate"),
                url=f"https://polymarket.us/event/{m['slug']}"))
        except (KeyError, TypeError, AttributeError):
            continue                            # an unknown shape skips that market only
    return out


async def fetch(limit: int = 200) -> List[BrowseMarket]:
    async with make_client(timeout=20) as client:
        r = await client.get(f"{GATEWAY}/markets", params={"active": "true", "closed": "false", "limit": limit})
        r.raise_for_status()
        return parse_markets(r.json())
