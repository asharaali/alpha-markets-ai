"""
Kalshi authenticated trading — LIVE order placement.

Only reachable when AUTOBET_LIVE_ALLOWED=true AND a Kalshi key is set. Kalshi signs every
request with an RSA private key (key id + RSA-PSS-SHA256 over timestamp+method+path).

NOTE: this path is untested until a real key is provided. The first live order should be a
tiny one (the $10 hard cap limits any blast radius) and watched closely.
"""
from __future__ import annotations
import base64
import time
import uuid

import httpx

from app.config import settings
from app.data_sources.kalshi_orderbook import orderbook_prices

BASE = "https://api.elections.kalshi.com/trade-api/v2"


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


async def place_order(ticker: str, side: str, stake_dollars: float):
    """
    Buy `side` ('yes'|'no') contracts on a specific Kalshi market for ~stake_dollars.
    Prices off the live ORDERBOOK (the markets-list yes_ask is always null — that bug is
    what made every order fail with 'no live ask price'). Returns (ok, info).
    """
    if not (settings.KALSHI_KEY_ID and settings.KALSHI_PRIVATE_KEY):
        return False, "no Kalshi key configured"
    side = side.lower()
    try:
        async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "AlphaMarketsAI/1.0"}) as c:
            yes_bid, yes_ask, depth = await orderbook_prices(c, ticker)
            # Cost to buy the side you want: YES at the yes ask, NO at (1 - yes bid).
            price = yes_ask if side == "yes" else ((1 - yes_bid) if yes_bid is not None else None)
            if not price:
                return False, "no live ask price (empty/illiquid order book)"
            cents = round(price * 100)
            count = int(stake_dollars / price)
            if count < 1:
                return False, f"stake ${stake_dollars} too small for one contract at {cents}¢"
            path = "/trade-api/v2/portfolio/orders"
            body = {"ticker": ticker, "action": "buy", "side": side,
                    "count": count, "type": "market", "client_order_id": uuid.uuid4().hex}
            r = await c.post(f"{BASE}/portfolio/orders", headers=_signed_headers("POST", path), json=body)
        if r.status_code in (200, 201):
            return True, f"bought {count} {side.upper()} @ ~{cents}¢ on {ticker}"
        return False, f"Kalshi rejected ({r.status_code}): {r.text[:140]}"
    except Exception as exc:
        return False, f"error: {exc}"


async def place_yes(home: str, away: str, selection: str, stake_dollars: float):
    """Buy YES on a World Cup matchup outcome for ~stake_dollars. Returns (ok, info)."""
    mk = await _find_market(home, away, selection)
    if not mk:
        return False, "no matching open Kalshi market"
    return await place_order(mk["ticker"], "yes", stake_dollars)
