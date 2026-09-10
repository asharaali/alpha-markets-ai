"""Kalshi trading fees.

Every expected-value number in this system used to be gross. On a venue whose fee peaks at
1.75c per contract, that is not a rounding error: it is larger than most of the edges the
model claims to find. A 1c edge at 50c is worth 2% per dollar gross and negative net, so
omitting the fee does not merely shade the answer, it inverts it.

The formula is Kalshi's published one:

    taker fee = ceil( M x 0.07   x C x P x (1 - P) )
    maker fee = ceil( M x 0.0175 x C x P x (1 - P) )

where C is contracts, P is price in dollars, M is a per-series multiplier (1 for the NFL
series this app trades), and the rounding is UP to the next cent. The P(1-P) shape means
the fee is worst at 50c and vanishes at the extremes, which is exactly where this model
tends to find its edges.

Everything here takes and returns dollars. Orders placed by this app are
immediate-or-cancel, so they cross the spread and pay the TAKER rate; the maker rate is
provided for completeness and for modelling a resting-order mode later.

Source: Kalshi fee schedule, July 2026 revision.
"""
from __future__ import annotations

import math
from typing import Optional

# Published rates. Named rather than inlined so a schedule change is a one-line edit with
# an obvious blast radius.
TAKER_RATE = 0.07
MAKER_RATE = 0.0175
DEFAULT_MULTIPLIER = 1.0


def _round_up_cent(amount: float) -> float:
    """Kalshi rounds a fee UP to the next cent. Never round this to nearest."""
    if amount <= 0:
        return 0.0
    return math.ceil(amount * 100.0 - 1e-9) / 100.0


def trading_fee(*, contracts: int, price: float, maker: bool = False,
                multiplier: float = DEFAULT_MULTIPLIER) -> float:
    """Fee in dollars for trading `contracts` at `price` (0-1)."""
    if contracts <= 0 or price <= 0.0 or price >= 1.0:
        return 0.0
    rate = MAKER_RATE if maker else TAKER_RATE
    return _round_up_cent(multiplier * rate * contracts * price * (1.0 - price))


def fee_per_contract(price: float, *, maker: bool = False,
                     multiplier: float = DEFAULT_MULTIPLIER) -> float:
    """Unrounded per-contract fee. Use for sizing maths where rounding noise misleads."""
    if price <= 0.0 or price >= 1.0:
        return 0.0
    rate = MAKER_RATE if maker else TAKER_RATE
    return multiplier * rate * price * (1.0 - price)


def round_trip_fee(*, contracts: int, entry_price: float,
                   exit_price: Optional[float] = None,
                   maker: bool = False,
                   multiplier: float = DEFAULT_MULTIPLIER) -> float:
    """Entry fee plus the exit fee, when the position is expected to be traded out of.

    A contract HELD TO SETTLEMENT pays no exit fee — Kalshi settles at $1 or $0 with no
    further charge — so `exit_price` is left None for the hold-to-expiry case. Passing an
    exit price models cashing out early, which costs a second fee.
    """
    total = trading_fee(contracts=contracts, price=entry_price, maker=maker,
                        multiplier=multiplier)
    if exit_price is not None:
        total += trading_fee(contracts=contracts, price=exit_price, maker=maker,
                             multiplier=multiplier)
    return total


def expected_value_after_fees(*, prob: float, cost: float, contracts: int,
                              maker: bool = False,
                              multiplier: float = DEFAULT_MULTIPLIER) -> Optional[float]:
    """Expected profit per dollar staked, net of the entry fee.

    Held to settlement: a winning contract returns $1, a loser $0, and the fee is paid
    either way at entry. Gross EV per dollar is prob/cost - 1; the fee is subtracted as a
    share of the capital actually committed.
    """
    if cost <= 0.0 or cost >= 1.0 or contracts <= 0:
        return None
    stake = contracts * cost
    fee = trading_fee(contracts=contracts, price=cost, maker=maker,
                      multiplier=multiplier)
    expected_return = prob * contracts          # $1 per winning contract
    return (expected_return - stake - fee) / stake


def breakeven_probability(cost: float, *, contracts: int = 100, maker: bool = False,
                          multiplier: float = DEFAULT_MULTIPLIER) -> Optional[float]:
    """The win probability at which this contract merely breaks even after fees.

    This is the number that belongs beside every recommendation: it turns "the model says
    58%" into "and you need 56.2% just to not lose money", which is the comparison that
    actually decides whether to place the bet.
    """
    if cost <= 0.0 or cost >= 1.0 or contracts <= 0:
        return None
    fee = trading_fee(contracts=contracts, price=cost, maker=maker,
                      multiplier=multiplier)
    return (contracts * cost + fee) / contracts


def fee_adjusted_cost(cost: float, *, contracts: int = 100, maker: bool = False,
                      multiplier: float = DEFAULT_MULTIPLIER) -> float:
    """The effective per-contract price once the entry fee is folded in."""
    breakeven = breakeven_probability(cost, contracts=contracts, maker=maker,
                                      multiplier=multiplier)
    return breakeven if breakeven is not None else cost
