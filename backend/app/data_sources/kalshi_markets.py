"""
Liquid Kalshi markets data source — currently the daily-high TEMPERATURE markets, which
trade with real liquidity AND are beatable with the NWS forecast (see weather_model).

Two things the old World Cup adapter got wrong and we fix here:
  1) Liquidity lives in the ORDERBOOK endpoint, not the markets list. Kalshi's
     /markets list returns yes_bid/yes_ask = None for everything; you must call
     /markets/{ticker}/orderbook to see the real resting depth.
  2) Best YES ask = 1 - best NO bid (a NO bid at price p is a YES offer at 1-p).
"""
from __future__ import annotations
import asyncio
from datetime import datetime, date, timezone
from typing import Dict, List, Optional

import httpx

from app import weather_model

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
_UA = {"User-Agent": "AlphaMarketsAI/1.0 (weather edge)"}

# Kalshi high-temp series -> (display city, NWS station lat/lon).
# Coords target the official climate station each market settles on (approximate).
CITIES = {
    "KXHIGHNY":   ("New York City", 40.7790, -73.9692),
    "KXHIGHCHI":  ("Chicago",        41.7860, -87.7520),
    "KXHIGHMIA":  ("Miami",          25.7905, -80.3164),
    "KXHIGHLAX":  ("Los Angeles",    33.9381, -118.3889),
    "KXHIGHDEN":  ("Denver",         39.8466, -104.6562),
    "KXHIGHAUS":  ("Austin",         30.1975, -97.6664),
    "KXHIGHPHIL": ("Philadelphia",   39.8721, -75.2411),
}

# Short cache so repeated UI polls don't hammer NWS / Kalshi.
_FORECAST_CACHE: Dict[str, object] = {}
_FORECAST_TTL = 1800  # 30 min — forecasts update a few times a day


async def _nws_daily_highs(client: httpx.AsyncClient, lat: float, lon: float) -> Dict[str, float]:
    """Return {YYYY-MM-DD: forecast_high_F} from the NWS gridpoint forecast."""
    pt = (await client.get(f"https://api.weather.gov/points/{lat},{lon}")).json()
    grid_url = pt["properties"]["forecastGridData"]
    gd = (await client.get(grid_url)).json()["properties"]
    out: Dict[str, float] = {}
    for v in gd.get("maxTemperature", {}).get("values", []):
        c = v.get("value")
        if c is None:
            continue
        day = v["validTime"][:10]
        out[day] = round(c * 9 / 5 + 32, 1)
    return out


async def _forecast_for(client, series: str) -> Dict[str, float]:
    cached = _FORECAST_CACHE.get(series)
    now = datetime.now(timezone.utc).timestamp()
    if cached and now - cached[0] < _FORECAST_TTL:
        return cached[1]
    _, lat, lon = CITIES[series]
    highs = await _nws_daily_highs(client, lat, lon)
    _FORECAST_CACHE[series] = (now, highs)
    return highs


_MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
           "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}


def _parse_market_date(ticker: str) -> Optional[date]:
    """KXHIGHNY-26JUN21-T87 -> date(2026, 6, 21)."""
    try:
        part = ticker.split("-")[1]            # 26JUN21
        yy = int(part[:2]); mon = _MONTHS[part[2:5]]; dd = int(part[5:7])
        return date(2000 + yy, mon, dd)
    except (IndexError, KeyError, ValueError):
        return None


# A price level must have at least this many dollars resting to count as a real, tradeable
# quote. Kalshi thin markets are littered with $1 stale orders at 1-2¢ that you can't
# actually trade size against — pricing off those manufactures fake edge.
_MIN_DEPTH = 20.0


def _best_priced(levels):
    """Highest-priced bid level with real depth -> (price, dollars). None if the book is dust."""
    real = [(float(p), float(d)) for p, d in levels if float(d) >= _MIN_DEPTH]
    if not real:
        return None, 0.0
    p = max(real, key=lambda x: x[0])
    return p[0], p[1]


async def _orderbook_prices(client, ticker: str):
    """Depth-aware best YES bid/ask (0-1) from the live orderbook. ask = cost to BUY yes."""
    try:
        ob = (await client.get(f"{KALSHI_BASE}/markets/{ticker}/orderbook")).json()
    except Exception:
        return None, None, 0.0
    fp = ob.get("orderbook_fp") or {}
    yes_bid, yes_depth = _best_priced(fp.get("yes_dollars") or [])
    no_bid, no_depth = _best_priced(fp.get("no_dollars") or [])
    yes_ask = (1 - no_bid) if no_bid is not None else None   # best NO bid implies the YES offer
    depth = min(yes_depth, no_depth) if (yes_bid and no_bid) else max(yes_depth, no_depth)
    return yes_bid, yes_ask, round(depth, 2)


async def get_weather_markets(series_filter: Optional[List[str]] = None) -> List[Dict]:
    """
    Pull open temperature contracts for each city, attach the NWS forecast + the model's
    fair probability and the live market price. One row per Kalshi contract.
    """
    series_list = series_filter or list(CITIES)
    today = datetime.now(timezone.utc).date()
    rows: List[Dict] = []

    async with httpx.AsyncClient(timeout=25, headers=_UA) as client:
        for series in series_list:
            city = CITIES[series][0]
            try:
                forecasts = await _forecast_for(client, series)
                resp = await client.get(f"{KALSHI_BASE}/markets",
                                        params={"series_ticker": series, "status": "open", "limit": 200})
                markets = resp.json().get("markets", [])
            except Exception:
                continue

            # Price the order books in parallel (one call each).
            books = await asyncio.gather(*[_orderbook_prices(client, m["ticker"]) for m in markets])

            for m, (yes_bid, yes_ask, depth) in zip(markets, books):
                mdate = _parse_market_date(m["ticker"])
                if not mdate:
                    continue
                fkey = mdate.isoformat()
                forecast = forecasts.get(fkey)
                if forecast is None:
                    continue  # no NWS forecast for that day -> can't price it honestly
                lead = (mdate - today).days
                p = weather_model.model_prob(forecast, lead, m.get("strike_type"),
                                             m.get("floor_strike"), m.get("cap_strike"))
                if p is None:
                    continue
                rows.append({
                    "ticker": m["ticker"],
                    "city": city,
                    "series": series,
                    "date": fkey,
                    "lead_days": lead,
                    "label": m.get("yes_sub_title") or m.get("subtitle") or m.get("title"),
                    "forecast_high_f": forecast,
                    "model_prob": round(p, 4),
                    "yes_bid": yes_bid,
                    "yes_ask": yes_ask,
                    "depth": depth,
                })
    return rows
