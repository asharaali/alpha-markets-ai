"""Read-only research endpoints: games, predictions, markets, strategies, model lab."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query, Request

from app import engine
from app.core.errors import NotFound
from app.core.types import Confidence, MarketType
from app.data import teams
from app.data.kalshi import series as kalshi_series
from app.models import calibration, ratings as ratings_model
from app.strategies import (ensemble, game_lines, injury_impact, line_movement,
                            matchup, mispricing, props, situational)
from app.tracking import store

router = APIRouter()

# Every strategy's self-description, so the UI never invents methodology text.
STRATEGY_META = [
    game_lines.MONEYLINE_META, game_lines.SPREAD_META, game_lines.TOTAL_META,
    game_lines.WIN_MARGIN_META, matchup.META, injury_impact.META, situational.META,
    line_movement.META, mispricing.META, props.META,
]


def _slate_row(analysis: engine.SlateAnalysis, game_id: str) -> Dict[str, Any]:
    ctx = analysis.contexts[game_id]
    signals = analysis.ensemble.get(game_id, [])
    best = max((s for s in signals if s.features.get("value")),
               key=lambda s: s.ev_per_dollar or 0, default=None)
    moneylines = [s for s in signals if s.market_type is MarketType.MONEYLINE]
    return {
        "game": ctx.game.to_dict(),
        "teams": {"home": teams.to_dict(ctx.game.home),
                  "away": teams.to_dict(ctx.game.away)},
        "projection": ctx.projection.to_dict(),
        "adjustment": ctx.adjustment.to_dict(),
        "weather": ctx.weather,
        "market_count": len(ctx.quotes),
        "signal_count": len(signals),
        "value_count": sum(1 for s in signals if s.features.get("value")),
        "moneyline": [s.to_dict() for s in moneylines],
        "best_opportunity": best.to_dict() if best else None,
    }


@router.get("/api/slate")
async def slate(week: Optional[int] = None, season: Optional[int] = None):
    """The current NFL slate with model projections and a summary of each game's edges."""
    analysis = await engine.cached_analysis(season=season, week=week)
    return {
        "season": analysis.season, "week": analysis.week,
        "built_at": analysis.built_at,
        "games": [_slate_row(analysis, g.game_id) for g in analysis.games],
        "board": analysis.board_stats,
        "ratings": {
            "seasons_used": analysis.ratings.seasons_used,
            "effective_games_per_team": analysis.ratings.effective_games_per_team,
            "confidence": analysis.ratings.confidence(),
        },
        "warnings": analysis.warnings,
    }


@router.get("/api/games/{game_id}")
async def game_detail(game_id: str, include_props: bool = Query(default=True)):
    """Everything the system knows about one game.

    Scoped to this game rather than reusing the slate analysis: with player props included
    the slate-wide path prices every prop on every game, which is thousands of order-book
    reads to render a single page.
    """
    analysis = await engine.game_analysis(game_id, include_props=include_props)
    ctx = analysis.contexts.get(game_id)
    if ctx is None:
        raise NotFound(f"no analysis available for game {game_id}")

    signals = analysis.signals.get(game_id, [])
    ensembled = analysis.ensemble.get(game_id, [])
    injury_detail = ctx.adjustment.detail.get("home"), ctx.adjustment.detail.get("away")

    return {
        "game": ctx.game.to_dict(),
        "teams": {"home": teams.to_dict(ctx.game.home),
                  "away": teams.to_dict(ctx.game.away)},
        "projection": ctx.projection.to_dict(),
        "base_projection": ctx.base_projection.to_dict(),
        "adjustment": ctx.adjustment.to_dict(),
        "injuries": {
            "home": [r.to_dict() for r in ctx.injuries.get(ctx.game.home, [])],
            "away": [r.to_dict() for r in ctx.injuries.get(ctx.game.away, [])],
            "impact": {"home": injury_detail[0], "away": injury_detail[1]},
        },
        "situational": situational.context(ctx.game),
        "weather": ctx.weather,
        "team_comparison": _comparison(analysis, ctx),
        "matchup": matchup.matchup_margin(ctx, calibration.load()),
        "markets": [q.to_dict() for q in ctx.quotes],
        "signals": [s.to_dict() for s in signals],
        "ensemble": [s.to_dict() for s in ensembled],
        "opportunities": [s.to_dict() for s in ensembled if s.features.get("value")],
        "mispricing": mispricing.summarise(ctx),
        "line_movement": [
            m for m in (line_movement.movement_for(q.ticker, q.mid) for q in ctx.quotes)
            if m is not None],
        "prediction_history": store.predictions_for_game(game_id, limit=200),
    }


