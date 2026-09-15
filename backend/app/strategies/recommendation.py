"""A recommendation, stated completely enough to be argued with.

The old opportunity card showed a probability, an edge and a stake. That is enough to place
a bet and not enough to evaluate one. Missing from it: what you actually pay, how old that
price is, what the fee does to the edge, how much size the book will take, what the model
was not told, how far wrong the model can be before the edge disappears, and the price above
which you should not buy.

Every field below exists because its absence let a bad bet look like a good one.

Two distinctions the card is built around:

  LIKELY TO WIN is not GOOD VALUE. An 85% favourite at 90c is very likely to win and is a
  bad bet. A 35% underdog at 25c is probably going to lose and is a good bet. Ranking by
  win probability picks the first every time, which is how a bankroll dies comfortably.

  AN EDGE IS AN ESTIMATE. The model's probability has error bars, and an edge smaller than
  those error bars is not an opportunity, it is noise with a decimal point. `robust` says
  whether the edge survives a plausible model error, and a recommendation that is +EV only
  at the point estimate is labelled fragile rather than promoted.

"No qualifying bets" is a normal, useful answer. Nothing here lowers a threshold to fill a
screen.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.config import settings
from app.core.types import Confidence, MarketType, Side, Signal
from app.risk import fees
from app.strategies import pricing

# How far the model's probability is assumed to be able to be wrong when testing whether an
# edge is real. Not a guess: it is roughly the calibration error the walk-forward evaluation
# measures on held-out seasons, so "robust" means "survives the error we have actually
# observed in this model", not "survives an error we hope is small".
MODEL_UNCERTAINTY = 0.03

# A quote older than this is stale enough that the user should be told, because the book
# can move several cents in a minute on a thin NFL market.
STALE_QUOTE_SECONDS = 90.0


@dataclass
class Recommendation:
    """One actionable bet, fully specified."""

    recommendation_id: str
    game_id: str
    kickoff: Optional[str]
    matchup: str
    ticker: Optional[str]
    market_type: str
    selection: str
    side: str
    settlement: str

    raw_model_prob: float
    final_prob: float
    market_prob: Optional[float]

    cost: float
    quote_age_seconds: Optional[float]
    depth_usd: float

    ev_gross: Optional[float]
    ev_after_fees: Optional[float]
    breakeven_prob: Optional[float]
    estimated_fee: float

    max_entry_price: float
    recommended_stake: float
    contracts: int
    sizing_basis: str
    limits_applied: List[str]

    supporting: List[str]
    missing: List[str]
    warnings: List[str]

    confidence: str
    robust: bool
    value_rating: str
    win_likelihood: str
    model_version: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    # Identifies the BET rather than the contract: every rung of "Browns by more than N" is
    # the same opinion about the Browns, so the board shows one of them, not four.
    bet_key: str = ""
    breakdown: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "recommendation_id": self.recommendation_id,
            "game_id": self.game_id,
            "kickoff": self.kickoff,
            "matchup": self.matchup,
            "ticker": self.ticker,
            "market_type": self.market_type,
            "selection": self.selection,
            "side": self.side,
            "settles": self.settlement,
            "probability": {
                "raw_model": round(self.raw_model_prob, 4),
                "final": round(self.final_prob, 4),
                "market": round(self.market_prob, 4) if self.market_prob is not None else None,
                "edge": (round(self.final_prob - self.market_prob, 4)
                         if self.market_prob is not None else None),
                "uncertainty": MODEL_UNCERTAINTY,
                "note": ("Raw is the strategies' own estimate before deferring to the "
                         "price. Final is that shaded toward the market, and is the number "
                         "every other figure on this card uses."),
            },
            "price": {
                "cost": round(self.cost, 4),
                "cost_cents": round(self.cost * 100, 1),
                "max_entry_price": round(self.max_entry_price, 4),
                "quote_age_seconds": (round(self.quote_age_seconds, 1)
                                      if self.quote_age_seconds is not None else None),
                "stale": (self.quote_age_seconds is not None
                          and self.quote_age_seconds > STALE_QUOTE_SECONDS),
                "depth_usd": round(self.depth_usd, 2),
                "depth_covers_stake": self.depth_usd >= self.recommended_stake,
            },
            "economics": {
                "ev_gross": round(self.ev_gross, 4) if self.ev_gross is not None else None,
                "ev_after_fees": (round(self.ev_after_fees, 4)
                                  if self.ev_after_fees is not None else None),
                "breakeven_probability": (round(self.breakeven_prob, 4)
                                          if self.breakeven_prob is not None else None),
                "estimated_fee": round(self.estimated_fee, 2),
                "note": ("Expected value is after Kalshi's trading fee. Break-even is the "
                         "win rate this price needs just to stop losing money."),
            },
            "sizing": {
                "recommended_stake": round(self.recommended_stake, 2),
                "contracts": self.contracts,
                "basis": self.sizing_basis,
                "limits_applied": self.limits_applied,
            },
            "assessment": {
                "confidence": self.confidence,
                "value_rating": self.value_rating,
                "win_likelihood": self.win_likelihood,
                "robust": self.robust,
                "robustness_note": (
                    f"The edge survives the model being {MODEL_UNCERTAINTY:.0%} wrong in "
                    "the unfavourable direction." if self.robust else
                    f"The edge disappears if the model is {MODEL_UNCERTAINTY:.0%} wrong. "
                    "This is inside the error this model actually shows on held-out "
                    "seasons, so treat the edge as unproven."),
                "separation_note": (
                    "Value rating and win likelihood are different things. A heavy "
                    "favourite is likely to win and is usually poor value; a longshot is "
                    "unlikely to win and can be excellent value."),
            },
            "reasoning": {
                "supporting": self.supporting,
                "missing": self.missing,
                "warnings": self.warnings,
            },
            "model_version": self.model_version,
            "created_at": self.created_at,
            "breakdown": self.breakdown,
        }


def settlement_text(signal: Signal) -> str:
    """The exact condition under which this contract pays, in words.

    Written out because "KC -3.5" means nothing to someone checking whether they were paid,
    and because the resolution rule is where pushes and ties hide.
    """
    team = signal.team or ""
    line = signal.line
    market = signal.market_type
    if signal.quote is not None and signal.quote.side is Side.NO and line is not None:
        if market is MarketType.SPREAD:
            return (f"Pays $1 unless {team} wins by MORE than {line:g} points. {team} "
                    f"losing, or winning by {int(line)} or fewer, both pay.")
        if market is MarketType.TOTAL:
            return f"Pays $1 if the two teams combine for {int(line)} points or fewer."
        if market is MarketType.TEAM_TOTAL:
            return f"Pays $1 if {team} alone scores {int(line)} points or fewer."

    if market is MarketType.MONEYLINE:
        return (f"Pays $1 if {team} wins. A tie voids the contract and the stake is "
                "returned.")
    if market is MarketType.SPREAD and line is not None:
        return (f"Pays $1 if {team} wins by MORE than {line:g} points. Exactly {line:g} "
                f"pays nothing — this is a 'more than' contract, not a spread with a push.")
    if market is MarketType.TOTAL and line is not None:
        return (f"Pays $1 if the two teams combine for MORE than {line:g} points. Exactly "
                f"{line:g} pays nothing.")
    if market is MarketType.TEAM_TOTAL and line is not None:
        return f"Pays $1 if {team} alone scores MORE than {line:g} points."
    if market is MarketType.WIN_MARGIN:
        return f"Pays $1 if the winning margin falls in the band '{signal.selection}'."
    return f"Pays $1 if '{signal.selection}' resolves YES at settlement."


def value_rating(ev_after_fees: Optional[float]) -> str:
    """How good the PRICE is. Independent of how likely the bet is to win."""
    if ev_after_fees is None:
        return "unpriced"
    if ev_after_fees >= 0.15:
        return "strong"
    if ev_after_fees >= 0.07:
        return "moderate"
    if ev_after_fees > 0:
        return "thin"
    return "negative"


def win_likelihood(prob: float) -> str:
    """How likely the bet is to WIN. Independent of whether it is good value."""
    if prob >= 0.75:
        return "likely"
    if prob >= 0.50:
        return "even-ish"
    if prob >= 0.30:
        return "unlikely"
    return "longshot"


def is_robust(*, final_prob: float, cost: float, contracts: int,
              uncertainty: float = MODEL_UNCERTAINTY) -> bool:
    """Does the edge survive the model being wrong by a realistic amount?

    Tested in the unfavourable direction only. An edge that exists at 58% and vanishes at
    55% is not an edge this model has earned the right to claim, because 3 points is inside
    its measured calibration error.
    """
    pessimistic = max(0.0, final_prob - uncertainty)
    net = fees.expected_value_after_fees(prob=pessimistic, cost=cost,
                                         contracts=max(contracts, 1))
    return net is not None and net > 0


def max_entry_price(final_prob: float, *, contracts: int = 100,
                    margin: float = 0.25) -> float:
    """The highest price at which this bet is still worth placing.

    Derived rather than picked: it is the price at which expected value after fees falls to
    `margin` of the edge currently on offer. Buying above it turns a recommendation into a
    different, worse bet, which is exactly what happened when execution refreshed the price
    and traded anyway.
    """
    lo, hi = 0.01, min(0.99, final_prob)
    target = margin
    best = lo
    for _ in range(40):
        mid = (lo + hi) / 2
        net = fees.expected_value_after_fees(prob=final_prob, cost=mid,
                                             contracts=max(contracts, 1))
        if net is not None and net >= target * _edge_scale(final_prob, mid):
            best = mid
            lo = mid
        else:
            hi = mid
    return round(best, 4)


def _edge_scale(prob: float, cost: float) -> float:
    """Normaliser so `margin` means the same thing at 20c and at 80c."""
    gross = (prob / cost) - 1.0 if cost > 0 else 0.0
    return max(gross, 1e-6)


def build(signal: Signal, *, matchup: str, kickoff: Optional[str],
          sizing: Optional[Dict[str, Any]] = None,
          quote_age: Optional[float] = None,
          missing: Optional[List[str]] = None,
          model_version: Optional[str] = None) -> Optional[Recommendation]:
    """Turn a priced signal into a full recommendation, or None if it is not actionable."""
    if not signal.actionable:
        return None
    quote = signal.quote
    cost = signal.features.get("cost")
    if quote is None or not cost:
        return None
    cost = float(cost)

    final_prob = pricing.published_prob(signal)
    depth = float(signal.features.get("depth_usd") or quote.depth_usd or 0.0)

    sizing = sizing or {}
    stake = float(sizing.get("stake") or 0.0)
    contracts = int(stake / cost) if cost > 0 else 0
    # Fee maths needs a contract count; use the recommended size, or a $100 reference when
    # no bankroll is configured, so the economics are still shown rather than blanked.
    fee_contracts = contracts if contracts > 0 else max(int(100.0 / cost), 1)

    fee = fees.trading_fee(contracts=fee_contracts, price=cost)
    ev_gross = pricing.expected_value(final_prob, cost)
    ev_net = fees.expected_value_after_fees(prob=final_prob, cost=cost,
                                            contracts=fee_contracts)
    breakeven = fees.breakeven_probability(cost, contracts=fee_contracts)

    warnings: List[str] = []
    if depth < stake and stake > 0:
        warnings.append(
            f"Only ${depth:.0f} is resting at this price, less than the ${stake:.0f} "
            "recommended stake. The rest would fill worse or not at all.")
    if quote_age is not None and quote_age > STALE_QUOTE_SECONDS:
        warnings.append(
            f"This price is {quote_age:.0f} seconds old. It is re-checked before any order "
            "is sent, and the order is refused if the edge has gone.")
    if ev_net is not None and ev_gross is not None and ev_gross > 0 >= ev_net:
        warnings.append(
            "This edge exists before fees and disappears after them. It is not a bet.")

    robust = is_robust(final_prob=final_prob, cost=cost, contracts=fee_contracts)
    if not robust:
        warnings.append(
            f"The edge does not survive a {MODEL_UNCERTAINTY:.0%} model error, which is "
            "inside this model's measured calibration error on held-out seasons.")

    return Recommendation(
        recommendation_id=uuid.uuid4().hex[:16],
        game_id=signal.game_id,
        kickoff=kickoff,
        matchup=matchup,
        ticker=quote.ticker,
        market_type=signal.market_type.value,
        selection=signal.selection,
        side=quote.side.value if quote.side else "yes",
        settlement=settlement_text(signal),
        raw_model_prob=float(signal.features.get("raw_model_prob") or signal.model_prob),
        final_prob=final_prob,
        market_prob=signal.market_prob,
        cost=cost,
        quote_age_seconds=quote_age,
        depth_usd=depth,
        ev_gross=ev_gross,
        ev_after_fees=ev_net,
        breakeven_prob=breakeven,
        estimated_fee=fee,
        max_entry_price=max_entry_price(final_prob, contracts=fee_contracts),
        recommended_stake=stake,
        contracts=contracts,
        sizing_basis=str(sizing.get("basis") or "no bankroll configured"),
        limits_applied=list(sizing.get("limits_applied")
                            or ([sizing["capped_by"]] if sizing.get("capped_by") else [])),
        supporting=list(signal.reasoning[:4]),
        missing=list(missing or []),
        warnings=warnings + list(sizing.get("warnings") or []),
        confidence=signal.confidence.value,
        robust=robust,
        value_rating=value_rating(ev_net),
        win_likelihood=win_likelihood(final_prob),
        model_version=model_version,
        bet_key=bet_key(signal),
    )


def rank(recommendations: List[Recommendation]) -> List[Recommendation]:
    """Order by risk-adjusted value, never by win probability or raw edge.

    Sorting by edge promotes whatever the model is most wrong about. Sorting by win
    probability promotes heavy favourites, which are reliably the worst prices on the
    board. The ordering here is expected value after fees, tempered by the square root of
    the win probability so a 4% shot with a flattering EV does not lead the page, with
    non-robust edges pushed below robust ones regardless of their headline number.
    """
    def key(rec: Recommendation):
        ev = rec.ev_after_fees or 0.0
        return (rec.robust, ev * (rec.final_prob ** 0.5))
    return sorted(recommendations, key=key, reverse=True)


def empty_reason(considered: int, filtered: Dict[str, int]) -> str:
    """Why there is nothing to show. A real answer, not an apology."""
    if considered == 0:
        return ("No NFL contracts were priced on this board. That usually means the week's "
                "markets have not opened yet, not that anything is broken.")
    parts = [f"{count} {reason}" for reason, count in filtered.items() if count]
    detail = ("; ".join(parts)) if parts else "none cleared the value gate"
    return (
        f"Nothing on this board qualifies. Of {considered} priced contracts: {detail}. "
        "No qualifying bets is a normal result — most slates have no edge worth taking on "
        "liquid markets, and the thresholds are not lowered to fill this page.")


def bet_key(signal: Signal) -> str:
    """Same game, same market, same team, same direction: the same bet at a different line."""
    side = signal.quote.side.value if signal.quote is not None else "yes"
    return f"{signal.game_id}|{signal.market_type.value}|{signal.team or ''}|{side}"


def profitable(recommendations: List[Recommendation]) -> List[Recommendation]:
    """Only cards that make money after Kalshi's fee. A card that loses after fees is not a
    marginal bet, it is a losing one, and it was being shown under 'qualifying bets'."""
    return [r for r in recommendations
            if r.ev_after_fees is not None and r.ev_after_fees > 0]


def one_per_bet(recommendations: List[Recommendation]) -> List[Recommendation]:
    """Keep the first card for each bet. Call on an already-ranked list."""
    seen = set()
    out: List[Recommendation] = []
    for r in recommendations:
        if r.bet_key in seen:
            continue
        seen.add(r.bet_key)
        out.append(r)
    return out


# The high-win-rate lane. Likely enough to hit most weeks, and still not overpriced: the
# probability we stand behind must at least cover the price plus Kalshi's fee. A 80% bet
# at 82c is a likely winner and a losing bet, and it is refused here like anywhere else.
HIGH_WIN_MIN_PROB = 0.65
HIGH_WIN_MAX_PROB = 0.95        # beyond this the payout is pennies and one loss erases a month
HIGH_WIN_MIN_DEPTH = 50.0


def is_high_win_rate(r: Recommendation) -> bool:
    if r.ev_after_fees is None or r.ev_after_fees < 0:
        return False
    if not (HIGH_WIN_MIN_PROB <= r.final_prob <= HIGH_WIN_MAX_PROB):
        return False
    # The market has to broadly agree this is likely. A "likely" bet the price calls a
    # coin flip is the model's opinion, not a safe bet.
    if r.market_prob is None or r.market_prob < HIGH_WIN_MIN_PROB - 0.05:
        return False
    return r.depth_usd >= HIGH_WIN_MIN_DEPTH


def rank_high_win_rate(recommendations: List[Recommendation]) -> List[Recommendation]:
    """Most likely first, value as the tie-break, one bet per game so a single upset cannot
    take out the whole list."""
    ordered = sorted((r for r in recommendations if is_high_win_rate(r)),
                     key=lambda r: (round(r.final_prob, 2), r.ev_after_fees or 0.0),
                     reverse=True)
    seen_games = set()
    out: List[Recommendation] = []
    for r in ordered:
        if r.game_id in seen_games:
            continue
        seen_games.add(r.game_id)
        out.append(r)
    return out
