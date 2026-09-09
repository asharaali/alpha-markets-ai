"""The research engine — assembles data, models, markets and strategies into one analysis.

This is the only place that knows the whole pipeline, and it exists so that the API layer,
the background jobs and the backtester all run the SAME code path. If the dashboard and the
backtest disagree about what the model said, the backtest is worthless — so they share this.

Order of operations, which matters:
  1. Ratings as of the slate's week (never seeing the week being predicted).
  2. Adjustment strategies (injuries, situational, weather) produce point shifts.
  3. The projection is rebuilt WITH those shifts, so every downstream price includes them.
  4. Kalshi discovery and pricing.
  5. Market strategies emit signals against real prices.
  6. The ensemble combines them.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.config import settings
from app.core.cache import AsyncTTLCache
from app.core.errors import InsufficientData, NotFound
from app.core.logging import get_logger
from app.core.types import Confidence, Game, MarketQuote, MarketType, Signal
from app.data import nflverse, odds_api, teams, weather
from app.data.kalshi import markets as kalshi_markets
from app.data.kalshi import series as kalshi_series
from app.features import players as player_features
from app.models import calibration, game_model
from app.models.ratings import RatingSet, build as build_ratings
from app.strategies import (book_consensus, ensemble, game_lines, injury_impact,
                            line_movement, matchup, mispricing, props, situational)
from app.strategies.base import GameContext, ProjectionAdjustment

log = get_logger(__name__)

_ratings_cache = AsyncTTLCache(ttl=1800, stale_ttl=6 * 3600, name="ratings")
_players_cache = AsyncTTLCache(ttl=3600, stale_ttl=12 * 3600, name="player-form")
_slate_cache = AsyncTTLCache(ttl=900, stale_ttl=6 * 3600, name="slate")

# Adjustment strategies, in the order their reasoning should read.
ADJUSTERS = (injury_impact, situational)


@dataclass
class SlateAnalysis:
    """Everything the app knows about a set of games right now."""

    season: int
    week: int
    games: List[Game]
    contexts: Dict[str, GameContext]
    signals: Dict[str, List[Signal]]          # game_id -> per-strategy signals
    ensemble: Dict[str, List[Signal]]         # game_id -> ensemble signals
    quotes: List[MarketQuote]
    board_stats: Dict[str, Any]
    ratings: RatingSet
    model_summary: Dict[str, Any]
    built_at: float = field(default_factory=time.time)
    warnings: List[str] = field(default_factory=list)
    book_consensus: Dict[str, Any] = field(default_factory=dict)

    def all_ensemble(self) -> List[Signal]:
        return [s for rows in self.ensemble.values() for s in rows]

    def all_signals(self) -> List[Signal]:
        return [s for rows in self.signals.values() for s in rows]


async def current_week(season: Optional[int] = None) -> Tuple[int, int]:
    """The season and week the app should be showing.

    'Current' means the week containing the next game that has not kicked off, which is the
    right answer both mid-season and on the Tuesday after a slate finishes.
    """
    season = season or settings.SEASON
    games = await nflverse.schedule(seasons=[season])
    if not games:
        # Before a season's schedule is published, fall back to the prior one.
        prior = await nflverse.schedule(seasons=[season - 1])
        if not prior:
            raise InsufficientData("no schedule data available for any recent season")
        return season - 1, max(g.week for g in prior)
    now = datetime.now(timezone.utc).isoformat()
    upcoming = [g for g in games if not g.completed and g.kickoff and g.kickoff >= now]
    if upcoming:
        return season, min(g.week for g in upcoming)
    return season, max(g.week for g in games)


async def slate_games(season: int, week: int) -> List[Game]:
    games = await nflverse.schedule(seasons=[season])
    return [g for g in games if g.week == week]


async def upcoming_games(*, days: int = 10, limit: int = 32) -> List[Game]:
    """Games kicking off within the next `days`, plus any still in progress."""
    season, _ = await current_week()
    games = await nflverse.schedule(seasons=[season])
    now = datetime.now(timezone.utc)
    horizon = (now + timedelta(days=days)).isoformat()
    floor = (now - timedelta(hours=6)).isoformat()
    picked = [g for g in games
              if g.kickoff and floor <= g.kickoff <= horizon and not g.completed]
    picked.sort(key=lambda g: g.kickoff)
    return picked[:limit]


async def _ratings_for(season: int, week: int) -> RatingSet:
    entry = await _ratings_cache.get(f"{season}:{week}",
                                     lambda: build_ratings(season, week))
    return entry.value


async def _player_forms(season: int, week: int) -> Dict[str, player_features.PlayerForm]:
    entry = await _players_cache.get(
        f"{season}:{week}",
        lambda: player_features.build(season, max_week=max(week - 1, 0)))
    return entry.value


def build_context(game: Game, *, ratings: RatingSet,
                  artifact: calibration.GameModelArtifact,
                  injuries: Dict[str, List],
                  depth_chart: Dict[str, List[Dict[str, Any]]],
                  forecast: Optional[Dict[str, Any]]) -> GameContext:
    """Project one game and apply every adjustment. Prices are attached separately.

    Projection happens BEFORE pricing on purpose: knowing where the model expects a game to
    land is what lets us read order books only for the ladder rungs that could plausibly
    matter, instead of every contract Kalshi lists.
    """
    base = game_model.project(game, ratings, artifact)

    adjustment = ProjectionAdjustment()
    for adjuster in ADJUSTERS:
        adjustment = adjustment.merge(adjuster.adjust(
            game, ratings, injuries=injuries, depth_chart=depth_chart,
            weather=forecast))

    adjusted = game_model.project(
        game, ratings, artifact,
        margin_shift=adjustment.margin_shift,
        total_shift=adjustment.total_shift,
        extra_margin_sigma=adjustment.sigma_add,
        adjustments={"margin": adjustment.margin_shift,
                     "total": adjustment.total_shift,
                     "sigma": adjustment.sigma_add})

    return GameContext(
        game=game, ratings=ratings, projection=adjusted, base_projection=base,
        injuries={t: injuries.get(t, []) for t in (game.home, game.away)},
        depth_chart={t: depth_chart.get(t, []) for t in (game.home, game.away)},
        weather=forecast, adjustment=adjustment, asof=time.time(),
    )


def projection_centres(contexts: Dict[str, GameContext]) -> Dict[str, Dict[str, float]]:
    """Where each game is expected to land, for choosing which ladder rungs to price."""
    out: Dict[str, Dict[str, float]] = {}
    for game_id, ctx in contexts.items():
        out[game_id] = {
            "margin": ctx.projection.expected_margin,
            "total": ctx.projection.expected_total,
            "home_score": ctx.projection.home_score,
            "away_score": ctx.projection.away_score,
            "home_team": ctx.game.home,
            "away_team": ctx.game.away,
        }
    return out


def run_strategies(ctx: GameContext, artifact: calibration.GameModelArtifact,
                   name_index: Dict[str, player_features.PlayerForm],
                   *, include_props: bool = True,
                   consensus: Optional[Any] = None) -> List[Signal]:
    """Every market strategy's view of one game."""
    out: List[Signal] = []
    out.extend(game_lines.moneyline_signals(ctx))
    out.extend(game_lines.spread_signals(ctx))
    out.extend(game_lines.total_signals(ctx))
    out.extend(game_lines.win_margin_signals(ctx))
    out.extend(matchup.signals(ctx, artifact))
    out.extend(line_movement.signals(ctx))
    if consensus is not None:
        out.extend(book_consensus.signals(ctx, consensus, artifact.margin_profile()))
    if include_props:
        out.extend(props.signals(ctx, name_index))
    return out


