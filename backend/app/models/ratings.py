"""Opponent-adjusted team ratings.

Raw season stats lie: a defence that has played three backup quarterbacks looks elite, and
an offence that has faced the league's best fronts looks broken. Every rating here is
fitted so that a team's offence and its opponents' defences are estimated jointly —

    observed_offensive_metric = league_mean + offense[team] - defense[opponent] + hfa*home

— which is the whole point of the exercise. Ratings are always computed AS OF a point in
the season: `build(season, week)` sees only games played strictly before that week, which
is what makes the backtester's results mean anything.

Small samples are handled by ridge shrinkage rather than by minimum-games cutoffs, so a
week-2 rating is simply a mostly-prior rating rather than a missing one. In week 1 of a new
season, with no current-season games at all, ratings are entirely the decayed prior season.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from app.config import settings
from app.core.logging import get_logger
from app.features import aggregate
from app.features.aggregate import METRICS, TeamGame
from app.models.linalg import SingularSystem, SparseRow, ridge_fit

log = get_logger(__name__)

# Recency: within a season a game's weight halves every ~14 games (a bit under a season),
# and each season further back is worth ~40% of the one after it. Both are deliberately
# gentle — NFL rosters turn over, but a 17-game sample is small enough that discarding
# history costs more than the staleness it removes.
GAME_HALF_LIFE = 14.0
SEASON_DECAY = 0.40
# Shrinkage strength, in units of "games of evidence". A team with 10 games of data sits
# halfway between the league mean and its raw performance.
RIDGE_GAMES = 10.0


@dataclass
class TeamRating:
    team: str
    offense: Dict[str, float] = field(default_factory=dict)
    defense: Dict[str, float] = field(default_factory=dict)
    games: float = 0.0            # effective (weighted) sample size

    def net(self, metric: str) -> float:
        """Offence plus defence on one metric, oriented so higher is always better.

        The fit is `offense_metric = mean + off[team] - def[opponent]`, so a defence that
        SUPPRESSES the metric carries a large positive `def` value. Both halves therefore
        add on metrics where more is better, and both flip on metrics where less is
        (sack rate, pressure allowed, turnovers).
        """
        sign = 1.0 if METRICS.get(metric, True) else -1.0
        return sign * (self.offense.get(metric, 0.0) + self.defense.get(metric, 0.0))


@dataclass
class RatingSet:
    season: int
    week: int                     # ratings reflect games strictly BEFORE this week
    teams: Dict[str, TeamRating]
    league_mean: Dict[str, float]
    home_field: Dict[str, float]
    sample_games: int                # raw team-game observations in the fit
    effective_games_per_team: float  # recency-weighted sample, the honest sample size
    seasons_used: List[int]

    def get(self, team: str) -> TeamRating:
        return self.teams.get(team) or TeamRating(team=team)

    def expected(self, metric: str, offense_team: str, defense_team: str,
                 home: bool) -> float:
        """The model's expectation for `offense_team`'s metric against `defense_team`."""
        o = self.get(offense_team).offense.get(metric, 0.0)
        d = self.get(defense_team).defense.get(metric, 0.0)
        base = self.league_mean.get(metric, 0.0)
        hfa = self.home_field.get(metric, 0.0) if home else 0.0
        return base + o - d + hfa

    def confidence(self) -> float:
        """0-1 read on how much evidence these ratings rest on.

        Week 1 of a season, running entirely on last year's decayed data, is genuinely less
        certain than week 12 — the UI is told so rather than presenting both identically.
        """
        eff = min(self.effective_games_per_team / 8.0, 1.0)
        return round(0.35 + 0.65 * eff, 3)


def _weight(row: TeamGame, season: int, week: int) -> float:
    """Recency weight for one team-game, viewed from (season, week)."""
    seasons_back = season - row.season
    if seasons_back < 0:
        return 0.0
    if seasons_back == 0:
        games_ago = max(week - row.week, 0)
    else:
        # Approximate distance in games across a season boundary.
        games_ago = week + (seasons_back - 1) * 18 + (18 - row.week)
    recency = 0.5 ** (games_ago / GAME_HALF_LIFE)
    return recency * (SEASON_DECAY ** seasons_back)


def _fit_metric(metric: str, rows: List[Tuple[TeamGame, float]],
                team_index: Dict[str, int]) -> Optional[Tuple[Dict[str, float],
                                                              Dict[str, float],
                                                              float, float]]:
    """Fit one metric. Returns (offense, defense, league_mean, home_field) or None."""
    n_teams = len(team_index)
    # Parameter layout: [mean, offense x32, defense x32, home_field]
    idx_mean = 0
    idx_off = 1
    idx_def = 1 + n_teams
    idx_hfa = 1 + 2 * n_teams
    n_params = idx_hfa + 1

    observations: List[SparseRow] = []
    total_weight = 0.0
    for tg, w in rows:
        value = tg.rates().get(metric)
        if value is None:
            continue
        oi = team_index.get(tg.team)
        di = team_index.get(tg.opponent)
        if oi is None or di is None:
            continue
        terms = [(idx_mean, 1.0), (idx_off + oi, 1.0), (idx_def + di, -1.0)]
        if tg.home:
            terms.append((idx_hfa, 1.0))
        observations.append((terms, float(value), w))
        total_weight += w

    if len(observations) < n_teams:      # not even one game per team's worth of signal
        return None

    penalty = RIDGE_GAMES * (total_weight / max(len(observations), 1))
    try:
        beta = ridge_fit(observations, n_params, penalty,
                         unpenalized=(idx_mean, idx_hfa))
    except SingularSystem as exc:
        log.warning("rating fit for %s failed: %s", metric, exc)
        return None

    offense = {t: beta[idx_off + i] for t, i in team_index.items()}
    defense = {t: beta[idx_def + i] for t, i in team_index.items()}
    return offense, defense, beta[idx_mean], beta[idx_hfa]


async def build(season: int, week: int, *,
                history_seasons: Optional[int] = None) -> RatingSet:
    """Ratings as of the start of `week` in `season`. Sees nothing from `week` onward."""
    back = history_seasons if history_seasons is not None else settings.HISTORY_SEASONS
    weighted: List[Tuple[TeamGame, float]] = []
    seasons_used: List[int] = []

    for s in range(season, season - back - 1, -1):
        max_week = week - 1 if s == season else None
        if s == season and week <= 1:
            continue                      # nothing has happened in this season yet
        rows = await aggregate.team_games(s, max_week=max_week)
        if not rows:
            continue
        rows = await aggregate.with_scores(s, rows)
        seasons_used.append(s)
        for tg in rows:
            w = _weight(tg, season, week)
            if w > 1e-4:
                weighted.append((tg, w))

    from app.data import teams as team_registry
    team_index = {abbr: i for i, abbr in enumerate(team_registry.ALL_ABBRS)}
    ratings = {abbr: TeamRating(team=abbr) for abbr in team_index}
    league_mean: Dict[str, float] = {}
    home_field: Dict[str, float] = {}

    if not weighted:
        log.warning("no historical data available for ratings as of %s week %s", season, week)
        return RatingSet(season=season, week=week, teams=ratings, league_mean={},
                         home_field={}, sample_games=0, effective_games_per_team=0.0,
                         seasons_used=[])

    for metric in METRICS:
        fitted = _fit_metric(metric, weighted, team_index)
        if fitted is None:
            continue
        offense, defense, mean, hfa = fitted
        for abbr in team_index:
            ratings[abbr].offense[metric] = offense[abbr]
            ratings[abbr].defense[metric] = defense[abbr]
        league_mean[metric] = mean
        home_field[metric] = hfa

    # Effective sample per team, for the confidence read and the UI.
    per_team: Dict[str, float] = {}
    for tg, w in weighted:
        per_team[tg.team] = per_team.get(tg.team, 0.0) + w
    for abbr, rating in ratings.items():
        rating.games = round(per_team.get(abbr, 0.0), 2)

    total_weight = sum(w for _, w in weighted)
    return RatingSet(season=season, week=week, teams=ratings, league_mean=league_mean,
                     home_field=home_field, sample_games=len(weighted),
                     effective_games_per_team=round(total_weight / len(team_index), 2),
                     seasons_used=sorted(set(seasons_used), reverse=True))


def rank_table(rating_set: RatingSet, metric: str = "epa_per_play") -> List[Dict[str, object]]:
    """Teams ordered by net rating on one metric — the Model Lab's power-ranking view."""
    rows = []
    for abbr, rating in rating_set.teams.items():
        rows.append({
            "team": abbr,
            "net": round(rating.net(metric), 5),
            "offense": round(rating.offense.get(metric, 0.0), 5),
            "defense": round(rating.defense.get(metric, 0.0), 5),
            "games": rating.games,
        })
    rows.sort(key=lambda r: r["net"], reverse=True)
    for i, row in enumerate(rows, start=1):
        row["rank"] = i
    return rows
