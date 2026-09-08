"""Situational model — the schedule spots and conditions the ratings cannot see.

Careful scope. Rest differential, travel distance, timezone shift and divisional status are
ALREADY fitted terms in the margin regression, and re-applying them here would double-count
effects the model has already priced. So this strategy handles only what the linear fit
cannot represent:

  * A bye week is not "six more days of rest" on a straight line. It is a different kind of
    preparation, and the linear rest term under-values it.
  * A Thursday game on four days' rest is likewise not simply "three fewer days".
  * Weather is a threshold effect, not a slope: wind matters sharply past about 15mph and
    barely at all below it, and freezing conditions suppress scoring in a way a linear
    temperature term smears out.

Everything else the situational panel displays — divisional, travel, timezone — is reported
as CONTEXT with the coefficient the regression actually fitted, so the user can see it was
accounted for without it being counted twice.
"""
from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional

from app.core.types import Game, MarketType, StrategyMeta
from app.data import teams
from app.data.nflverse import InjuryReport
from app.models.ratings import RatingSet
from app.strategies.base import ProjectionAdjustment

# Non-linear rest effects, in points of margin, on top of the fitted linear rest term.
BYE_WEEK_BONUS = 0.6          # rest of 13+ days
SHORT_WEEK_PENALTY = 0.5      # rest of 4 days or fewer (Thursday games)
BYE_REST_DAYS = 13
SHORT_REST_DAYS = 4

# Weather thresholds. Wind is the dominant weather effect on scoring; temperature only
# matters once it is genuinely cold.
WIND_THRESHOLD_MPH = 15.0
WIND_POINTS_PER_MPH_OVER = 0.35     # total points removed per mph above the threshold
WIND_MAX_PENALTY = 6.0
FREEZING_F = 32.0
COLD_TOTAL_PENALTY = 1.5
HEAVY_RAIN_PCT = 70.0
RAIN_TOTAL_PENALTY = 1.0
SNOW_TOTAL_PENALTY = 2.0

META = StrategyMeta(
    key="situational",
    name="Situational",
    market_types=[MarketType.MONEYLINE, MarketType.SPREAD, MarketType.TOTAL],
    methodology=(
        "Applies the non-linear schedule and weather effects the margin regression cannot "
        "express: bye-week preparation, short-week fatigue, and threshold weather (wind "
        "above 15mph, freezing temperatures, heavy precipitation). Rest, travel, timezone "
        "and divisional status are already fitted terms in the core model and are reported "
        "here as context rather than re-applied."
    ),
    inputs=["nflverse schedule (rest days, roof, divisional flag)",
            "team travel geography", "Open-Meteo kickoff forecast"],
    limitations=(
        "Bye and short-week adjustments are prior estimates layered on a fitted linear "
        "term. Weather thresholds are applied to a forecast, and a forecast several days "
        "out is not the conditions that will actually be played in."
    ),
)


def _kickoff_local_hour(game: Game) -> Optional[int]:
    try:
        return datetime.fromisoformat(game.kickoff).hour
    except (ValueError, TypeError):
        return None


def context(game: Game) -> Dict[str, object]:
    """Everything situational about a game, for display. Purely descriptive."""
    away_travel = teams.travel_miles(game.away, game.home)
    tz_shift = teams.timezone_shift(game.away, game.home)
    return {
        "home_rest": game.home_rest,
        "away_rest": game.away_rest,
        "rest_edge": (game.home_rest or 7) - (game.away_rest or 7),
        "home_off_bye": (game.home_rest or 0) >= BYE_REST_DAYS,
        "away_off_bye": (game.away_rest or 0) >= BYE_REST_DAYS,
        "home_short_week": (game.home_rest or 7) <= SHORT_REST_DAYS,
        "away_short_week": (game.away_rest or 7) <= SHORT_REST_DAYS,
        "away_travel_miles": away_travel,
        "away_timezone_shift": tz_shift,
        "divisional": game.div_game,
        "roof": game.roof,
        "surface": game.surface,
        "kickoff_utc_hour": _kickoff_local_hour(game),
    }


