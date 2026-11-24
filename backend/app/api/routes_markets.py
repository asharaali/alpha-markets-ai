"""Markets page: Kalshi and Polymarket US open markets, side by side."""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Query

from app.data import polymarket_us
from app.data.browse_market import BrowseMarket
from app.data.kalshi import browse as kalshi_browse

router = APIRouter()
_FETCHERS = {"kalshi": kalshi_browse.fetch, "polymarket": polymarket_us.fetch}
_NAMES = {"kalshi": "Kalshi", "polymarket": "Polymarket US"}
_CACHE: Dict[str, Any] = {}
CACHE_SECONDS = 120


async def fetch_all() -> Tuple[List[BrowseMarket], List[str]]:
    if _CACHE and time.time() - _CACHE["at"] < CACHE_SECONDS:
        return _CACHE["markets"], _CACHE["warnings"]
    names = list(_FETCHERS)
    results = await asyncio.gather(*(_FETCHERS[n]() for n in names), return_exceptions=True)
    markets: List[BrowseMarket] = []
    warnings: List[str] = []
    for name, res in zip(names, results):
        if isinstance(res, BaseException):
            warnings.append(f"{_NAMES[name]} unavailable: {res}")
        else:
            markets.extend(res)
    _CACHE.update(at=time.time(), markets=markets, warnings=warnings)
    return markets, warnings


@router.get("/api/markets/browse")
async def browse(q: Optional[str] = Query(default=None, max_length=80),
                 category: Optional[str] = Query(default=None, max_length=60)):
    markets, warnings = await fetch_all()
    rows = markets
    if q:
        rows = [m for m in rows if q.lower() in m.title.lower()]
    if category:
        rows = [m for m in rows if m.category.lower() == category.lower()]
    return {"markets": [m.to_dict() for m in rows],
            "categories": sorted({m.category for m in markets}),
            "warnings": warnings, "as_of": _CACHE.get("at", time.time())}
