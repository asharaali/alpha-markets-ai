"""Settling stored predictions against what actually happened.

Predictions are written before kickoff with the price that was showing at the time, and
this module fills in the outcome afterwards. Nothing else ever touches a stored
probability — that separation is the entire basis for the performance page meaning
anything.

Outcomes come from the nflverse final score, resolved through the same market definitions
the strategies used to make the prediction, so a spread graded here is graded by exactly
the rule that priced it.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.core.logging import get_logger
from app.core.types import MarketType
from app.data import nflverse, teams
from app.data.kalshi import markets as kalshi_markets
from app.core.http import make_client
from app.data.kalshi import orderbook as ob
from app.tracking import store

log = get_logger(__name__)


def _resolve(market_type: str, selection: str, team: Optional[str],
             line: Optional[float], home: str, home_score: int,
             away_score: int) -> Optional[bool]:
    """Did this exact contract pay? None means the market voided or we cannot say."""
    margin = home_score - away_score
    total = home_score + away_score
    is_home = team == home

    if market_type == MarketType.MONEYLINE.value:
        if margin == 0:
            return None                 # tie voids the Kalshi moneyline
        return (margin > 0) if is_home else (margin < 0)

    if market_type == MarketType.SPREAD.value and line is not None:
        # Contracts read "team wins by over N", so a push is impossible on a .5 line and
        # a whole-number line resolves NO on the exact number.
        return (margin > line) if is_home else (-margin > line)

    if market_type == MarketType.TOTAL.value and line is not None:
        return total > line

    if market_type == MarketType.TEAM_TOTAL.value and line is not None:
        points = home_score if is_home else away_score
        return points > line

    if market_type == MarketType.WIN_MARGIN.value:
        lowered = (selection or "").lower()
        signed = margin if is_home else -margin
        if "tie" in lowered:
            return margin == 0
        if "1-6" in lowered:
            return 1 <= signed <= 6
        if "7-14" in lowered:
            return 7 <= signed <= 14
        if "15+" in lowered:
            return signed >= 15
        return None

    # Player props and half markets cannot be settled from a final score alone.
    return None


async def _closing_prices(tickers: List[str]) -> Dict[str, Optional[float]]:
    """Last recorded mid for each ticker — the closing price, for CLV.

    Read from our own snapshot history rather than the venue: after a game resolves the
    market is settled at 0 or 100, which would make every CLV look enormous and meaningless.
    """
    out: Dict[str, Optional[float]] = {}
    for ticker in tickers:
        history = store.snapshot_history(ticker, limit=400)
        out[ticker] = float(history[-1]["mid"]) if history and history[-1].get("mid") else None
    return out


async def grade_finished_games(*, season: Optional[int] = None) -> Dict[str, Any]:
    """Settle every pending prediction whose game has finished."""
    from app.config import settings

    season = season or settings.SEASON
    pending = store.pending_predictions()
    if not pending:
        return {"graded": 0, "voided": 0, "still_pending": 0,
                "note": "No pending predictions to grade."}

    schedule = {g.game_id: g for g in await nflverse.schedule(seasons=[season])}
    # Predictions can outlive a season boundary; pull the prior season too if needed.
    missing = {p["game_id"] for p in pending} - set(schedule)
    if missing:
        for game in await nflverse.schedule(seasons=[season - 1]):
            schedule.setdefault(game.game_id, game)

    closing = await _closing_prices(
        [p["ticker"] for p in pending if p.get("ticker")])

    graded = voided = still_pending = 0
    for row in pending:
        game = schedule.get(row["game_id"])
        if game is None or not game.completed:
            still_pending += 1
            continue
        outcome = _resolve(row["market_type"], row.get("selection") or "",
                           row.get("team"), row.get("line"), game.home,
                           int(game.home_score or 0), int(game.away_score or 0))
        if outcome is None:
            store.void_prediction(row["id"], "market voided or not settleable from score")
            voided += 1
            continue
        store.settle_prediction(row["id"], outcome=outcome,
                                closing_prob=closing.get(row.get("ticker") or ""))
        graded += 1

    if graded or voided:
        log.info("graded %d predictions, voided %d, %d still pending",
                 graded, voided, still_pending)
    return {"graded": graded, "voided": voided, "still_pending": still_pending,
            "note": (f"Settled {graded} prediction(s) from finished games."
                     if graded else "No games have finished since the last grading pass.")}


async def settle_positions(*, season: Optional[int] = None) -> Dict[str, Any]:
    """Close out paper and live positions on games that have finished."""
    from app.config import settings

    season = season or settings.SEASON
    schedule = {g.game_id: g for g in await nflverse.schedule(seasons=[season])}
    settled = 0
    from app.tracking.store import connection, init
    init()
    rows = [dict(r) for r in connection().execute(
        "SELECT * FROM positions WHERE status='open'").fetchall()]

    for position in rows:
        game = schedule.get(position.get("game_id") or "")
        if game is None or not game.completed:
            continue
        outcome = _resolve(position.get("market_type") or "",
                           position.get("label") or "", None, None,
                           game.home, int(game.home_score or 0),
                           int(game.away_score or 0))
        if outcome is None:
            continue
        # A settled binary contract is worth exactly $1 or $0 per contract.
        store.close_position(position["id"], exit_price=1.0 if outcome else 0.0,
                             note="settled at game result")
        settled += 1
    return {"settled_positions": settled}
