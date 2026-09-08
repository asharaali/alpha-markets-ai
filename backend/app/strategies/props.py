"""Player prop models — passing, rushing, receiving, receptions, anytime touchdown.

A prop projection is a player's recency-weighted per-game rate, scaled twice: by the game
script this model expects (a team projected for 30 points runs more plays and scores more
touchdowns than one projected for 17) and by the specific defence being faced. The spread
around it is the player's own measured game-to-game variance wherever there is enough
history to measure it.

Props are held to a deliberately higher standard than game lines, for a reason that is easy
to state and expensive to ignore: this model does not know the game plan. It cannot see
that a receiver is on a snap count, that a coordinator has changed, or that a rookie is
about to take over a backfield. So a prop only becomes actionable when the player is
confirmed on the current depth chart, has real measured history, and is not on the injury
report — and even then it needs roughly double the edge of a game line, and can never be
graded better than MEDIUM.

Everything else prices for REFERENCE: shown, explained, and never recommended or parlayed.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional

from app.config import settings
from app.core.types import (Confidence, MarketQuote, MarketType, Signal, StrategyMeta,
                            PLAYER_PROP_MARKETS)
from app.data import teams
from app.features.players import PlayerForm, lookup
from app.models import distributions as dist
from app.strategies import pricing
from app.strategies.base import GameContext, liquidity_ok

META = StrategyMeta(
    key="player_props",
    name="Player Props",
    market_types=sorted(PLAYER_PROP_MARKETS, key=lambda m: m.value),
    methodology=(
        "Recency-weighted per-game rates for each player, scaled by projected team scoring "
        "(game script) and by the opposing defence's efficiency in that phase. Yardage and "
        "reception markets are priced from a Normal with the player's own measured "
        "game-to-game standard deviation; touchdown markets from a Poisson on expected "
        "scores. Kalshi writes props as 'N+', so probabilities are computed at or above the "
        "line with a continuity correction."
    ),
    inputs=["nflverse weekly player game logs", "nflverse depth charts and injury reports",
            "projected team scoring", "opponent phase-specific defensive ratings"],
    limitations=(
        "The model has no access to game plans, snap counts for the coming week, or "
        "coaching intent. Props are therefore priced for reference unless the player is a "
        "confirmed starter with measured history and no injury designation, and are held to "
        "roughly double the edge threshold of a game line even then."
    ),
)

# Props must clear about double a game line's edge before they are worth acting on.
PROP_MIN_EDGE = max(settings.MIN_EDGE * 2.0, 0.06)
MIN_GAMES_FOR_ACTIONABLE = 4
LEAGUE_AVG_TEAM_POINTS = 22.0

# Which stat and distribution each prop market reads.
_MARKET_STAT = {
    MarketType.PASS_YARDS: ("passing_yards", "normal"),
    MarketType.RUSH_YARDS: ("rushing_yards", "normal"),
    MarketType.RECV_YARDS: ("receiving_yards", "normal"),
    MarketType.RECEPTIONS: ("receptions", "count"),
    MarketType.PASS_TDS: ("passing_tds", "poisson"),
    MarketType.ANYTIME_TD: ("anytime_td", "poisson"),
}

# Which side of the ball each market depends on, for the opponent adjustment.
_PHASE = {
    MarketType.PASS_YARDS: "pass", MarketType.PASS_TDS: "pass",
    MarketType.RECV_YARDS: "pass", MarketType.RECEPTIONS: "pass",
    MarketType.RUSH_YARDS: "rush", MarketType.ANYTIME_TD: "mixed",
}


def _game_script_scale(ctx: GameContext, team: str) -> float:
    """How much more (or less) production this game's projected scoring implies."""
    projected = (ctx.projection.home_score if team == ctx.game.home
                 else ctx.projection.away_score)
    scale = projected / LEAGUE_AVG_TEAM_POINTS
    return min(max(scale, 0.75), 1.30)


