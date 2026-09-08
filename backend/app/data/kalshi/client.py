"""Kalshi API client: public market reads plus authenticated portfolio access.

Kalshi signs authenticated requests with an RSA private key — the key ID goes in a header
and an RSA-PSS/SHA-256 signature covers timestamp + method + path. Public market data needs
no credentials at all, which is why the entire research side of this app works before you
have ever connected an account.
"""
from __future__ import annotations

import base64
import time
from typing import Any, Dict, List, Optional

import httpx

from app.config import settings
from app.core.errors import ConfigError, UpstreamError
from app.core.http import get_json, make_client, request
from app.core.logging import get_logger

log = get_logger(__name__)

BASE = settings.KALSHI_BASE


def credentials_present() -> bool:
    return bool(settings.KALSHI_KEY_ID and settings.KALSHI_PRIVATE_KEY)


def signed_headers(method: str, path: str) -> Dict[str, str]:
    """RSA-PSS signature over (timestamp_ms + METHOD + path), per Kalshi's auth scheme.

    `path` must be the full request path including the /trade-api/v2 prefix — signing the
    wrong string produces a 401 that looks exactly like a bad key.
    """
    if not credentials_present():
        raise ConfigError("Kalshi API credentials are not configured",
                          detail="set KALSHI_KEY_ID and KALSHI_PRIVATE_KEY")
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    timestamp = str(int(time.time() * 1000))
    message = (timestamp + method.upper() + path).encode()
    try:
        key = serialization.load_pem_private_key(
            settings.KALSHI_PRIVATE_KEY.replace("\\n", "\n").encode(), password=None)
    except (ValueError, TypeError) as exc:
        raise ConfigError("KALSHI_PRIVATE_KEY is not a readable PEM private key",
                          detail=str(exc)[:200]) from exc
    signature = key.sign(
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": settings.KALSHI_KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
        "Content-Type": "application/json",
    }


async def public_get(client: httpx.AsyncClient, path: str,
                     params: Optional[Dict[str, Any]] = None) -> Any:
    """Unauthenticated market-data read."""
    return await get_json(client, f"{BASE}{path}", params=params, source="kalshi")


async def events(client: httpx.AsyncClient, series_ticker: str, *,
                 status: str = "open", limit: int = 200) -> List[Dict[str, Any]]:
    """All open events for a series, with their nested markets.

    Kalshi paginates with a cursor; a busy NFL Sunday can exceed one page, and stopping at
    page one would silently drop half the slate.
    """
    out: List[Dict[str, Any]] = []
    cursor: Optional[str] = None
    for _ in range(10):                     # hard stop: 2,000 events is far beyond a slate
        params: Dict[str, Any] = {"series_ticker": series_ticker,
                                  "with_nested_markets": "true",
                                  "status": status, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        payload = await public_get(client, "/events", params)
        batch = payload.get("events") or []
        out.extend(batch)
        cursor = payload.get("cursor") or None
        if not cursor or not batch:
            break
    return out


# ----------------------------------------------------------------- authenticated reads

async def balance() -> Dict[str, Any]:
    """Account balance. Read-only — proves the key works without placing anything."""
    path = "/trade-api/v2/portfolio/balance"
    async with make_client() as client:
        resp = await request(client, "GET", f"{BASE}/portfolio/balance",
                             headers=signed_headers("GET", path), source="kalshi")
    if resp.status_code != 200:
        raise UpstreamError(f"Kalshi rejected the balance request ({resp.status_code})",
                            detail=resp.text[:200])
    cents = resp.json().get("balance", 0)
    return {"balance_usd": round(cents / 100.0, 2)}


async def positions() -> Dict[str, int]:
    """Currently held contract counts, keyed by ticker."""
    path = "/trade-api/v2/portfolio/positions"
    async with make_client() as client:
        resp = await request(client, "GET", f"{BASE}/portfolio/positions",
                             headers=signed_headers("GET", path),
                             params={"count_filter": "position", "limit": 1000},
                             source="kalshi")
    if resp.status_code != 200:
        raise UpstreamError(f"Kalshi rejected the positions request ({resp.status_code})",
                            detail=resp.text[:200])
    held: Dict[str, int] = {}
    for row in resp.json().get("market_positions", []):
        count = row.get("position", 0)
        if count:
            held[row.get("ticker")] = count
    return held
