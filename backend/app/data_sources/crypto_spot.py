"""
Live crypto spot price + short-term realized volatility, from Coinbase's public market-data
API (no key needed). Feeds the 15-minute binary-option model in app.crypto_model.

  - spot: the current price (Coinbase Exchange ticker)
  - sigma_annual: annualized volatility estimated from the last ~hour of 1-minute candles, so
    the option price adapts to whether the market is calm or wild right now (that's the whole
    point of pricing these live). Floored/capped so an ultra-quiet or gappy window can't produce
    a degenerate, over-confident probability.

Cached briefly (these markets turn over every 15 min, so we refresh fast — but not every hit).
"""
from __future__ import annotations
import math
import time
from typing import Dict, Optional, Tuple

import httpx

_CB = "https://api.exchange.coinbase.com"
_UA = {"User-Agent": "AlphaMarketsAI/1.0"}

# Coin -> Coinbase product id.
PRODUCTS: Dict[str, str] = {"BTC": "BTC-USD", "ETH": "ETH-USD"}

_MIN_PER_YEAR = 525_600.0
_SIGMA_FLOOR = 0.15          # never treat the market as calmer than ~15% annual vol
_SIGMA_CAP = 3.0
_FALLBACK_SIGMA = 0.55       # if candles are missing, a sane BTC/ETH-ish annual vol

_CACHE: Dict[str, Tuple[float, float, float]] = {}   # coin -> (ts, spot, sigma_annual)
_TTL = 8.0


def _annualized_vol(closes) -> float:
    """Annualized vol from a series of 1-minute closes (newest first)."""
    rets = [math.log(closes[i] / closes[i + 1])
            for i in range(len(closes) - 1)
            if closes[i] > 0 and closes[i + 1] > 0]
    if len(rets) < 5:
        return _FALLBACK_SIGMA
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    sigma_min = math.sqrt(var)
    sigma_annual = sigma_min * math.sqrt(_MIN_PER_YEAR)
    return min(_SIGMA_CAP, max(_SIGMA_FLOOR, sigma_annual))


async def _fetch(client: httpx.AsyncClient, product: str) -> Tuple[float, float]:
    t = await client.get(f"{_CB}/products/{product}/ticker")
    spot = float(t.json()["price"])
    cd = await client.get(f"{_CB}/products/{product}/candles", params={"granularity": 60})
    candles = cd.json() if cd.status_code == 200 else []
    # Coinbase candle = [time, low, high, open, close, volume]; sort newest-first, take last hour.
    candles = sorted(candles, key=lambda c: c[0], reverse=True)[:60]
    closes = [c[4] for c in candles]
    sigma = _annualized_vol(closes) if closes else _FALLBACK_SIGMA
    return spot, sigma


async def get_spot_vol(coin: str, client: Optional[httpx.AsyncClient] = None) -> Tuple[float, float]:
    """(spot, sigma_annual) for a coin, cached ~8s. Raises if the coin isn't supported."""
    coin = coin.upper()
    if coin not in PRODUCTS:
        raise ValueError(f"unsupported coin {coin}")
    now = time.time()
    cached = _CACHE.get(coin)
    if cached and now - cached[0] < _TTL:
        return cached[1], cached[2]
    own = client is None
    if own:
        client = httpx.AsyncClient(timeout=12, headers=_UA)
    try:
        spot, sigma = await _fetch(client, PRODUCTS[coin])
        _CACHE[coin] = (now, spot, sigma)
        return spot, sigma
    finally:
        if own:
            await client.aclose()
