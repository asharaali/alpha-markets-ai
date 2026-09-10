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
from typing import Any, Dict, List, Optional, Sequence

from app.core.logging import get_logger
from app.core.types import MarketType
from app.data import nflverse, teams
from app.data.kalshi import markets as kalshi_markets
from app.core.http import make_client
from app.data.kalshi import orderbook as ob
from app.tracking import store

log = get_logger(__name__)


def resolve_contract(*, market_type: str, selection: str, team: Optional[str],
                     line: Optional[float], home: str, home_score: int,
                     away_score: int, side: str = "yes") -> Optional[bool]:
    """Did the holder of this exact contract get paid?

    None means "cannot say" — a voided market, or missing metadata. That distinction is
    the whole fix here. The previous version took `team` and `line` positionally and was
    called from position settlement with None for both, at which point:

      * `is_home = (team == home)` quietly became False, so every HOME moneyline was
        graded against the AWAY team and a winning bet settled as a loss;
      * spreads, totals and team totals fell through to None and never settled at all.

    Returning None for missing metadata is not a regression in coverage. It converts a
    silent wrong answer into a visible unresolved position, which is the only honest
    behaviour when the information needed to grade is absent.

    `side` handles NO positions: the contract's YES outcome is computed first and then
    inverted, because holding NO on a winning YES contract is a loss.
    """
    yes = _resolve_yes(market_type=market_type, selection=selection, team=team,
                       line=line, home=home, home_score=home_score,
                       away_score=away_score)
    if yes is None:
        return None
    return yes if str(side).lower() != "no" else (not yes)


def _resolve_yes(*, market_type: str, selection: str, team: Optional[str],
                 line: Optional[float], home: str, home_score: int,
                 away_score: int) -> Optional[bool]:
    """The YES-side outcome, or None when it cannot be determined."""
    margin = home_score - away_score
    total = home_score + away_score

    # Team-relative markets are unresolvable without knowing whose contract this is.
    needs_team = {MarketType.MONEYLINE.value, MarketType.SPREAD.value,
                  MarketType.TEAM_TOTAL.value, MarketType.WIN_MARGIN.value}
    if market_type in needs_team and not team:
        return None
    is_home = team == home

    if market_type == MarketType.MONEYLINE.value:
        if margin == 0:
            return None                 # tie voids the Kalshi moneyline
        return (margin > 0) if is_home else (margin < 0)

    if market_type == MarketType.SPREAD.value:
        if line is None:
            return None
        # Contracts read "team wins by over N", so a push is impossible on a .5 line and
        # a whole-number line resolves NO on the exact number.
        return (margin > line) if is_home else (-margin > line)

    if market_type == MarketType.TOTAL.value:
        if line is None:
            return None
        return total > line

    if market_type == MarketType.TEAM_TOTAL.value:
        if line is None:
            return None
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


def _resolve(market_type: str, selection: str, team: Optional[str],
             line: Optional[float], home: str, home_score: int,
             away_score: int) -> Optional[bool]:
    """Positional shim for existing callers. Prefer resolve_contract()."""
    return resolve_contract(market_type=market_type, selection=selection, team=team,
                            line=line, home=home, home_score=home_score,
                            away_score=away_score)


def closing_price_from(history: Sequence[Dict[str, Any]],
                       kickoff_ts: Optional[float]) -> Optional[float]:
    """The last mid recorded BEFORE kickoff, which is what "closing price" means.

    Taking the final snapshot outright was the defect. Snapshot capture does not stop when
    a game starts, so the last row was routinely an in-game price — a team up two scores
    trades at 97c — and comparing a 55c entry against that manufactured 42 points of
    closing-line value out of nothing but the game being played. CLV is supposed to
    measure whether the model saw something the market had not yet priced; measuring
    against a price that already knows the result measures nothing.

    Returns None rather than guessing when kickoff is unknown or no pregame snapshot
    exists. A missing CLV is honest; a fabricated one poisons the only metric that
    distinguishes edge from variance.
    """
    if kickoff_ts is None:
        return None
    pregame = [row for row in history
               if row.get("mid") is not None
               and row.get("captured_at") is not None
               and float(row["captured_at"]) <= float(kickoff_ts)]
    if not pregame:
        return None
    pregame.sort(key=lambda r: float(r["captured_at"]))
    return float(pregame[-1]["mid"])


