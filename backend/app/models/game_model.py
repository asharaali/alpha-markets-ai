"""The core game projection: ratings + calibration -> a full distribution over outcomes.

Everything a strategy needs about a single game comes from one GameProjection: expected
margin and total, the discrete distributions around them, and therefore a probability for
any moneyline, spread, total or team-total the market cares to quote.

Deliberately, this produces DISTRIBUTIONS rather than point estimates. "We project Chiefs
by 4.5" is not a bet; "the margin distribution puts 58.1% above -3.5, and the market is
paying you 55c" is.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from app.core.logging import get_logger
from app.core.types import Game
from app.data import teams
from app.models import distributions as dist
from app.models.calibration import (GameModelArtifact, margin_predictors,
                                    total_predictors, _is_indoor)
from app.models.ratings import RatingSet

log = get_logger(__name__)


@dataclass
class GameProjection:
    game: Game
    expected_margin: float           # positive = home favoured, in points
    expected_total: float
    margin_sigma: float
    total_sigma: float
    margin: dist.DiscreteDistribution
    total: dist.DiscreteDistribution
    home_win: float
    tie: float
    away_win: float
    drivers: List[Dict[str, object]] = field(default_factory=list)
    confidence: float = 0.5
    adjustments: Dict[str, float] = field(default_factory=dict)

    # ---- derived scoring ----
    @property
    def home_score(self) -> float:
        return (self.expected_total + self.expected_margin) / 2.0

    @property
    def away_score(self) -> float:
        return (self.expected_total - self.expected_margin) / 2.0

    def spread_probability(self, team: str, line: float) -> Tuple[float, float]:
        """(cover, push) for `team` at betting line `line` (-3.5 = laying 3.5)."""
        is_home = teams.resolve(team) == self.game.home
        return dist.cover_probability(self.margin, line, team_is_home=is_home)

    def margin_over(self, threshold: float, team: str) -> float:
        """P(`team` wins by more than `threshold`) — Kalshi's spread markets are written
        exactly this way ('Seattle wins by over 13.5 points')."""
        if teams.resolve(team) == self.game.home:
            return self.margin.prob_over(threshold)
        return self.margin.prob_under(-threshold)

    def total_over(self, line: float) -> float:
        return self.total.prob_over(line)

    def total_under(self, line: float) -> float:
        return self.total.prob_under(line)

    def team_total_over(self, team: str, line: float) -> float:
        """P(one team's points > line).

        A team's own score is roughly (total + margin)/2, and its variance is dominated by
        the same drive-level noise as the game total — so we model it directly rather than
        convolving two correlated distributions, which would understate the spread.
        """
        is_home = teams.resolve(team) == self.game.home
        mu = self.home_score if is_home else self.away_score
        # Single-team scoring is tighter than the game total but not by half: about 70%
        # of the total's spread, which is what the historical split implies.
        sigma = self.total_sigma * 0.70
        team_dist = dist.build_distribution(mu, sigma, low=0)
        return team_dist.prob_over(line)

    def to_dict(self) -> Dict[str, object]:
        return {
            "game_id": self.game.game_id,
            "home": self.game.home, "away": self.game.away,
            "kickoff": self.game.kickoff, "week": self.game.week,
            "expected_margin": round(self.expected_margin, 2),
            "expected_total": round(self.expected_total, 2),
            "projected_score": {
                "home": round(self.home_score, 1),
                "away": round(self.away_score, 1),
            },
            "margin_sigma": round(self.margin_sigma, 2),
            "total_sigma": round(self.total_sigma, 2),
            "win_probability": {
                "home": round(self.home_win, 4),
                "tie": round(self.tie, 4),
                "away": round(self.away_win, 4),
            },
            "most_likely_margins": [
                {"margin": m, "prob": round(p, 4)} for m, p in self.margin.top_outcomes(6)
            ],
            # The distributions themselves, trimmed to the region carrying the mass. The
            # chart needs the shape, not just the top few outcomes — seeing the spikes at
            # 3 and 7 is the whole point of modelling them.
            "margin_distribution": _serialise(self.margin),
            "total_distribution": _serialise(self.total),
            "drivers": self.drivers,
            "adjustments": {k: round(v, 3) for k, v in self.adjustments.items()},
            "confidence": round(self.confidence, 3),
        }


def _serialise(distribution: dist.DiscreteDistribution,
               floor: float = 0.0008) -> Dict[str, object]:
    """A distribution shaped for the wire: trimmed tails, rounded mass.

    The full support runs five standard deviations either side, most of which is
    indistinguishable from zero and would trip the payload size for no benefit.
    """
    mass = distribution.mass
    start, end = 0, len(mass) - 1
    while start < end and mass[start] < floor:
        start += 1
    while end > start and mass[end] < floor:
        end -= 1
    return {
        "low": distribution.low + start,
        "mass": [round(m, 5) for m in mass[start:end + 1]],
        "mean": round(distribution.mean(), 2),
        "stdev": round(distribution.stdev(), 2),
    }


def _driver_rows(rs: RatingSet, game: Game) -> List[Dict[str, object]]:
    """The handful of rating gaps that actually moved this projection.

    Shown to the user as the model's reasoning. Only genuine model inputs appear here — no
    narrative colour the numbers do not support.
    """
    home, away = game.home, game.away
    rows: List[Dict[str, object]] = []
    for metric, label in (
        ("epa_per_play", "Overall efficiency (EPA/play)"),
        ("pass_epa_per_dropback", "Passing efficiency"),
        ("rush_epa_per_carry", "Rushing efficiency"),
        ("success_rate", "Success rate"),
        ("pressure_rate", "Pressure allowed"),
        ("explosive_rate", "Explosive plays"),
        ("turnover_rate", "Turnovers per drive"),
        ("redzone_td_rate", "Red-zone touchdown rate"),
    ):
        h = rs.get(home).net(metric)
        a = rs.get(away).net(metric)
        gap = h - a
        rows.append({
            "metric": metric, "label": label,
            "home": round(h, 4), "away": round(a, 4),
            "edge_to": home if gap > 0 else away,
            "gap": round(abs(gap), 4),
        })
    rows.sort(key=lambda r: r["gap"], reverse=True)
    return rows


def project(game: Game, rs: RatingSet, artifact: GameModelArtifact, *,
            margin_shift: float = 0.0, total_shift: float = 0.0,
            extra_margin_sigma: float = 0.0,
            adjustments: Optional[Dict[str, float]] = None) -> GameProjection:
    """Build the full projection for one game.

    `margin_shift` / `total_shift` are points of adjustment contributed by downstream
    models (injuries, weather, a backup quarterback). They are applied here rather than
    buried in the ratings so every adjustment is visible and auditable in `adjustments`.
    """
    mx = margin_predictors(rs, game.home, game.away,
                           home_rest=game.home_rest, away_rest=game.away_rest,
                           div_game=game.div_game)
    tx = total_predictors(rs, game.home, game.away,
                          indoor=_is_indoor(game), wind=game.wind)
    base_margin = sum(b * v for b, v in zip(artifact.margin_coefficients, mx))
    base_total = sum(b * v for b, v in zip(artifact.total_coefficients, tx))

    expected_margin = base_margin + margin_shift
    expected_total = max(base_total + total_shift, 20.0)

    margin_sigma = artifact.margin_sigma + max(extra_margin_sigma, 0.0)
    total_sigma = artifact.total_sigma

    margin_dist = dist.margin_distribution(expected_margin, margin_sigma,
                                           artifact.margin_profile())
    total_dist = dist.total_distribution(expected_total, total_sigma,
                                         artifact.total_profile())
    home_win, tie, away_win = dist.win_probability(margin_dist)

    # Confidence blends how much evidence the ratings rest on with how much extra
    # uncertainty the adjustments injected. A game with a quarterback question is not as
    # knowable as one without, and the number should say so.
    uncertainty_penalty = min(extra_margin_sigma / 6.0, 0.35)
    confidence = max(0.15, rs.confidence() - uncertainty_penalty)

    return GameProjection(
        game=game,
        expected_margin=expected_margin,
        expected_total=expected_total,
        margin_sigma=margin_sigma,
        total_sigma=total_sigma,
        margin=margin_dist,
        total=total_dist,
        home_win=home_win, tie=tie, away_win=away_win,
        drivers=_driver_rows(rs, game),
        confidence=confidence,
        adjustments=dict(adjustments or {}),
    )
