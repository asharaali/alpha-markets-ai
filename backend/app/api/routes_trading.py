"""Parlays, portfolio, execution, bankroll and performance endpoints."""
from __future__ import annotations

import time
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
from app.risk import fees as fee_model
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
    """Two lists, answering two different questions.

    `high_win_rate`: bets likely to win (65%+, and the market agrees) that are still priced
    fairly after fees, one per game, each with a plain-language breakdown. A likely winner
    at a losing price is refused here like anywhere else.

    `opportunities`: the best value on the board, which is often a less likely bet. Only
    cards that make money after fees, and one line per bet rather than every ladder rung.
    """
    from app.data import teams
    from app.strategies import explain
    from app.strategies import recommendation as rec

    analysis = await engine.cached_analysis()
    user = getattr(request.state, "user", None) if request else None

    if user:
        risk, _ = await execution.live_risk(user)
        exposure = risk_bankroll.current_exposure(user)
    else:
        risk = exposure = None

    def build_card(signal):
        context = analysis.contexts.get(signal.game_id)
        game = context.game if context else None
        sizing = (risk_bankroll.size_bet(signal, risk, exposure)
                  if risk is not None else None)
        missing = []
        if context is not None:
            missing = list(getattr(context, "missing_notes", None) or [])
            if not context.injuries or not any(context.injuries.values()):
                missing.append(
                    "No injury report reached this game. Availability is unknown, not "
                    "assumed healthy.")
            if context.weather is None:
                missing.append("No kickoff weather forecast was available.")
        card = rec.build(
            signal,
            matchup=(f"{teams.display(game.away)} at {teams.display(game.home)}"
                     if game else signal.game_id),
            kickoff=game.kickoff if game else None,
            sizing=sizing, quote_age=analysis.quote_age_seconds
            if hasattr(analysis, "quote_age_seconds") else None,
            missing=missing,
            model_version=getattr(analysis, "model_version", None))
        if card is not None:
            card.breakdown = explain.breakdown(card, signal, context)
        return card

    # A wide pull, because filtering happens after fees on the built card.
    value_cards = [c for c in (build_card(s) for s in
                               engine.best_opportunities(analysis, limit=200)) if c]
    ordered = rec.one_per_bet(rec.rank(rec.profitable(value_cards)))[:limit]

    likely_cards = [c for c in (build_card(s) for s in engine.likely_candidates(analysis))
                    if c]
    high_win = rec.rank_high_win_rate(likely_cards)[:8]

    return {
        "season": analysis.season, "week": analysis.week,
        "high_win_rate": [c.to_dict() for c in high_win],
        "high_win_rate_count": len(high_win),
        "high_win_rate_basis": (
            f"Win probability {rec.HIGH_WIN_MIN_PROB:.0%}-{rec.HIGH_WIN_MAX_PROB:.0%}, the "
            "market also rates it likely, and the price still makes money after Kalshi's "
            "fee. One per game, most likely first."),
        "high_win_rate_empty_reason": (None if high_win else (
            "No likely bet is priced fairly right now. The favourites on this board cost "
            "more than their chance of winning is worth, and paying that loses money "
            "slowly. Check back after the injury reports land Wed-Fri, when prices move.")),
        "opportunities": [c.to_dict() for c in ordered],
        "count": len(ordered),
        "empty_reason": (None if ordered else rec.empty_reason(
            len(analysis.all_bets()), {})),
        "ranking_basis": (
            "Expected value after fees, tempered by hit probability, with edges that do "
            "not survive a 3% model error ranked below those that do. One line per bet. "
            "NOT by win probability and NOT by raw edge."),
        "warnings": analysis.warnings,
        "disclaimer": ("A model edge is an estimate, not a promise. Every one of these can "
                       "lose. Held-out evaluation shows this model does not beat closing "
                       "sportsbook lines — see Performance before sizing anything."),
    }