def weather_impact(weather: Optional[Dict[str, object]]) -> Dict[str, object]:
    """Points removed from the game total by conditions, with the reasons."""
    if not weather or weather.get("indoor"):
        return {"total_shift": 0.0, "reasons": [], "severity": "none"}

    reasons: List[str] = []
    shift = 0.0
    wind = weather.get("wind_mph")
    if isinstance(wind, (int, float)) and wind > WIND_THRESHOLD_MPH:
        penalty = min((wind - WIND_THRESHOLD_MPH) * WIND_POINTS_PER_MPH_OVER,
                      WIND_MAX_PENALTY)
        shift -= penalty
        reasons.append(f"{wind:.0f}mph wind removes {penalty:.1f} pts from the total")

    temp = weather.get("temperature_f")
    if isinstance(temp, (int, float)) and temp <= FREEZING_F:
        shift -= COLD_TOTAL_PENALTY
        reasons.append(f"{temp:.0f}F at kickoff removes {COLD_TOTAL_PENALTY:.1f} pts")

    snow = weather.get("snowfall_in")
    if isinstance(snow, (int, float)) and snow > 0.1:
        shift -= SNOW_TOTAL_PENALTY
        reasons.append(f"Snow in the forecast removes {SNOW_TOTAL_PENALTY:.1f} pts")
    else:
        precip = weather.get("precipitation_pct")
        if isinstance(precip, (int, float)) and precip >= HEAVY_RAIN_PCT:
            shift -= RAIN_TOTAL_PENALTY
            reasons.append(f"{precip:.0f}% precipitation chance removes "
                           f"{RAIN_TOTAL_PENALTY:.1f} pts")

    severity = "none"
    if shift <= -4.0:
        severity = "high"
    elif shift <= -1.5:
        severity = "moderate"
    elif shift < 0:
        severity = "low"
    return {"total_shift": shift, "reasons": reasons, "severity": severity}


def adjust(game: Game, ratings: RatingSet, *,
           injuries: Dict[str, List[InjuryReport]],
           depth_chart: Dict[str, List[Dict[str, object]]],
           weather: Optional[Dict[str, object]] = None) -> ProjectionAdjustment:
    ctx = context(game)
    reasons: List[str] = []
    margin_shift = 0.0

    if ctx["home_off_bye"] and not ctx["away_off_bye"]:
        margin_shift += BYE_WEEK_BONUS
        reasons.append(f"{teams.display(game.home)} coming off a bye (+{BYE_WEEK_BONUS} pts "
                       "beyond the linear rest term)")
    if ctx["away_off_bye"] and not ctx["home_off_bye"]:
        margin_shift -= BYE_WEEK_BONUS
        reasons.append(f"{teams.display(game.away)} coming off a bye (-{BYE_WEEK_BONUS} pts "
                       "beyond the linear rest term)")
    if ctx["home_short_week"] and not ctx["away_short_week"]:
        margin_shift -= SHORT_WEEK_PENALTY
        reasons.append(f"{teams.display(game.home)} on a short week")
    if ctx["away_short_week"] and not ctx["home_short_week"]:
        margin_shift += SHORT_WEEK_PENALTY
        reasons.append(f"{teams.display(game.away)} on a short week")

    wx = weather_impact(weather)
    reasons.extend(wx["reasons"])

    if ctx["divisional"]:
        reasons.append("Divisional game — familiarity historically compresses margins; the "
                       "core model already carries the fitted divisional term")

    return ProjectionAdjustment(
        margin_shift=margin_shift,
        total_shift=float(wx["total_shift"]),
        # Bad weather makes a game less predictable, not just lower-scoring.
        sigma_add=1.5 if wx["severity"] == "high" else (0.7 if wx["severity"] == "moderate" else 0.0),
        reasons=reasons,
        detail={"context": ctx, "weather": wx},
    )
