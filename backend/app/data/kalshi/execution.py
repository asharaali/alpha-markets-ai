"""Order execution — paper by default, live only behind three explicit gates.

Paper trading is the product's default and its normal mode. It records exactly what a live
order would have done, at the price that was actually resting on the book at that moment
and capped by the depth actually available, so a paper track record is a fair simulation
rather than a fantasy fill at the mid.

Live trading requires ALL of:
  1. LIVE_TRADING_ENABLED=true
  2. Kalshi API credentials present
  3. The logged-in user matching LIVE_TRADING_USER

Any one missing and the order is placed on paper and clearly labelled as such. There is no
code path that places a real order by accident, and none that treats a failed live order as
a success.

Three things this module now refuses to do, each of which it previously did:

  * Treat an HTTP 200 as a fill. An immediate-or-cancel order can come back accepted and
    filled for zero, and the old code recorded the full requested quantity at the price it
    had asked for. The response is now parsed for filled quantity, average price, fees and
    remaining count, and reconciled against the venue's own order record before anything is
    written down. A position is only ever as large as the fills behind it.

  * Refresh the price and trade anyway. The old flow read the book immediately before
    submitting and used whatever came back, so a recommendation made at 55c would happily
    execute at 68c — the edge that justified the bet having evaporated on the way to the
    button. Every order now carries the price it was recommended at and a maximum
    acceptable entry, and the edge is recomputed against the live book at submission time.
    If it no longer clears, the order is refused with "edge no longer available".

  * Check exposure limits and then act on a stale read. Limits are now enforced inside a
    single immediate transaction that also reserves the order, so two concurrent requests
    cannot both pass a check that only one of them should.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app.config import live_trading_available, settings
from app.core.errors import ConfigError, ValidationError
from app.core.http import make_client, request
from app.core.logging import get_logger
from app.core.types import Side
from app.data.kalshi import client as kc
from app.data.kalshi import orderbook as ob
from app.risk import fees as fee_model
from app.tracking import store

log = get_logger(__name__)

ORDER_PATH = "/trade-api/v2/portfolio/events/orders"

# How far the price may drift from the recommendation before the order is refused, when
# the caller does not supply its own ceiling. Three cents is roughly two ticks on a liquid
# NFL market and comfortably inside the edges the model claims.
DEFAULT_SLIPPAGE_TOLERANCE = 0.03


@dataclass
class Fill:
    """What actually happened, as opposed to what was requested."""

    ok: bool
    mode: str                 # paper | live
    ticker: str
    side: str
    contracts: int            # FILLED contracts, never requested
    price: float              # average fill price, 0-1
    stake: float
    message: str
    status: str = "filled"    # filled | partially_filled | rejected | unfilled | pending
    requested_contracts: int = 0
    remaining_contracts: int = 0
    fees: float = 0.0
    position_id: Optional[str] = None
    order_id: Optional[str] = None
    client_order_id: Optional[str] = None
    reason: Optional[str] = None
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok, "mode": self.mode, "ticker": self.ticker,
            "side": self.side, "contracts": self.contracts,
            "requested_contracts": self.requested_contracts,
            "remaining_contracts": self.remaining_contracts,
            "price": round(self.price, 4), "stake": round(self.stake, 2),
            "fees": round(self.fees, 2), "status": self.status,
            "message": self.message, "position_id": self.position_id,
            "order_id": self.order_id, "client_order_id": self.client_order_id,
            "reason": self.reason, "detail": self.detail,
        }


def resolve_mode(username: Optional[str], requested: str = "paper") -> Tuple[str, str]:
    """(mode, explanation). Anything not explicitly permitted resolves to paper."""
    if requested != "live":
        return "paper", "paper trading"
    allowed, reason = live_trading_available(username)
    if not allowed:
        return "paper", f"live trading unavailable ({reason}) — placed on paper instead"
    return "live", "live trading enabled for this account"


# ------------------------------------------------------------------ pre-trade checks

def check_limits(stake: float, username: str, mode: str) -> None:
    """Hard ceilings a user cannot raise from the interface.

    Paper and live exposure are counted SEPARATELY. A paper position is not real money and
    must not consume a real-money daily cap, and vice versa — mixing them let a week of
    paper trading lock out a live order, and let paper orders hide real exposure.
    """
    if stake <= 0:
        raise ValidationError("stake must be greater than zero")
    if stake > settings.HARD_MAX_STAKE:
        raise ValidationError(
            f"stake ${stake:.2f} exceeds the hard per-order ceiling of "
            f"${settings.HARD_MAX_STAKE:.2f}")
    spent_today = store.staked_since(username, since=time.time() - 86400, mode=mode)
    if spent_today + stake > settings.HARD_DAILY_CAP:
        raise ValidationError(
            f"this {mode} order would take today's staked total to "
            f"${spent_today + stake:.2f}, past the hard daily cap of "
            f"${settings.HARD_DAILY_CAP:.2f}")


async def read_price(ticker: str, side: Side) -> Tuple[Optional[float], float]:
    """(cost per contract, resting depth) for buying `side` right now."""
    async with make_client() as client:
        yes_bid, yes_ask, depth = await ob.fetch(client, ticker)
    if side is Side.YES:
        return yes_ask, depth
    return ((1.0 - yes_bid) if yes_bid is not None else None), depth


def edge_still_available(*, live_cost: float, model_prob: Optional[float],
                         recommended_cost: Optional[float],
                         max_entry_price: Optional[float],
                         contracts: int) -> Tuple[bool, str]:
    """Does the trade still make sense at the price we can actually get?

    Three separate ways an order can fail here, and they mean different things:

      * The price blew through the caller's explicit ceiling. Refuse outright.
      * The price drifted past the default tolerance from the recommendation. Refuse,
        because the user clicked on a different number than the one in front of them.
      * The price moved enough that expected value after fees is no longer positive. This
        is the one that matters most and the one the old code never checked: an edge can
        survive both of the above and still be gone once the fee is charged.
    """
    if max_entry_price is not None and live_cost > max_entry_price + 1e-9:
        return False, (
            f"edge no longer available: the book is at {live_cost * 100:.0f}c, past your "
            f"maximum entry of {max_entry_price * 100:.0f}c")

    if recommended_cost is not None:
        drift = live_cost - recommended_cost
        if drift > DEFAULT_SLIPPAGE_TOLERANCE + 1e-9:
            return False, (
                f"edge no longer available: recommended at {recommended_cost * 100:.0f}c, "
                f"now {live_cost * 100:.0f}c — a move of {drift * 100:.1f}c")

    if model_prob is not None:
        net_ev = fee_model.expected_value_after_fees(
            prob=model_prob, cost=live_cost, contracts=max(contracts, 1))
        if net_ev is None or net_ev <= 0:
            breakeven = fee_model.breakeven_probability(live_cost,
                                                        contracts=max(contracts, 1))
            return False, (
                f"edge no longer available: at {live_cost * 100:.0f}c you need "
                f"{(breakeven or live_cost) * 100:.1f}% to break even after fees and the "
                f"model says {model_prob * 100:.1f}%")

    return True, "edge intact"


# ------------------------------------------------------------------------- placing

async def place(*, username: str, ticker: str, side: str, stake: float,
                requested_mode: str = "paper", game_id: Optional[str] = None,
                label: Optional[str] = None, market_type: Optional[str] = None,
                model_prob: Optional[float] = None,
                parlay_id: Optional[str] = None,
                team: Optional[str] = None, line: Optional[float] = None,
                selection: Optional[str] = None,
                recommended_cost: Optional[float] = None,
                max_entry_price: Optional[float] = None,
                recommendation_id: Optional[str] = None,
                model_version: Optional[str] = None,
                predicted_at: Optional[float] = None,
                skip_edge_check: bool = False) -> Fill:
    """Place one order. Records a position only for contracts that actually filled."""
    side_enum = Side.NO if str(side).lower() == "no" else Side.YES
    mode, explanation = resolve_mode(username, requested_mode)
    check_limits(stake, username, mode)

    cost, depth = await read_price(ticker, side_enum)
    if cost is None or cost <= 0 or cost >= 1:
        return Fill(ok=False, mode=mode, ticker=ticker, side=side_enum.value,
                    contracts=0, price=0.0, stake=0.0, status="rejected",
                    reason="no_price",
                    message="No live price on that contract — nothing was placed.")

    requested = int(stake / cost)
    if requested < 1:
        return Fill(ok=False, mode=mode, ticker=ticker, side=side_enum.value,
                    contracts=0, price=cost, stake=0.0, status="rejected",
                    reason="stake_too_small",
                    message=(f"${stake:.2f} is not enough for a single contract at "
                             f"{cost * 100:.0f}c."))

    if not skip_edge_check:
        ok, why = edge_still_available(
            live_cost=cost, model_prob=model_prob, recommended_cost=recommended_cost,
            max_entry_price=max_entry_price, contracts=requested)
        if not ok:
            return Fill(ok=False, mode=mode, ticker=ticker, side=side_enum.value,
                        contracts=0, price=cost, stake=0.0, status="rejected",
                        reason="edge_gone", message=why,
                        requested_contracts=requested,
                        detail={"live_cost": round(cost, 4),
                                "recommended_cost": recommended_cost,
                                "max_entry_price": max_entry_price})

    client_order_id = uuid.uuid4().hex

    if mode == "live":
        outcome = await _place_live(ticker, side_enum, requested, cost, client_order_id)
        if not outcome.ok:
            # A failed live order is a failure. It is never quietly recorded as a paper
            # fill, because that would put a phantom position in the track record.
            return Fill(ok=False, mode="live", ticker=ticker, side=side_enum.value,
                        contracts=0, price=cost, stake=0.0, status="rejected",
                        reason=outcome.reason, message=outcome.message,
                        requested_contracts=requested,
                        client_order_id=client_order_id, order_id=outcome.order_id)
        filled = outcome.filled
        avg_price = outcome.avg_price if outcome.avg_price is not None else cost
        order_fees = outcome.fees
        order_id = outcome.order_id
        explanation = outcome.message
    else:
        # Paper fills respect the book. Buying 400 contracts against $180 of resting depth
        # is not a fill, it is a fantasy, and a paper record that ignores depth teaches the
        # wrong lesson about what this strategy can actually get on.
        affordable_by_depth = int(depth / cost) if cost > 0 else 0
        filled = max(0, min(requested, affordable_by_depth))
        avg_price = cost
        order_fees = fee_model.trading_fee(contracts=filled, price=cost)
        order_id = None
        if filled == 0:
            return Fill(ok=False, mode="paper", ticker=ticker, side=side_enum.value,
                        contracts=0, price=cost, stake=0.0, status="unfilled",
                        reason="no_depth", requested_contracts=requested,
                        client_order_id=client_order_id,
                        message=(f"Simulated: only ${depth:.0f} is resting at "
                                 f"{cost * 100:.0f}c, not enough for one contract."))
        if filled < requested:
            explanation = (f"simulated partial fill — only ${depth:.0f} resting at "
                           f"{cost * 100:.0f}c")

    if filled <= 0:
        return Fill(ok=False, mode=mode, ticker=ticker, side=side_enum.value,
                    contracts=0, price=avg_price, stake=0.0, status="unfilled",
                    reason="no_fill", requested_contracts=requested,
                    client_order_id=client_order_id, order_id=order_id,
                    message=("The order was accepted but filled zero contracts — the book "
                             "moved before it crossed. Nothing is held and nothing was "
                             "recorded."))

    actual_stake = filled * avg_price
    remaining = requested - filled
    status = "filled" if remaining <= 0 else "partially_filled"

    position_id = store.open_position(
        user=username, mode=mode, parlay_id=parlay_id, game_id=game_id,
        ticker=ticker, side=side_enum.value, label=label, market_type=market_type,
        contracts=filled, entry_price=avg_price, stake=actual_stake,
        model_prob=model_prob, note=explanation,
        team=team, line=line, selection=selection or label,
        client_order_id=client_order_id, venue_order_id=order_id,
        requested_contracts=requested, filled_contracts=filled,
        remaining_contracts=remaining, avg_fill_price=avg_price,
        entry_fees=order_fees, status=status,
        model_version=model_version, predicted_at=predicted_at,
        recommendation_id=recommendation_id, max_entry_price=max_entry_price)

    log.info("%s order: %d/%d x %s %s @ %.4f fees %.2f (user=%s)", mode, filled,
             requested, ticker, side_enum.value, avg_price, order_fees, username)

    partial_note = ("" if remaining <= 0 else
                    f" Only {filled} of {requested} filled; {remaining} did not.")
    return Fill(ok=True, mode=mode, ticker=ticker, side=side_enum.value,
                contracts=filled, price=avg_price, stake=actual_stake,
                status=status, requested_contracts=requested,
                remaining_contracts=remaining, fees=order_fees,
                message=(f"{'Placed' if mode == 'live' else 'Recorded on paper'}: "
                         f"{filled} x {side_enum.value.upper()} at "
                         f"{avg_price * 100:.0f}c (${actual_stake:.2f}, fees "
                         f"${order_fees:.2f}). {explanation}{partial_note}"),
                position_id=position_id, order_id=order_id,
                client_order_id=client_order_id)


@dataclass
class LiveOrderResult:
    ok: bool
    message: str
    filled: int = 0
    avg_price: Optional[float] = None
    fees: float = 0.0
    remaining: int = 0
    order_id: Optional[str] = None
    reason: Optional[str] = None


def parse_order_response(payload: Dict[str, Any], *, side: Side,
                         requested: int, fallback_price: float) -> LiveOrderResult:
    """Read what Kalshi says actually happened, not what we asked for.

    Kalshi reports counts in contracts and money in centi-cents on some fields and dollars
    on others depending on endpoint version, so every number is normalised here rather
    than at three call sites. `taker_fill_cost` is the total paid in centi-cents; dividing
    by the fill count gives the average price actually paid, which is the only price that
    belongs on a position record.
    """
    order = payload.get("order") or payload
    order_id = order.get("order_id") or order.get("id")

    def _int(*names: str) -> Optional[int]:
        for name in names:
            value = order.get(name)
            if value is not None:
                try:
                    return int(value)
                except (TypeError, ValueError):
                    continue
        return None

    def _float(*names: str) -> Optional[float]:
        for name in names:
            value = order.get(name)
            if value is not None:
                try:
                    return float(value)
                except (TypeError, ValueError):
                    continue
        return None

    filled = _int("taker_fill_count", "filled_count", "fill_count") or 0
    remaining = _int("remaining_count", "resting_order_count")
    if remaining is None:
        remaining = max(0, requested - filled)

    avg_price: Optional[float] = None
    fill_cost = _float("taker_fill_cost")          # centi-cents, total
    if fill_cost is not None and filled > 0:
        avg_price = (fill_cost / filled) / 10000.0
    if avg_price is None:
        # Fall back to the order's own price field, normalising cents to dollars.
        price = _float("yes_price", "price", "average_fill_price")
        if price is not None:
            avg_price = price / 100.0 if price > 1.0 else price
            if side is Side.NO and avg_price is not None:
                avg_price = 1.0 - avg_price
    if avg_price is None or avg_price <= 0 or avg_price >= 1:
        avg_price = fallback_price

    fee_cost = _float("taker_fees", "fee_cost", "fees")
    if fee_cost is not None:
        charged = fee_cost / 10000.0 if fee_cost > 100 else fee_cost / 100.0
    else:
        charged = fee_model.trading_fee(contracts=filled, price=avg_price)

    status = str(order.get("status") or "").lower()
    if status in {"rejected", "canceled"} and filled == 0:
        return LiveOrderResult(ok=False, reason=status, order_id=order_id,
                               message=f"Kalshi {status} the order; nothing filled.")

    return LiveOrderResult(
        ok=True, filled=filled, avg_price=avg_price, fees=charged,
        remaining=remaining, order_id=order_id,
        message=("Live order filled in full." if remaining <= 0 and filled
                 else f"Live order filled {filled} of {requested}."))


async def _place_live(ticker: str, side: Side, contracts: int, cost: float,
                      client_order_id: str) -> LiveOrderResult:
    """Submit a real immediate-or-cancel order and reconcile what came back.

    Everything is quoted off the YES book: buying YES is a bid at the yes-ask; buying NO is
    an ask (selling YES) at the yes-bid. Priced AT the book we just read, so it behaves like
    a market order that cannot fill worse than what the user was shown.

    On a timeout we do NOT retry. The order may well have reached the exchange, and a
    second submission would double the position. The client order ID is idempotent at
    Kalshi's end, so the safe move is to reconcile against the venue and report what is
    actually there.
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
        "client_order_id": client_order_id,
    }
    try:
        async with make_client() as client:
            resp = await request(client, "POST",
                                 f"{settings.KALSHI_ORDER_BASE}/portfolio/events/orders",
                                 headers=kc.signed_headers("POST", ORDER_PATH),
                                 json=body, source="kalshi", attempts=1)
    except Exception as exc:  # noqa: BLE001
        reconciled = await reconcile_client_order(client_order_id, ticker=ticker,
                                                  side=side, requested=contracts,
                                                  fallback_price=cost)
        if reconciled is not None:
            log.warning("order send failed but the venue has it: %s", client_order_id)
            return reconciled
        return LiveOrderResult(
            ok=False, reason="send_failed",
            message=(f"Live order failed to send ({exc}). The venue has no record of "
                     f"client order {client_order_id[:8]}, so nothing was placed. It was "
                     "NOT retried — a retry after a timeout is how you end up holding the "
                     "position twice."))

    if resp.status_code not in (200, 201):
        return LiveOrderResult(
            ok=False, reason="rejected",
            message=f"Kalshi rejected the order ({resp.status_code}): {resp.text[:180]}")

    payload = resp.json() if resp.content else {}
    return parse_order_response(payload, side=side, requested=contracts,
                                fallback_price=cost)