@router.get("/api/evaluation")
async def evaluation(seasons: Optional[str] = None):
    """The chronological walk-forward evaluation: baselines, ablations, and a verdict."""
    from app.evaluation import report as evaluation_report

    if seasons:
        wanted = [int(s) for s in seasons.split(",") if s.strip().isdigit()]
    else:
        wanted = [settings.SEASON - 4, settings.SEASON - 3, settings.SEASON - 2,
                  settings.SEASON - 1]
    wanted = [s for s in wanted if s >= 1999]
    if len(wanted) < 3:
        raise ValidationError("at least three seasons are needed for a chronological split")
    return await evaluation_report.run(seasons=wanted)


# Which statuses mean "I still hold something". Named once so the portfolio, the risk
# ledger and settlement cannot drift apart about what "open" means.
HOLDING = ("open", "partially_closed", "filled", "partially_filled")
FINISHED = ("closed", "settled", "voided")


@router.get("/api/portfolio")
async def portfolio(request: Request):
    """Positions split by what they actually are, with paper and live kept apart.

    Paper and live are separated at every level — balances, exposure, realised P&L — because
    combining them produces a track record that is neither. A simulated win is not money.
    """
    user = require_user(request)
    positions = store.positions_for(user)

    def summarise(mode: str) -> Dict[str, Any]:
        rows = [p for p in positions if p["mode"] == mode]
        holding = [p for p in rows if p["status"] in HOLDING]
        finished = [p for p in rows if p["status"] in FINISHED]
        exposure = sum(
            (int(p.get("filled_contracts") or p["contracts"])
             - int(p.get("closed_contracts") or 0)) * float(p["entry_price"])
            for p in holding)
        return {
            "mode": mode,
            "simulated": mode == "paper",
            "open_positions": holding,
            "closed_positions": finished[:100],
            "open_count": len(holding),
            "open_exposure": round(exposure, 2),
            "realised_pnl": round(sum(float(p.get("pnl") or 0) for p in finished), 2),
            "fees_paid": round(sum(float(p.get("entry_fees") or 0)
                                   + float(p.get("exit_fees") or 0) for p in rows), 2),
        }

    status = await execution.account_status(user)
    risk, live = await execution.live_risk(user)
    return {
        "user": user,
        "risk": risk_bankroll.portfolio(user, risk, live),
        "paper": summarise("paper"),
        "live": summarise("live"),
        "parlays": store.parlays_for(user),
        "execution": status,
        "status_legend": {
            "submitted": "sent to the venue, no fill confirmed yet",
            "partially_filled": "some contracts filled, the rest did not",
            "filled": "fully filled",
            "open": "held, game not finished",
            "partially_closed": "some contracts sold, the rest still held",
            "closed": "sold out before settlement",
            "settled": "the game finished and the contract paid $1 or $0",
            "rejected": "the venue refused the order; nothing is held",
            "voided": "the market voided; the stake was returned",
        },
        "separation_note": (
            "Paper and live are reported separately and never summed. A paper result is a "
            "simulation against the real book; it is not money and does not belong in the "
            "same total as money."),
    }


@router.post("/api/orders/preflight")
async def preflight(body: schemas.PreflightRequest, request: Request):
    """Re-check a recommendation against the live book WITHOUT placing anything.

    The bet slip calls this before showing a confirm button, so the price on the ticket is
    the price the exchange is actually showing rather than whatever was cached when the
    page loaded.
    """
    require_user(request)
    from app.core.types import Side

    side = Side.NO if body.side == "no" else Side.YES
    cost, depth = await execution.read_price(body.ticker, side)
    if cost is None:
        return {"ok": False, "reason": "no_price", "cost": None, "depth_usd": depth,
                "message": "There is no live price on that contract right now."}

    contracts = int(body.stake / cost)
    ok, why = execution.edge_still_available(
        live_cost=cost, model_prob=body.model_prob,
        recommended_cost=body.recommended_cost,
        max_entry_price=body.max_entry_price, contracts=max(contracts, 1))
    fee = fee_model.trading_fee(contracts=max(contracts, 0), price=cost)
    net_ev = (fee_model.expected_value_after_fees(
        prob=body.model_prob, cost=cost, contracts=max(contracts, 1))
        if body.model_prob is not None else None)

    return {
        "ok": ok,
        "reason": None if ok else "edge_gone",
        "message": why,
        "cost": round(cost, 4),
        "contracts": contracts,
        "stake": round(contracts * cost, 2),
        "estimated_fee": round(fee, 2),
        "ev_after_fees": round(net_ev, 4) if net_ev is not None else None,
        "breakeven_probability": fee_model.breakeven_probability(
            cost, contracts=max(contracts, 1)),
        "depth_usd": round(depth, 2),
        "depth_covers_stake": depth >= body.stake,
        "checked_at": time.time(),
    }


