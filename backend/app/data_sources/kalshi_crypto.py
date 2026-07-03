"""
Kalshi 15-minute crypto markets (BTC, ETH) overlaid with the driftless-GBM option model.

Each series (KXBTC15M, KXETH15M) lists one binary per 15-minute window: "will <coin> be >= the
target price at the mark?". For every open market we pull the live order book (real price), the
current spot + live vol (Coinbase), and compute the model probability, edge, EV and value flag.
Both sides are surfaced: YES = coin at/above the target (buy YES), NO = below (buy NO) — so you
can bet up OR down. Cached briefly since these turn over fast.
"""
from __future__ import annotations
import asyncio
import re
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import httpx

from app.analysis import MIN_EDGE, LONGSHOT_FLOOR, HEAVY_FAV_CAP, _tier
from app.crypto_model import prob_at_or_above
from app.data_sources.crypto_spot import get_spot_vol
from app.data_sources.kalshi_orderbook import orderbook_prices

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
_UA = {"User-Agent": "AlphaMarketsAI/1.0"}

# Series -> coin. BTC + ETH to start (both deeply liquid with clean spot feeds).
SERIES: Dict[str, str] = {"KXBTC15M": "BTC", "KXETH15M": "ETH"}

_CACHE: Dict[str, object] = {"data": None, "ts": 0.0}
_CACHE_TTL = 8.0


def _parse_dt(iso: Optional[str]) -> Optional[datetime]:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None


def _strike(m: Dict) -> Optional[float]:
    """The target the YES side settles at/above. greater_* uses floor_strike, less_* cap_strike;
    fall back to parsing 'Target Price: $61,671.49' from the sub-title."""
    st = (m.get("strike_type") or "").lower()
    strike = m.get("floor_strike") if "greater" in st else m.get("cap_strike")
    if strike is None:
        strike = m.get("floor_strike") if m.get("floor_strike") is not None else m.get("cap_strike")
    if strike is None:
        mt = re.search(r"\$([\d,]+(?:\.\d+)?)", m.get("yes_sub_title", "") or "")
        if mt:
            strike = float(mt.group(1).replace(",", ""))
    return float(strike) if strike is not None else None


def _evaluate(model_prob: float, price: float) -> Dict:
    """Edge/EV vs the Kalshi price for one side. No sportsbook here — the model itself is the
    reference (spot + live vol), so 'confidence' just reflects that it's model-priced."""
    ev = (model_prob / price - 1.0) if price > 0 else 0.0
    edge = model_prob - price
    value = (edge >= MIN_EDGE and ev > 0 and LONGSHOT_FLOOR <= price <= HEAVY_FAV_CAP)
    return {
        "model_prob": round(model_prob, 4),
        "fair_prob": round(model_prob, 4),
        "kalshi_price_cents": round(price * 100, 1),
        "market_odds_decimal": round(1.0 / price, 4) if price > 0 else None,
        "edge": round(edge, 4),
        "ev_per_dollar": round(ev, 4),
        "value_bet": value,
        "tier": _tier(model_prob),
        "confidence": "model",
    }


async def get_crypto_markets(coin_filter: Optional[str] = None) -> List[Dict]:
    """Every open 15-min BTC/ETH market with the model overlay, both sides, best value first."""
    now = time.time()
    if _CACHE["data"] is not None and now - float(_CACHE["ts"]) < _CACHE_TTL:
        data = _CACHE["data"]  # type: ignore[assignment]
        return [b for b in data if not coin_filter or b["coin"].lower() == coin_filter.lower()]

    out: List[Dict] = []
    async with httpx.AsyncClient(timeout=20, headers=_UA, follow_redirects=True) as c:
        # Live spot + vol per coin (once each).
        sv: Dict[str, Tuple[Optional[float], Optional[float]]] = {}
        for coin in set(SERIES.values()):
            try:
                sv[coin] = await get_spot_vol(coin, c)
            except Exception as exc:
                print(f"[kalshi_crypto] spot/vol {coin} failed: {exc}")
                sv[coin] = (None, None)

        for series, coin in SERIES.items():
            spot, sigma = sv.get(coin, (None, None))
            if spot is None or sigma is None:
                continue
            try:
                r = await c.get(f"{KALSHI_BASE}/events",
                                params={"series_ticker": series, "with_nested_markets": "true",
                                        "status": "open", "limit": 50})
                events = r.json().get("events", [])
            except Exception as exc:
                print(f"[kalshi_crypto] {series} fetch failed: {exc}")
                continue

            markets = [m for e in events for m in (e.get("markets") or []) if m.get("ticker")]
            books = await asyncio.gather(*[orderbook_prices(c, m["ticker"]) for m in markets],
                                         return_exceptions=True)
            nowdt = datetime.now(timezone.utc)
            for m, book in zip(markets, books):
                if isinstance(book, Exception):
                    continue
                yb, ya, _depth = book
                if yb is None or ya is None:
                    continue
                yes_price = (yb + ya) / 2.0
                strike = _strike(m)
                if strike is None:
                    continue
                close = _parse_dt(m.get("close_time"))
                secs = (close - nowdt).total_seconds() if close else 0.0
                if secs <= 0:
                    continue
                st = (m.get("strike_type") or "").lower()
                p_above = prob_at_or_above(spot, strike, secs, sigma)
                model_yes = p_above if "less" not in st else (1.0 - p_above)

                sides = [
                    ("yes", model_yes, yes_price, f"{coin} ≥ ${strike:,.0f}"),
                    ("no", 1.0 - model_yes, 1.0 - yes_price, f"{coin} < ${strike:,.0f}"),
                ]
                for side, mp, price, label in sides:
                    if not (0.0 < price < 1.0):
                        continue
                    ev = _evaluate(mp, price)
                    out.append({
                        "ticker": m.get("ticker"), "side": side,
                        "coin": coin, "category": "Crypto", "bet_type": f"{coin} 15-min",
                        "strike": strike, "spot": round(spot, 2), "sigma_annual": round(sigma, 3),
                        "seconds_to_close": int(secs), "close_time": m.get("close_time"),
                        "home": coin, "away": "15-min", "selection": label,
                        **ev,
                    })

    out.sort(key=lambda b: (b["value_bet"], b["ev_per_dollar"]), reverse=True)
    _CACHE["data"] = out
    _CACHE["ts"] = now
    return [b for b in out if not coin_filter or b["coin"].lower() == coin_filter.lower()]