def _kickoff_timestamp(value: Any) -> Optional[float]:
    """Kickoff as a POSIX timestamp, from either an ISO string or a number."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        text = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (ValueError, TypeError):
        return None


async def _closing_prices(rows: Sequence[Dict[str, Any]]) -> Dict[str, Optional[float]]:
    """Closing mid per ticker, cut off at that ticker's own kickoff.

    Keyed per prediction row rather than per ticker because two predictions on the same
    ticker can carry different kickoffs after a reschedule.
    """
    out: Dict[str, Optional[float]] = {}
    cache: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        ticker = row.get("ticker")
        if not ticker:
            continue
        if ticker not in cache:
            cache[ticker] = store.snapshot_history(ticker, limit=2000)
        kickoff = _kickoff_timestamp(row.get("kickoff_ts") or row.get("kickoff"))
        out[row["id"]] = closing_price_from(cache[ticker], kickoff)
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

    closing = await _closing_prices(pending)

    graded = voided = still_pending = 0
    for row in pending:
        game = schedule.get(row["game_id"])
        if game is None or not game.completed:
            still_pending += 1
            continue
        outcome = resolve_contract(
            market_type=row["market_type"], selection=row.get("selection") or "",
            team=row.get("team"), line=row.get("line"), home=game.home,
            home_score=int(game.home_score or 0),
            away_score=int(game.away_score or 0))
        if outcome is None:
            store.void_prediction(row["id"], "market voided or not settleable from score")
            voided += 1
            continue
        store.settle_prediction(row["id"], outcome=outcome,
                                closing_prob=closing.get(row["id"]))
        graded += 1

    if graded or voided:
        log.info("graded %d predictions, voided %d, %d still pending",
                 graded, voided, still_pending)
    return {"graded": graded, "voided": voided, "still_pending": still_pending,
            "note": (f"Settled {graded} prediction(s) from finished games."
                     if graded else "No games have finished since the last grading pass.")}


async def settle_positions(*, season: Optional[int] = None) -> Dict[str, Any]:
    """Close out paper and live positions on games that have finished.

    Every position now carries the team, line and selection that priced it, so the
    resolver gets the same metadata a prediction does. Positions written before those
    columns existed have NULL there and come back unresolved rather than mis-graded; they
    are counted and reported so the gap is visible instead of silent.
    """
    from app.config import settings

    season = season or settings.SEASON
    schedule = {g.game_id: g for g in await nflverse.schedule(seasons=[season])}
    settled = 0
    unresolved: List[Dict[str, Any]] = []
    store.init()
    rows = [dict(r) for r in store.connection().execute(
        "SELECT * FROM positions WHERE status IN ('open','partially_closed')").fetchall()]

    for position in rows:
        game = schedule.get(position.get("game_id") or "")
        if game is None or not game.completed:
            continue
        outcome = resolve_contract(
            market_type=position.get("market_type") or "",
            selection=position.get("selection") or position.get("label") or "",
            team=position.get("team"),
            line=position.get("line"),
            home=game.home,
            home_score=int(game.home_score or 0),
            away_score=int(game.away_score or 0),
            side=position.get("side") or "yes")
        if outcome is None:
            unresolved.append({
                "position_id": position["id"],
                "ticker": position.get("ticker"),
                "market_type": position.get("market_type"),
                "reason": ("missing team/line metadata — this position predates the "
                           "settlement columns and must be resolved by hand"
                           if not position.get("team") and not position.get("line")
                           else "market voided or not settleable from a final score"),
            })
            continue
        # A settled binary contract is worth exactly $1 or $0 per contract, and Kalshi
        # charges no fee at settlement — only trading in and out costs money.
        store.settle_position(position["id"], won=outcome,
                              note="settled at game result")
        settled += 1

    return {
        "settled_positions": settled,
        "unresolved": unresolved,
        "unresolved_count": len(unresolved),
        "note": (f"Settled {settled} position(s)."
                 + (f" {len(unresolved)} could not be resolved and were left open rather "
                    "than graded on a guess." if unresolved else "")),
    }