@router.post("/api/orders")
async def place_order(body: schemas.PlaceOrderRequest, request: Request):
    """Place one order. Paper unless every live-trading gate is satisfied.

    The edge is recomputed against the live book inside execution.place() immediately
    before submitting, so a price that moved between the recommendation and the click is
    refused rather than filled.
    """
    user = require_user(request)
    fill = await execution.place(
        username=user, ticker=body.ticker, side=body.side, stake=body.stake,
        requested_mode=body.mode, game_id=body.game_id, label=body.label,
        market_type=body.market_type, model_prob=body.model_prob,
        team=body.team, line=body.line, selection=body.selection,
        recommended_cost=body.recommended_cost,
        max_entry_price=body.max_entry_price,
        recommendation_id=body.recommendation_id,
        model_version=body.model_version)
    return fill.to_dict()


@router.post("/api/positions/reconcile")
async def reconcile(request: Request):
    """Compare live positions against the exchange. The exchange is the source of truth."""
    user = require_user(request)
    return await execution.reconcile_open_positions(user)


@router.post("/api/parlays/place")
async def place_parlay(body: schemas.PlaceParlayRequest, request: Request):
    """Buy a multi-leg position as the product the caller actually named.

    `product` has no default because the two products pay differently, and treating them
    as one was the original defect: the board priced an all-or-nothing parlay by
    multiplying leg costs while this endpoint split the stake across separate singles.

      basket_of_singles  N separate contracts. Payouts ADD. A partial result still pays.
      kalshi_combo       One real combination contract from a multivariate event
                         collection, with its own ticker and book. Requires a quote.

    There is no code path that places a hypothetical parlay, because there is nothing to
    place — no venue has quoted it.
    """
    user = require_user(request)

    if body.product == "kalshi_combo":
        if not body.combo_ticker:
            raise ValidationError(
                "a Kalshi combination order needs the combo market's ticker; look it up "
                "through /api/parlays/quote first")
        parlay_id = store.save_parlay(
            user=user, mode=body.mode, category=body.category,
            legs=[leg.model_dump() for leg in body.legs], leg_count=len(body.legs),
            combined_prob=body.combined_prob, combined_odds=body.combined_odds,
            ev_per_dollar=body.ev_per_dollar, risk_rating=body.risk_rating,
            stake=body.stake, product="kalshi_combo", executable=1,
            pricing_basis="live_combo_order_book")
        fill = await execution.place(
            username=user, ticker=body.combo_ticker, side="yes", stake=body.stake,
            requested_mode=body.mode, label=f"{len(body.legs)}-leg combination",
            market_type="combination", model_prob=body.combined_prob,
            parlay_id=parlay_id, selection=f"{len(body.legs)}-leg combination")
        return {
            "parlay_id": parlay_id,
            "product": "kalshi_combo",
            "payout_is_additive": False,
            "legs": [fill.to_dict()],
            "filled": 1 if fill.ok else 0,
            "requested": 1,
            "complete": fill.ok,
            "note": ("Bought as ONE combination contract. It pays only if every leg "
                     "lands, and pays nothing otherwise."),
        }

    # Basket of singles: N orders, additive payouts, partial results are real results.
    per_leg = body.stake / len(body.legs)
    fills = []
    parlay_id = store.save_parlay(
        user=user, mode=body.mode, category=body.category,
        legs=[leg.model_dump() for leg in body.legs], leg_count=len(body.legs),
        combined_prob=body.combined_prob, combined_odds=body.combined_odds,
        ev_per_dollar=body.ev_per_dollar, risk_rating=body.risk_rating,
        stake=body.stake, product="basket_of_singles", executable=1,
        pricing_basis="live_order_books")

    for leg in body.legs:
        fill = await execution.place(
            username=user, ticker=leg.ticker, side=leg.side, stake=per_leg,
            requested_mode=body.mode, game_id=leg.game_id, label=leg.label,
            market_type=leg.market_type, model_prob=leg.model_prob,
            parlay_id=parlay_id, selection=leg.label, skip_edge_check=True)
        fills.append(fill.to_dict())

    filled = sum(1 for f in fills if f["ok"])
    payout_if_all_win = sum(f["contracts"] for f in fills if f["ok"])
    spent = sum(f["stake"] for f in fills if f["ok"])
    return {
        "parlay_id": parlay_id,
        "product": "basket_of_singles",
        "payout_is_additive": True,
        "legs": fills,
        "filled": filled,
        "requested": len(fills),
        "complete": filled == len(fills),
        "staked": round(spent, 2),
        "max_payout": round(payout_if_all_win, 2),
        "note": (
            f"Bought as {filled} SEPARATE contracts, not a parlay. Each leg pays on its "
            f"own, so a partial result still returns money. If every leg wins this returns "
            f"${payout_if_all_win:.2f} on ${spent:.2f} staked — far less than multiplying "
            "the leg prices would suggest, because that is a different product."
            if filled == len(fills) else
            f"Only {filled} of {len(fills)} legs filled. You hold {filled} separate "
            "contracts; review them before treating this as one bet."),
    }