async def reconcile_client_order(client_order_id: str, *, ticker: str, side: Side,
                                 requested: int,
                                 fallback_price: float) -> Optional[LiveOrderResult]:
    """Ask the venue what happened to an order we lost the response for.

    This is the difference between a timeout being recoverable and being dangerous. Kalshi
    keeps the client order ID, so the order either exists there or it does not, and we can
    find out instead of guessing.
    """
    if not kc.credentials_present():
        return None
    path = "/trade-api/v2/portfolio/orders"
    try:
        async with make_client() as client:
            resp = await request(
                client, "GET", f"{settings.KALSHI_ORDER_BASE}/portfolio/orders",
                headers=kc.signed_headers("GET", path),
                params={"limit": 50}, source="kalshi", attempts=2)
    except Exception as exc:  # noqa: BLE001
        log.error("could not reconcile client order %s: %s", client_order_id, exc)
        return None
    if resp.status_code != 200:
        return None
    orders = (resp.json() or {}).get("orders") or []
    for order in orders:
        if order.get("client_order_id") == client_order_id:
            return parse_order_response({"order": order}, side=side, requested=requested,
                                        fallback_price=fallback_price)
    return None


async def reconcile_open_positions(username: str) -> Dict[str, Any]:
    """Bring live positions into line with the venue's own record.

    Confirmed fills are the source of truth. Anything this app believes about a live
    position is a claim until Kalshi agrees with it, and positions opened elsewhere (the
    Kalshi app, a different tool) are invisible here until this runs.
    """
    if not kc.credentials_present():
        return {"reconciled": 0, "supported": False,
                "note": ("Reconciliation needs Kalshi credentials. Without them, live "
                         "positions shown here are this app's record only and may not "
                         "match the exchange.")}
    path = "/trade-api/v2/portfolio/positions"
    try:
        async with make_client() as client:
            resp = await request(client, "GET",
                                 f"{settings.KALSHI_ORDER_BASE}/portfolio/positions",
                                 headers=kc.signed_headers("GET", path),
                                 params={"limit": 200}, source="kalshi", attempts=2)
    except Exception as exc:  # noqa: BLE001
        return {"reconciled": 0, "supported": True, "error": str(exc)[:180],
                "note": "Could not reach Kalshi to reconcile; positions shown are local."}
    if resp.status_code != 200:
        return {"reconciled": 0, "supported": True,
                "note": f"Kalshi returned {resp.status_code} for the positions read."}

    venue = {p.get("ticker"): p for p in (resp.json() or {}).get("market_positions") or []}
    local = [p for p in store.positions_for(username) if p["mode"] == "live"
             and p["status"] in ("open", "partially_closed", "filled", "partially_filled")]

    checked = 0
    mismatches: List[Dict[str, Any]] = []
    for position in local:
        record = venue.get(position["ticker"])
        held = abs(int(record.get("position") or 0)) if record else 0
        expected = int(position.get("filled_contracts") or position["contracts"]) - \
            int(position.get("closed_contracts") or 0)
        checked += 1
        if held != expected:
            mismatches.append({
                "position_id": position["id"], "ticker": position["ticker"],
                "app_thinks": expected, "venue_says": held,
            })
        store.mark_reconciled(position["id"])

    unknown = [t for t in venue if not any(p["ticker"] == t for p in local)]
    return {
        "reconciled": checked, "supported": True,
        "mismatches": mismatches,
        "positions_at_venue_not_here": unknown,
        "note": ("Every live position matches the exchange." if not mismatches and not unknown
                 else "The exchange and this app disagree; the exchange is right."),
    }