async def analyze(*, season: Optional[int] = None, week: Optional[int] = None,
                  games: Optional[Sequence[Game]] = None,
                  include_props: bool = False,
                  series_tickers: Optional[Sequence[str]] = None,
                  price_all_rungs: bool = False) -> SlateAnalysis:
    """Full analysis of a slate. The single entry point for every read endpoint."""
    if season is None or week is None:
        season, week = await current_week(season)
    slate = list(games) if games is not None else await slate_games(season, week)
    warnings: List[str] = []
    if not slate:
        raise NotFound(f"no games scheduled for {season} week {week}")

    artifact = calibration.load()
    if artifact is None:
        raise InsufficientData(
            "the game model has not been calibrated yet",
            detail="calibration runs in the background on first start; retry shortly")

    ratings = await _ratings_for(season, week)
    if ratings.sample_games == 0:
        warnings.append("No historical play-by-play was available, so team ratings are "
                        "empty and projections are league-average.")

    injury_rows = await nflverse.injuries(season, week=week)
    injuries: Dict[str, List] = {}
    for row in injury_rows:
        injuries.setdefault(row.team, []).append(row)

    chart_rows = await nflverse.depth_charts(season)
    depth_chart: Dict[str, List[Dict[str, Any]]] = {}
    for row in chart_rows:
        depth_chart.setdefault(str(row["team"]), []).append(row)

    forecasts = await weather.for_games(slate)
    # Sportsbook consensus is an enhancement: consensus() never raises, and an empty result
    # simply means the cross-check does not run this cycle.
    consensus = await odds_api.consensus(slate)

    # Step 1: project every game before touching the venue.
    contexts: Dict[str, GameContext] = {}
    for game in slate:
        contexts[game.game_id] = build_context(
            game, ratings=ratings, artifact=artifact, injuries=injuries,
            depth_chart=depth_chart, forecast=forecasts.get(game.game_id))

    # Step 2: discover contracts, then read books only where a bet could plausibly live.
    tickers = list(series_tickers or kalshi_series.GAME_LINE_SERIES)
    if include_props:
        tickers += kalshi_series.PLAYER_PROP_SERIES
    quotes: List[MarketQuote] = []
    try:
        discovered, discovery_stats = await kalshi_markets.discover(tickers, slate)
        selected = kalshi_markets.select_for_pricing(
            discovered, projection_centres(contexts), price_all=price_all_rungs)
        quotes, price_stats = await kalshi_markets.price(selected)
        discovery_stats.contracts_priced = price_stats.contracts_priced
        board_stats = discovery_stats.to_dict()
        board_stats["contracts_selected"] = len(selected)
    except Exception as exc:  # noqa: BLE001 - the research view must survive a dead venue
        log.error("Kalshi board unavailable: %s", exc)
        board_stats = {"error": str(exc)[:200], "contracts_priced": 0}
        warnings.append("Kalshi market data is unavailable right now — model projections "
                        "are shown without prices, so no edges are calculated.")

    by_game: Dict[str, List[MarketQuote]] = {}
    for quote in quotes:
        if quote.game_id:
            by_game.setdefault(quote.game_id, []).append(quote)

    name_index = (player_features.index_by_name(await _player_forms(season, week))
                  if include_props else {})

    # Step 3: strategies read the adjusted projection against real prices.
    signals: Dict[str, List[Signal]] = {}
    multipliers, _ = ensemble.performance_weights()
    ensembles: Dict[str, List[Signal]] = {}

    for game in slate:
        ctx = contexts[game.game_id]
        ctx.quotes = by_game.get(game.game_id, [])
        rows = run_strategies(ctx, artifact, name_index, include_props=include_props,
                              consensus=consensus.get(game.game_id))
        signals[game.game_id] = rows
        ensembles[game.game_id] = ensemble.combine(rows, multipliers=multipliers)

    return SlateAnalysis(
        season=season, week=week, games=slate, contexts=contexts, signals=signals,
        ensemble=ensembles, quotes=quotes, board_stats=board_stats, ratings=ratings,
        model_summary=artifact.summary(), warnings=warnings,
        book_consensus={gid: row.to_dict() for gid, row in consensus.items()},
    )


