"""Book Consensus — comparing Kalshi to eleven sportsbooks instead of to our model.

This is the strongest signal in the application, and the reason is simple: it does not
depend on the model being right.

Every other strategy asks "does our projection disagree with the price?", which is only
useful if the projection is good — and the backtest says ours has not beaten a closing
line. This one asks "does KALSHI disagree with DraftKings, FanDuel, BetMGM, Pinnacle-class
low-vig books, and seven others?". When a thin exchange contract is priced away from a deep
consensus, the exchange is usually the one that is wrong, and no opinion of ours is needed
to see it.

The consensus is converted into a full margin and total distribution — using the same
key-number profile the model uses — so it can be evaluated against whatever line Kalshi
happens to quote, not just the lines the books happen to post.

Two honest limits are enforced. Book disagreement is a veto: if the books themselves cannot
agree within a couple of points, there is no consensus to be away from. And a market whose
consensus rests on a handful of books is not a consensus.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from app.config import settings
from app.core.types import (Confidence, MarketQuote, MarketType, Signal, StrategyMeta)
from app.data import teams
from app.data.odds_api import GameConsensus
from app.models import distributions as dist
from app.strategies import pricing
from app.strategies.base import GameContext, liquidity_ok

META = StrategyMeta(
    key="book_consensus",
    name="Book Consensus",
    market_types=[MarketType.MONEYLINE, MarketType.SPREAD, MarketType.TOTAL,
                  MarketType.TEAM_TOTAL],
    methodology=(
        "De-vigs each sportsbook's two-way prices individually, runs every book's line "
        "backwards into the expected margin and total it implies, and takes the median "
        "across books. That consensus is expanded into a key-number-aware distribution and "
        "evaluated at whatever line Kalshi quotes. The signal is Kalshi's price against the "
        "sportsbook consensus — a market-versus-market comparison that does not rely on our "
        "model at all."
    ),
    inputs=["The Odds API: h2h, spreads and totals across US sportsbooks",
            "live Kalshi order book", "fitted key-number profile"],
    limitations=(
        "Sportsbook lines are not free money: they carry vig, they move, and a Kalshi price "
        "that differs may reflect information the books have not taken yet rather than an "
        "error. Requires at least six books in agreement — where the books disagree among "
        "themselves there is no consensus to be away from. Covers only the markets "
        "sportsbooks post; Kalshi lists many they do not."
    ),
)

# Below this many contributing books there is no consensus worth the name.
MIN_BOOKS = 6
# If the books' own implied margins span more than this, they disagree and we abstain.
MAX_BOOK_DISAGREEMENT = 2.5
# How much of the Kalshi-vs-books gap we are willing to treat as real. Sportsbooks are
# sharper than Kalshi but not oracles, and Kalshi can legitimately lead on late news.
CONSENSUS_TRUST = 0.65
# A gap smaller than this is inside the noise of de-vigging eleven different margins.
MIN_GAP = 0.02


def consensus_probability(quote: MarketQuote, row: GameConsensus,
                          profile: Optional[Dict[int, float]],
                          home: str) -> Optional[float]:
    """What the sportsbook consensus says this exact Kalshi contract is worth."""
    if quote.market_type is MarketType.MONEYLINE:
        if row.home_win_prob is None or quote.team is None:
            return None
        return row.home_win_prob if quote.team == home else 1.0 - row.home_win_prob

    if quote.market_type is MarketType.SPREAD and quote.line is not None:
        margin = row.margin_distribution(profile)
        if margin is None or quote.team is None:
            return None
        # Kalshi spreads read "team wins by over N".
        if quote.team == home:
            return margin.prob_over(quote.line)
        return margin.prob_under(-quote.line)

    if quote.market_type is MarketType.TOTAL and quote.line is not None:
        total = row.total_distribution(profile)
        return total.prob_over(quote.line) if total else None

    if quote.market_type is MarketType.TEAM_TOTAL and quote.line is not None:
        if row.margin is None or row.total is None or quote.team is None:
            return None
        expected = ((row.total + row.margin) / 2.0 if quote.team == home
                    else (row.total - row.margin) / 2.0)
        team_dist = dist.build_distribution(expected, 13.2 * 0.70, low=0)
        return team_dist.prob_over(quote.line)

    return None


def usable(row: Optional[GameConsensus]) -> bool:
    if row is None or row.book_count < MIN_BOOKS:
        return False
    spread = row.margin_spread_across_books
    return spread is None or spread <= MAX_BOOK_DISAGREEMENT


def signals(ctx: GameContext, row: Optional[GameConsensus],
            profile: Optional[Dict[int, float]]) -> List[Signal]:
    if not usable(row):
        return []

    out: List[Signal] = []
    for quote in ctx.quotes:
        book_prob = consensus_probability(quote, row, profile, ctx.game.home)
        if book_prob is None:
            continue
        kalshi_prob = quote.implied_prob()
        if kalshi_prob is None:
            continue
        gap = book_prob - kalshi_prob
        if abs(gap) < MIN_GAP:
            continue

        # Move only part of the way from Kalshi's price toward the books. The books are
        # sharper, not infallible, and Kalshi occasionally leads them on breaking news.
        fair = kalshi_prob + gap * CONSENSUS_TRUST

        signal = Signal(
            strategy=META.key,
            game_id=ctx.game.game_id,
            market_type=quote.market_type,
            label=quote.label,
            selection=quote.label,
            model_prob=min(max(fair, 1e-4), 1 - 1e-4),
            confidence=Confidence.LOW,
            line=quote.line, team=quote.team,
            reasoning=[
                f"{row.book_count} sportsbooks price this at {book_prob:.1%}; "
                f"Kalshi is at {kalshi_prob:.1%} ({gap:+.1%})",
                f"Consensus expects {teams.display(ctx.game.home)} "
                f"{row.margin:+.1f} with a total of {row.total:.1f}"
                if row.margin is not None and row.total is not None else
                "Consensus derived from the moneyline only",
                f"Books agree within {row.margin_spread_across_books:.1f} pts of each other"
                if row.margin_spread_across_books is not None else "",
            ],
        )
        signal.reasoning = [r for r in signal.reasoning if r]
        # The book-anchored probability is already final — the shrink toward Kalshi is
        # CONSENSUS_TRUST above — so it is attached rather than blended again.
        pricing.attach_quote(signal, quote, signal.model_prob)
        signal.features.update({
            "fair_prob": round(signal.model_prob, 4),
            "book_prob": round(book_prob, 4),
            "book_count": row.book_count,
            "books": row.books,
            "book_disagreement": row.margin_spread_across_books,
            "consensus_margin": row.margin,
            "consensus_total": row.total,
            "raw_model_prob": round(book_prob, 4),
        })
        signal.confidence = _grade(signal, row, quote)
        out.append(signal)
    return out


def _grade(signal: Signal, row: GameConsensus, quote: MarketQuote) -> Confidence:
    if not liquidity_ok(quote):
        return Confidence.LOW
    gap = abs(signal.features.get("book_prob", 0) - (signal.market_prob or 0))
    tight = (row.margin_spread_across_books or 0) <= 1.0
    if row.book_count >= 9 and tight and gap >= 0.04:
        return Confidence.HIGH
    if row.book_count >= MIN_BOOKS and gap >= 0.025:
        return Confidence.MEDIUM
    return Confidence.LOW
