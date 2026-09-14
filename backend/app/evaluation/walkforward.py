"""Walk-forward evaluation with the look-ahead actually closed.

The old backtester built ratings week by week — genuinely walk-forward — and then loaded a
single calibration artifact fitted by pooling every season it was about to score. Its own
`limitations` string admitted this and called the in-sample advantage "real though small".
Nobody had measured it, because measuring it requires the thing being built here.

The rule enforced below: to predict any game in season S, week N, the only inputs allowed
are

    * ratings built from weeks strictly before N within S, plus prior seasons, and
    * a game-model artifact fitted on seasons strictly before S.

The artifact is refitted once per season rather than once per week. That is a deliberate
trade: a weekly refit would be a few hundred second-stage regressions for numbers that move
in the fourth decimal, and the guarantee that matters — no season sees itself — is
identical either way.

Output is one row per contract per game, carrying every baseline's probability so they are
scored on identical observations, plus the metadata the protocol module needs to cluster by
game and collapse repeated forecasts.

The benchmark remains the closing sportsbook line, and that limitation is now stated
prominently rather than buried: Kalshi publishes no usable price history, so this measures
forecast quality against sportsbook closes and says nothing directly about what was
executable on Kalshi days earlier. Those are different claims and the report keeps them
apart.
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
from app.evaluation import baselines, protocol
from app.models import calibration, distributions as dist, game_model
from app.models.ratings import build as build_ratings
from app.risk import fees
from app.strategies import pricing

log = get_logger(__name__)


def american_to_prob(odds: Optional[float]) -> Optional[float]:
    if odds is None:
        return None
    if odds > 0:
        return 100.0 / (odds + 100.0)
    return abs(odds) / (abs(odds) + 100.0)


def devig(a: Optional[float], b: Optional[float]) -> Optional[Tuple[float, float]]:
    """Strip the bookmaker's margin. Two-way markets only, proportional method."""
    if a is None or b is None:
        return None
    total = a + b
    if total <= 0:
        return None
    return a / total, b / total


@dataclass
class Forecast:
    """One contract, one game, every competitor's probability side by side."""

    season: int
    week: int
    game_id: str
    kickoff: Optional[str]
    market_type: str
    selection: str
    team: Optional[str]
    line: Optional[float]
    outcome: int
    market_prob: float
    model_prob: float
    blend_prob: float
    ensemble_prob: float
    cost: float
    ablations: Dict[str, float] = field(default_factory=dict)

    def to_row(self) -> Dict[str, Any]:
        row: Dict[str, Any] = {
            "season": self.season, "week": self.week, "game_id": self.game_id,
            "kickoff": self.kickoff, "market_type": self.market_type,
            "strategy": self.market_type, "selection": self.selection,
            "team": self.team, "line": self.line, "outcome": self.outcome,
            "market_prob": self.market_prob, "model_prob": self.model_prob,
            "blend_prob": self.blend_prob, "ensemble_prob": self.ensemble_prob,
            "cost": self.cost,
        }
        for key, value in self.ablations.items():
            row[f"{key}_prob"] = value
        return row


def _moneyline(game: Game, projection, weight: float) -> List[Forecast]:
    pair = devig(american_to_prob(getattr(game, "home_moneyline", None)),
                 american_to_prob(getattr(game, "away_moneyline", None)))
    if pair is None or game.margin is None or game.margin == 0:
        return []                      # a tie voids the market; scoring it would be wrong
    home_market, away_market = pair
    decisive = projection.home_win + projection.away_win
    if decisive <= 0:
        return []
    out = []
    for team, model_prob, market_prob, won in (
        (game.home, projection.home_win / decisive, home_market, game.margin > 0),
        (game.away, projection.away_win / decisive, away_market, game.margin < 0),
    ):
        out.append(Forecast(
            season=game.season, week=game.week, game_id=game.game_id,
            kickoff=game.kickoff, market_type=MarketType.MONEYLINE.value,
            selection=f"{teams.display(team)} ML", team=team, line=None,
            outcome=1 if won else 0, market_prob=market_prob, model_prob=model_prob,
            blend_prob=pricing.blend(model_prob, market_prob, weight),
            ensemble_prob=pricing.blend(model_prob, market_prob, weight),
            cost=market_prob,
            ablations={"no_market_blend": model_prob}))
    return out