_analysis_cache = AsyncTTLCache(ttl=180, stale_ttl=1800, name="analysis")
_game_cache = AsyncTTLCache(ttl=180, stale_ttl=1800, name="game-analysis")


async def cached_analysis(*, include_props: bool = False,
                          season: Optional[int] = None,
                          week: Optional[int] = None) -> SlateAnalysis:
    """Slate analysis with a short TTL, so a dashboard refresh is cheap.

    Kalshi order books move by the second, but the model does not — 3 minutes keeps the
    board honest without hammering the venue on every page load.
    """
    key = f"{season or 'cur'}:{week or 'cur'}:{int(include_props)}"
    entry = await _analysis_cache.get(
        key, lambda: analyze(season=season, week=week, include_props=include_props))
    if entry.stale:
        entry.value.warnings.append(
            "Showing the last successful analysis — the live refresh failed.")
    return entry.value


async def game_analysis(game_id: str, *, include_props: bool = True) -> SlateAnalysis:
    """Analyse ONE game, pricing only that game's contracts.

    A game page used to run the whole-slate analysis, which with player props meant reading
    order books for every prop on every game — about 3,500 HTTP calls to render one page,
    and 126 seconds on a cold cache. Scoping discovery to a single game cuts that to a few
    hundred contracts, because a market that maps to a different game is dropped before it
    is ever priced.
    """
    season, week = await current_week()
    games = await nflverse.schedule(seasons=[season])
    match = next((g for g in games if g.game_id == game_id), None)
    if match is None:
        # A game from an adjacent season (a January playoff) still has to resolve.
        for other in (season - 1, season + 1):
            games = await nflverse.schedule(seasons=[other])
            match = next((g for g in games if g.game_id == game_id), None)
            if match:
                season = other
                break
    if match is None:
        raise NotFound(f"no scheduled game with id {game_id}")

    key = f"{game_id}:{int(include_props)}"
    entry = await _game_cache.get(
        key, lambda: analyze(season=match.season, week=match.week, games=[match],
                             include_props=include_props))
    if entry.stale:
        entry.value.warnings.append(
            "Showing the last successful analysis for this game — the live refresh failed.")
    return entry.value


def invalidate() -> None:
    _analysis_cache.invalidate()
    _game_cache.invalidate()
    _ratings_cache.invalidate()


