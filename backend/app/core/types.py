"""Typed domain interfaces shared across the whole app.

These are the contracts between layers: raw data -> normalized data -> features -> model
predictions -> market data -> strategy signals -> recommendations. Every layer boundary
speaks in these types, so a change to (say) how a strategy reports confidence is a compile-
time-ish problem instead of a silent dict-key mismatch three modules downstream.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class MarketType(str, Enum):
    """The bet families we model. Value is the stable wire/DB representation."""

    MONEYLINE = "moneyline"
    SPREAD = "spread"
    TOTAL = "total"
    TEAM_TOTAL = "team_total"
    WIN_MARGIN = "win_margin"
    FIRST_HALF_WINNER = "first_half_winner"
    FIRST_HALF_TOTAL = "first_half_total"
    FIRST_HALF_SPREAD = "first_half_spread"
    PASS_YARDS = "pass_yards"
    PASS_TDS = "pass_tds"
    RUSH_YARDS = "rush_yards"
    RECV_YARDS = "recv_yards"
    RECEPTIONS = "receptions"
    ANYTIME_TD = "anytime_td"
    OTHER = "other"


PLAYER_PROP_MARKETS = {
    MarketType.PASS_YARDS, MarketType.PASS_TDS, MarketType.RUSH_YARDS,
    MarketType.RECV_YARDS, MarketType.RECEPTIONS, MarketType.ANYTIME_TD,
}


class Confidence(str, Enum):
    """How much weight a signal has earned.

    REFERENCE is deliberately NOT a confidence level you can bet on: it means the model
    can price the market but has a known blind spot (usually lineup/usage), so we display
    it and never let it into a recommendation or a parlay leg.
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    REFERENCE = "reference"


CONFIDENCE_ORDER = {Confidence.REFERENCE: 0, Confidence.LOW: 1,
                    Confidence.MEDIUM: 2, Confidence.HIGH: 3}


class Side(str, Enum):
    YES = "yes"
    NO = "no"


@dataclass(frozen=True)
class Team:
    abbr: str            # nflverse abbreviation, e.g. "KC"
    name: str            # "Chiefs"
    full_name: str       # "Kansas City Chiefs"
    city: str
    conference: str      # AFC | NFC
    division: str        # AFC West
    stadium: str
    roof: str            # outdoors | dome | closed | open
    surface: str
    lat: float
    lon: float
    timezone: str


@dataclass
class Game:
    """A scheduled NFL game, normalized from the nflverse schedule."""

    game_id: str                 # nflverse id, e.g. "2026_01_NE_SEA"
    season: int
    week: int
    game_type: str               # REG | WC | DIV | CON | SB
    kickoff: str                 # ISO-8601 UTC
    home: str                    # team abbr
    away: str
    home_score: Optional[int] = None
    away_score: Optional[int] = None
    completed: bool = False
    roof: Optional[str] = None
    surface: Optional[str] = None
    div_game: bool = False
    home_rest: Optional[int] = None
    away_rest: Optional[int] = None
    stadium: Optional[str] = None
    temp: Optional[float] = None
    wind: Optional[float] = None
    home_qb: Optional[str] = None
    away_qb: Optional[str] = None
    # Historical closing lines, present only for past games (used by the backtester).
    spread_line: Optional[float] = None
    total_line: Optional[float] = None

    @property
    def margin(self) -> Optional[int]:
        if self.home_score is None or self.away_score is None:
            return None
        return self.home_score - self.away_score

    @property
    def total_points(self) -> Optional[int]:
        if self.home_score is None or self.away_score is None:
            return None
        return self.home_score + self.away_score

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["margin"] = self.margin
        d["total_points"] = self.total_points
        return d


@dataclass
class MarketQuote:
    """A live tradeable price on one contract, sourced from a real venue. Never synthesised."""

    venue: str                    # "kalshi"
    ticker: str
    event_ticker: str
    market_type: MarketType
    label: str                    # human-readable side, e.g. "Chiefs -3.5"
    side: Side = Side.YES
    yes_bid: Optional[float] = None      # 0-1
    yes_ask: Optional[float] = None      # 0-1
    depth_usd: float = 0.0
    line: Optional[float] = None         # spread/total threshold when applicable
    team: Optional[str] = None
    player: Optional[str] = None
    game_id: Optional[str] = None
    close_time: Optional[str] = None
    captured_at: Optional[float] = None

    @property
    def mid(self) -> Optional[float]:
        if self.yes_bid is not None and self.yes_ask is not None:
            return (self.yes_bid + self.yes_ask) / 2.0
        return self.yes_bid if self.yes_bid is not None else self.yes_ask

    @property
    def cost(self) -> Optional[float]:
        """What one contract of this SIDE actually costs to buy right now."""
        if self.side is Side.YES:
            return self.yes_ask
        return (1.0 - self.yes_bid) if self.yes_bid is not None else None

    @property
    def spread_width(self) -> Optional[float]:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return self.yes_ask - self.yes_bid

    def implied_prob(self) -> Optional[float]:
        """Market-implied probability of THIS side, from the mid. Not vig-free on its own."""
        m = self.mid
        if m is None:
            return None
        return m if self.side is Side.YES else 1.0 - m

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["market_type"] = self.market_type.value
        d["side"] = self.side.value
        d["mid"] = self.mid
        d["cost"] = self.cost
        d["spread_width"] = self.spread_width
        d["implied_prob"] = self.implied_prob()
        return d


@dataclass
class Signal:
    """One strategy's opinion about one market side.

    This is the atomic unit everything downstream consumes: prediction cards, the parlay
    builder, the ensemble, and the tracking store all read Signals.
    """

    strategy: str
    game_id: str
    market_type: MarketType
    label: str
    selection: str
    model_prob: float
    confidence: Confidence
    reasoning: List[str] = field(default_factory=list)
    line: Optional[float] = None
    team: Optional[str] = None
    player: Optional[str] = None
    # Filled in by the market-overlay step; absent until a real quote is matched.
    quote: Optional[MarketQuote] = None
    market_prob: Optional[float] = None       # vig-free where a complement exists
    edge: Optional[float] = None
    ev_per_dollar: Optional[float] = None
    features: Dict[str, Any] = field(default_factory=dict)

    @property
    def priced(self) -> bool:
        return self.quote is not None and self.quote.cost is not None

    @property
    def actionable(self) -> bool:
        """Reference-grade signals are displayed but never recommended or parlayed."""
        return self.confidence is not Confidence.REFERENCE and self.priced

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy": self.strategy,
            "game_id": self.game_id,
            "market_type": self.market_type.value,
            "label": self.label,
            "selection": self.selection,
            "model_prob": round(self.model_prob, 4),
            "confidence": self.confidence.value,
            "reasoning": self.reasoning,
            "line": self.line,
            "team": self.team,
            "player": self.player,
            "quote": self.quote.to_dict() if self.quote else None,
            "market_prob": round(self.market_prob, 4) if self.market_prob is not None else None,
            "edge": round(self.edge, 4) if self.edge is not None else None,
            "ev_per_dollar": round(self.ev_per_dollar, 4) if self.ev_per_dollar is not None else None,
            "actionable": self.actionable,
            "features": self.features,
        }


@dataclass
class StrategyMeta:
    """Self-description every strategy must publish, so the UI never invents methodology text."""

    key: str
    name: str
    market_types: List[MarketType]
    methodology: str
    inputs: List[str]
    limitations: str
