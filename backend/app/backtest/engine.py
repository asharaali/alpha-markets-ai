"""Walk-forward backtesting with a hard look-ahead guarantee.

The guarantee is structural rather than a promise: to predict week N of a season, the
engine calls the same `ratings.build(season, N)` the live app calls, and that function
reads play-by-play with `max_week = N - 1`. There is no code path by which a week-N result
can reach a week-N prediction, and the calibration artifact is fitted the same way.

The benchmark is the CLOSING SPORTSBOOK LINE, not an easier target. Kalshi has no usable
price history to backtest against, and picking a softer benchmark would make the numbers
meaningless. Closing lines are the most efficient prices in sports betting — beating them
out of sample is hard, and a model that does not beat them is telling you something you
need to know.

What comes out is not a win percentage. It is calibration, Brier and log loss against the
market on identical predictions, and flat-stake ROI at the actual closing odds.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.config import settings
from app.core.errors import InsufficientData
from app.core.logging import get_logger
from app.core.types import Game, MarketType
from app.data import nflverse, teams
from app.models import calibration, distributions as dist, game_model
from app.models.ratings import build as build_ratings
from app.strategies import pricing
from app.tracking import metrics

log = get_logger(__name__)

# Ratings need a few games of the current season plus the prior season's carry-over before
# a walk-forward prediction is worth scoring. Week 1 predictions are made and shown, but
# they lean almost entirely on last year.
DEFAULT_START_WEEK = 1


@dataclass
class BacktestPrediction:
    season: int
    week: int
    game_id: str
    market_type: str
    selection: str
    model_prob: float
    fair_prob: float
    market_prob: float
    cost: float
    outcome: int
    line: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "season": self.season, "week": self.week, "game_id": self.game_id,
            "market_type": self.market_type, "selection": self.selection,
            "model_prob": round(self.model_prob, 4),
            "fair_prob": round(self.fair_prob, 4),
            "market_prob": round(self.market_prob, 4),
            "cost": round(self.cost, 4), "outcome": self.outcome, "line": self.line,
        }


def american_to_prob(odds: Optional[float]) -> Optional[float]:
    if odds is None:
        return None
    if odds > 0:
        return 100.0 / (odds + 100.0)
    return abs(odds) / (abs(odds) + 100.0)


def _devig(a: Optional[float], b: Optional[float]) -> Optional[Tuple[float, float]]:
    if a is None or b is None:
        return None
    total = a + b
    if total <= 0:
        return None
    return a / total, b / total


def _moneyline_rows(game: Game, projection, blend_weight: float
                    ) -> List[BacktestPrediction]:
    pair = _devig(american_to_prob(getattr(game, "home_moneyline", None)),
                  american_to_prob(getattr(game, "away_moneyline", None)))
    if pair is None or game.margin is None:
        return []
    home_market, away_market = pair
    decisive = projection.home_win + projection.away_win
    if decisive <= 0:
        return []
    rows = []
    for team, model_prob, market_prob, won in (
        (game.home, projection.home_win / decisive, home_market, game.margin > 0),
        (game.away, projection.away_win / decisive, away_market, game.margin < 0),
    ):
        if game.margin == 0:
            continue  # a tie voids the market; scoring it either way would be wrong
        fair = pricing.blend(model_prob, market_prob, blend_weight)
        rows.append(BacktestPrediction(
            season=game.season, week=game.week, game_id=game.game_id,
            market_type=MarketType.MONEYLINE.value,
            selection=f"{teams.display(team)} ML",
            model_prob=model_prob, fair_prob=fair, market_prob=market_prob,
            cost=market_prob, outcome=1 if won else 0))
    return rows


def _spread_rows(game: Game, projection, blend_weight: float) -> List[BacktestPrediction]:
    if game.spread_line is None or game.margin is None:
        return []
    pair = _devig(american_to_prob(getattr(game, "home_spread_odds", None)),
                  american_to_prob(getattr(game, "away_spread_odds", None)))
    # Most closing spreads are priced near -110 both ways; when the per-side odds are not
    # published, the fair split at the closing number is 50/50 by construction.
    home_market, away_market = pair if pair else (0.5, 0.5)
    line = float(game.spread_line)
    if abs(game.margin - line) < 1e-9:
        return []           # push
    home_cover, _ = dist.cover_probability(projection.margin, -line, team_is_home=True)
    away_cover, _ = dist.cover_probability(projection.margin, line, team_is_home=False)
    rows = []
    for team, model_prob, market_prob, won in (
        (game.home, home_cover, home_market, game.margin > line),
        (game.away, away_cover, away_market, game.margin < line),
    ):
        fair = pricing.blend(model_prob, market_prob, blend_weight)
        rows.append(BacktestPrediction(
            season=game.season, week=game.week, game_id=game.game_id,
            market_type=MarketType.SPREAD.value,
            selection=f"{teams.display(team)} {-line if team == game.home else line:+g}",
            model_prob=model_prob, fair_prob=fair, market_prob=market_prob,
            cost=market_prob, outcome=1 if won else 0, line=line))
    return rows


def _total_rows(game: Game, projection, blend_weight: float) -> List[BacktestPrediction]:
    if game.total_line is None or game.total_points is None:
        return []
    pair = _devig(american_to_prob(getattr(game, "over_odds", None)),
                  american_to_prob(getattr(game, "under_odds", None)))
    over_market, under_market = pair if pair else (0.5, 0.5)
    line = float(game.total_line)
    if abs(game.total_points - line) < 1e-9:
        return []
    over_prob = projection.total.prob_over(line)
    rows = []
    for name, model_prob, market_prob, won in (
        ("Over", over_prob, over_market, game.total_points > line),
        ("Under", 1.0 - over_prob, under_market, game.total_points < line),
    ):
        fair = pricing.blend(model_prob, market_prob, blend_weight)
        rows.append(BacktestPrediction(
            season=game.season, week=game.week, game_id=game.game_id,
            market_type=MarketType.TOTAL.value,
            selection=f"{name} {line:g}",
            model_prob=model_prob, fair_prob=fair, market_prob=market_prob,
            cost=market_prob, outcome=1 if won else 0, line=line))
    return rows


async def run(*, seasons: Sequence[int], start_week: int = DEFAULT_START_WEEK,
              markets: Optional[Sequence[str]] = None,
              blend_weight: Optional[float] = None) -> Dict[str, Any]:
    """Walk forward through the requested seasons, predicting each week from its past."""
    artifact = calibration.load()
    if artifact is None:
        raise InsufficientData("the game model has not been calibrated yet")

    wanted = set(markets or [MarketType.MONEYLINE.value, MarketType.SPREAD.value,
                             MarketType.TOTAL.value])
    started = time.time()
    schedule = await nflverse.schedule(seasons=list(seasons))
    by_week: Dict[Tuple[int, int], List[Game]] = {}
    for game in schedule:
        if game.completed and game.game_type == "REG" and game.week >= start_week:
            by_week.setdefault((game.season, game.week), []).append(game)
    if not by_week:
        raise InsufficientData(f"no completed regular-season games in {list(seasons)}")

    # Extra closing-line columns the schedule loader does not put on the Game dataclass.
    extras = await _closing_line_extras(list(seasons))

    predictions: List[BacktestPrediction] = []
    for (season, week) in sorted(by_week):
        ratings = await build_ratings(season, week)
        if ratings.sample_games == 0:
            continue
        weight = (blend_weight if blend_weight is not None
                  else pricing.model_weight(MarketType.SPREAD,
                                            sample_confidence=ratings.confidence()))
        for game in by_week[(season, week)]:
            for name, value in (extras.get(game.game_id) or {}).items():
                setattr(game, name, value)
            projection = game_model.project(game, ratings, artifact)
            if MarketType.MONEYLINE.value in wanted:
                predictions.extend(_moneyline_rows(game, projection, weight))
            if MarketType.SPREAD.value in wanted:
                predictions.extend(_spread_rows(game, projection, weight))
            if MarketType.TOTAL.value in wanted:
                predictions.extend(_total_rows(game, projection, weight))

    rows = [p.to_dict() for p in predictions]
    for row in rows:
        row["strategy"] = row["market_type"]
        row["confidence"] = _confidence_bucket(row["fair_prob"], row["market_prob"])
        row["ev_per_dollar"] = (row["fair_prob"] / row["cost"]) - 1.0 if row["cost"] else None
        row["created_at"] = 0.0
        row["settled_at"] = 0.0

    elapsed = time.time() - started
    log.info("backtest over %s produced %d predictions in %.1fs",
             list(seasons), len(rows), elapsed)

    return {
        "seasons": list(seasons),
        "start_week": start_week,
        "predictions": len(rows),
        "elapsed_seconds": round(elapsed, 2),
        "overall": metrics.summarise(rows, label="all markets"),
        "by_market": metrics.by_group(rows, "market_type"),
        "by_confidence": metrics.by_group(rows, "confidence"),
        "by_season": metrics.by_group(rows, "season"),
        "value_only": _value_subset(rows),
        "verdict": _verdict(rows),
        "benchmark": (
            "Every model probability is scored against the closing sportsbook line on the "
            "same game, taken from nflverse. Closing lines are the most efficient prices in "
            "the market, so a Brier or log loss at or below the market column is a genuine "
            "result and anything above it means the model is worse than simply taking the "
            "price."
        ),
        "look_ahead_guarantee": (
            "Ratings for week N are built with max_week=N-1, using the same function the "
            "live app calls. No result from the week being predicted is visible to the "
            "prediction."
        ),
        "limitations": (
            "The benchmark is the sportsbook close, not Kalshi — Kalshi publishes no price "
            "history to backtest against, so live results on Kalshi will differ. "
            "Second-stage model coefficients are fitted across these same seasons, so the "
            "in-sample advantage is real though small; the ratings themselves are strictly "
            "walk-forward."
        ),
    }


def _verdict(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """A plain-language answer to 'does this model actually work?'

    Stated bluntly and without hedging, because a backtest that has to be interpreted
    charitably is not evidence. If the model loses to the closing line, the page says so.
    """
    summary = metrics.summarise(rows, label="verdict")
    model_brier = summary.get("brier")
    market_brier = summary.get("brier_market")
    if model_brier is None or market_brier is None:
        return {"beats_market": None,
                "summary": "Not enough graded predictions to reach a verdict."}
    delta = model_brier - market_brier
    beats = delta < 0
    return {
        "beats_market": beats,
        "brier_delta": round(delta, 5),
        "summary": (
            f"The model's Brier score is {model_brier:.4f} against the closing line's "
            f"{market_brier:.4f} — "
            + ("the model is more accurate than the market on these predictions."
               if beats else
               "the model is LESS accurate than simply taking the closing price. On this "
               "benchmark it has not demonstrated an edge.")),
        "what_it_means": (
            "Closing sportsbook lines are the most efficient prices in sports betting, and "
            "very few models beat them. This does not automatically mean there is no edge "
            "on Kalshi: this product trades days before kickoff on an exchange with far "
            "thinner books than a Sunday-morning close, and those are different prices. It "
            "does mean the edge is UNPROVEN, and the live tracked record — which is "
            "recorded before outcomes and cannot be edited afterwards — is what will settle "
            "it. Size accordingly until it does."
        ),
    }


def _confidence_bucket(fair: float, market: float) -> str:
    gap = abs(fair - market)
    if gap >= 0.06:
        return "high"
    if gap >= 0.03:
        return "medium"
    return "low"


def _value_subset(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """How the bets we would actually have RECOMMENDED performed, not every prediction.

    This is the number that matters: the model produces a probability for both sides of
    every market, and half of those are trivially the other half. Only the side that
    cleared the value gate would ever have been bet.
    """
    picked = [r for r in rows
              if r.get("ev_per_dollar") is not None
              and pricing.is_value(edge=r["fair_prob"] - r["market_prob"],
                                   ev=r["ev_per_dollar"],
                                   market_prob=r["market_prob"], liquid=True)]
    summary = metrics.summarise(picked, label="value bets only")
    summary["selection_rate"] = round(len(picked) / len(rows), 4) if rows else 0.0
    return summary


async def _closing_line_extras(seasons: Sequence[int]) -> Dict[str, Dict[str, float]]:
    """Per-side closing odds, which the Game dataclass does not carry."""
    from app.data import csvstream as cs
    from app.data.feed import fetch_file

    path = await fetch_file(f"{settings.NFLVERSE_BASE}/schedules/games.csv",
                            ttl=settings.SCHEDULE_CACHE_TTL, source="nflverse")
    if path is None:
        return {}
    want = {str(s) for s in seasons}
    columns = ["game_id", "season", "home_moneyline", "away_moneyline",
               "home_spread_odds", "away_spread_odds", "over_odds", "under_odds"]
    out: Dict[str, Dict[str, float]] = {}
    for row in cs.stream(path, columns, where=lambda r: r.get("season") in want):
        values = {name: cs.num(row.get(name)) for name in columns[2:]}
        out[row["game_id"]] = {k: v for k, v in values.items() if v is not None}
    return out
