"""
Kalshi adapter — pulls live World Cup game markets from Kalshi's public API
(no auth needed for market data) and overlays our trained model on each one.

Kalshi prices YES contracts in cents (0–100). A 'Tie' market plus one market per
team. We read the price, compare it to the model's trained probability, and flag
value with the same discipline as the sportsbook board (shrink-to-market + guardrails).

Series ticker for World Cup games: KXWCGAME.
"""
from __future__ import annotations
import asyncio
import time
from typing import Dict, List, Optional

import httpx

from app import probability as P
from app.analysis import MODEL_WEIGHT, MIN_EDGE, LONGSHOT_FLOOR, HEAVY_FAV_CAP, _tier
from app.config import settings
from app.soccer_model import match_probabilities
from app.data_sources.kalshi_orderbook import orderbook_prices

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
WC_SERIES = "KXWCGAME"

# Kalshi team names -> the canonical names our model/odds feed use.
KALSHI_NAME_MAP = {
    "Congo DR": "DR Congo",
    "United States": "USA",
    "Korea Republic": "South Korea",
    "Cote d'Ivoire": "Ivory Coast",
}

_CACHE: Dict[str, object] = {"data": None, "ts": 0.0}


def _canon(name: str) -> str:
    return KALSHI_NAME_MAP.get(name.strip(), name.strip())


def _price(market: Dict, prices: Dict[str, float]) -> Optional[float]:
    """Live mid price (0-1) from the order book (markets-list bid/ask is always null)."""
    return prices.get(market.get("ticker"))


async def _fetch_events() -> List[Dict]:
    params = {"series_ticker": WC_SERIES, "with_nested_markets": "true",
              "status": "open", "limit": 200}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{KALSHI_BASE}/events", params=params)
        resp.raise_for_status()
        return resp.json().get("events", [])


def _evaluate_side(name: str, role: str, model_prob: float,
                   price: float, market_prob_vigfree: float) -> Dict:
    """Same value logic as the sportsbook board, expressed for a Kalshi YES contract."""
    fair = MODEL_WEIGHT * model_prob + (1 - MODEL_WEIGHT) * market_prob_vigfree
    decimal_odds = 1.0 / price if price > 0 else 999
    ev = (fair / price - 1) if price > 0 else 0.0   # buy YES at `price`, pays $1
    edge = fair - market_prob_vigfree
    value = (edge >= MIN_EDGE and ev > 0
             and LONGSHOT_FLOOR <= market_prob_vigfree <= HEAVY_FAV_CAP)
    return {
        "selection": name,
        "role": role,
        "kalshi_price_cents": round(price * 100, 1),
        "model_prob": round(model_prob, 4),
        "fair_prob": round(fair, 4),
        "market_prob": round(market_prob_vigfree, 4),
        "decimal_odds": round(decimal_odds, 2),
        "edge": round(edge, 4),
        "ev_per_dollar": round(ev, 4),
        "value_bet": value,
        "tier": _tier(fair),
    }


def _evaluate_event(event: Dict, prices: Dict[str, float]) -> Optional[Dict]:
    # Kalshi titles now carry a market suffix ("A vs B: Regulation Time Moneyline") — strip it,
    # or the away team never parses and every game reads as untradeable.
    title = event.get("title", "").split(":", 1)[0]
    if " vs " not in title:
        return None
    home, away = [_canon(t) for t in title.split(" vs ", 1)]
    markets = event.get("markets", [])

    sides, priced = {}, {}
    for m in markets:
        # Subtitles also grew a prefix ("Reg Time: Argentina") — keep only the pick itself.
        sub = (m.get("yes_sub_title") or "").split(":", 1)[-1].strip()
        pr = _price(m, prices)
        if sub.lower() in ("tie", "draw"):
            key = "tie"
        elif _canon(sub) == home:
            key = "home"
        elif _canon(sub) == away:
            key = "away"
        else:
            continue
        sides[key] = sub
        if pr is not None:
            priced[key] = pr

    # Need all three priced to remove the Kalshi margin fairly.
    if len(priced) < 3:
        return {"event_ticker": event.get("event_ticker"), "home": home, "away": away,
                "tradeable": False, "note": "No live Kalshi prices yet (untraded market)."}

    model = match_probabilities(home, away)
    order = ["home", "tie", "away"]
    vigfree = P.remove_vig([1.0 / priced[k] for k in order])
    model_map = {"home": model["probs"]["home"], "tie": model["probs"]["draw"],
                 "away": model["probs"]["away"]}
    name_map = {"home": home, "tie": "Tie", "away": away}

    evald = [
        _evaluate_side(name_map[k], k, model_map[k], priced[k], vf)
        for k, vf in zip(order, vigfree)
    ]
    values = [s for s in evald if s["value_bet"]]
    values.sort(key=lambda s: s["ev_per_dollar"], reverse=True)
    return {
        "event_ticker": event.get("event_ticker"),
        "home": home, "away": away,
        "tradeable": True,
        "model_probs": model["probs"],
        "sides": evald,
        "best_value": values[0] if values else None,
        "value_count": len(values),
    }


async def get_kalshi_wc_games() -> List[Dict]:
    age = time.time() - float(_CACHE["ts"])
    if _CACHE["data"] is not None and age < 120:
        return _CACHE["data"]  # type: ignore[return-value]
    try:
        events = await _fetch_events()
    except Exception as exc:
        print(f"[kalshi] fetch failed: {exc}")
        return _CACHE["data"] or []  # type: ignore[return-value]

    # Real prices live in the orderbook endpoint — fetch them for every market, THROTTLED
    # (an unbounded burst trips Kalshi's rate limit and the whole board reads untradeable),
    # then build a ticker -> mid-price map. (markets-list bid/ask is always null.)
    tickers = [m.get("ticker") for e in events for m in e.get("markets", []) if m.get("ticker")]
    prices: Dict[str, float] = {}
    try:
        sem = asyncio.Semaphore(8)

        async def _book(client, t):
            async with sem:
                return t, await orderbook_prices(client, t)

        async with httpx.AsyncClient(timeout=20, headers={"User-Agent": "AlphaMarketsAI/1.0"}) as client:
            books = await asyncio.gather(*[_book(client, t) for t in tickers])
        for t, (yes_bid, yes_ask, _depth) in books:
            if yes_bid is not None and yes_ask is not None:
                prices[t] = (yes_bid + yes_ask) / 2.0
    except Exception as exc:
        print(f"[kalshi] orderbook fetch failed: {exc}")

    games = [g for g in (_evaluate_event(e, prices) for e in events) if g]
    # Tradeable + most value first.
    games.sort(key=lambda g: (g.get("tradeable", False), g.get("value_count", 0)), reverse=True)
    _CACHE["data"] = games
    _CACHE["ts"] = time.time()
    return games
