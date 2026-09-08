"""Parlays, portfolio, execution, bankroll and performance endpoints."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query, Request

from app import engine
from app.api import schemas
from app.api.deps import require_user
from app.config import settings
from app.core.errors import NotFound, ValidationError
from app.core.types import MarketType
from app.data.kalshi import execution
from app.risk import bankroll as risk_bankroll
from app.tracking import metrics, store

router = APIRouter()


@router.get("/api/parlays")
async def parlays(category: Optional[str] = None,
                  include_props: bool = Query(default=False)):
    """The parlay board: conservative, balanced, aggressive, and the model's best."""
    return await engine.parlays(include_props=include_props, category=category)


@router.get("/api/opportunities")
async def opportunities(limit: int = Query(default=15, le=100),
                        request: Request = None):
    """Ranked edges with a recommended stake for the logged-in user's bankroll."""
    analysis = await engine.cached_analysis()
    picks = engine.best_opportunities(analysis, limit=limit)
    user = getattr(request.state, "user", None) if request else None

    sized: List[Dict[str, Any]] = []
    if user:
        risk = risk_bankroll.RiskSettings.load(user)
        exposure = risk_bankroll.current_exposure(user)
    else:
        risk = exposure = None

    for signal in picks:
        row = signal.to_dict()
        row["kickoff"] = analysis.contexts[signal.game_id].game.kickoff
        if risk is not None:
            row["sizing"] = risk_bankroll.size_bet(signal, risk, exposure)
        sized.append(row)

    return {
        "season": analysis.season, "week": analysis.week,
        "opportunities": sized,
        "count": len(sized),
        "empty_reason": (None if sized else
                         "Nothing on this board clears the value gate right now. That is a "
                         "normal result, not a failure — most slates have no edge worth "
                         "taking on liquid markets."),
        "warnings": analysis.warnings,
        "disclaimer": ("A model edge is an estimate, not a promise. Every one of these can "
                       "lose."),
    }


@router.get("/api/portfolio")
async def portfolio(request: Request):
    user = require_user(request)
    positions = store.positions_for(user)
    open_positions = [p for p in positions if p["status"] == "open"]
    closed = [p for p in positions if p["status"] == "closed"]
    return {
        "user": user,
        "risk": risk_bankroll.portfolio(user),
        "open_positions": open_positions,
        "closed_positions": closed[:100],
        "parlays": store.parlays_for(user),
        "realised_pnl": round(sum(float(p.get("pnl") or 0) for p in closed), 2),
        "execution": await execution.account_status(user),
    }


@router.post("/api/orders")
async def place_order(body: schemas.PlaceOrderRequest, request: Request):
    """Place one order. Paper unless every live-trading gate is satisfied."""
    user = require_user(request)
    fill = await execution.place(
        username=user, ticker=body.ticker, side=body.side, stake=body.stake,
        requested_mode=body.mode, game_id=body.game_id, label=body.label,
        market_type=body.market_type, model_prob=body.model_prob)
    return fill.to_dict()


@router.post("/api/parlays/place")
async def place_parlay(body: schemas.PlaceParlayRequest, request: Request):
    """Record a parlay and place each leg.

    Kalshi has no native parlay product: a multi-leg bet is placed as separate contracts,
    and the combined payout only materialises if every leg wins. The stake is split evenly
    across legs, and any leg that fails to fill is reported rather than hidden — a partial
    parlay is a different bet from the one that was recommended, and you need to know.
    """
    user = require_user(request)
    per_leg = body.stake / len(body.legs)
    fills = []
    parlay_id = store.save_parlay(
        user=user, mode=body.mode, category=body.category,
        legs=[leg.model_dump() for leg in body.legs], leg_count=len(body.legs),
        combined_prob=body.combined_prob, combined_odds=body.combined_odds,
        ev_per_dollar=body.ev_per_dollar, risk_rating=body.risk_rating,
        stake=body.stake)

    for leg in body.legs:
        fill = await execution.place(
            username=user, ticker=leg.ticker, side=leg.side, stake=per_leg,
            requested_mode=body.mode, game_id=leg.game_id, label=leg.label,
            market_type=leg.market_type, model_prob=leg.model_prob,
            parlay_id=parlay_id)
        fills.append(fill.to_dict())

    filled = sum(1 for f in fills if f["ok"])
    return {
        "parlay_id": parlay_id,
        "legs": fills,
        "filled": filled,
        "requested": len(fills),
        "complete": filled == len(fills),
        "note": (
            "All legs placed." if filled == len(fills) else
            f"Only {filled} of {len(fills)} legs filled. The positions you now hold are "
            "NOT the parlay that was recommended — review them before treating this as a "
            "single bet."),
    }


@router.post("/api/positions/close")
async def close_position(body: schemas.ClosePositionRequest, request: Request):
    user = require_user(request)
    return await execution.close(username=user, position_id=body.position_id)


@router.get("/api/bankroll")
async def get_bankroll(request: Request):
    user = require_user(request)
    return risk_bankroll.portfolio(user)


@router.post("/api/bankroll")
async def update_bankroll(body: schemas.BankrollRequest, request: Request):
    user = require_user(request)
    config = {k: v for k, v in body.model_dump().items()
              if v is not None and k not in ("starting", "current")}
    store.set_bankroll(user, starting=body.starting, current=body.current,
                       config=config or None)
    return risk_bankroll.portfolio(user)


@router.get("/api/performance")
async def performance(strategy: Optional[str] = None,
                      market_type: Optional[str] = None):
    """The live track record. Predictions were stored before outcomes and never edited."""
    rows = store.settled_predictions(strategy=strategy, market_type=market_type)
    pending = store.pending_predictions()
    counts = store.stats_snapshot()
    return {
        "overall": metrics.summarise(rows, label="all predictions"),
        "by_strategy": metrics.by_group(rows, "strategy"),
        "by_market": metrics.by_group(rows, "market_type"),
        "by_confidence": metrics.by_group(rows, "confidence"),
        "equity_curve": metrics.equity_curve(rows),
        "pending_predictions": len(pending),
        "counts": counts,
        "integrity": (
            "Every prediction is written to the database at the moment it is made, with the "
            "market price that was showing then, and its outcome column starts empty. "
            "Grading only ever fills the outcome in — no stored probability is ever "
            "rewritten. Losing predictions are included here; there is no path in the code "
            "that removes one."
        ),
        "empty_reason": (None if rows else
                         f"No predictions have settled yet. {len(pending)} are recorded and "
                         "waiting on results; this page fills in as games finish."),
    }


@router.post("/api/backtest")
async def run_backtest(body: schemas.BacktestRequest):
    """Walk-forward backtest against closing sportsbook lines."""
    from app.backtest import engine as backtest_engine

    if any(s < 1999 or s > settings.SEASON for s in body.seasons):
        raise ValidationError("seasons must be between 1999 and the current season")
    return await backtest_engine.run(seasons=body.seasons, start_week=body.start_week,
                                     markets=body.markets, blend_weight=body.blend_weight)


@router.get("/api/backtest/default")
async def default_backtest():
    """The standing backtest shown on the Backtests page, over recent seasons."""
    from app.backtest import engine as backtest_engine

    seasons = [settings.SEASON - 3, settings.SEASON - 2, settings.SEASON - 1]
    return await backtest_engine.run(seasons=[s for s in seasons if s >= 1999])
