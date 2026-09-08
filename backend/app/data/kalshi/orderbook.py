"""Kalshi order-book reader — the only honest source of a Kalshi price.

Kalshi's /markets LIST endpoint returns yes_bid, yes_ask, last_price and volume as null for
every market. The real book lives at /markets/{ticker}/orderbook, as resting dollar depth
by price level. Anything in this app that needs a Kalshi price comes through here, so the
"every market looks unpriced" failure cannot come back.

Two details that matter and are easy to get wrong:
  * The best YES ask is 1 minus the best NO bid. The book only quotes bids on both sides.
  * A price level with a few dollars resting is not a price. Thin books are littered with
    stale dust at 1-2c that you cannot trade against, so levels below a depth floor are
    ignored rather than reported as tradeable.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

import httpx

from app.config import settings
from app.core.errors import RateLimited, UpstreamError
from app.core.http import get_json
from app.core.logging import get_logger

log = get_logger(__name__)

BASE = settings.KALSHI_BASE
MIN_DEPTH = settings.KALSHI_MIN_DEPTH

BookPrices = Tuple[Optional[float], Optional[float], float]


def best_bid(levels: Optional[Sequence[Sequence[Any]]],
             min_depth: float = MIN_DEPTH) -> Tuple[Optional[float], float]:
    """Highest bid level carrying real depth -> (price, dollars resting)."""
    real: List[Tuple[float, float]] = []
    for level in levels or []:
        try:
            price, depth = float(level[0]), float(level[1])
        except (TypeError, ValueError, IndexError):
            continue
        if depth >= min_depth:
            real.append((price, depth))
    if not real:
        return None, 0.0
    return max(real, key=lambda pd: pd[0])


def prices_from_book(orderbook_fp: Dict[str, Any],
                     min_depth: float = MIN_DEPTH) -> BookPrices:
    """(yes_bid, yes_ask, tradeable_depth) in 0-1 from an orderbook_fp payload."""
    yes_bid, yes_depth = best_bid(orderbook_fp.get("yes_dollars"), min_depth)
    no_bid, no_depth = best_bid(orderbook_fp.get("no_dollars"), min_depth)
    yes_ask = (1.0 - no_bid) if no_bid is not None else None
    if yes_bid is not None and no_bid is not None:
        depth = min(yes_depth, no_depth)
    else:
        depth = max(yes_depth, no_depth)
    return yes_bid, yes_ask, round(depth, 2)


async def fetch(client: httpx.AsyncClient, ticker: str,
                min_depth: float = MIN_DEPTH) -> BookPrices:
    """Best bid/ask/depth for one market. Never raises — an unreadable book is 'no price'.

    Rate limits and transport errors are already retried inside app.core.http; if we still
    could not read the book we report no price rather than inventing one, and the market is
    displayed as untradeable.
    """
    try:
        payload = await get_json(client, f"{BASE}/markets/{ticker}/orderbook",
                                 source="kalshi")
    except RateLimited:
        log.warning("kalshi: rate-limited reading the book for %s", ticker)
        return None, None, 0.0
    except UpstreamError as exc:
        log.debug("kalshi: no book for %s (%s)", ticker, exc)
        return None, None, 0.0
    return prices_from_book(payload.get("orderbook_fp") or {}, min_depth)
