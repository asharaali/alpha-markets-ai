"""The NO side of a Kalshi contract, as its own bet.

Kalshi lists spreads as "team wins by more than N" and totals as "over N". Reading only YES
meant the application could recommend "Dolphins win by 3+" but never "Dolphins +13.5", and
could never recommend an under at all. The sensible side of most lines lives on NO.

A NO signal is derived from the ensemble's YES signal rather than re-run through every
strategy: the probability is the complement of the same published number, so the two sides
can never disagree with each other. Moneylines are skipped because NO on one team is the
other team's YES, which is already on the board.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Optional

from app.core.types import Game, MarketType, Side, Signal
from app.data import teams
from app.strategies import pricing

MIRRORED = {MarketType.SPREAD, MarketType.TOTAL, MarketType.TEAM_TOTAL}


def no_selection(signal: Signal, game: Game) -> Optional[str]:
    line = signal.line
    if line is None:
        return None
    if signal.market_type is MarketType.SPREAD and signal.team:
        opponent = game.away if signal.team == game.home else game.home
        return f"{teams.display(opponent)} +{line:g}"
    if signal.market_type is MarketType.TOTAL:
        return f"Under {line:g}"
    if signal.market_type is MarketType.TEAM_TOTAL and signal.team:
        return f"{teams.display(signal.team)} under {line:g}"
    return None


def no_side(signal: Signal, game: Game) -> Optional[Signal]:
    """The NO side of a priced YES ensemble signal, or None where it adds nothing."""
    if signal.market_type not in MIRRORED or signal.quote is None:
        return None
    if signal.quote.side is not Side.YES or signal.quote.yes_bid is None:
        return None
    fair = signal.features.get("fair_prob")
    selection = no_selection(signal, game)
    if fair is None or selection is None:
        return None

    mirror = Signal(
        strategy=signal.strategy,
        game_id=signal.game_id,
        market_type=signal.market_type,
        label=selection,
        selection=selection,
        model_prob=1.0 - signal.model_prob,
        confidence=signal.confidence,
        reasoning=list(signal.reasoning),
        line=signal.line, team=signal.team, player=signal.player,
    )
    pricing.attach_quote(mirror, replace(signal.quote, side=Side.NO), 1.0 - float(fair))
    raw = signal.features.get("raw_model_prob")
    mirror.features.update({
        "raw_model_prob": (1.0 - float(raw)) if raw is not None else None,
        "mirrors": signal.quote.ticker,
        "contributors": signal.features.get("contributors"),
        "disagreement": signal.features.get("disagreement"),
    })
    return mirror
