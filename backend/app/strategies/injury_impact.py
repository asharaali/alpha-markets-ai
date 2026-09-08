"""Injury Impact model — what a team's availability report is worth, in points.

Method: for each listed player, multiply three things —

  * POSITIONAL VALUE: how much a starter at that position is worth to a team's scoring
    margin over a full game. Quarterback dwarfs everything else and nothing else is close;
    the rest are small enough that only a pile of them moves a line.
  * DEPTH: whether the player is actually a starter. A third-string guard on the injury
    report is not news, and treating every listed name equally is the classic way to turn
    an injury model into noise.
  * SEVERITY: the published game-status ladder (Out / Doubtful / Questionable), with
    practice participation used when no game status has been published yet.

Just as important as the point shift is the UNCERTAINTY. A questionable quarterback does
not mean "half a quarterback" — it means the game has two very different versions and we
genuinely do not know which one we get. That widens the margin distribution rather than
just nudging its centre, which correctly stops the model claiming a confident edge on a
game whose most important input is unresolved.

The positional values below are prior estimates drawn from the public consensus on
positional replacement value, not quantities fitted in this repository. They are collected
here, named, and documented so they can be challenged and changed in one place.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from app.core.types import Game, MarketType, StrategyMeta
from app.data import teams
from app.data.nflverse import InjuryReport
from app.models.ratings import RatingSet
from app.strategies.base import ProjectionAdjustment

# Points of scoring margin lost when a healthy STARTER at this position is fully absent.
POSITION_VALUE: Dict[str, float] = {
    "QB": 5.5,
    "RB": 0.8, "FB": 0.1,
    "WR": 0.9, "TE": 0.6,
    "T": 0.8, "G": 0.5, "C": 0.6, "OL": 0.6,
    "EDGE": 0.9, "DE": 0.9, "OLB": 0.7, "DT": 0.6, "NT": 0.4,
    "LB": 0.4, "ILB": 0.4, "MLB": 0.4,
    "CB": 0.8, "S": 0.5, "FS": 0.5, "SS": 0.5, "DB": 0.5,
    "K": 0.4, "P": 0.1, "LS": 0.05,
}
DEFAULT_POSITION_VALUE = 0.3

# How much of a starter's value each depth-chart rank carries. Rank 1 is the starter.
DEPTH_MULTIPLIER = {1: 1.0, 2: 0.35, 3: 0.10}
DEFAULT_DEPTH_MULTIPLIER = 0.05

# A quarterback whose status is unresolved adds this much margin uncertainty at peak
# doubt (severity 0.5). Nothing else in football comes close to mattering this much.
QB_UNCERTAINTY_POINTS = 4.0
SKILL_UNCERTAINTY_POINTS = 0.8

# Losing an offence's key players lowers the game total as well as the margin. A missing
# quarterback removes points from the board; a missing cornerback adds them back.
OFFENSIVE_POSITIONS = {"QB", "RB", "FB", "WR", "TE", "T", "G", "C", "OL"}
TOTAL_SHARE_OFFENSE = -0.55       # fraction of the margin hit that leaves the total
TOTAL_SHARE_DEFENSE = 0.45        # a weakened defence puts points back on the board

META = StrategyMeta(
    key="injury_impact",
    name="Injury Impact",
    market_types=[MarketType.MONEYLINE, MarketType.SPREAD, MarketType.TOTAL],
    methodology=(
        "Each listed player is valued as positional replacement value x depth-chart rank x "
        "published availability severity, summed per team into a points-of-margin shift and "
        "a points-of-total shift. Unresolved quarterback status widens the margin "
        "distribution instead of only moving its centre."
    ),
    inputs=["nflverse official injury reports", "nflverse depth charts",
            "positional replacement-value priors"],
    limitations=(
        "Positional values are public priors, not fitted here. The feed carries a player's "
        "status, not the quality of the specific backup, so two teams losing the same "
        "position are treated alike. In-game injuries are invisible to a pre-game report."
    ),
)


def _depth_rank(player_id: Optional[str], player: str,
                chart: List[Dict[str, object]]) -> Optional[int]:
    for row in chart:
        if player_id and row.get("player_id") == player_id:
            return row.get("rank")  # type: ignore[return-value]
    lowered = (player or "").strip().lower()
    for row in chart:
        if str(row.get("player") or "").strip().lower() == lowered:
            return row.get("rank")  # type: ignore[return-value]
    return None


def _normalise_position(position: Optional[str]) -> str:
    p = (position or "").strip().upper()
    return p if p in POSITION_VALUE else p[:2]


def team_impact(reports: List[InjuryReport],
                chart: List[Dict[str, object]]) -> Dict[str, object]:
    """Points of margin and total a single team's report is worth, plus the detail."""
    margin_loss = 0.0
    total_shift = 0.0
    variance_points = 0.0
    items: List[Dict[str, object]] = []

    for report in reports:
        severity = report.severity
        if severity <= 0:
            continue
        position = _normalise_position(report.position)
        base = POSITION_VALUE.get(position, DEFAULT_POSITION_VALUE)
        rank = _depth_rank(report.player_id, report.player, chart)
        depth = DEPTH_MULTIPLIER.get(rank or 0, DEFAULT_DEPTH_MULTIPLIER)
        # A quarterback on the report is a starter until the chart says otherwise; the
        # depth chart lags roster news by days and would otherwise mute the one injury
        # that actually matters.
        if position == "QB" and rank is None:
            depth = DEPTH_MULTIPLIER[1]
        impact = base * depth * severity
        if impact < 0.02:
            continue
        margin_loss += impact
        if position in OFFENSIVE_POSITIONS:
            total_shift += impact * TOTAL_SHARE_OFFENSE
        else:
            total_shift += impact * TOTAL_SHARE_DEFENSE

        # Peak uncertainty at severity 0.5: a player who is definitely out is KNOWN, and a
        # player who is definitely playing is known too. Doubt lives in the middle.
        doubt = 1.0 - abs(severity - 0.5) * 2.0
        if position == "QB":
            variance_points += QB_UNCERTAINTY_POINTS * depth * max(doubt, 0.0)
        elif position in {"RB", "WR", "TE"}:
            variance_points += SKILL_UNCERTAINTY_POINTS * depth * max(doubt, 0.0)

        items.append({
            "player": report.player, "position": report.position,
            "status": report.report_status or report.practice_status,
            "injury": report.injury, "depth_rank": rank,
            "severity": round(severity, 2), "points": round(impact, 2),
        })

    items.sort(key=lambda i: i["points"], reverse=True)
    return {"margin_loss": margin_loss, "total_shift": total_shift,
            "uncertainty_points": variance_points, "items": items}


