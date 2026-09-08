"""Fitting the model artifacts: rating -> points mapping, spread of outcomes, key numbers.

Two things are fitted here, both from historical data, both cached to disk:

1. The SECOND-STAGE map. Team ratings are in EPA-per-play and points-per-game units; a
   bettor needs an expected margin and an expected total in real points, plus honest
   uncertainty around them. We fit that map walk-forward — for every historical game, the
   predictors are ratings built only from games played before it — so the residual spread
   we measure is out-of-sample error, not in-sample flattery.

2. The KEY-NUMBER profiles. Using every game on file with a closing line, we compare how
   often each exact margin and total actually occurred against how often a smooth Normal
   model says it should. The resulting multipliers are what make a -2.5 and a -3.5 price
   differently, as they must.

Nothing here is hand-tuned. If the data says home-field is worth 1.6 points, that is what
the model uses.
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from app.config import settings
from app.core.logging import get_logger
from app.data import nflverse, teams
from app.models import distributions as dist
from app.models.linalg import (SingularSystem, least_squares_stats,
                               r_squared, rmse)
from app.models.ratings import RatingSet, build as build_ratings

log = get_logger(__name__)

ARTIFACT_DIR = Path(settings.CACHE_DIR) / "artifacts"
ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
ARTIFACT_PATH = ARTIFACT_DIR / "game_model.json"

# Artifact schema version. Bump when the predictor list changes so a stale file on a
# deployed disk is refitted instead of silently mis-indexing coefficients.
ARTIFACT_VERSION = 7

MARGIN_FEATURES = ["intercept", "epa_diff", "points_diff", "rest_diff",
                   "away_travel_k", "away_tz_shift", "div_game"]
TOTAL_FEATURES = ["intercept", "points_sum", "epa_sum", "plays_sum", "indoor", "wind"]

# Indices of the terms that ARE the model rather than adjustments to it. Everything
# else is shrunk by how strongly the data supports it, so a coefficient that is
# indistinguishable from noise cannot quietly move a projection by a point.
MARGIN_PROTECTED = (0, 1, 2)
TOTAL_PROTECTED = (0, 1, 2)

# Seasons of closing lines used for the key-number profiles. Scoring environment shifted
# materially with the mid-2010s rule changes, so we do not reach back further.
KEY_NUMBER_FROM_SEASON = 2010


@dataclass
class GameModelArtifact:
    version: int
    fitted_at: float
    seasons_fitted: List[int]
    sample_games: int

    margin_coefficients: List[float]
    margin_sigma: float
    margin_r2: float
    margin_rmse: float

    total_coefficients: List[float]
    total_sigma: float
    total_r2: float
    total_rmse: float

    # JSON object keys are strings; converted back to ints on load.
    margin_key_profile: Dict[str, float] = field(default_factory=dict)
    total_key_profile: Dict[str, float] = field(default_factory=dict)
    key_number_games: int = 0
    # Pre-shrinkage estimates and their t-statistics, kept so the Model Lab can show which
    # factors are genuinely supported instead of presenting every coefficient as fact.
    margin_diagnostics: List[Dict[str, float]] = field(default_factory=list)
    total_diagnostics: List[Dict[str, float]] = field(default_factory=list)
    # Correlation between margin error and total error, measured from history. Needed to
    # simulate a game's margin and total JOINTLY — same-game parlay legs live or die on
    # this number, and assuming zero would quietly misprice every one of them.
    margin_total_correlation: float = 0.0

    def margin_profile(self) -> Dict[int, float]:
        return {int(k): v for k, v in self.margin_key_profile.items()}

    def total_profile(self) -> Dict[int, float]:
        return {int(k): v for k, v in self.total_key_profile.items()}

    def summary(self) -> Dict[str, object]:
        return {
            "version": self.version,
            "fitted_at": self.fitted_at,
            "seasons_fitted": self.seasons_fitted,
            "sample_games": self.sample_games,
            "margin": {
                "features": MARGIN_FEATURES,
                "coefficients": [round(c, 4) for c in self.margin_coefficients],
                "sigma": round(self.margin_sigma, 2),
                "r2": round(self.margin_r2, 4),
                "rmse": round(self.margin_rmse, 3),
            },
            "total": {
                "features": TOTAL_FEATURES,
                "coefficients": [round(c, 4) for c in self.total_coefficients],
                "sigma": round(self.total_sigma, 2),
                "r2": round(self.total_r2, 4),
                "rmse": round(self.total_rmse, 3),
            },
            "margin_total_correlation": round(self.margin_total_correlation, 4),
            "margin_diagnostics": self.margin_diagnostics,
            "total_diagnostics": self.total_diagnostics,
            "key_numbers": {
                "games": self.key_number_games,
                "margin_top": _top_profile(self.margin_profile()),
                "total_top": _top_profile(self.total_profile()),
            },
        }


def _top_profile(profile: Dict[int, float], n: int = 8) -> List[Tuple[int, float]]:
    items = sorted(profile.items(), key=lambda kv: kv[1], reverse=True)[:n]
    return [(k, round(v, 3)) for k, v in items]


# --------------------------------------------------------------------- predictors

def margin_predictors(rs: RatingSet, home: str, away: str, *,
                      home_rest: Optional[int], away_rest: Optional[int],
                      div_game: bool) -> List[float]:
    """The feature vector behind every expected-margin prediction. One definition, used by
    the fit, the live model, and the backtester alike — so they cannot drift apart."""
    home_epa = rs.expected("epa_per_play", home, away, home=True)
    away_epa = rs.expected("epa_per_play", away, home, home=False)
    home_pts = rs.expected("points", home, away, home=True)
    away_pts = rs.expected("points", away, home, home=False)
    rest_diff = float((home_rest or 7) - (away_rest or 7))
    travel = teams.travel_miles(away, home) / 1000.0
    tz_shift = float(teams.timezone_shift(away, home))
    return [1.0, home_epa - away_epa, home_pts - away_pts, rest_diff,
            travel, tz_shift, 1.0 if div_game else 0.0]


def total_predictors(rs: RatingSet, home: str, away: str, *,
                     indoor: bool, wind: Optional[float]) -> List[float]:
    home_epa = rs.expected("epa_per_play", home, away, home=True)
    away_epa = rs.expected("epa_per_play", away, home, home=False)
    home_pts = rs.expected("points", home, away, home=True)
    away_pts = rs.expected("points", away, home, home=False)
    home_plays = rs.expected("plays_per_game", home, away, home=True)
    away_plays = rs.expected("plays_per_game", away, home, home=False)
    # Wind only matters outdoors, and an unknown wind is not a calm day — treat missing as
    # the league-average breeze so an unreported game is not silently scored as a dome.
    w = 0.0 if indoor else float(wind if wind is not None else 8.0)
    return [1.0, home_pts + away_pts, home_epa + away_epa, home_plays + away_plays,
            1.0 if indoor else 0.0, w]


def _is_indoor(game) -> bool:
    roof = (game.roof or "").lower()
    if roof in {"dome", "closed"}:
        return True
    if roof in {"outdoors", "open"}:
        return False
    home = teams.get(game.home)
    return bool(home and home.roof == "dome")


# --------------------------------------------------------------------- key numbers

async def fit_key_numbers(from_season: int = KEY_NUMBER_FROM_SEASON
                          ) -> Tuple[Dict[int, float], Dict[int, float], int]:
    """Empirical key-number multipliers for margin and total.

    For each completed historical game with a closing line we ask a smooth Normal model how
    often each exact outcome should occur, sum those expectations across all games, and
    compare to what actually happened. Margins of 3 land far more often than smoothness
    predicts; that ratio is the multiplier.
    """
    games = await nflverse.schedule()
    rows = [g for g in games
            if g.completed and g.season >= from_season
            and g.spread_line is not None and g.total_line is not None
            and g.margin is not None and g.total_points is not None]
    if len(rows) < 500:
        log.warning("only %d games available for key-number fitting; skipping profiles",
                    len(rows))
        return {}, {}, len(rows)

    # Provisional spreads from the market itself, so the profile isolates the SHAPE of the
    # outcome distribution rather than our own model's bias.
    margin_sigma = _sigma([g.margin - g.spread_line for g in rows])
    total_sigma = _sigma([g.total_points - g.total_line for g in rows])

    margin_obs: Dict[int, float] = {}
    margin_exp: Dict[int, float] = {}
    total_obs: Dict[int, float] = {}
    total_exp: Dict[int, float] = {}

    for g in rows:
        margin_obs[g.margin] = margin_obs.get(g.margin, 0.0) + 1.0
        total_obs[g.total_points] = total_obs.get(g.total_points, 0.0) + 1.0
        for k in range(int(g.spread_line - 4 * margin_sigma),
                       int(g.spread_line + 4 * margin_sigma) + 1):
            margin_exp[k] = margin_exp.get(k, 0.0) + dist.normal_pmf(k, g.spread_line,
                                                                     margin_sigma)
        lo = max(0, int(g.total_line - 4 * total_sigma))
        for k in range(lo, int(g.total_line + 4 * total_sigma) + 1):
            total_exp[k] = total_exp.get(k, 0.0) + dist.normal_pmf(k, g.total_line,
                                                                   total_sigma)

    margin_profile = dist.fit_profile(margin_obs, margin_exp)
    total_profile = dist.fit_profile(total_obs, total_exp)
    log.info("key-number profiles fitted from %d games (margin sigma %.2f, total sigma %.2f)",
             len(rows), margin_sigma, total_sigma)
    return margin_profile, total_profile, len(rows)


def _correlation(a: Sequence[float], b: Sequence[float]) -> float:
    """Pearson correlation, used for the joint margin/total simulation."""
    n = min(len(a), len(b))
    if n < 3:
        return 0.0
    mean_a = sum(a[:n]) / n
    mean_b = sum(b[:n]) / n
    cov = sum((a[i] - mean_a) * (b[i] - mean_b) for i in range(n))
    var_a = sum((a[i] - mean_a) ** 2 for i in range(n))
    var_b = sum((b[i] - mean_b) ** 2 for i in range(n))
    if var_a <= 0 or var_b <= 0:
        return 0.0
    return max(min(cov / math.sqrt(var_a * var_b), 0.95), -0.95)


def _sigma(residuals: Sequence[float]) -> float:
    n = len(residuals)
    if n < 2:
        return 13.5
    mean = sum(residuals) / n
    var = sum((r - mean) ** 2 for r in residuals) / (n - 1)
    return math.sqrt(max(var, 1e-6))


# --------------------------------------------------------------------- second stage

async def fit(*, seasons: Optional[Sequence[int]] = None,
              force: bool = False) -> GameModelArtifact:
    """Fit (or load) the game-model artifact.

    Walk-forward by construction: the ratings used as predictors for a week-N game are
    built from weeks 1..N-1 only. This is the same guarantee the backtester relies on, and
    the reason the reported RMSE is a fair estimate of live error.
    """
    if not force:
        cached = load()
        if cached is not None:
            return cached

    started = time.time()
    usable = await nflverse.available_seasons(settings.SEASON,
                                              max(settings.CALIBRATION_SEASONS, 4))
    if not usable:
        raise SingularSystem("no play-by-play seasons available to fit the game model")
    # Need at least one prior season behind each fitted season for week-1 ratings.
    fit_seasons = sorted(s for s in usable if (s - 1) in usable)
    if seasons:
        fit_seasons = [s for s in fit_seasons if s in set(seasons)]
    if not fit_seasons:
        fit_seasons = [max(usable)]

    schedule = await nflverse.schedule(seasons=fit_seasons)
    by_week: Dict[Tuple[int, int], List] = {}
    for g in schedule:
        if g.completed and g.game_type == "REG":
            by_week.setdefault((g.season, g.week), []).append(g)

    margin_x: List[List[float]] = []
    margin_y: List[float] = []
    total_x: List[List[float]] = []
    total_y: List[float] = []

    for (season, week) in sorted(by_week):
        rs = await build_ratings(season, week)
        if rs.sample_games == 0:
            continue
        for g in by_week[(season, week)]:
            margin_x.append(margin_predictors(rs, g.home, g.away,
                                              home_rest=g.home_rest,
                                              away_rest=g.away_rest,
                                              div_game=g.div_game))
            margin_y.append(float(g.margin))
            total_x.append(total_predictors(rs, g.home, g.away,
                                            indoor=_is_indoor(g), wind=g.wind))
            total_y.append(float(g.total_points))

    if len(margin_y) < 100:
        raise SingularSystem(f"only {len(margin_y)} fitted games; refusing to publish a "
                             "game model on that little evidence")

    margin_fit = least_squares_stats(margin_x, margin_y, penalty=1e-3)
    total_fit = least_squares_stats(total_x, total_y, penalty=1e-3)
    margin_beta = _recenter(margin_fit.shrunk(protect=MARGIN_PROTECTED), margin_x, margin_y)
    total_beta = _recenter(total_fit.shrunk(protect=TOTAL_PROTECTED), total_x, total_y)

    # Re-measure fit quality with the coefficients we will actually ship, not the
    # unshrunk ones — otherwise the published RMSE describes a model we do not use.
    margin_pred = [_dot(margin_beta, x) for x in margin_x]
    total_pred = [_dot(total_beta, x) for x in total_x]
    margin_resid = [a - p for a, p in zip(margin_y, margin_pred)]
    total_resid = [a - p for a, p in zip(total_y, total_pred)]

    m_profile, t_profile, key_games = await fit_key_numbers()

    artifact = GameModelArtifact(
        version=ARTIFACT_VERSION,
        fitted_at=time.time(),
        seasons_fitted=fit_seasons,
        sample_games=len(margin_y),
        margin_coefficients=margin_beta,
        margin_sigma=_sigma(margin_resid),
        margin_r2=r_squared(margin_y, margin_pred),
        margin_rmse=rmse(margin_y, margin_pred),
        total_coefficients=total_beta,
        total_sigma=_sigma(total_resid),
        total_r2=r_squared(total_y, total_pred),
        total_rmse=rmse(total_y, total_pred),
        margin_diagnostics=_diagnostics(MARGIN_FEATURES, margin_fit, margin_beta),
        total_diagnostics=_diagnostics(TOTAL_FEATURES, total_fit, total_beta),
        margin_total_correlation=_correlation(margin_resid, total_resid),
        margin_key_profile={str(k): round(v, 5) for k, v in m_profile.items()},
        total_key_profile={str(k): round(v, 5) for k, v in t_profile.items()},
        key_number_games=key_games,
    )
    save(artifact)
    log.info("game model fitted on %d games from %s in %.1fs "
             "(margin RMSE %.2f, sigma %.2f; total RMSE %.2f, sigma %.2f)",
             artifact.sample_games, fit_seasons, time.time() - started,
             artifact.margin_rmse, artifact.margin_sigma,
             artifact.total_rmse, artifact.total_sigma)
    return artifact


def _dot(beta: Sequence[float], x: Sequence[float]) -> float:
    return sum(b * v for b, v in zip(beta, x))


def _recenter(beta: List[float], design: Sequence[Sequence[float]],
              targets: Sequence[float]) -> List[float]:
    """Re-fit the intercept after shrinkage so predictions stay unbiased.

    Shrinking the coefficient on a feature whose mean is not zero (plays per game averages
    about 124, wind about 8mph) removes `delta * mean(x)` from EVERY prediction. Left
    uncorrected that is a systematic bias, not a regularisation — it is what pushed the
    totals model below the accuracy of simply predicting the league average. The intercept
    absorbs it.
    """
    if not design:
        return beta
    adjusted = list(beta)
    mean_pred = sum(_dot(adjusted, x) for x in design) / len(design)
    mean_actual = sum(targets) / len(targets)
    adjusted[0] += mean_actual - mean_pred
    return adjusted


def _diagnostics(names: Sequence[str], fit, shipped: Sequence[float]
                 ) -> List[Dict[str, float]]:
    """Per-coefficient transparency: raw estimate, its uncertainty, and what we shipped."""
    out = []
    for i, name in enumerate(names):
        t = fit.t_stats()[i]
        out.append({
            "feature": name,
            "estimate": round(fit.coefficients[i], 4),
            "std_error": round(fit.std_errors[i], 4),
            "t_stat": round(t, 2),
            "shipped": round(shipped[i], 4),
            "supported": abs(t) >= 2.0,
        })
    return out


def save(artifact: GameModelArtifact) -> None:
    ARTIFACT_PATH.write_text(json.dumps(asdict(artifact), indent=2))


def load() -> Optional[GameModelArtifact]:
    if not ARTIFACT_PATH.exists():
        return None
    try:
        blob = json.loads(ARTIFACT_PATH.read_text())
        if blob.get("version") != ARTIFACT_VERSION:
            log.info("game model artifact is version %s, need %s — refitting",
                     blob.get("version"), ARTIFACT_VERSION)
            return None
        return GameModelArtifact(**blob)
    except (json.JSONDecodeError, TypeError) as exc:
        log.warning("game model artifact unreadable (%s); refitting", exc)
        return None
