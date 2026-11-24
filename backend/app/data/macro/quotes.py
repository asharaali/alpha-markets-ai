"""Delayed market quotes: Yahoo chart feed (unofficial, best-effort) and Coinbase spot."""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional

from app.core.http import make_client

YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
SYMBOLS = {"SPY": "S&P 500 (SPY)", "QQQ": "Nasdaq 100 (QQQ)", "GLD": "Gold (GLD)",
           "USO": "Crude oil (USO)", "TLT": "20Y bonds (TLT)", "^VIX": "VIX"}


@dataclass
class Quote:
    symbol: str
    label: str
    price: float
    prev_close: Optional[float]
    change_pct: Optional[float]
    as_of: float
    market_open: bool
    kind: str = "quote"
    source: str = "Yahoo (unofficial, delayed)"

    def to_dict(self):
        return asdict(self)


def parse_yahoo(payload: Dict[str, Any], *, now: Optional[float] = None) -> Optional[Quote]:
    try:
        meta = payload["chart"]["result"][0]["meta"]
        price, prev = float(meta["regularMarketPrice"]), meta.get("chartPreviousClose")
        reg = meta.get("currentTradingPeriod", {}).get("regular", {})
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    now = time.time() if now is None else now
    is_open = bool(reg) and reg.get("start", 0) <= now < reg.get("end", 0)
    sym = meta.get("symbol", "?")
    return Quote(symbol=sym, label=SYMBOLS.get(sym, sym), price=price,
                 prev_close=float(prev) if prev else None,
                 change_pct=round((price / float(prev) - 1) * 100, 4) if prev else None,
                 as_of=float(meta.get("regularMarketTime") or now), market_open=is_open)


def parse_coinbase(payload: Dict[str, Any]) -> Quote:
    d = payload["data"]
    return Quote(symbol=d["base"], label="Bitcoin", price=float(d["amount"]), prev_close=None,
                 change_pct=None, as_of=time.time(), market_open=True, source="Coinbase")


async def fetch_yahoo(symbol: str) -> Quote:
    async with make_client(timeout=20, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}) as c:
        r = await c.get(YAHOO.format(symbol=symbol), params={"range": "1d", "interval": "5m"})
        r.raise_for_status()
        q = parse_yahoo(r.json())
        if q is None:
            raise RuntimeError(f"Yahoo returned no quote for {symbol}")
        return q


async def fetch_btc() -> Quote:
    async with make_client(timeout=20) as c:
        r = await c.get("https://api.coinbase.com/v2/prices/BTC-USD/spot")
        r.raise_for_status()
        return parse_coinbase(r.json())
