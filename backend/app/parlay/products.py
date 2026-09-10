"""What a multi-leg recommendation actually IS, priced as the product that executes.

The defect this module exists to remove: the builder multiplied the individual leg costs
and published the reciprocal as a payout multiple, while the place-parlay endpoint split
the stake across separate single contracts. Those are not the same bet and they do not pay
the same money. Two legs at 50c and 40c:

  * as an all-or-nothing parlay, $100 returns $500 if both land, nothing otherwise;
  * as a basket of singles, $50 into each returns $225 if both land, and $100 or $125 if
    exactly one lands.

Showing the first number and executing the second overstated the payout by more than
double and hid the fact that a "parlay" which half-lands still returns money.

Three products, each labelled and each priced by its own arithmetic:

  BASKET_OF_SINGLES   Separate contracts, additive payouts, partial wins are real.
                      Always executable, because it is just N ordinary orders.
  KALSHI_COMBO        A genuine combination market from a multivariate event collection,
                      with its own ticker and its own order book. Executable, and the
                      price is a real quote rather than a calculation.
  HYPOTHETICAL_PARLAY The all-or-nothing product priced by multiplication, marked NOT
                      executable. This is analysis, not an offer. It exists so the user
                      can see what a parlay WOULD pay, never so they can click buy.

Nothing here may report `executable: True` without a quote behind it.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence

from app.risk import fees

BASKET = "basket_of_singles"
COMBO = "kalshi_combo"
HYPOTHETICAL = "hypothetical_parlay"


def _contracts_for(stake: float, cost: float) -> int:
    if cost <= 0:
        return 0
    return int(stake / cost)


def price_basket(legs: Sequence[Dict[str, Any]], *, stake: float,
                 weights: Optional[Sequence[float]] = None) -> Dict[str, Any]:
    """N separate contracts bought with one click. Payouts ADD; they do not multiply.

    `legs` carry at least `cost` (0-1) and `prob`. The stake is split by `weights` when
    given, evenly otherwise, and each slice buys whole contracts at that leg's price.

    The headline number is `max_payout`: what lands if EVERY leg wins. It is the honest
    ceiling for this product and it is far below a parlay's, which is the entire point.
    """
    legs = list(legs)
    if not legs or stake <= 0:
        return {"product": BASKET, "executable": True, "legs": [], "stake": 0.0,
                "max_payout": 0.0, "note": "No legs."}

    if weights is None:
        weights = [1.0 / len(legs)] * len(legs)
    total_weight = sum(weights) or 1.0
    weights = [w / total_weight for w in weights]

    priced: List[Dict[str, Any]] = []
    spent = 0.0
    fee_total = 0.0
    payout_if_all_win = 0.0
    for leg, weight in zip(legs, weights):
        cost = float(leg.get("cost") or 0.0)
        slice_stake = stake * weight
        contracts = _contracts_for(slice_stake, cost)
        leg_stake = contracts * cost
        leg_fee = fees.trading_fee(contracts=contracts, price=cost)
        spent += leg_stake
        fee_total += leg_fee
        payout_if_all_win += contracts * 1.0
        priced.append({
            **leg,
            "contracts": contracts,
            "cost": round(cost, 4),
            "stake": round(leg_stake, 2),
            "fee": round(leg_fee, 2),
            # What this leg alone returns if it wins. Visible per leg because in a basket
            # a single winner really does pay out on its own.
            "payout_if_this_leg_wins": round(contracts * 1.0, 2),
        })

    all_win_prob = 1.0
    for leg in legs:
        all_win_prob *= float(leg.get("prob") or 0.0)

    net_if_all_win = payout_if_all_win - spent - fee_total
    return {
        "product": BASKET,
        "executable": True,
        "pricing_basis": "live_order_books",
        "payout_is_additive": True,
        "legs": priced,
        "leg_count": len(priced),
        "stake": round(spent, 2),
        "fees": round(fee_total, 2),
        "max_payout": round(payout_if_all_win, 2),
        "all_legs_win_payout": round(payout_if_all_win, 2),
        "net_if_all_legs_win": round(net_if_all_win, 2),
        "partial_wins_pay": True,
        "note": (
            "Separate contracts, not a parlay. Each leg pays on its own, so a partial "
            "result still returns money — and the ceiling is far below what multiplying "
            "the leg prices would suggest."),
    }


def price_hypothetical_parlay(legs: Sequence[Dict[str, Any]], *, joint_prob: float,
                              stake: float = 100.0,
                              standard_error: Optional[float] = None) -> Dict[str, Any]:
    """All-or-nothing pricing by multiplication. ANALYSIS ONLY — never executable.

    This is what a sportsbook parlay on these legs would look like if one existed at the
    product of the current single-leg prices. No venue has quoted it, so `executable` is
    False and `pricing_basis` says `hypothetical`. The UI must not offer a buy button for
    an object shaped like this.
    """
    legs = list(legs)
    combined_cost = 1.0
    for leg in legs:
        cost = float(leg.get("cost") or 0.0)
        if cost <= 0.0 or cost >= 1.0:
            return {"product": HYPOTHETICAL, "executable": False,
                    "pricing_basis": "hypothetical", "legs": legs,
                    "note": "A leg has no usable price, so no combination can be priced."}
        combined_cost *= cost

    payout_multiple = 1.0 / combined_cost if combined_cost > 0 else 0.0
    contracts = _contracts_for(stake, combined_cost)
    # A real combo market would charge the fee on the combined price, not per leg.
    fee = fees.trading_fee(contracts=contracts, price=combined_cost)
    gross_ev = (joint_prob / combined_cost) - 1.0 if combined_cost > 0 else None
    net_ev = fees.expected_value_after_fees(
        prob=joint_prob, cost=combined_cost, contracts=contracts) if contracts else None

    result = {
        "product": HYPOTHETICAL,
        "executable": False,
        "pricing_basis": "hypothetical",
        "payout_is_additive": False,
        "legs": list(legs),
        "leg_count": len(legs),
        "combined_cost_per_dollar": round(combined_cost, 4),
        "payout_multiple": round(payout_multiple, 3),
        "market_implied_probability": round(combined_cost, 4),
        "model_probability": round(joint_prob, 4),
        "estimated_edge": round(joint_prob - combined_cost, 4),
        "ev_per_dollar": round(gross_ev, 4) if gross_ev is not None else None,
        "ev_per_dollar_after_fees": round(net_ev, 4) if net_ev is not None else None,
        "estimated_fee": round(fee, 2),
        "note": (
            "HYPOTHETICAL. No venue has quoted this combination. The price is the product "
            "of the individual leg prices, which is what a parlay would cost if one were "
            "offered — it is not an available payout and cannot be bought here."),
    }
    if standard_error is not None:
        result["standard_error"] = round(standard_error, 5)
        result["probability_range"] = [
            round(max(0.0, joint_prob - 1.96 * standard_error), 4),
            round(min(1.0, joint_prob + 1.96 * standard_error), 4),
        ]
    return result


def price_combo_quote(legs: Sequence[Dict[str, Any]], *, ticker: str,
                      yes_ask: Optional[float], yes_bid: Optional[float],
                      depth_usd: float, joint_prob: float, stake: float = 100.0,
                      quote_age_seconds: Optional[float] = None,
                      standard_error: Optional[float] = None) -> Dict[str, Any]:
    """A REAL Kalshi combination market, priced off its own order book.

    Reached through a multivariate event collection: the legs resolve to a single combo
    ticker with its own book, and from that point it trades exactly like any other binary
    contract. This is the only multi-leg product that may report `executable: True`, and
    only when the book actually has an ask.
    """
    if yes_ask is None or yes_ask <= 0.0 or yes_ask >= 1.0:
        return {
            "product": COMBO, "executable": False, "ticker": ticker,
            "pricing_basis": "no_quote", "legs": list(legs),
            "note": ("The combination market exists but has no resting ask, so there is "
                     "nothing to buy at any price."),
        }

    contracts = _contracts_for(stake, yes_ask)
    fee = fees.trading_fee(contracts=contracts, price=yes_ask)
    net_ev = fees.expected_value_after_fees(
        prob=joint_prob, cost=yes_ask, contracts=contracts) if contracts else None
    breakeven = fees.breakeven_probability(yes_ask, contracts=max(contracts, 1))

    result = {
        "product": COMBO,
        "executable": True,
        "pricing_basis": "live_combo_order_book",
        "payout_is_additive": False,
        "ticker": ticker,
        "legs": list(legs),
        "leg_count": len(legs),
        "quoted_cost": round(yes_ask, 4),
        "quoted_bid": round(yes_bid, 4) if yes_bid is not None else None,
        "payout_multiple": round(1.0 / yes_ask, 3),
        "market_implied_probability": round(yes_ask, 4),
        "model_probability": round(joint_prob, 4),
        "estimated_edge": round(joint_prob - yes_ask, 4),
        "ev_per_dollar_after_fees": round(net_ev, 4) if net_ev is not None else None,
        "breakeven_probability": round(breakeven, 4) if breakeven is not None else None,
        "estimated_fee": round(fee, 2),
        "contracts_at_stake": contracts,
        "depth_usd": round(depth_usd, 2),
        "quote_age_seconds": quote_age_seconds,
        "note": ("Quoted by Kalshi as a single combination contract. The payout below is "
                 "the venue's price, not a calculation."),
    }
    if standard_error is not None:
        result["standard_error"] = round(standard_error, 5)
        result["probability_range"] = [
            round(max(0.0, joint_prob - 1.96 * standard_error), 4),
            round(min(1.0, joint_prob + 1.96 * standard_error), 4),
        ]
    return result


def compare(basket: Dict[str, Any], parlay: Dict[str, Any]) -> Dict[str, Any]:
    """A side-by-side the user can read without knowing what either word means."""
    return {
        "basket_max_payout": basket.get("max_payout"),
        "parlay_payout_multiple": parlay.get("payout_multiple"),
        "difference_explained": (
            "The parlay figure is bigger because it pays only when every leg lands. The "
            "basket pays something whenever any leg lands. Comparing the two headline "
            "numbers directly is the mistake this panel exists to prevent."),
    }