def adjust(game: Game, ratings: RatingSet, *,
           injuries: Dict[str, List[InjuryReport]],
           depth_chart: Dict[str, List[Dict[str, object]]],
           weather: Optional[Dict[str, object]] = None) -> ProjectionAdjustment:
    home = team_impact(injuries.get(game.home, []), depth_chart.get(game.home, []))
    away = team_impact(injuries.get(game.away, []), depth_chart.get(game.away, []))

    # Home losses hurt the home margin; away losses help it.
    margin_shift = away["margin_loss"] - home["margin_loss"]
    total_shift = home["total_shift"] + away["total_shift"]
    sigma_add = (home["uncertainty_points"] ** 2 + away["uncertainty_points"] ** 2) ** 0.5

    reasons: List[str] = []
    for side, data in (("home", home), ("away", away)):
        abbr = game.home if side == "home" else game.away
        if data["items"]:
            top = data["items"][0]
            reasons.append(
                f"{teams.display(abbr)} injuries worth {data['margin_loss']:.1f} pts "
                f"(led by {top['player']}, {top['position']}, {top['status']})")

    if sigma_add > 1.0:
        reasons.append(f"Unresolved availability adds {sigma_add:.1f} pts of margin "
                       f"uncertainty — the model is deliberately less confident here")

    return ProjectionAdjustment(
        margin_shift=margin_shift, total_shift=total_shift, sigma_add=sigma_add,
        reasons=reasons,
        detail={"home": {"team": game.home, **_public(home)},
                "away": {"team": game.away, **_public(away)}},
    )


def _public(data: Dict[str, object]) -> Dict[str, object]:
    return {"margin_points": round(float(data["margin_loss"]), 2),
            "total_points": round(float(data["total_shift"]), 2),
            "uncertainty_points": round(float(data["uncertainty_points"]), 2),
            "players": data["items"][:8]}
