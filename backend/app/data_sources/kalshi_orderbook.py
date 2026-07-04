"""
Shared Kalshi order-book reader.

Kalshi's /markets LIST endpoint returns yes_bid/yes_ask/volume/last_price = null for every
market — real liquidity only shows up on /markets/{ticker}/orderbook (orderbook_fp with
resting dollar depth by price). Best YES ask = 1 - best NO bid. Everything that needs a
live Kalshi price (pricing, the weather tab, live order placement) goes through here so the
"null price" bug can't come back.
"""
from __future__ import annotations
import asyncio
from typing import List, Optional, Tuple

import httpx

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
_UA = {"User-Agent": "AlphaMarketsAI/1.0"}

# A price level needs at least this many dollars resting to count as tradeable — thin
# Kalshi books are littered with $1 stale orders at 1-2¢ you can't trade size against.
MIN_DEPTH = 20.0


def best_priced(levels, min_depth: float = MIN_DEPTH) -> Tuple[Optional[float], float]:
    """Highest-priced bid level with real depth -> (price, dollars). (None, 0) if dust."""
    real = [(float(p), float(d)) for p, d in (levels or []) if float(d) >= min_depth]
    if not real:
        return None, 0.0
    p = max(real, key=lambda x: x[0])
    return p[0], p[1]


def prices_from_fp(fp: dict, min_depth: float = MIN_DEPTH):
    """(yes_bid, yes_ask, depth) in 0-1 from an orderbook_fp payload. ask = cost to BUY yes."""
    yes_bid, yes_depth = best_priced(fp.get("yes_dollars"), min_depth)
    no_bid, no_depth = best_priced(fp.get("no_dollars"), min_depth)
    yes_ask = (1 - no_bid) if no_bid is not None else None
    depth = min(yes_depth, no_depth) if (yes_bid and no_bid) else max(yes_depth, no_depth)
    return yes_bid, yes_ask, round(depth, 2)


async def orderbook_prices(client: httpx.AsyncClient, ticker: str, min_depth: float = MIN_DEPTH):
    """Depth-aware best YES bid/ask/depth for one market via the orderbook endpoint.
    Retries on 429 — Kalshi rate-limits bursts, and silently treating a 429 as 'no price'
    made whole boards read as untradeable."""
    for attempt in range(4):
        try:
            r = await client.get(f"{KALSHI_BASE}/markets/{ticker}/orderbook")
            if r.status_code == 429:
                await asyncio.sleep(0.6 * (attempt + 1))
                continue
            ob = r.json()
        except Exception:
            return None, None, 0.0
        return prices_from_fp(ob.get("orderbook_fp") or {}, min_depth)
    return None, None, 0.0
