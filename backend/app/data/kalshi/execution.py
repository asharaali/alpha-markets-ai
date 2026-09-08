"""Order execution — paper by default, live only behind three explicit gates.

Paper trading is the product's default and its normal mode. It records exactly what a live
order would have done, at the price that was actually resting on the book at that moment,
so a paper track record is a fair simulation rather than a fantasy fill at the mid.

Live trading requires ALL of:
  1. LIVE_TRADING_ENABLED=true
  2. Kalshi API credentials present
  3. The logged-in user matching LIVE_TRADING_USER

Any one missing and the order is placed on paper and clearly labelled as such. There is no
code path that places a real order by accident, and none that treats a failed live order as
a success.

Live orders are immediate-or-cancel at the price we just read, so a real fill can never be
worse than the book we showed the user. If the book has moved, the order simply does not
fill — which is the correct outcome, not an error to route around.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

from app.config import live_trading_available, settings
from app.core.errors import ConfigError, ValidationError
from app.core.http import make_client, request
from app.core.logging import get_logger
from app.core.types import Side
from app.data.kalshi import client as kc
from app.data.kalshi import orderbook as ob
from app.tracking import store

log = get_logger(__name__)

ORDER_PATH = "/trade-api/v2/portfolio/events/orders"


@dataclass
class Fill:
    ok: bool
    mode: str                 # paper | live
    ticker: str
    side: str
    contracts: int
    price: float              # cost per contract, 0-1
    stake: float
    message: str
    position_id: Optional[str] = None
    order_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok, "mode": self.mode, "ticker": self.ticker,
            "side": self.side, "contracts": self.contracts,
            "price": round(self.price, 4), "stake": round(self.stake, 2),
            "message": self.message, "position_id": self.position_id,
            "order_id": self.order_id,
        }


def resolve_mode(username: Optional[str], requested: str = "paper") -> tuple[str, str]:
    """(mode, explanation). Anything not explicitly permitted resolves to paper."""
    if requested != "live":
        return "paper", "paper trading"
    allowed, reason = live_trading_available(username)
    if not allowed:
        return "paper", f"live trading unavailable ({reason}) — placed on paper instead"
    return "live", "live trading enabled for this account"


def _check_limits(stake: float, username: str) -> None:
    """Hard ceilings a user cannot raise from the interface."""
    if stake <= 0:
        raise ValidationError("stake must be greater than zero")
    if stake > settings.HARD_MAX_STAKE:
        raise ValidationError(
            f"stake ${stake:.2f} exceeds the hard per-order ceiling of "
            f"${settings.HARD_MAX_STAKE:.2f}")
    spent_today = sum(
        float(p.get("stake") or 0)
        for p in store.positions_for(username)
        if float(p.get("created_at") or 0) > time.time() - 86400)
    if spent_today + stake > settings.HARD_DAILY_CAP:
        raise ValidationError(
            f"this order would take today's staked total to "
            f"${spent_today + stake:.2f}, past the hard daily cap of "
            f"${settings.HARD_DAILY_CAP:.2f}")


async def read_price(ticker: str, side: Side) -> tuple[Optional[float], float]:
    """(cost per contract, resting depth) for buying `side` right now."""
    async with make_client() as client:
        yes_bid, yes_ask, depth = await ob.fetch(client, ticker)
    if side is Side.YES:
        return yes_ask, depth
    return ((1.0 - yes_bid) if yes_bid is not None else None), depth


async def place(*, username: str, ticker: str, side: str, stake: float,
                requested_mode: str = "paper", game_id: Optional[str] = None,
                label: Optional[str] = None, market_type: Optional[str] = None,
                model_prob: Optional[float] = None,
                parlay_id: Optional[str] = None) -> Fill:
    """Place one order. Records a position either way, tagged with the mode used."""
    side_enum = Side.NO if str(side).lower() == "no" else Side.YES
    _check_limits(stake, username)

    cost, depth = await read_price(ticker, side_enum)
    if cost is None or cost <= 0 or cost >= 1:
        return Fill(ok=False, mode="paper", ticker=ticker, side=side_enum.value,
                    contracts=0, price=0.0, stake=0.0,
                    message="No live price on that contract — nothing was placed.")

    contracts = int(stake / cost)
    if contracts < 1:
        return Fill(ok=False, mode="paper", ticker=ticker, side=side_enum.value,
                    contracts=0, price=cost, stake=0.0,
                    message=(f"${stake:.2f} is not enough for a single contract at "
                             f"{cost * 100:.0f}c."))
    actual_stake = contracts * cost

    mode, explanation = resolve_mode(username, requested_mode)
    order_id = None
    if mode == "live":
        ok, info, order_id = await _place_live(ticker, side_enum, contracts, cost)
        if not ok:
            # A failed live order is a failure. It is never quietly recorded as a paper
            # fill, because that would put a phantom position in the track record.
            return Fill(ok=False, mode="live", ticker=ticker, side=side_enum.value,
                        contracts=0, price=cost, stake=0.0, message=info)
        explanation = info

    position_id = store.open_position(
        user=username, mode=mode, parlay_id=parlay_id, game_id=game_id,
        ticker=ticker, side=side_enum.value, label=label, market_type=market_type,
        contracts=contracts, entry_price=cost, stake=actual_stake,
        model_prob=model_prob,
        note=explanation)

    log.info("%s order: %d x %s %s @ %.2f (user=%s)", mode, contracts, ticker,
             side_enum.value, cost, username)
    return Fill(ok=True, mode=mode, ticker=ticker, side=side_enum.value,
                contracts=contracts, price=cost, stake=actual_stake,
                message=(f"{'Placed' if mode == 'live' else 'Recorded on paper'}: "
                         f"{contracts} x {side_enum.value.upper()} at "
                         f"{cost * 100:.0f}c (${actual_stake:.2f}). {explanation}"),
                position_id=position_id, order_id=order_id)


async def _place_live(ticker: str, side: Side, contracts: int,
                      cost: float) -> tuple[bool, str, Optional[str]]:
    """Submit a real immediate-or-cancel order to Kalshi.

    Everything is quoted off the YES book: buying YES is a bid at the yes-ask; buying NO is
    an ask (selling YES) at the yes-bid. Priced AT the book we just read, so it behaves like
    a market order that cannot fill worse than what the user was shown.
    """
    if not kc.credentials_present():
        raise ConfigError("Kalshi credentials are missing")
    yes_price = cost if side is Side.YES else (1.0 - cost)
    body = {
        "ticker": ticker,
        "side": "bid" if side is Side.YES else "ask",
        "count": str(contracts),
        "price": f"{yes_price:.4f}",
        "time_in_force": "immediate_or_cancel",
        "self_trade_prevention_type": "taker_at_cross",
        "client_order_id": uuid.uuid4().hex,
    }
    try:
        async with make_client() as client:
            resp = await request(client, "POST",
                                 f"{settings.KALSHI_ORDER_BASE}/portfolio/events/orders",
                                 headers=kc.signed_headers("POST", ORDER_PATH),
                                 json=body, source="kalshi", attempts=1)
    except Exception as exc:  # noqa: BLE001
        return False, f"Live order failed to send: {exc}", None
    if resp.status_code in (200, 201):
        payload = resp.json() if resp.content else {}
        order_id = (payload.get("order") or {}).get("order_id") or payload.get("order_id")
        return True, "Live order accepted by Kalshi.", order_id
    return False, f"Kalshi rejected the order ({resp.status_code}): {resp.text[:180]}", None


async def close(*, username: str, position_id: str) -> Dict[str, Any]:
    """Close an open position at the current book, recording realised P&L."""
    rows = [p for p in store.positions_for(username, status="open")
            if p["id"] == position_id]
    if not rows:
        raise ValidationError("no open position with that id")
    position = rows[0]

    held_side = Side.NO if position["side"] == "no" else Side.YES
    async with make_client() as client:
        yes_bid, yes_ask, _depth = await ob.fetch(client, position["ticker"])
    # Closing sells what we hold: a YES position is sold into the yes bid.
    exit_price = yes_bid if held_side is Side.YES else (
        (1.0 - yes_ask) if yes_ask is not None else None)
    if exit_price is None:
        raise ValidationError("no live bid to close into — the book is empty")

    if position["mode"] == "live":
        ok, info, _ = await _place_live(
            position["ticker"],
            Side.NO if held_side is Side.YES else Side.YES,
            int(position["contracts"]), 1.0 - exit_price)
        if not ok:
            raise ValidationError(info)

    updated = store.close_position(position_id, exit_price=float(exit_price),
                                   note="closed at live book")
    return {"closed": updated, "exit_price": round(float(exit_price), 4)}


async def account_status(username: Optional[str]) -> Dict[str, Any]:
    """What execution modes are available, and why. Surfaced in Settings."""
    allowed, reason = live_trading_available(username)
    status: Dict[str, Any] = {
        "paper_available": True,
        "live_available": allowed,
        "reason": reason,
        "credentials_configured": kc.credentials_present(),
        "hard_max_stake": settings.HARD_MAX_STAKE,
        "hard_daily_cap": settings.HARD_DAILY_CAP,
    }
    if kc.credentials_present():
        try:
            status["kalshi_balance"] = await kc.balance()
        except Exception as exc:  # noqa: BLE001
            status["kalshi_balance_error"] = str(exc)[:180]
    return status