def _opponent_scale(ctx: GameContext, opponent: str, market_type: MarketType) -> float:
    """Defensive adjustment: a good pass defence suppresses passing production."""
    phase = _PHASE.get(market_type, "mixed")
    if phase == "pass":
        metric = "pass_epa_per_dropback"
    elif phase == "rush":
        metric = "rush_epa_per_carry"
    else:
        metric = "epa_per_play"
    suppression = ctx.ratings.get(opponent).defense.get(metric, 0.0)
    # Defence ratings are in EPA units; a 0.10 EPA/play suppression is a strong unit and
    # translates to roughly a 10% production haircut.
    return min(max(1.0 - suppression, 0.85), 1.15)


def _confirmed_starter(ctx: GameContext, team: str, player: str) -> Optional[int]:
    chart = ctx.depth_chart.get(team) or []
    lowered = player.strip().lower()
    for row in chart:
        if str(row.get("player") or "").strip().lower() == lowered:
            return row.get("rank")  # type: ignore[return-value]
    return None


def _injury_flagged(ctx: GameContext, team: str, player: str) -> Optional[str]:
    lowered = player.strip().lower()
    for report in ctx.injuries.get(team, []):
        if report.player.strip().lower() == lowered and report.severity > 0:
            return report.report_status or report.practice_status
    return None


def _team_of(ctx: GameContext, form: Optional[PlayerForm], quote: MarketQuote
             ) -> Optional[str]:
    """Which side of THIS game the player is on."""
    if form and form.team in (ctx.game.home, ctx.game.away):
        return form.team
    # Kalshi encodes the team in the ticker, e.g. ...-SEACKUPP10-25.
    for abbr in (ctx.game.home, ctx.game.away):
        if f"-{abbr}" in quote.ticker.upper():
            return abbr
    return None


def _probability(market_type: MarketType, form: PlayerForm, line: float,
                 scale: float, ctx: GameContext, team: str) -> Optional[float]:
    spec = _MARKET_STAT.get(market_type)
    if spec is None:
        return None
    stat, shape = spec

    if market_type is MarketType.ANYTIME_TD:
        # Expected touchdowns = the player's own scoring rate, rescaled by how many
        # touchdowns this game projects for their team.
        base = max(form.mean("rushing_tds"), 0.0) + max(form.mean("receiving_tds"), 0.0)
        lam = max(base * scale, 0.0)
        if lam <= 0:
            return None
        return dist.poisson_at_least_one(lam)

    mu = max(form.mean(stat), 0.0) * scale
    if mu <= 0:
        return None

    if shape == "poisson":
        lam = mu
        # Kalshi's "N+" means at or above N.
        below = sum(dist.poisson_pmf(k, lam) for k in range(0, int(math.ceil(line))))
        return max(0.0, 1.0 - below)

    sigma = max(form.stdev(stat) * scale, 1e-3)
    if shape == "count":
        below = dist.build_distribution(mu, sigma, low=0)
        return max(0.0, 1.0 - below.cdf(int(math.ceil(line)) - 1))
    # Continuous yardage: "150+" is P(X >= 150), continuity-corrected.
    return dist.prob_over_continuous(mu, sigma, line - 0.5)


