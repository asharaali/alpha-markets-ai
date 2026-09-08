"""Line Movement model — what the market has been doing, and when that is worth reading.

Every price this app reads is written to a snapshot table. That history is the raw material
here: how far a contract has moved, how fast, and whether the move is unusual relative to
how that market type normally drifts.

The reasoning is deliberately narrow, because market-movement analysis is where sports
models most often fool themselves:

  * A move on real depth is information. A move on a thin book is one person changing their
    mind, and reading it as "sharp money" is a story, not a signal.
  * Movement TOWARD our model's number corroborates it. Movement AWAY is a warning that the
    market is learning something the model has not seen — so this strategy publishes it as
    a downgrade, not as a contrarian buy.
  * With no stored history yet, this strategy emits nothing at all rather than pretending.
"""
from __future__ import annotations

import time
from typing import Dict, List, Optional, Sequence

from app.core.types import Confidence, MarketQuote, MarketType, Signal, StrategyMeta
from app.strategies import pricing
from app.strategies.base import GameContext, liquidity_ok
from app.tracking import store

META = StrategyMeta(
    key="line_movement",
    name="Line Movement",
    market_types=[MarketType.MONEYLINE, MarketType.SPREAD, MarketType.TOTAL],
    methodology=(
        "Compares each contract's current mid against stored snapshots over 6, 24 and 72 "
        "hours, scores the move against the typical drift for that market type, and flags "
        "unusual movement on genuine depth. A move toward the model's number raises "
        "confidence in an existing signal; a move away lowers it."
    ),
    inputs=["stored Kalshi order-book snapshots", "current order book"],
    limitations=(
        "Requires accumulated history — a freshly deployed instance has none and this "
        "strategy correctly stays silent until snapshots build up. Kalshi volume is small "
        "relative to sportsbook handle, so a move here is weaker evidence than the same "
        "move on a major book."
    ),
)

# Movement smaller than this is noise on a market that ticks in whole cents.
MIN_MOVE = 0.02
# A move larger than this over the window is 'unusual' and worth surfacing.
UNUSUAL_MOVE = 0.06
LOOKBACKS = ((6 * 3600, "6h"), (24 * 3600, "24h"), (72 * 3600, "72h"))