def best_opportunities(analysis: SlateAnalysis, *, limit: int = 15,
                       min_confidence: Confidence = Confidence.LOW) -> List[Signal]:
    """The slate's ranked edges, after every discipline gate."""
    order = {Confidence.REFERENCE: 0, Confidence.LOW: 1,
             Confidence.MEDIUM: 2, Confidence.HIGH: 3}
    floor = order[min_confidence]
    picked = [s for s in analysis.all_ensemble()
              if s.features.get("value") and order.get(s.confidence, 0) >= floor
              and s.ev_per_dollar is not None]
    picked.sort(key=lambda s: (order.get(s.confidence, 0), s.ev_per_dollar or 0),
                reverse=True)
    return picked[:limit]


def prediction_rows(analysis: SlateAnalysis) -> List[Dict[str, Any]]:
    """Ensemble signals shaped for persistence — written before any outcome exists."""
    rows: List[Dict[str, Any]] = []
    for game_id, ensemble_signals in analysis.ensemble.items():
        game = analysis.contexts[game_id].game
        for signal in ensemble_signals:
            if signal.quote is None or signal.market_prob is None:
                continue
            rows.append({
                "season": analysis.season, "week": analysis.week,
                "game_id": game_id, "kickoff": game.kickoff,
                "strategy": signal.strategy,
                "market_type": signal.market_type.value,
                "ticker": signal.quote.ticker,
                "selection": signal.selection, "label": signal.label,
                "team": signal.team, "player": signal.player, "line": signal.line,
                "model_prob": signal.model_prob,
                "fair_prob": signal.features.get("fair_prob"),
                "market_prob": signal.market_prob,
                "edge": signal.edge,
                "ev_per_dollar": signal.ev_per_dollar,
                "confidence": signal.confidence.value,
                "cost": signal.features.get("cost"),
                "depth_usd": signal.quote.depth_usd,
                "reasoning": signal.reasoning,
            })
    return rows


def per_strategy_rows(analysis: SlateAnalysis) -> List[Dict[str, Any]]:
    """Individual strategy signals for persistence, so each earns its own track record."""
    rows: List[Dict[str, Any]] = []
    for game_id, strategy_signals in analysis.signals.items():
        game = analysis.contexts[game_id].game
        for signal in strategy_signals:
            if (signal.quote is None or signal.market_prob is None
                    or signal.confidence is Confidence.REFERENCE):
                continue
            rows.append({
                "season": analysis.season, "week": analysis.week,
                "game_id": game_id, "kickoff": game.kickoff,
                "strategy": signal.strategy,
                "market_type": signal.market_type.value,
                "ticker": signal.quote.ticker,
                "selection": signal.selection, "label": signal.label,
                "team": signal.team, "player": signal.player, "line": signal.line,
                "model_prob": signal.model_prob,
                "fair_prob": signal.features.get("fair_prob"),
                "market_prob": signal.market_prob,
                "edge": signal.edge,
                "ev_per_dollar": signal.ev_per_dollar,
                "confidence": signal.confidence.value,
                "cost": signal.features.get("cost"),
                "depth_usd": signal.quote.depth_usd,
                "reasoning": signal.reasoning[:4],
            })
    return rows


def build_simulator(analysis: SlateAnalysis) -> "SlateSimulator":
    """A slate simulator primed with every game's joint score distribution."""
    from app.parlay.simulation import SlateSimulator

    artifact = calibration.load()
    correlation = artifact.margin_total_correlation if artifact else 0.0
    simulator = SlateSimulator(correlation=correlation)
    for game_id, ctx in analysis.contexts.items():
        simulator.register(game_id, ctx.projection, ctx.game.home)
    return simulator


def home_team_map(analysis: SlateAnalysis) -> Dict[str, str]:
    return {game_id: ctx.game.home for game_id, ctx in analysis.contexts.items()}


async def parlays(*, include_props: bool = False,
                  category: Optional[str] = None) -> Dict[str, Any]:
    """Build the parlay board for the current slate."""
    from app.parlay import builder

    analysis = await cached_analysis(include_props=include_props)
    simulator = build_simulator(analysis)
    homes = home_team_map(analysis)
    signals = analysis.all_ensemble()

    if category:
        result = [builder.build(signals, simulator, homes, category_key=category,
                                allow_props=include_props, top_n=3)]
    else:
        result = builder.build_all(signals, simulator, homes, allow_props=include_props)
    return {
        "season": analysis.season, "week": analysis.week,
        "categories": result,
        "built_at": analysis.built_at,
        "warnings": analysis.warnings,
        "disclaimer": (
            "Parlays multiply payout and multiply the ways to lose. Every combination here "
            "is built only from legs that are positive on their own and priced with "
            "correlation simulated from each game's score distribution — that makes them "
            "honest, not safe."
        ),
    }
