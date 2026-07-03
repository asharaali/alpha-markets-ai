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
from app.crypto_model import prob_at_or_above, directional_signal
from app.data_sources.crypto_spot import get_market_state
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


def _side(coin: str, strike: float, ticker: str, side: str, model_prob: float,
          price: float, label: str) -> Dict:
    """One tradeable side (buy YES or buy NO), priced vs the live Kalshi price."""
    ev = (model_prob / price - 1.0) if price > 0 else 0.0
    edge = model_prob - price
    return {
        "side": side, "selection": label, "kalshi_ticker": ticker,
        "coin": coin, "strike": strike,
        "model_prob": round(model_prob, 4),
        "kalshi_price_cents": round(price * 100, 1),
        "market_odds_decimal": round(1.0 / price, 4) if price > 0 else None,
        "edge": round(edge, 4), "ev_per_dollar": round(ev, 4),
        "tradeable_price": price,
    }


def _pick(signal: Dict, yes: Dict, no: Dict) -> Dict:
    """Decide what to actually place: the higher-EV side, but only if it clears the value gate
    (real edge, +EV, not a longshot/heavy-fav trap). Otherwise: sit out. Confidence blends the
    size of the edge with how strong the chart signal is and whether the two agree."""
    best = yes if yes["ev_per_dollar"] >= no["ev_per_dollar"] else no
    price = best["tradeable_price"]
    clears = (best["edge"] >= MIN_EDGE and best["ev_per_dollar"] > 0
              and LONGSHOT_FLOOR <= price <= HEAVY_FAV_CAP)
    if not clears:
        return {"side": None, "action": "PASS",
                "reason": "Kalshi's price is fair — no edge worth taking. Sit this window out."}
    signal_agrees = ((best["side"] == "yes" and signal["direction"] == "up")
                     or (best["side"] == "no" and signal["direction"] == "down"))
    if best["edge"] >= 0.08 and signal["strength"] >= 0.4 and signal_agrees:
        conf = "high"
    elif best["edge"] >= 0.05:
        conf = "medium"
    else:
        conf = "low"
    arrow = "▲ UP" if best["side"] == "yes" else "▼ DOWN"
    reason = (f"chart {signal['direction'].upper()} ({signal['note']}) "
              + ("backs this" if signal_agrees else "is mixed vs this")
              + f"; model {best['model_prob']*100:.0f}% vs {best['kalshi_price_cents']:.0f}¢")
    return {"side": best["side"], "action": f"BUY {best['side'].upper()} · {arrow}",
            "selection": best["selection"], "kalshi_ticker": best["kalshi_ticker"],
            "price_cents": best["kalshi_price_cents"], "ev_per_dollar": best["ev_per_dollar"],
            "model_prob": best["model_prob"], "confidence": conf, "reason": reason}


async def get_crypto_markets(coin_filter: Optional[str] = None) -> List[Dict]:
    """Every open 15-min BTC/ETH window: chart signal, both sides priced vs Kalshi, and the
    recommended pick (buy up / buy down / pass). Strongest actionable pick first."""
    now = time.time()
    if _CACHE["data"] is not None and now - float(_CACHE["ts"]) < _CACHE_TTL:
        data = _CACHE["data"]  # type: ignore[assignment]
        return [b for b in data if not coin_filter or b["coin"].lower() == coin_filter.lower()]

    out: List[Dict] = []
    async with httpx.AsyncClient(timeout=20, headers=_UA, follow_redirects=True) as c:
        states: Dict[str, Optional[Dict]] = {}
        for coin in set(SERIES.values()):
            try:
                states[coin] = await get_market_state(coin, c)
            except Exception as exc:
                print(f"[kalshi_crypto] state {coin} failed: {exc}")
                states[coin] = None

        for series, coin in SERIES.items():
            state = states.get(coin)
            if not state:
                continue
            spot, sigma = state["spot"], state["sigma_annual"]
            signal = directional_signal(state["ret_5m"], state["ret_15m"], sigma)
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
                if not (0.0 < yes_price < 1.0):
                    continue
                strike = _strike(m)
                if strike is None:
                    continue
                close = _parse_dt(m.get("close_time"))
                secs = (close - nowdt).total_seconds() if close else 0.0
                if secs <= 0:
                    continue
                st = (m.get("strike_type") or "").lower()
                p_above = prob_at_or_above(spot, strike, secs, sigma, state["ret_5m"])
                model_yes = p_above if "less" not in st else (1.0 - p_above)
                ticker = m.get("ticker")
                yes = _side(coin, strike, ticker, "yes", model_yes, yes_price, f"{coin} ≥ ${strike:,.0f}")
                no = _side(coin, strike, ticker, "no", 1.0 - model_yes, 1.0 - yes_price, f"{coin} < ${strike:,.0f}")
                pick = _pick(signal, yes, no)
                out.append({
                    "ticker": ticker, "coin": coin, "category": "Crypto",
                    "bet_type": f"{coin} 15-min", "strike": strike,
                    "spot": round(spot, 2), "sigma_annual": round(sigma, 3),
                    "seconds_to_close": int(secs), "close_time": m.get("close_time"),
                    "signal": signal, "tier": _tier(model_yes),
                    "yes": {k: yes[k] for k in ("model_prob", "kalshi_price_cents", "market_odds_decimal", "ev_per_dollar", "edge", "selection")},
                    "no": {k: no[k] for k in ("model_prob", "kalshi_price_cents", "market_odds_decimal", "ev_per_dollar", "edge", "selection")},
                    "pick": pick,
                    "has_pick": pick["side"] is not None,
                })

    # Strongest actionable pick first (real pick > pass; then by the pick's EV), soonest close next.
    out.sort(key=lambda b: (b["has_pick"], b["pick"].get("ev_per_dollar", 0.0)), reverse=True)
    _CACHE["data"] = out
    _CACHE["ts"] = now
    # Snapshot any new actionable picks so the model keeps its own honest track record.
    try:
        from app import crypto_log
        crypto_log.record_picks(out)
    except Exception as exc:
        print(f"[kalshi_crypto] pick logging failed: {exc}")
    return [b for b in out if not coin_filter or b["coin"].lower() == coin_filter.lower()]


async def fetch_result(ticker: str) -> Optional[str]:
    """The settled outcome of a market: 'yes', 'no', or None if not settled yet / unavailable."""
    try:
        async with httpx.AsyncClient(timeout=15, headers=_UA, follow_redirects=True) as c:
            r = await c.get(f"{KALSHI_BASE}/markets/{ticker}")
            res = ((r.json().get("market") or {}).get("result") or "").lower()
            return res if res in ("yes", "no") else None
    except Exception as exc:
        print(f"[kalshi_crypto] result {ticker} failed: {exc}")
        return None
