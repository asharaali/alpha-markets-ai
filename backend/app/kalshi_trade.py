"""
Kalshi authenticated trading — LIVE order placement.

Only reachable when AUTOBET_LIVE_ALLOWED=true AND a Kalshi key is set. Kalshi signs every
request with an RSA private key (key id + RSA-PSS-SHA256 over timestamp+method+path).

NOTE: this path is untested until a real key is provided. The first live order should be a
tiny one (the $10 hard cap limits any blast radius) and watched closely.
"""
from __future__ import annotations
import base64
import os
import time
import uuid

import httpx

from app.config import settings
from app.data_sources.kalshi_orderbook import orderbook_prices

BASE = "https://api.elections.kalshi.com/trade-api/v2"
# V2 order endpoint (the old /portfolio/orders was deprecated -> 410). New shape: side bid/ask
# from the YES book, fixed-point dollar price/count strings. Host overridable via env in case
# prod differs from the documented one.
ORDER_BASE = os.getenv("KALSHI_ORDER_BASE", "https://external-api.kalshi.com/trade-api/v2").rstrip("/")
ORDER_PATH = "/trade-api/v2/portfolio/events/orders"


def _signed_headers(method: str, path: str):
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    ts = str(int(time.time() * 1000))
    msg = (ts + method + path).encode()
    key = serialization.load_pem_private_key(settings.KALSHI_PRIVATE_KEY.encode(), password=None)
    sig = key.sign(
        msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": settings.KALSHI_KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "Content-Type": "application/json",
    }


async def get_balance():
    """Read-only: confirm the key works by fetching the account balance. Places nothing."""
    if not (settings.KALSHI_KEY_ID and settings.KALSHI_PRIVATE_KEY):
        return False, "no Kalshi key configured"
    try:
        path = "/trade-api/v2/portfolio/balance"
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{BASE}/portfolio/balance", headers=_signed_headers("GET", path))
        if r.status_code == 200:
            cents = r.json().get("balance", 0)
            return True, {"balance_usd": round(cents / 100, 2)}
        return False, f"Kalshi rejected ({r.status_code}): {r.text[:140]}"
    except Exception as exc:
        return False, f"error: {exc}"


async def _find_market(home: str, away: str, selection: str):
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{BASE}/events", params={"series_ticker": "KXWCGAME",
                        "with_nested_markets": "true", "status": "open", "limit": 200})
        r.raise_for_status()
        for e in r.json().get("events", []):
            title = e.get("title", "")
            if home in title and away in title:
                for m in e.get("markets", []):
                    sub = (m.get("yes_sub_title") or "").strip()
                    if selection == "Draw" and sub.lower() in ("tie", "draw"):
                        return m
                    if sub == selection or (selection and selection in sub):
                        return m
    return None


async def live_cashout_price(home: str, away: str, selection: str):
    """
    The REAL price you could cash out a position for right now, from the live Kalshi book.
    A position is YES on its selection; cashing out = SELLING your YES into the current bid.
    Returns {ticker, cashout_price (0-1, what you'd get selling YES), market_yes (current
    fair-ish mid), depth} or None if the market can't be found / has no book.
    """
    mk = await _find_market(home, away, selection)
    if not mk:
        return None
    async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "AlphaMarketsAI/1.0"}) as c:
        yes_bid, yes_ask, depth = await orderbook_prices(c, mk["ticker"])
    if yes_bid is None and yes_ask is None:
        return {"ticker": mk["ticker"], "cashout_price": None, "market_yes": None, "depth": depth}
    mid = ((yes_bid + yes_ask) / 2) if (yes_bid and yes_ask) else (yes_bid or yes_ask)
    return {
        "ticker": mk["ticker"],
        "cashout_price": yes_bid,   # what you can SELL your YES for right now
        "market_yes": round(mid, 4) if mid else None,
        "depth": depth,
    }


async def place_order(ticker: str, side: str, stake_dollars: float):
    """
    Buy `side` ('yes'|'no') contracts on a specific Kalshi market for ~stake_dollars, using
    the V2 order endpoint. Everything is quoted from the YES book: buying YES is a 'bid' at
    the yes-ask; buying NO is an 'ask' (sell YES) at the yes-bid. We send an immediate-or-
    cancel order priced AT the book, so it behaves like a market order but can never fill at
    a worse price than we saw. Returns (ok, info).
    """
    if not (settings.KALSHI_KEY_ID and settings.KALSHI_PRIVATE_KEY):
        return False, "no Kalshi key configured"
    side = side.lower()
    try:
        async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "AlphaMarketsAI/1.0"}) as c:
            yes_bid, yes_ask, depth = await orderbook_prices(c, ticker)
            if side == "yes":
                v2_side, yes_price, cost = "bid", yes_ask, yes_ask           # pay the ask
            else:                                                            # buy NO == sell YES at the bid
                v2_side, yes_price, cost = "ask", yes_bid, (1 - yes_bid) if yes_bid is not None else None
            if not yes_price or not cost:
                return False, "no live price (empty/illiquid order book)"
            count = int(stake_dollars / cost)
            if count < 1:
                return False, f"stake ${stake_dollars} too small for one contract at {round(cost*100)}¢"
            body = {
                "ticker": ticker,
                "side": v2_side,
                "count": str(count),
                "price": f"{yes_price:.4f}",                # fixed-point dollars (YES price)
                "time_in_force": "immediate_or_cancel",      # take now, don't rest
                "self_trade_prevention_type": "taker_at_cross",
                "client_order_id": uuid.uuid4().hex,
            }
            r = await c.post(f"{ORDER_BASE}/portfolio/events/orders",
                             headers=_signed_headers("POST", ORDER_PATH), json=body)
        if r.status_code in (200, 201):
            return True, f"bought {count} {side.upper()} @ ~{round(cost*100)}¢ on {ticker}"
        return False, f"Kalshi rejected ({r.status_code}): {r.text[:160]}"
    except Exception as exc:
        return False, f"error: {exc}"