def _comparison(analysis: engine.SlateAnalysis, ctx) -> List[Dict[str, Any]]:
    """Side-by-side opponent-adjusted ratings, with league rank for context."""
    from app.features.aggregate import METRICS

    rows = []
    for metric in METRICS:
        table = ratings_model.rank_table(analysis.ratings, metric)
        ranks = {r["team"]: r["rank"] for r in table}
        home = analysis.ratings.get(ctx.game.home)
        away = analysis.ratings.get(ctx.game.away)
        rows.append({
            "metric": metric,
            "home": {"net": round(home.net(metric), 5), "rank": ranks.get(ctx.game.home),
                     "offense": round(home.offense.get(metric, 0.0), 5),
                     "defense": round(home.defense.get(metric, 0.0), 5)},
            "away": {"net": round(away.net(metric), 5), "rank": ranks.get(ctx.game.away),
                     "offense": round(away.offense.get(metric, 0.0), 5),
                     "defense": round(away.defense.get(metric, 0.0), 5)},
        })
    return rows


@router.get("/api/predictions")
async def predictions(
    min_confidence: str = Query(default="low"),
    market_type: Optional[str] = None,
    game_id: Optional[str] = None,
    strategy: Optional[str] = None,
    value_only: bool = Query(default=True),
    include_props: bool = Query(default=False),
    sort: str = Query(default="ev"),
    limit: int = Query(default=100, le=500),
):
    """Prediction cards, filterable and sortable — the Predictions view."""
    analysis = await engine.cached_analysis(include_props=include_props)
    order = {"reference": 0, "low": 1, "medium": 2, "high": 3}
    floor = order.get(min_confidence.lower(), 1)

    rows = analysis.all_ensemble()
    if strategy and strategy != "ensemble":
        rows = [s for s in analysis.all_signals() if s.strategy == strategy]
    if value_only:
        rows = [s for s in rows if s.features.get("value")]
    if market_type:
        rows = [s for s in rows if s.market_type.value == market_type]
    if game_id:
        rows = [s for s in rows if s.game_id == game_id]
    rows = [s for s in rows if order.get(s.confidence.value, 0) >= floor]

    keys = {
        "ev": lambda s: s.ev_per_dollar or -9,
        "edge": lambda s: s.edge or -9,
        "confidence": lambda s: order.get(s.confidence.value, 0),
        "kickoff": lambda s: analysis.contexts[s.game_id].game.kickoff,
        "market": lambda s: s.market_type.value,
        "game": lambda s: s.game_id,
    }
    key = keys.get(sort, keys["ev"])
    rows.sort(key=key, reverse=sort not in ("kickoff", "market", "game"))

    kickoffs = {gid: ctx.game.kickoff for gid, ctx in analysis.contexts.items()}
    payload = []
    for signal in rows[:limit]:
        row = signal.to_dict()
        row["kickoff"] = kickoffs.get(signal.game_id)
        payload.append(row)
    return {
        "season": analysis.season, "week": analysis.week,
        "count": len(payload), "total_before_limit": len(rows),
        "predictions": payload,
        "filters": {
            "market_types": sorted({s.market_type.value for s in analysis.all_ensemble()}),
            "strategies": sorted({s.strategy for s in analysis.all_signals()}),
            "games": sorted(analysis.contexts),
            "sorts": sorted(keys),
        },
        "warnings": analysis.warnings,
        "empty_reason": _empty_reason(analysis, payload, value_only),
    }


def _empty_reason(analysis, payload, value_only: bool) -> Optional[str]:
    if payload:
        return None
    if not analysis.quotes:
        return ("No Kalshi prices are available right now, so nothing can be priced. "
                "Model projections are still on the game pages.")
    if value_only:
        return ("Nothing on this board clears the value gate: an edge has to be on a "
                "liquid market, beat the expected-return threshold, and sit inside a sane "
                "price band. On most slates that is the correct answer.")
    return "No predictions match these filters."


