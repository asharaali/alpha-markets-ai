"""The strategy contract.

A strategy is a self-contained opinion generator: given everything known about a game and
the prices currently available on it, it emits Signals — a probability for a specific side
of a specific market, with its reasoning attached.

Two kinds live here, and keeping them separate is what stops the system double-counting:

  * ADJUSTMENT strategies (injuries, situational spots, weather) change the projection
    itself. They run first, and their output is a shift in expected points plus added
    uncertainty. They do not emit market signals, because their effect is already inside
    every price the projection produces.

  * MARKET strategies (moneyline, spread, totals, matchup, props, line movement,
    mispricing) read the adjusted projection and the live book, and emit Signals.

The ensemble then combines market signals; nothing else is allowed to.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Protocol, Sequence

from app.core.types import (Confidence, Game, MarketQuote, MarketType, Signal,
                            StrategyMeta)
from app.data.nflverse import InjuryReport
from app.models.game_model import GameProjection
from app.models.ratings import RatingSet


@dataclass
class ProjectionAdjustment:
    """A change to a game's projection, with the reasons that justify it."""

    margin_shift: float = 0.0          # points, positive favours the home team
    total_shift: float = 0.0           # points added to the game total
    sigma_add: float = 0.0             # extra margin uncertainty
    reasons: List[str] = field(default_factory=list)
    detail: Dict[str, object] = field(default_factory=dict)

    def merge(self, other: "ProjectionAdjustment") -> "ProjectionAdjustment":
        return ProjectionAdjustment(
            margin_shift=self.margin_shift + other.margin_shift,
            total_shift=self.total_shift + other.total_shift,
            # Uncertainty adds in quadrature: two independent unknowns do not make a game
            # twice as unknowable.
            sigma_add=(self.sigma_add ** 2 + other.sigma_add ** 2) ** 0.5,
            reasons=self.reasons + other.reasons,
            detail={**self.detail, **other.detail},
        )

    def to_dict(self) -> Dict[str, object]:
        return {"margin_shift": round(self.margin_shift, 2),
                "total_shift": round(self.total_shift, 2),
                "sigma_add": round(self.sigma_add, 2),
                "reasons": self.reasons, "detail": self.detail}


@dataclass
class GameContext:
    """Everything the strategies are allowed to see about one game.

    Assembling this in one place is what enforces the look-ahead guarantee: the backtester
    hands over a context built strictly from data available before kickoff, and no strategy
    can reach around it to a live feed.
    """

    game: Game
    ratings: RatingSet
    projection: GameProjection
    base_projection: GameProjection            # before adjustments, for explanation
    quotes: List[MarketQuote] = field(default_factory=list)
    injuries: Dict[str, List[InjuryReport]] = field(default_factory=dict)
    depth_chart: Dict[str, List[Dict[str, object]]] = field(default_factory=dict)
    player_history: Dict[str, List[Dict[str, object]]] = field(default_factory=dict)
    weather: Optional[Dict[str, object]] = None
    line_history: List[Dict[str, object]] = field(default_factory=list)
    adjustment: ProjectionAdjustment = field(default_factory=ProjectionAdjustment)
    book_consensus: Dict[str, float] = field(default_factory=dict)
    asof: float = 0.0

    def quotes_of(self, market_type: MarketType) -> List[MarketQuote]:
        return [q for q in self.quotes if q.market_type is market_type]


class AdjustmentStrategy(Protocol):
    """Changes the projection. Runs before any market strategy."""

    meta: StrategyMeta

    def adjust(self, game: Game, ratings: RatingSet, *,
               injuries: Dict[str, List[InjuryReport]],
               depth_chart: Dict[str, List[Dict[str, object]]],
               weather: Optional[Dict[str, object]]) -> ProjectionAdjustment:
        ...


class MarketStrategy(Protocol):
    """Emits Signals against live prices."""

    meta: StrategyMeta

    def signals(self, ctx: GameContext) -> List[Signal]:
        ...


def confidence_from(*, sample_confidence: float, market_agreement: float,
                    liquidity_ok: bool, reference: bool = False) -> Confidence:
    """Map continuous evidence onto the published confidence ladder.

    `market_agreement` is 1 minus the absolute model-vs-market gap. A model that disagrees
    violently with a liquid market is usually wrong, not brilliant, so a huge gap LOWERS
    confidence rather than raising it — which is the opposite of what a naive edge sort
    would do.
    """
    if reference:
        return Confidence.REFERENCE
    if not liquidity_ok:
        return Confidence.LOW
    score = 0.55 * sample_confidence + 0.45 * market_agreement
    if score >= 0.78:
        return Confidence.HIGH
    if score >= 0.62:
        return Confidence.MEDIUM
    return Confidence.LOW


def liquidity_ok(quote: MarketQuote, *, min_depth: float = 50.0,
                 max_spread: float = 0.12) -> bool:
    """Is this price real enough to bet into?

    A 20c-wide market with $30 resting is not a price, it is a placeholder. Treating it as
    one manufactures edge out of an absent counterparty.
    """
    width = quote.spread_width
    if width is None or width > max_spread:
        return False
    return quote.depth_usd >= min_depth