def movement_for(ticker: str, current_mid: Optional[float]) -> Optional[Dict[str, object]]:
    """How far this contract has moved over each lookback window."""
    if current_mid is None:
        return None
    history = store.snapshot_history(ticker, limit=400)
    if len(history) < 2:
        return None
    now = time.time()
    moves: Dict[str, Optional[float]] = {}
    for seconds, label in LOOKBACKS:
        cutoff = now - seconds
        prior = None
        for row in history:
            if row["captured_at"] <= cutoff:
                prior = row
            else:
                break
        if prior is None:
            prior = history[0]
        if prior.get("mid") is None:
            moves[label] = None
        else:
            moves[label] = current_mid - float(prior["mid"])

    mids = [float(r["mid"]) for r in history if r.get("mid") is not None]
    opened = mids[0] if mids else current_mid
    return {
        "ticker": ticker,
        # The mid at each stored snapshot, thinned to a sparkline-sized series. Sending the
        # full history would be hundreds of points per contract for a 300px chart.
        "series": [{"captured_at": r["captured_at"], "mid": r["mid"]}
                   for r in history[:: max(1, len(history) // 60)] if r.get("mid") is not None],
        "current": round(current_mid, 4),
        "first_seen": round(opened, 4),
        "since_open": round(current_mid - opened, 4),
        "moves": {k: (round(v, 4) if v is not None else None) for k, v in moves.items()},
        "high": round(max(mids), 4) if mids else None,
        "low": round(min(mids), 4) if mids else None,
        "samples": len(history),
        "first_seen_at": history[0]["captured_at"],
    }


def signals(ctx: GameContext) -> List[Signal]:
    """Emit a movement read for each liquid contract that has actually moved."""
    out: List[Signal] = []
    for quote in ctx.quotes:
        move = movement_for(quote.ticker, quote.mid)
        if move is None:
            continue
        recent = move["moves"].get("24h") or move["moves"].get("6h") or 0.0
        if abs(recent) < MIN_MOVE:
            continue
        if not liquidity_ok(quote):
            continue

        direction = "toward" if recent > 0 else "away from"
        reasoning = [
            f"Mid moved {recent:+.0%} over 24h (first seen {move['first_seen']:.0%}, "
            f"now {move['current']:.0%})",
            f"Range since we started tracking: {move['low']:.0%}-{move['high']:.0%} "
            f"across {move['samples']} snapshots",
        ]
        if abs(recent) >= UNUSUAL_MOVE:
            reasoning.append(
                f"Unusual move — {abs(recent):.0%} is well beyond normal drift for this "
                f"market type, on ${quote.depth_usd:,.0f} of resting depth")

        # The movement model's own probability IS the market's current price: its content
        # is the trajectory and what that implies about confidence, not a rival estimate.
        signal = Signal(
            strategy=META.key, game_id=ctx.game.game_id,
            market_type=quote.market_type, label=quote.label,
            selection=quote.label,
            model_prob=quote.mid or 0.5,
            confidence=Confidence.REFERENCE,
            team=quote.team, player=quote.player, line=quote.line,
            reasoning=reasoning,
        )
        pricing.price_signal(signal, quote, sample_confidence=ctx.projection.confidence)
        signal.features.update({
            "movement": move,
            "unusual": abs(recent) >= UNUSUAL_MOVE,
            "direction": direction,
        })
        out.append(signal)
    out.sort(key=lambda s: abs(s.features.get("movement", {}).get("moves", {}).get("24h") or 0),
             reverse=True)
    return out


# A "move" on a book with nothing resting in it is one person changing their mind, not the
# market repricing. Movers shown on the dashboard have to clear a real depth floor, or the
# panel fills with thin team-total contracts swinging 13% on no volume and buries the
# moves that actually mean something.
MOVER_MIN_DEPTH = 500.0


def market_movers(quotes: Sequence[MarketQuote], limit: int = 12,
                  min_depth: float = MOVER_MIN_DEPTH) -> List[Dict[str, object]]:
    """The biggest recent moves across a whole slate, for the dashboard panel."""
    rows: List[Dict[str, object]] = []
    for quote in quotes:
        if quote.depth_usd < min_depth or not liquidity_ok(quote):
            continue
        move = movement_for(quote.ticker, quote.mid)
        if move is None:
            continue
        recent = move["moves"].get("24h") or move["moves"].get("6h") or 0.0
        if abs(recent) < MIN_MOVE:
            continue
        rows.append({
            "ticker": quote.ticker, "game_id": quote.game_id, "label": quote.label,
            "market_type": quote.market_type.value,
            "current": move["current"], "move_24h": recent,
            "since_open": move["since_open"],
            "depth_usd": quote.depth_usd,
            "unusual": abs(recent) >= UNUSUAL_MOVE,
            "samples": move["samples"],
        })
    rows.sort(key=lambda r: abs(r["move_24h"]), reverse=True)
    return rows[:limit]


def corroboration(signal: Signal) -> Optional[float]:
    """How the recent move relates to a signal's direction, in [-1, 1].

    Positive means the market has been moving toward the side the signal likes, which is
    the single best real-time check that an edge is real. Negative means the opposite, and
    the ensemble treats it as a reason for caution.
    """
    quote = signal.quote
    if quote is None or signal.market_prob is None:
        return None
    move = movement_for(quote.ticker, quote.mid)
    if move is None:
        return None
    recent = move["moves"].get("24h") or move["moves"].get("6h")
    if recent is None or abs(recent) < MIN_MOVE:
        return 0.0
    wants_higher = signal.model_prob > signal.market_prob
    aligned = (recent > 0) == wants_higher
    magnitude = min(abs(recent) / UNUSUAL_MOVE, 1.0)
    return magnitude if aligned else -magnitude
