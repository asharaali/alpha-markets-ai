"""Kalshi macro markets: Fed decision odds (KXFED) and recession odds (KXRECSSNBER). Public data.

KXFED rules (read 2026-11-23): "If the upper bound of the target federal funds rate ... is greater
than X% following the meeting, the market resolves to Yes." Each event is a ladder of P(upper > X);
odds for cut / hold / hike come from differences between rungs around today's upper bound.
These are market prices, not our forecast — `kind = "market"`.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.config import settings
from app.core.http import make_client
from app.data.browse_market import to_price

STEP = 0.25


def _mid(m: Dict[str, Any]) -> Optional[float]:
    bid, ask = to_price(m.get("yes_bid_dollars")), to_price(m.get("yes_ask_dollars"))
    # to_price drops 0 and 1; a 0.99/1.00 book is "certain yes", a 0.00/0.01 book "certain no".
    if bid is None and ask is None:
        raw_bid = m.get("yes_bid_dollars")
        try:
            return 1.0 if float(raw_bid or 0) >= 0.99 else None
        except ValueError:
            return None
    if bid is None:
        return ask / 2
    if ask is None:
        return (bid + 1.0) / 2
    return (bid + ask) / 2


def _ts(iso: Optional[str]) -> float:
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return float("inf")


@dataclass
class FedOdds:
    event: str
    title: str
    meeting_date: str
    current_upper: float
    outcomes: List[Dict[str, Any]]
    url: str = "https://kalshi.com/markets/kxfed"
    kind: str = "market"
    source: str = "Kalshi"
    as_of: float = field(default_factory=time.time)

    def to_dict(self):
        return asdict(self)


def _ladder(event: Dict[str, Any]) -> List[tuple]:
    rungs = []
    for m in event.get("markets") or []:
        strike, p = m.get("floor_strike"), _mid(m)
        if strike is not None and p is not None:
            rungs.append((float(strike), min(max(p, 0.0), 1.0)))
    rungs.sort()
    repaired, running = [], 1.0
    for strike, p in rungs:            # P(upper > x) can only fall as x rises
        running = min(running, p)
        repaired.append((strike, running))
    return repaired


def _p_above(ladder: List[tuple], x: float) -> float:
    for strike, p in ladder:
        if abs(strike - x) < 1e-9:
            return p
    if ladder and x < ladder[0][0]:
        return 1.0
    if ladder and x > ladder[-1][0]:
        return 0.0
    below = [p for s, p in ladder if s < x]          # between rungs: be conservative
    return below[-1] if below else 1.0


def fed_decision(payload: Dict[str, Any], *, current_upper: float, now: Optional[float] = None) -> Optional[FedOdds]:
    now = time.time() if now is None else now
    events = [e for e in payload.get("events") or [] if _ts(e.get("strike_date")) >= now]
    if not events:
        return None
    event = min(events, key=lambda e: _ts(e.get("strike_date")))
    ladder = _ladder(event)
    if not ladder:
        return None
    u = current_upper
    p = lambda x: _p_above(ladder, x)                  # noqa: E731
    outcomes = [
        ("Cut 50bp+", 1.0 - p(u - 2 * STEP)),
        ("Cut 25bp", p(u - 2 * STEP) - p(u - STEP)),
        ("Hold", p(u - STEP) - p(u)),
        ("Hike 25bp", p(u) - p(u + STEP)),
        ("Hike 50bp+", p(u + STEP)),
    ]
    rows = sorted(({"label": lab, "prob": round(max(v, 0.0), 4)} for lab, v in outcomes),
                  key=lambda o: -o["prob"])
    total = sum(o["prob"] for o in rows) or 1.0
    for o in rows:
        o["prob"] = o["prob"] / total
    return FedOdds(event=event["event_ticker"], title=event.get("title", ""),
                   meeting_date=str(event.get("strike_date")), current_upper=u, outcomes=rows)


@dataclass
class Odds:
    label: str
    prob: float
    url: str
    kind: str = "market"
    source: str = "Kalshi"
    as_of: float = field(default_factory=time.time)

    def to_dict(self):
        return asdict(self)


def recession(payload: Dict[str, Any]) -> Optional[Odds]:
    for event in payload.get("events") or []:
        for m in event.get("markets") or []:
            p = _mid(m)
            if p is not None:
                return Odds(label=event.get("title", "Recession"), prob=round(p, 4),
                            url=f"https://kalshi.com/markets/{str(event.get('series_ticker', 'kxrecssnber')).lower()}")
    return None


async def _events(series: str) -> Dict[str, Any]:
    async with make_client(timeout=20) as c:
        r = await c.get(f"{settings.KALSHI_BASE}/events",
                        params={"series_ticker": series, "status": "open", "with_nested_markets": "true", "limit": 6})
        r.raise_for_status()
        return r.json()


async def fetch_fed(current_upper: float) -> FedOdds:
    odds = fed_decision(await _events("KXFED"), current_upper=current_upper)
    if odds is None:
        raise RuntimeError("no open Kalshi Fed meeting market")
    return odds


async def fetch_recession() -> Odds:
    odds = recession(await _events("KXRECSSNBER"))
    if odds is None:
        raise RuntimeError("no priced Kalshi recession market")
    return odds