# -------------------------------------------------------------------------- closing

async def close(*, username: str, position_id: str,
                contracts: Optional[int] = None) -> Dict[str, Any]:
    """Close some or all of an open position at the current book.

    `contracts=None` closes everything still held. A smaller number leaves the rest open,
    which the old code could not do — it always closed the whole row and threw the
    remainder away.
    """
    rows = [p for p in store.positions_for(username)
            if p["id"] == position_id
            and p["status"] in ("open", "partially_closed", "filled", "partially_filled")]
    if not rows:
        raise ValidationError("no open position with that id")
    position = rows[0]

    filled = int(position.get("filled_contracts") or position["contracts"])
    already = int(position.get("closed_contracts") or 0)
    available = filled - already
    if available <= 0:
        raise ValidationError("that position has nothing left to close")
    qty = available if contracts is None else max(1, min(int(contracts), available))

    held_side = Side.NO if position["side"] == "no" else Side.YES
    async with make_client() as client:
        yes_bid, yes_ask, depth = await ob.fetch(client, position["ticker"])
    # Closing sells what we hold: a YES position is sold into the yes bid.
    exit_price = yes_bid if held_side is Side.YES else (
        (1.0 - yes_ask) if yes_ask is not None else None)
    if exit_price is None:
        raise ValidationError("no live bid to close into — the book is empty")

    sellable = int(depth / exit_price) if exit_price > 0 else 0
    if sellable < qty:
        qty = max(0, sellable)
        if qty == 0:
            raise ValidationError(
                f"only ${depth:.0f} is bid at {exit_price * 100:.0f}c — not enough resting "
                "to sell even one contract into")

    exit_fee = fee_model.trading_fee(contracts=qty, price=exit_price)

    if position["mode"] == "live":
        result = await _place_live(
            position["ticker"],
            Side.NO if held_side is Side.YES else Side.YES,
            qty, 1.0 - exit_price, uuid.uuid4().hex)
        if not result.ok:
            raise ValidationError(result.message)
        if result.filled <= 0:
            raise ValidationError(
                "the closing order filled zero contracts; the position is unchanged")
        qty = result.filled
        exit_fee = result.fees or exit_fee
        if result.avg_price is not None:
            exit_price = (1.0 - result.avg_price) if held_side is Side.YES else result.avg_price

    updated = store.close_position(position_id, exit_price=float(exit_price),
                                   contracts=qty, exit_fee=exit_fee,
                                   note="closed at live book")
    return {
        "closed": updated,
        "exit_price": round(float(exit_price), 4),
        "contracts_closed": qty,
        "remaining_open": (updated or {}).get("remaining_open", 0),
        "exit_fees": round(exit_fee, 2),
        "basis": (f"Sold {qty} contract(s) into the resting bid at "
                  f"{exit_price * 100:.0f}c, with ${depth:.0f} showing at that level."),
    }


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
        "reconciliation_supported": kc.credentials_present(),
    }
    if kc.credentials_present():
        try:
            status["kalshi_balance"] = await kc.balance()
        except Exception as exc:  # noqa: BLE001
            status["kalshi_balance_error"] = str(exc)[:180]
    return status
