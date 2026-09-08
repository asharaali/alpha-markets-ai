"""Market Mispricing model — two different kinds of wrong price.

MODEL-VS-MARKET: the ordinary kind. Our probability differs from the traded price by more
than the disciplined thresholds allow for noise. Useful, but always conditional on the
model being right.

MARKET-INTERNAL: the more interesting kind, because it needs no model at all. Kalshi lists
a whole ladder of contracts on the same underlying quantity, and those contracts have to
obey arithmetic:

  * A spread ladder must be monotonic. P(win by more than 6.5) can never exceed P(win by
    more than 3.5) — the first event is a subset of the second. When the book says
    otherwise, one of those two prices is wrong, and you can see it without an opinion on
    the game.
  * A totals ladder must be monotonic in the same way.
  * The moneyline and the spread ladder must agree about P(margin > 0).

These inconsistencies are rare and usually small, and they close fast. They are also the
only "edge" in this entire application that does not depend on the model being any good,
which is exactly why they are worth surfacing separately rather than blending in.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from app.config import settings
from app.core.types import Confidence, MarketQuote, MarketType, Signal, StrategyMeta
from app.data import teams
from app.strategies.base import GameContext, liquidity_ok

META = StrategyMeta(
    key="mispricing",
    name="Market Mispricing",
    market_types=[MarketType.MONEYLINE, MarketType.SPREAD, MarketType.TOTAL],
    methodology=(
        "Two passes. First, ranks disciplined model-versus-market gaps across the slate. "
        "Second, checks the market against itself: spread and total ladders must be "
        "monotonic (a harder line cannot be more likely than an easier one) and the "
        "moneyline must agree with the spread ladder about who wins. A violation is a "
        "mispricing that does not depend on the model being right."
    ),
    inputs=["live Kalshi order books across every ladder on a game",
            "ensemble model probabilities"],
    limitations=(
        "Internal inconsistencies are usually small, often sit on thin contracts, and can "
        "close before an order fills. Both legs of an arbitrage must be liquid for it to be "
        "real, which is checked, but depth can vanish between reading and trading."
    ),
)

# A ladder violation smaller than this is a one-cent rounding artefact, not a mispricing.
MIN_VIOLATION = 0.02


def _ladder(quotes: Sequence[MarketQuote], market_type: MarketType,
            team: Optional[str] = None) -> List[MarketQuote]:
    rungs = [q for q in quotes
             if q.market_type is market_type and q.line is not None
             and q.mid is not None and (team is None or q.team == team)]
    rungs.sort(key=lambda q: q.line)  # type: ignore[arg-type,return-value]
    return rungs


def ladder_violations(ctx: GameContext) -> List[Dict[str, object]]:
    """Every place the book contradicts itself on this game."""
    findings: List[Dict[str, object]] = []

    ladders: List[tuple] = [("Total points", _ladder(ctx.quotes, MarketType.TOTAL))]
    for team in (ctx.game.home, ctx.game.away):
        ladders.append((f"{teams.display(team)} spread",
                        _ladder(ctx.quotes, MarketType.SPREAD, team)))
        ladders.append((f"{teams.display(team)} team total",
                        _ladder(ctx.quotes, MarketType.TEAM_TOTAL, team)))

    for name, rungs in ladders:
        for lower, higher in zip(rungs, rungs[1:]):
            # Both are "over" contracts, so the higher line must be no more likely.
            gap = (higher.mid or 0) - (lower.mid or 0)
            if gap <= MIN_VIOLATION:
                continue
            both_liquid = liquidity_ok(lower) and liquidity_ok(higher)
            findings.append({
                "kind": "ladder_monotonicity",
                "market": name,
                "detail": (f"'{higher.label}' is priced at {higher.mid:.0%} while the "
                           f"easier '{lower.label}' is only {lower.mid:.0%} — the harder "
                           f"outcome cannot be more likely than the easier one"),
                "size": round(gap, 4),
                "tickers": [lower.ticker, higher.ticker],
                "tradeable": both_liquid,
                "depth_usd": min(lower.depth_usd, higher.depth_usd),
            })

    findings.extend(_moneyline_vs_spread(ctx))
    findings.sort(key=lambda f: (f["tradeable"], f["size"]), reverse=True)
    return findings


def _moneyline_vs_spread(ctx: GameContext) -> List[Dict[str, object]]:
    """The moneyline and the spread ladder must tell the same story about who wins."""
    out: List[Dict[str, object]] = []
    ml = {q.team: q for q in ctx.quotes_of(MarketType.MONEYLINE) if q.team}
    for team, ml_quote in ml.items():
        rungs = _ladder(ctx.quotes, MarketType.SPREAD, team)
        if not rungs or ml_quote.mid is None:
            continue
        # The lowest rung is the closest thing the ladder has to "wins by any amount".
        easiest = rungs[0]
        if easiest.mid is None or easiest.line is None:
            continue
        # P(win at all) must be at least P(win by more than any positive margin).
        gap = easiest.mid - ml_quote.mid
        if gap <= MIN_VIOLATION:
            continue
        out.append({
            "kind": "moneyline_vs_spread",
            "market": f"{teams.display(team)} moneyline vs spread",
            "detail": (f"'{easiest.label}' trades at {easiest.mid:.0%} but "
                       f"{teams.display(team)} to win outright is only "
                       f"{ml_quote.mid:.0%} — winning by more than {easiest.line:g} "
                       f"requires winning"),
            "size": round(gap, 4),
            "tickers": [ml_quote.ticker, easiest.ticker],
            "tradeable": liquidity_ok(ml_quote) and liquidity_ok(easiest),
            "depth_usd": min(ml_quote.depth_usd, easiest.depth_usd),
        })
    return out


def top_discrepancies(signals: Sequence[Signal], limit: int = 20) -> List[Signal]:
    """The largest disciplined model-versus-market gaps, best first.

    Sorted by expected value rather than raw edge: a 4-point edge on a 20c contract is worth
    far more per dollar than the same edge on an 80c one, and sorting by edge alone
    systematically buries the best bets on the board.
    """
    candidates = [s for s in signals
                  if s.actionable and s.features.get("value")
                  and s.ev_per_dollar is not None]
    candidates.sort(key=lambda s: s.ev_per_dollar or 0.0, reverse=True)
    return candidates[:limit]


def summarise(ctx: GameContext) -> Dict[str, object]:
    violations = ladder_violations(ctx)
    tradeable = [v for v in violations if v["tradeable"]]
    return {
        "game_id": ctx.game.game_id,
        "violations": violations[:10],
        "tradeable_count": len(tradeable),
        "note": (
            "No internal inconsistencies in this game's ladders." if not violations else
            f"{len(violations)} internal inconsistency(ies) found, {len(tradeable)} on "
            "contracts with enough resting depth to act on."
        ),
    }