def _spread(game: Game, projection, weight: float,
            smooth_margin=None) -> List[Forecast]:
    if game.spread_line is None or game.margin is None:
        return []
    pair = devig(american_to_prob(getattr(game, "home_spread_odds", None)),
                 american_to_prob(getattr(game, "away_spread_odds", None)))
    home_market, away_market = pair if pair else (0.5, 0.5)
    line = float(game.spread_line)
    if abs(game.margin - line) < 1e-9:
        return []                      # push
    home_cover, _ = dist.cover_probability(projection.margin, -line, team_is_home=True)
    away_cover, _ = dist.cover_probability(projection.margin, line, team_is_home=False)

    smooth = {}
    if smooth_margin is not None:
        sh, _ = dist.cover_probability(smooth_margin, -line, team_is_home=True)
        sa, _ = dist.cover_probability(smooth_margin, line, team_is_home=False)
        smooth = {game.home: sh, game.away: sa}

    out = []
    for team, model_prob, market_prob, won in (
        (game.home, home_cover, home_market, game.margin > line),
        (game.away, away_cover, away_market, game.margin < line),
    ):
        ablations = {"no_market_blend": model_prob}
        if team in smooth:
            ablations["no_key_numbers"] = pricing.blend(smooth[team], market_prob, weight)
        out.append(Forecast(
            season=game.season, week=game.week, game_id=game.game_id,
            kickoff=game.kickoff, market_type=MarketType.SPREAD.value,
            selection=f"{teams.display(team)} {-line if team == game.home else line:+g}",
            team=team, line=line, outcome=1 if won else 0, market_prob=market_prob,
            model_prob=model_prob,
            blend_prob=pricing.blend(model_prob, market_prob, weight),
            ensemble_prob=pricing.blend(model_prob, market_prob, weight),
            cost=market_prob, ablations=ablations))
    return out


def _total(game: Game, projection, weight: float) -> List[Forecast]:
    if game.total_line is None or game.total_points is None:
        return []
    pair = devig(american_to_prob(getattr(game, "over_odds", None)),
                 american_to_prob(getattr(game, "under_odds", None)))
    over_market, under_market = pair if pair else (0.5, 0.5)
    line = float(game.total_line)
    if abs(game.total_points - line) < 1e-9:
        return []
    over_prob = projection.total.prob_over(line)
    out = []
    for name, model_prob, market_prob, won in (
        ("Over", over_prob, over_market, game.total_points > line),
        ("Under", 1.0 - over_prob, under_market, game.total_points < line),
    ):
        out.append(Forecast(
            season=game.season, week=game.week, game_id=game.game_id,
            kickoff=game.kickoff, market_type=MarketType.TOTAL.value,
            selection=f"{name} {line:g}", team=None, line=line,
            outcome=1 if won else 0, market_prob=market_prob, model_prob=model_prob,
            blend_prob=pricing.blend(model_prob, market_prob, weight),
            ensemble_prob=pricing.blend(model_prob, market_prob, weight),
            cost=market_prob, ablations={"no_market_blend": model_prob}))
    return out