@router.post("/api/parlays/quote")
async def quote_parlay(body: schemas.PlaceParlayRequest):
    """Is there a REAL Kalshi combination market for these legs?

    Returns the genuine quote when one exists, and an honest reason when it does not.
    Nothing here invents a price.
    """
    from app.data.kalshi import combos

    legs = [{"market_ticker": leg.ticker, "event_ticker": leg.ticker.split("-")[0]}
            for leg in body.legs]
    combo, reason = await combos.find_quote(legs)
    if combo is None or not combo.tradeable:
        return {
            "executable": False,
            "reason": reason,
            "capability": combos.capability_note(),
            "alternative": ("These legs can still be bought as a basket of separate "
                            "single contracts, which pays additively rather than "
                            "all-or-nothing."),
        }
    return {
        "executable": True,
        "ticker": combo.ticker,
        "collection_ticker": combo.collection_ticker,
        "cost": combo.yes_ask,
        "bid": combo.yes_bid,
        "depth_usd": combo.depth_usd,
        "quote_age_seconds": round(combo.age_seconds, 1),
        "capability": combos.capability_note(),
    }


@router.post("/api/positions/close")
async def close_position(body: schemas.ClosePositionRequest, request: Request):
    """Close some or all of a position. The remainder stays open."""
    user = require_user(request)
    return await execution.close(username=user, position_id=body.position_id,
                                 contracts=body.contracts)


@router.get("/api/bankroll")
async def get_bankroll(request: Request):
    user = require_user(request)
    risk, live = await execution.live_risk(user)
    return risk_bankroll.portfolio(user, risk, live)


@router.post("/api/bankroll")
async def update_bankroll(body: schemas.BankrollRequest, request: Request):
    user = require_user(request)
    config = {k: v for k, v in body.model_dump().items()
              if v is not None and k not in ("starting", "current")}
    store.set_bankroll(user, starting=body.starting, current=body.current,
                       config=config or None)
    risk, live = await execution.live_risk(user)
    return risk_bankroll.portfolio(user, risk, live)


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