def signals(ctx: GameContext, name_index: Dict[str, PlayerForm]) -> List[Signal]:
    out: List[Signal] = []
    for quote in ctx.quotes:
        if quote.market_type not in PLAYER_PROP_MARKETS or quote.player is None:
            continue
        if quote.line is None:
            continue
        form = lookup(name_index, quote.player)
        team = _team_of(ctx, form, quote)
        opponent = None
        if team:
            opponent = ctx.game.away if team == ctx.game.home else ctx.game.home

        if form is None or team is None or opponent is None:
            out.append(_unpriceable(ctx, quote,
                                    "No game-log history for this player — the model has "
                                    "nothing to project from"))
            continue

        script = _game_script_scale(ctx, team)
        defence = _opponent_scale(ctx, opponent, quote.market_type)
        scale = script * defence
        prob = _probability(quote.market_type, form, quote.line, scale, ctx, team)
        if prob is None:
            out.append(_unpriceable(ctx, quote,
                                    "Player has no recorded production in this category"))
            continue

        rank = _confirmed_starter(ctx, team, quote.player)
        injury = _injury_flagged(ctx, team, quote.player)
        stat = _MARKET_STAT[quote.market_type][0]
        base_mean = max(form.mean(stat), 0.0) if stat != "anytime_td" else None

        reasoning = [
            f"{form.player} ({team}) over {form.games} recent games"
            + (f": {base_mean:.1f} per game baseline" if base_mean is not None else ""),
            f"Game script x{script:.2f} ({teams.display(team)} projected for "
            f"{(ctx.projection.home_score if team == ctx.game.home else ctx.projection.away_score):.1f} pts)",
            f"Opponent adjustment x{defence:.2f} vs {teams.display(opponent)}",
        ]
        if not form.measured_variance:
            reasoning.append("Too few games to measure this player's own variance — a "
                             "positional prior is standing in, so treat the spread as "
                             "approximate")
        if rank is not None:
            reasoning.append(f"Depth chart rank {rank}")
        if injury:
            reasoning.append(f"On the injury report: {injury}")

        actionable = (rank is not None and rank <= 2
                      and form.measured_variance
                      and form.games >= MIN_GAMES_FOR_ACTIONABLE
                      and injury is None)

        signal = Signal(
            strategy=META.key, game_id=ctx.game.game_id,
            market_type=quote.market_type, label=quote.label,
            selection=quote.label, model_prob=prob,
            confidence=Confidence.REFERENCE,
            line=quote.line, team=team, player=form.player,
            reasoning=reasoning,
        )
        pricing.price_signal(signal, quote, sample_confidence=ctx.projection.confidence * 0.8)
        signal.features.update({
            "player_form": form.to_dict(),
            "game_script_scale": round(script, 3),
            "opponent_scale": round(defence, 3),
            "depth_rank": rank,
            "injury_status": injury,
            "prop_min_edge": PROP_MIN_EDGE,
        })
        signal.confidence = _grade(signal, quote, actionable=actionable)
        # Props re-run the value gate at the stricter prop threshold.
        if signal.edge is not None:
            signal.features["value"] = bool(
                signal.confidence is not Confidence.REFERENCE
                and signal.edge >= PROP_MIN_EDGE
                and (signal.ev_per_dollar or 0) > 0
                and liquidity_ok(quote))
        out.append(signal)
    return out


def _grade(signal: Signal, quote: MarketQuote, *, actionable: bool) -> Confidence:
    if not actionable or not liquidity_ok(quote):
        return Confidence.REFERENCE
    if signal.market_prob is None:
        return Confidence.REFERENCE
    gap = abs(signal.model_prob - signal.market_prob)
    # A prop where the model wildly disagrees with the market is the model missing a game
    # plan, not an edge. Those stay reference-only however tempting the number looks.
    if gap > 0.25:
        return Confidence.REFERENCE
    return Confidence.MEDIUM if gap <= 0.12 else Confidence.LOW


def _unpriceable(ctx: GameContext, quote: MarketQuote, why: str) -> Signal:
    """A listed prop we decline to price. Shown with the reason, never as a number."""
    signal = Signal(
        strategy=META.key, game_id=ctx.game.game_id,
        market_type=quote.market_type, label=quote.label,
        selection=quote.label,
        model_prob=quote.mid if quote.mid is not None else 0.5,
        confidence=Confidence.REFERENCE,
        line=quote.line, player=quote.player, team=quote.team,
        reasoning=[why],
    )
    signal.quote = quote
    signal.market_prob = quote.implied_prob()
    signal.features.update({"unpriced": True, "value": False})
    return signal
