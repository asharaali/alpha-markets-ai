"""Kalshi combination markets, via multivariate event collections.

Kalshi does sell genuine multi-leg contracts. A "multivariate event collection" is a family
of combinations over a set of underlying markets; asking it to resolve a specific set of
legs returns a single combo market with its own ticker, its own order book and its own
settlement rule. From that point it is an ordinary binary contract and the same order
endpoint trades it.

What that means for this app, precisely:

  * A combination whose collection exists AND whose specific leg set has been materialised
    can be quoted for real. The payout shown is Kalshi's price, not our multiplication.
  * A combination that has never been materialised returns 404 from the lookup. Kalshi
    offers a create call for exactly this, rate-limited to 5,000 per week, but creating a
    market as a side effect of rendering a research page is not something this app should
    do quietly. Creation is therefore opt-in and off by default.
  * Anything not covered by a collection cannot be quoted at all, and is shown as an
    analytical combination with hypothetical pricing.

The old code asserted in a docstring that "Kalshi has no native parlay product", which was
wrong, and then split the stake across singles anyway. Both halves are fixed: this module
finds the real product when it exists, and app.parlay.products labels what happens when it
does not.

Verified against Kalshi's API reference for multivariate event collections, September 2026.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.config import settings
from app.core.http import make_client, request
from app.core.logging import get_logger
from app.data.kalshi import client as kc
from app.data.kalshi import orderbook as ob

log = get_logger(__name__)

COLLECTIONS_PATH = "/trade-api/v2/multivariate_event_collections"


class _Memo:
    """A plain time-boxed memo. The async cache in app.core.cache coalesces concurrent
    loaders, which these calls do not need — they are cheap, rare and already serialised
    behind the parlay board's own refresh."""

    def __init__(self, ttl: float):
        self.ttl = ttl
        self._items: Dict[str, Tuple[float, Any]] = {}

    def get(self, key: str) -> Optional[Any]:
        entry = self._items.get(key)
        if entry is None or time.time() - entry[0] > self.ttl:
            return None
        return entry[1]

    def set(self, key: str, value: Any) -> None:
        self._items[key] = (time.time(), value)

    def clear(self) -> None:
        self._items.clear()


# Collections change rarely; the lookup result for a given leg set changes not at all.
_collections_cache = _Memo(ttl=3600.0)
_lookup_cache = _Memo(ttl=900.0)


@dataclass
class ComboQuote:
    """A real combination market with a real book behind it."""

    ticker: str
    collection_ticker: str
    legs: List[Dict[str, str]]
    yes_bid: Optional[float]
    yes_ask: Optional[float]
    depth_usd: float
    fetched_at: float = field(default_factory=time.time)

    @property
    def age_seconds(self) -> float:
        return time.time() - self.fetched_at

    @property
    def tradeable(self) -> bool:
        return self.yes_ask is not None and 0.0 < self.yes_ask < 1.0


async def collections(series_ticker: Optional[str] = None) -> List[Dict[str, Any]]:
    """Multivariate event collections available to this account.

    Public data, so this works without credentials — which matters, because the research
    side of the app must keep working for someone who has never connected an account.
    """
    cache_key = f"collections:{series_ticker or 'all'}"
    cached = _collections_cache.get(cache_key)
    if cached is not None:
        return cached

    params: Dict[str, Any] = {"limit": 200}
    if series_ticker:
        params["series_ticker"] = series_ticker
    try:
        async with make_client() as client:
            resp = await request(client, "GET",
                                 f"{settings.KALSHI_BASE}/multivariate_event_collections",
                                 params=params, source="kalshi", attempts=2)
    except Exception as exc:  # noqa: BLE001
        log.debug("kalshi: could not list combination collections (%s)", exc)
        return []
    if resp.status_code != 200:
        return []
    found = (resp.json() or {}).get("multivariate_event_collections") or []
    _collections_cache.set(cache_key, found)
    return found