async def place_yes(home: str, away: str, selection: str, stake_dollars: float):
    """Buy YES on a World Cup matchup outcome for ~stake_dollars. Returns (ok, info)."""
    mk = await _find_market(home, away, selection)
    if not mk:
        return False, "no matching open Kalshi market"
    return await place_order(mk["ticker"], "yes", stake_dollars)


async def place_leg_detailed(home: str, away: str, selection: str, stake_dollars: float):
    """
    Place one combo leg (buy YES) and return STRUCTURED info we can store for live tracking
    and cash-out: {ok, ticker, entry_price (0-1 cost per contract), count, info}.
    """
    mk = await _find_market(home, away, selection)
    if not mk:
        return {"ok": False, "info": "no matching open Kalshi market", "ticker": None}
    ticker = mk["ticker"]
    try:
        async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "AlphaMarketsAI/1.0"}) as c:
            yes_bid, yes_ask, _ = await orderbook_prices(c, ticker)
    except Exception as exc:
        return {"ok": False, "info": f"book error: {exc}", "ticker": ticker}
    cost = yes_ask
    if not cost:
        return {"ok": False, "info": "no live ask (illiquid)", "ticker": ticker}
    ok, info = await place_order(ticker, "yes", stake_dollars)
    return {"ok": ok, "info": info, "ticker": ticker,
            "entry_price": round(cost, 4), "count": int(stake_dollars / cost)}


async def place_ticker_detailed(ticker: str, stake_dollars: float, side: str = "yes"):
    """Buy `side` ('yes'|'no') on a specific Kalshi ticker (used for single bets / combo legs that
    already know their exact ticker — all the non-moneyline markets, plus No-side picks like
    'BTTS No'). Returns structured info like place_leg_detailed."""
    side = (side or "yes").lower()
    try:
        async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "AlphaMarketsAI/1.0"}) as c:
            yes_bid, yes_ask, _d = await orderbook_prices(c, ticker)
    except Exception as exc:
        return {"ok": False, "info": f"book error: {exc}", "ticker": ticker}
    # Cost per contract: buying YES pays the yes-ask; buying NO pays (1 - yes-bid).
    cost = yes_ask if side == "yes" else ((1 - yes_bid) if yes_bid is not None else None)
    if not cost:
        return {"ok": False, "info": "no live price (illiquid)", "ticker": ticker}
    ok, info = await place_order(ticker, side, stake_dollars)
    return {"ok": ok, "info": info, "ticker": ticker, "side": side,
            "entry_price": round(cost, 4), "count": int(stake_dollars / cost)}


async def close_ticker(ticker: str, stake_dollars: float):
    """Cash out a YES position by ticker: sell YES (= buy NO into the bid). Returns (ok, info)."""
    return await place_order(ticker, "no", stake_dollars)


async def close_position(home: str, away: str, selection: str, stake_dollars: float):
    """
    Cash out a YES position: SELL YES by buying NO into the current bid (this is exactly how
    Kalshi closes a long YES). Returns (ok, info). stake_dollars sizes how much to unwind.
    """
    mk = await _find_market(home, away, selection)
    if not mk:
        return False, "no matching open Kalshi market"
    return await place_order(mk["ticker"], "no", stake_dollars)


async def get_positions():
    """
    Read-only: the tickers you currently hold contracts in (for manual cash-out sync —
    if a tracked leg's position is gone, you sold it yourself). Returns (ok, {ticker: count}).
    """
    if not (settings.KALSHI_KEY_ID and settings.KALSHI_PRIVATE_KEY):
        return False, "no Kalshi key configured"
    try:
        path = "/trade-api/v2/portfolio/positions"
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{BASE}/portfolio/positions", headers=_signed_headers("GET", path),
                            params={"count_filter": "position", "limit": 1000})
        if r.status_code != 200:
            return False, f"Kalshi rejected ({r.status_code}): {r.text[:140]}"
        held = {}
        for p in r.json().get("market_positions", []):
            pos = p.get("position", 0)
            if pos:                              # non-zero => still holding
                held[p.get("ticker")] = pos
        return True, held
    except Exception as exc:
        return False, f"error: {exc}"
