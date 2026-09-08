"""The parlay engine — building combinations that are actually worth placing.

The honest starting point is that parlays are usually a bad idea. Every leg multiplies the
payout and multiplies the ways to lose, and a combination of individually break-even legs is
a losing bet. So this builder only ever assembles legs that clear the value gate on their
own, prices the combination with correlation-aware simulation rather than multiplication,
and will return NOTHING for a category rather than pad it with legs it does not believe in.

Four categories, differing in how much per-leg risk they accept:

  CONSERVATIVE  2-3 legs, each individually likely, independent games preferred.
  BALANCED      3-4 legs at moderate per-leg probability.
  AGGRESSIVE    4-5 legs including longer odds — higher variance, explicitly labelled.
  BEST          Whatever combination maximises risk-adjusted expected value, any size.

Every returned parlay carries its own combined probability, the naive multiplied
probability for comparison, the correlation effect between them, the market's implied
probability from real prices, expected value, a risk rating, and a written explanation of
why those legs belong together.
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.config import settings
from app.core.logging import get_logger
from app.core.types import CONFIDENCE_ORDER, Confidence, MarketType, Signal
from app.data import teams
from app.parlay import conflicts
from app.parlay.simulation import SlateSimulator
from app.strategies import pricing

log = get_logger(__name__)


@dataclass
class Category:
    key: str
    name: str
    description: str
    min_legs: int
    max_legs: int
    min_leg_prob: float
    max_leg_prob: float
    min_confidence: Confidence
    prefer_independent: bool


CATEGORIES: Dict[str, Category] = {
    "conservative": Category(
        "conservative", "Conservative",
        "Two or three legs the model rates as individually likely, drawn from separate "
        "games so nothing compounds.",
        min_legs=2, max_legs=3, min_leg_prob=0.60, max_leg_prob=0.93,
        min_confidence=Confidence.MEDIUM, prefer_independent=True),
    "balanced": Category(
        "balanced", "Balanced",
        "Three or four legs at moderate per-leg probability — more payout, more ways to "
        "lose, still every leg positive on its own.",
        min_legs=3, max_legs=4, min_leg_prob=0.45, max_leg_prob=0.85,
        min_confidence=Confidence.LOW, prefer_independent=True),
    "aggressive": Category(
        "aggressive", "Aggressive",
        "Four or five legs including longer prices. High variance by design: expect this "
        "to lose far more often than it wins, and size it accordingly.",
        min_legs=4, max_legs=5, min_leg_prob=0.25, max_leg_prob=0.75,
        min_confidence=Confidence.LOW, prefer_independent=False),
    "best": Category(
        "best", "Model's Best",
        "The combination with the strongest risk-adjusted expected value on the board, "
        "whatever size that turns out to be.",
        min_legs=2, max_legs=4, min_leg_prob=0.30, max_leg_prob=0.92,
        min_confidence=Confidence.MEDIUM, prefer_independent=True),
}

# Never consider more than this many candidate legs; the combination search is exponential.
MAX_CANDIDATES = 16
# A parlay must beat this expected return to be worth showing at all.
MIN_PARLAY_EV = 0.05


@dataclass
class ParlayLeg:
    signal: Signal
    probability: float
    cost: float

    def to_dict(self) -> Dict[str, Any]:
        quote = self.signal.quote
        return {
            "game_id": self.signal.game_id,
            "ticker": quote.ticker if quote else None,
            "market_type": self.signal.market_type.value,
            "label": self.signal.label,
            "selection": self.signal.selection,
            "team": self.signal.team,
            "player": self.signal.player,
            "line": self.signal.line,
            "model_prob": round(self.probability, 4),
            "market_prob": round(self.signal.market_prob, 4) if self.signal.market_prob else None,
            "edge": round(self.signal.edge, 4) if self.signal.edge is not None else None,
            "cost": round(self.cost, 4),
            "confidence": self.signal.confidence.value,
            "depth_usd": quote.depth_usd if quote else 0.0,
            "reasoning": self.signal.reasoning[:3],
        }


@dataclass
class Parlay:
    category: str
    legs: List[ParlayLeg]
    combined_prob: float
    naive_prob: float
    correlation_effect: float
    combined_cost: float
    payout_multiple: float
    market_implied_prob: float
    ev_per_dollar: float
    risk_rating: str
    explanation: List[str]
    per_game: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "category": self.category,
            "leg_count": len(self.legs),
            "legs": [leg.to_dict() for leg in self.legs],
            "model_probability": round(self.combined_prob, 4),
            "naive_multiplied_probability": round(self.naive_prob, 4),
            "correlation_effect": round(self.correlation_effect, 4),
            "market_implied_probability": round(self.market_implied_prob, 4),
            "estimated_edge": round(self.combined_prob - self.market_implied_prob, 4),
            "combined_cost_per_dollar": round(self.combined_cost, 4),
            "payout_multiple": round(self.payout_multiple, 2),
            "ev_per_dollar": round(self.ev_per_dollar, 4),
            "risk_rating": self.risk_rating,
            "explanation": self.explanation,
            "per_game": self.per_game,
            "warnings": self.warnings,
        }


def _risk_rating(prob: float, leg_count: int) -> str:
    if prob >= 0.45 and leg_count <= 3:
        return "moderate"
    if prob >= 0.28:
        return "elevated"
    if prob >= 0.15:
        return "high"
    return "very high"


def candidates(signals: Sequence[Signal], category: Category, *,
               allow_props: bool = False) -> List[Signal]:
    """Legs eligible for this category: value bets only, inside its probability band."""
    order_floor = CONFIDENCE_ORDER[category.min_confidence]
    picked: List[Signal] = []
    for signal in signals:
        if not signal.features.get("value"):
            continue
        if signal.quote is None or signal.quote.cost is None:
            continue
        if CONFIDENCE_ORDER.get(signal.confidence, 0) < order_floor:
            continue
        if not allow_props and signal.market_type.value in {
                "pass_yards", "pass_tds", "rush_yards", "recv_yards", "receptions",
                "anytime_td"}:
            continue
        prob = pricing.published_prob(signal)
        if not (category.min_leg_prob <= prob <= category.max_leg_prob):
            continue
        picked.append(signal)

    # Best expected value first, then de-duplicate so one game cannot supply five near-
    # identical rungs and crowd out the rest of the board.
    picked.sort(key=lambda s: s.ev_per_dollar or 0.0, reverse=True)
    seen_ladder: Dict[Tuple[str, str, str], int] = {}
    trimmed: List[Signal] = []
    for signal in picked:
        key = (signal.game_id, signal.market_type.value, signal.team or "")
        count = seen_ladder.get(key, 0)
        if count >= 1:
            continue
        seen_ladder[key] = count + 1
        trimmed.append(signal)
        if len(trimmed) >= MAX_CANDIDATES:
            break
    return trimmed


def evaluate(legs: Sequence[Signal], simulator: SlateSimulator, category: str,
             home_teams: Dict[str, str]) -> Optional[Parlay]:
    """Price one specific combination, with correlation handled by simulation."""
    if len(legs) < 2:
        return None
    allowed, found = conflicts.is_allowed(legs, home_teams)
    if not allowed:
        return None

    # The published probabilities are what the user sees on each leg card, so they are the
    # marginals the combination must be consistent with.
    marginals = [pricing.published_prob(leg) for leg in legs]
    joint = simulator.joint_probability(legs, marginals=marginals)
    if joint is None:
        return None

    combined_cost = 1.0
    for leg in legs:
        cost = leg.quote.cost if leg.quote else None
        if not cost or cost <= 0:
            return None
        combined_cost *= cost
    if combined_cost <= 0 or combined_cost >= 1:
        return None

    combined_prob = float(joint["combined_prob"])
    payout_multiple = 1.0 / combined_cost
    ev = (combined_prob / combined_cost) - 1.0

    parlay_legs = [ParlayLeg(signal=s, probability=pricing.published_prob(s),
                             cost=float(s.quote.cost))       # type: ignore[union-attr]
                   for s in legs]

    explanation = conflicts.coexistence_notes(legs, home_teams)
    effect = float(joint["correlation_effect"])
    if abs(effect) >= 0.005:
        direction = "higher" if effect > 0 else "lower"
        explanation.append(
            f"Correlation moves the combined probability {abs(effect):.1%} {direction} than "
            f"simply multiplying the legs ({joint['naive_prob']:.1%} naive vs "
            f"{combined_prob:.1%} simulated). Multiplying would have been wrong here.")
    else:
        explanation.append(
            "Simulated and multiplied probabilities agree to within half a point, so these "
            "legs are effectively independent.")

    warnings: List[str] = []
    if len(legs) >= 4:
        warnings.append(
            f"A {len(legs)}-leg parlay wins about {combined_prob:.0%} of the time. Most of "
            "the time this loses, and that is the expected behaviour, not a failure.")
    for conflict in found:
        if conflict.kind == "redundant":
            warnings.append(conflict.explanation)
    thin = [leg for leg in parlay_legs if leg.signal.quote and leg.signal.quote.depth_usd < 200]
    if thin:
        warnings.append(
            f"{len(thin)} leg(s) have under $200 of resting depth — the quoted price may "
            "not be available in size.")

    return Parlay(
        category=category,
        legs=parlay_legs,
        combined_prob=combined_prob,
        naive_prob=float(joint["naive_prob"]),
        correlation_effect=effect,
        combined_cost=combined_cost,
        payout_multiple=payout_multiple,
        market_implied_prob=combined_cost,
        ev_per_dollar=ev,
        risk_rating=_risk_rating(combined_prob, len(legs)),
        explanation=explanation,
        per_game=list(joint["per_game"]),
        warnings=warnings,
    )


def build(signals: Sequence[Signal], simulator: SlateSimulator,
          home_teams: Dict[str, str], *, category_key: str,
          allow_props: bool = False, top_n: int = 3) -> Dict[str, Any]:
    """Build the best parlays for one category, or explain why there are none."""
    category = CATEGORIES.get(category_key)
    if category is None:
        return {"category": category_key, "parlays": [],
                "note": f"Unknown parlay category {category_key!r}."}

    pool = candidates(signals, category, allow_props=allow_props)
    pool = [s for s in pool if simulator.can_simulate(s)]
    if len(pool) < category.min_legs:
        return {
            "category": category.key, "name": category.name,
            "description": category.description, "parlays": [],
            "note": (
                (f"Nothing on this board clears the value gate at the '{category.name}' "
                 f"risk level."
                 if not pool else
                 f"Only {len(pool)} leg{'' if len(pool) == 1 else 's'} on this board clear "
                 f"the value gate at the '{category.name}' risk level, and this parlay "
                 f"needs at least {category.min_legs}.")
                + " Padding it with legs the model does not rate would make the parlay look "
                  "better and pay worse — so there is nothing here."),
        }

    ranked: List[Tuple[float, Parlay]] = []
    for size in range(category.min_legs, category.max_legs + 1):
        for combo in itertools.combinations(pool, size):
            if category.prefer_independent:
                if len({leg.game_id for leg in combo}) != size:
                    continue
            parlay = evaluate(list(combo), simulator, category.key, home_teams)
            if parlay is None or parlay.ev_per_dollar < MIN_PARLAY_EV:
                continue
            # Score by expected value tempered by hit probability: a 2% chance at a huge
            # payout has a flattering EV and is not a bet anyone should be shown first.
            score = parlay.ev_per_dollar * math.sqrt(parlay.combined_prob)
            ranked.append((score, parlay))

    if not ranked:
        return {
            "category": category.key, "name": category.name,
            "description": category.description, "parlays": [],
            "note": (
                "No combination at this risk level clears a positive expected return once "
                "correlation is priced in. The honest answer on this board is no parlay."),
        }

    ranked.sort(key=lambda pair: pair[0], reverse=True)
    return {
        "category": category.key, "name": category.name,
        "description": category.description,
        "parlays": [parlay.to_dict() for _, parlay in ranked[:top_n]],
        "considered": len(ranked),
        "candidate_legs": len(pool),
        "note": (
            "Every leg below independently clears the value gate; the combination is priced "
            "by simulating each game's joint score distribution, not by multiplying "
            "probabilities."),
    }


def build_all(signals: Sequence[Signal], simulator: SlateSimulator,
              home_teams: Dict[str, str], *, allow_props: bool = False
              ) -> List[Dict[str, Any]]:
    return [build(signals, simulator, home_teams, category_key=key,
                  allow_props=allow_props, top_n=2 if key != "best" else 1)
            for key in ("conservative", "balanced", "aggressive", "best")]
