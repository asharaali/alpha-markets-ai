"""Whether a set of legs can legally and sensibly coexist.

Three failure modes, and they are not the same thing:

  * IMPOSSIBLE — the legs cannot all win. Both moneylines in one game; a total over and the
    same total under. The simulation would price these at zero, but a builder that has to
    discover impossibility by simulation is a builder that wastes its time proposing
    nonsense, and a user who is shown a 0% parlay learns nothing.

  * NESTED — one leg logically implies another. "Over 3.5" and "Over 6.5" on the same total
    is not a two-leg parlay; it is one bet whose odds have been multiplied as though it were
    two. This is the single most common way a parlay builder flatters itself, and it is
    always blocked.

  * REDUNDANT — not strictly nested, but so tightly coupled that the second leg adds almost
    no independent risk while multiplying the payout: a team's moneyline stacked with the
    smallest rung of that same team's spread ladder. Allowed, but flagged, because the
    correlated pricing already reflects it and the user should see why the payout looks
    generous.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from app.core.types import MarketType, Signal
from app.data import teams


@dataclass
class Conflict:
    kind: str            # impossible | nested | redundant
    legs: Tuple[int, int]
    explanation: str


# Markets whose contracts form a ladder over the same underlying quantity.
_LADDER_MARKETS = {MarketType.SPREAD, MarketType.TOTAL, MarketType.TEAM_TOTAL,
                   MarketType.FIRST_HALF_TOTAL, MarketType.FIRST_HALF_SPREAD}


def _ladder_key(signal: Signal) -> Optional[Tuple[str, str, str]]:
    if signal.market_type not in _LADDER_MARKETS:
        return None
    return (signal.game_id, signal.market_type.value, signal.team or "")


def find(legs: Sequence[Signal], home_teams: Dict[str, str]) -> List[Conflict]:
    """Every conflict in a candidate leg set."""
    found: List[Conflict] = []
    for i, a in enumerate(legs):
        for j in range(i + 1, len(legs)):
            b = legs[j]
            conflict = _pair(a, b, i, j, home_teams)
            if conflict:
                found.append(conflict)
    return found


def _pair(a: Signal, b: Signal, i: int, j: int,
          home_teams: Dict[str, str]) -> Optional[Conflict]:
    ticker_a = a.quote.ticker if a.quote else None
    ticker_b = b.quote.ticker if b.quote else None
    if ticker_a and ticker_a == ticker_b:
        return Conflict("impossible", (i, j),
                        "The same contract cannot be used as two legs.")

    if a.game_id != b.game_id:
        return None

    # Both sides of a moneyline: exactly one can win.
    if a.market_type is b.market_type is MarketType.MONEYLINE and a.team != b.team:
        return Conflict("impossible", (i, j),
                        f"{teams.display(a.team)} and {teams.display(b.team)} cannot both "
                        "win the same game.")

    # Winning-margin bands are mutually exclusive by construction.
    if a.market_type is b.market_type is MarketType.WIN_MARGIN:
        return Conflict("impossible", (i, j),
                        "Only one winning-margin band can be correct.")

    key_a, key_b = _ladder_key(a), _ladder_key(b)
    if key_a and key_a == key_b:
        low, high = sorted([a, b], key=lambda s: s.line or 0)
        return Conflict("nested", (i, j),
                        f"'{high.label}' already requires '{low.label}' — stacking two "
                        "rungs of the same ladder multiplies the payout without adding a "
                        "second real outcome.")

    # A team's moneyline plus that team's spread: the spread implies the win.
    ml, spread = _moneyline_and_spread(a, b)
    if ml is not None and spread is not None and ml.team == spread.team:
        if (spread.line or 0) >= 0:
            return Conflict("nested", (i, j),
                            f"{teams.display(ml.team)} covering '{spread.label}' already "
                            "means they won the game outright.")
        return Conflict("redundant", (i, j),
                        f"{teams.display(ml.team)}'s moneyline and spread move together — "
                        "the correlated pricing accounts for it, but this is closer to one "
                        "bet than two.")

    # A team's moneyline against the OTHER team's spread on a positive line.
    if ml is not None and spread is not None and ml.team != spread.team:
        if (spread.line or 0) >= 0:
            return Conflict("impossible", (i, j),
                            f"{teams.display(spread.team)} winning by more than "
                            f"{spread.line:g} rules out {teams.display(ml.team)} winning.")

    # Opposite spread ladders in the same game can be jointly impossible.
    if a.market_type is b.market_type is MarketType.SPREAD and a.team != b.team:
        return Conflict("impossible", (i, j),
                        f"Both teams cannot win by more than {a.line:g} and "
                        f"{b.line:g} points respectively.")

    # A team total that already exceeds the game total under.
    return None


def _moneyline_and_spread(a: Signal, b: Signal) -> Tuple[Optional[Signal], Optional[Signal]]:
    if a.market_type is MarketType.MONEYLINE and b.market_type is MarketType.SPREAD:
        return a, b
    if b.market_type is MarketType.MONEYLINE and a.market_type is MarketType.SPREAD:
        return b, a
    return None, None


def is_allowed(legs: Sequence[Signal], home_teams: Dict[str, str],
               *, allow_redundant: bool = True) -> Tuple[bool, List[Conflict]]:
    """Can this leg set be offered? Impossible and nested combinations never can."""
    conflicts = find(legs, home_teams)
    blocking = [c for c in conflicts
                if c.kind in ("impossible", "nested")
                or (c.kind == "redundant" and not allow_redundant)]
    return (not blocking), conflicts


def coexistence_notes(legs: Sequence[Signal], home_teams: Dict[str, str]) -> List[str]:
    """Plain-language notes on how the surviving legs relate to each other."""
    notes: List[str] = []
    by_game: Dict[str, List[Signal]] = {}
    for leg in legs:
        by_game.setdefault(leg.game_id, []).append(leg)

    for game_id, group in by_game.items():
        if len(group) < 2:
            continue
        labels = ", ".join(leg.label for leg in group)
        notes.append(
            f"{len(group)} legs share {game_id} ({labels}) — these move together, so the "
            "combined probability is simulated from that game's joint score distribution "
            "rather than multiplied.")
    if len(by_game) == len(legs) and len(legs) > 1:
        notes.append("Every leg is in a different game, so the legs are genuinely "
                     "independent and multiply cleanly.")
    for conflict in find(legs, home_teams):
        if conflict.kind == "redundant":
            notes.append(conflict.explanation)
    return notes