@router.get("/api/markets")
async def markets(game_id: Optional[str] = None, market_type: Optional[str] = None,
                  include_props: bool = Query(default=False)):
    """The raw Kalshi board as we see it — prices, depth, and what mapped."""
    analysis = await engine.cached_analysis(include_props=include_props)
    quotes = analysis.quotes
    if game_id:
        quotes = [q for q in quotes if q.game_id == game_id]
    if market_type:
        quotes = [q for q in quotes if q.market_type.value == market_type]
    return {
        "season": analysis.season, "week": analysis.week,
        "count": len(quotes),
        "markets": [q.to_dict() for q in quotes],
        "board": analysis.board_stats,
        "series": [{"ticker": s.ticker, "label": s.label, "category": s.category,
                    "market_type": s.market_type.value,
                    "reference_only": s.reference_only}
                   for s in kalshi_series.all_specs()],
        "warnings": analysis.warnings,
    }


@router.get("/api/market-movers")
async def market_movers(limit: int = Query(default=12, le=50)):
    """Biggest recent price moves across the slate."""
    analysis = await engine.cached_analysis()
    movers = line_movement.market_movers(analysis.quotes, limit=limit)
    return {
        "movers": movers,
        "note": ("Line movement needs stored history, and only contracts with real resting "
                 "depth qualify. A freshly started instance has no history and this list "
                 "fills in as snapshots accumulate."
                 if not movers else
                 f"Moves measured against our own stored order-book snapshots, restricted "
                 f"to contracts with at least ${line_movement.MOVER_MIN_DEPTH:,.0f} of "
                 f"resting depth — a swing on an empty book is not a market move."),
        "min_depth_usd": line_movement.MOVER_MIN_DEPTH,
        "snapshot_count": store.stats_snapshot()["market_snapshots"],
    }


@router.get("/api/strategies")
async def strategies():
    """Every strategy's methodology, inputs, limitations and measured record."""
    from app.tracking import metrics

    settled = store.settled_predictions()
    by_strategy = {row["strategy"]: [] for row in settled}
    for row in settled:
        by_strategy[row["strategy"]].append(row)

    return {
        "strategies": [
            {
                "key": meta.key, "name": meta.name,
                "market_types": [m.value for m in meta.market_types],
                "methodology": meta.methodology,
                "inputs": meta.inputs,
                "limitations": meta.limitations,
                "record": metrics.summarise(by_strategy.get(meta.key, []),
                                            label=meta.key),
            }
            for meta in STRATEGY_META
        ],
        "ensemble": ensemble.explain_weights(),
    }


@router.get("/api/model")
async def model_lab():
    """The Model Lab: what was fitted, how well, and the current power ratings."""
    artifact = calibration.load()
    analysis = await engine.cached_analysis()
    return {
        "artifact": artifact.summary() if artifact else None,
        "ratings": {
            "as_of": {"season": analysis.ratings.season, "week": analysis.ratings.week},
            "seasons_used": analysis.ratings.seasons_used,
            "effective_games_per_team": analysis.ratings.effective_games_per_team,
            "confidence": analysis.ratings.confidence(),
            "power": ratings_model.rank_table(analysis.ratings, "epa_per_play"),
            "offense": ratings_model.rank_table(analysis.ratings,
                                                "pass_epa_per_dropback"),
            "points": ratings_model.rank_table(analysis.ratings, "points"),
        },
        "ensemble": ensemble.explain_weights(),
        "pipeline": [
            {"stage": "Raw data",
             "detail": "nflverse play-by-play, schedules, injuries, depth charts, rosters "
                       "and weekly player stats; Open-Meteo forecasts; Kalshi order books."},
            {"stage": "Normalized",
             "detail": "Play-by-play reduced to one row per team per game: EPA per play, "
                       "success rate, pressure, explosiveness, red-zone conversion, "
                       "turnover rate and neutral-script pace."},
            {"stage": "Features",
             "detail": "Ridge-fitted opponent-adjusted offence and defence ratings per "
                       "metric, with recency weighting and shrinkage, built strictly from "
                       "games before the week being predicted."},
            {"stage": "Model",
             "detail": "A fitted second stage maps rating differentials to expected margin "
                       "and total, expanded into key-number-aware discrete distributions."},
            {"stage": "Market",
             "detail": "Kalshi contracts discovered, mapped to games, and priced from the "
                       "order book — never from the market list, which reports null."},
            {"stage": "Strategies",
             "detail": "Independent strategies price each market type and publish "
                       "confidence, reasoning and their own track record."},
            {"stage": "Recommendations",
             "detail": "The ensemble blends strategies in log-odds space, then the value "
                       "gate, risk sizing and the parlay engine decide what is actionable."},
        ],
    }