async def generate(seasons: Sequence[int], *, start_week: int = 1,
                   blend_weight: Optional[float] = None,
                   include_ablations: bool = True
                   ) -> Tuple[List[Dict[str, Any]], protocol.EvaluationGuard]:
    """Produce walk-forward forecasts for every completed game in `seasons`.

    Returns the rows plus the guard object recording what each fit was allowed to see, so
    the caller can verify the look-ahead claim rather than reprint it.
    """
    schedule = await nflverse.schedule(seasons=list(seasons))
    by_week: Dict[Tuple[int, int], List[Game]] = {}
    for game in schedule:
        if game.completed and game.game_type == "REG" and game.week >= start_week:
            by_week.setdefault((game.season, game.week), []).append(game)
    if not by_week:
        raise InsufficientData(f"no completed regular-season games in {list(seasons)}")

    extras = await _closing_line_extras(list(seasons))

    guard = protocol.EvaluationGuard(
        as_of_season=min(seasons),
        scored_seasons=tuple(sorted({s for s, _ in by_week})))

    artifacts: Dict[int, Any] = {}
    rows: List[Dict[str, Any]] = []

    for (season, week) in sorted(by_week):
        if season not in artifacts:
            # THE fix: coefficients, sigmas, correlation and key profiles from seasons
            # strictly before this one.
            artifacts[season] = await calibration.fit_as_of(season)
            # Recorded per scored season: this season's predictions may only ever see
            # seasons strictly before it.
            guard.fits_by_season[season] = tuple(
                sorted(artifacts[season].seasons_fitted))
        artifact = artifacts[season]

        ratings = await build_ratings(season, week)
        if ratings.sample_games == 0:
            continue
        guard.ratings_max_week[(season, week)] = week - 1

        weight = (blend_weight if blend_weight is not None
                  else pricing.model_weight(MarketType.SPREAD,
                                            sample_confidence=ratings.confidence()))

        for game in by_week[(season, week)]:
            for name, value in (extras.get(game.game_id) or {}).items():
                setattr(game, name, value)
            projection = game_model.project(game, ratings, artifact)

            smooth_margin = None
            if include_ablations:
                # Same mean and sigma, no key-number reweighting: isolates what the
                # profile is worth rather than what it is assumed to be worth.
                smooth_margin = dist.build_distribution(
                    projection.expected_margin, projection.margin_sigma, profile=None)

            forecasts: List[Forecast] = []
            forecasts.extend(_moneyline(game, projection, weight))
            forecasts.extend(_spread(game, projection, weight,
                                     smooth_margin=smooth_margin))
            forecasts.extend(_total(game, projection, weight))
            rows.extend(f.to_row() for f in forecasts)

    return rows, guard


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


def flat_stake_return(rows: Sequence[Dict[str, Any]], column: str, *,
                      stake: float = 100.0,
                      min_edge: float = 0.0) -> Dict[str, Any]:
    """What a flat-stake bettor following `column` would have made, after fees.

    Reported separately from forecast quality because they answer different questions. A
    model can be better calibrated than the market and still lose money, if the places it
    disagrees are the places the price is widest.

    The fee charged is Kalshi's, applied to a sportsbook-priced backtest. That is an
    approximation and it is stated as one: it answers "would this edge survive Kalshi's
    fee", not "this is what Kalshi would have paid".
    """
    bets = []
    for row in rows:
        prob = row.get(column)
        cost = row.get("cost")
        outcome = row.get("outcome")
        if prob is None or cost is None or outcome is None:
            continue
        if float(prob) - float(cost) < min_edge:
            continue
        contracts = int(stake / float(cost)) if float(cost) > 0 else 0
        if contracts < 1:
            continue
        fee = fees.trading_fee(contracts=contracts, price=float(cost))
        spent = contracts * float(cost) + fee
        returned = contracts * 1.0 if int(outcome) == 1 else 0.0
        bets.append({"game_id": row.get("game_id"), "pnl": returned - spent,
                     "staked": spent})

    if not bets:
        return {"bets": 0, "note": "No forecast cleared the edge threshold."}

    staked = sum(b["staked"] for b in bets)
    pnl = sum(b["pnl"] for b in bets)
    per_game = protocol.clustered_mean(
        [{"game_id": b["game_id"], "roi": b["pnl"] / b["staked"]} for b in bets], "roi")
    return {
        "bets": len(bets),
        "games": per_game["clusters"] if per_game else 0,
        "staked": round(staked, 2),
        "pnl": round(pnl, 2),
        "roi": round(pnl / staked, 4) if staked else None,
        "roi_se": round(per_game["standard_error"], 4)
                  if per_game and per_game.get("standard_error") else None,
        "fees_included": True,
        "basis": ("Flat stake at the de-vigged closing sportsbook price, with Kalshi's "
                  "taker fee applied. This tests whether the edge survives a fee, not "
                  "what Kalshi would have paid — Kalshi publishes no price history to "
                  "backtest against."),
    }