async def lookup(collection_ticker: str,
                 legs: Sequence[Dict[str, str]]) -> Tuple[Optional[str], str]:
    """Resolve a set of legs to a combo market ticker.

    Returns (ticker, reason). A 404 is the documented answer for "this combination has
    never been created", and it is a normal outcome rather than an error: most leg sets a
    model dreams up have never been traded by anyone.
    """
    selected = [{"market_ticker": leg["market_ticker"],
                 "event_ticker": leg["event_ticker"]} for leg in legs]
    cache_key = f"{collection_ticker}:" + "|".join(
        sorted(f"{s['event_ticker']}/{s['market_ticker']}" for s in selected))
    cached = _lookup_cache.get(cache_key)
    if cached is not None:
        return cached

    path = f"{COLLECTIONS_PATH}/{collection_ticker}/lookup"
    try:
        async with make_client() as client:
            resp = await request(
                client, "PUT",
                f"{settings.KALSHI_BASE}/multivariate_event_collections/"
                f"{collection_ticker}/lookup",
                headers=(kc.signed_headers("PUT", path)
                         if kc.credentials_present() else None),
                json={"selected_markets": selected}, source="kalshi", attempts=1)
    except Exception as exc:  # noqa: BLE001
        return None, f"lookup failed: {str(exc)[:120]}"

    if resp.status_code == 404:
        result = (None, "no such combination market has been created on Kalshi yet")
        _lookup_cache.set(cache_key, result)
        return result
    if resp.status_code != 200:
        return None, f"lookup returned {resp.status_code}"

    payload = resp.json() or {}
    ticker = (payload.get("market") or {}).get("ticker") or payload.get("market_ticker")
    if not ticker:
        return None, "lookup succeeded but returned no market ticker"
    result = (ticker, "resolved")
    _lookup_cache.set(cache_key, result)
    return result


async def quote(collection_ticker: str,
                legs: Sequence[Dict[str, str]]) -> Tuple[Optional[ComboQuote], str]:
    """A real, executable price for this combination — or an honest reason there is none."""
    ticker, reason = await lookup(collection_ticker, legs)
    if ticker is None:
        return None, reason
    async with make_client() as client:
        yes_bid, yes_ask, depth = await ob.fetch(client, ticker)
    combo = ComboQuote(ticker=ticker, collection_ticker=collection_ticker,
                       legs=[dict(leg) for leg in legs], yes_bid=yes_bid,
                       yes_ask=yes_ask, depth_usd=depth)
    if not combo.tradeable:
        return combo, "the combination market exists but has no resting ask"
    return combo, "quoted"


async def find_quote(legs: Sequence[Dict[str, str]], *,
                     series_ticker: Optional[str] = None
                     ) -> Tuple[Optional[ComboQuote], str]:
    """Try every collection that might cover these legs. First real quote wins.

    Returns (None, reason) when nothing covers them, which is the common case and is
    reported to the user rather than papered over with a multiplied price.
    """
    if len(legs) < 2:
        return None, "a combination needs at least two legs"
    available = await collections(series_ticker)
    if not available:
        return None, ("Kalshi lists no combination collections for this series, so no "
                      "genuine multi-leg quote is available")
    for collection in available:
        ticker = collection.get("collection_ticker") or collection.get("ticker")
        if not ticker:
            continue
        combo, reason = await quote(ticker, legs)
        if combo is not None and combo.tradeable:
            return combo, reason
    return None, ("no combination market covering these exact legs has been created on "
                  "Kalshi")


def capability_note() -> Dict[str, Any]:
    """What this integration can and cannot do, for the UI to state plainly."""
    return {
        "native_parlays_exist": True,
        "reachable_over_rest": True,
        "this_app_can_quote": True,
        "this_app_can_create_markets": False,
        "explanation": (
            "Kalshi sells genuine multi-leg contracts through multivariate event "
            "collections, and this app quotes them when a collection covers the legs and "
            "the combination market already exists. It does not create new combination "
            "markets — Kalshi allows it, capped at 5,000 a week, but materialising a "
            "tradeable market as a side effect of loading a research page is not "
            "something this app does on its own. When no real quote exists, the "
            "combination is shown as analysis with hypothetical pricing, and the "
            "executable alternative is a basket of separate single contracts."),
        "source": "Kalshi API reference, multivariate event collections",
    }
