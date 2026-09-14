"""The rules a forecast evaluation has to obey to mean anything.

Three separate mistakes were inflating this system's apparent evidence, and they compound:

  1. LOOK-AHEAD. The ratings were walk-forward, but the second-stage coefficients, residual
     sigmas, margin/total correlation and key-number profiles were fitted by pooling every
     season in the request — including the ones being scored. The old backtest even said so
     in its `limitations` field and called the advantage "real though small". It is neither
     measurable nor small until you remove it, which is what `as_of_season` is for.

  2. REPEATED FORECASTS. The snapshot job records a fresh prediction row every hour. In the
     live database that turned 16 distinct games into 54,422 prediction rows and 4,077
     "settled predictions". Scoring those as independent observations overstates the
     evidence by a factor of roughly 250 and makes every confidence interval a fiction. A
     forecast refreshed 43 times is one forecast observed 43 times.

  3. RELATED CONTRACTS. A moneyline, four spread rungs and a team total on the same game
     are not six independent tests of the model. They resolve off one scoreline. Treating
     them as separate observations inflates n the same way, just more subtly.

The unit of independent evidence here is the GAME, not the row. Everything below is built
around that: forecasts are clustered by game, standard errors are computed between clusters
rather than between rows, and the reported n always carries the cluster count beside it.

Evaluation checkpoints exist because "before kickoff" is not one moment. A forecast made
six days out and one made an hour out are different claims about different information
sets, and averaging them hides which one the model is actually good at.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# Hours before kickoff at which a forecast is scored. A prediction is assigned to the
# nearest checkpoint at or after it, so each game contributes at most one row per
# checkpoint per contract.
CHECKPOINTS_HOURS: Tuple[float, ...] = (168.0, 24.0, 1.0)

CHECKPOINT_LABELS = {
    168.0: "one week out",
    24.0: "24 hours out",
    1.0: "one hour out",
}


@dataclass(frozen=True)
class Split:
    """A chronological train / validation / test partition.

    Chronological means by SEASON, not by shuffling games. A random split lets a model
    trained on week 12 predict week 3 of the same season, having already seen how that team
    turned out. Every split boundary here is a point in time you could actually have stood
    at.
    """

    train: Tuple[int, ...]
    validation: Tuple[int, ...]
    test: Tuple[int, ...]

    def describe(self) -> Dict[str, Any]:
        return {
            "train_seasons": list(self.train),
            "validation_seasons": list(self.validation),
            "test_seasons": list(self.test),
            "rule": (
                "Chronological by season. Anything fitted for a season uses only seasons "
                "strictly before it. The test seasons are not consulted while choosing "
                "anything — no feature selection, no blend weight, no threshold."),
        }


def chronological_split(seasons: Sequence[int], *, test_seasons: int = 1,
                        validation_seasons: int = 1) -> Split:
    """Carve the available seasons into train / validation / held-out test.

    The most recent seasons are the test set, because the only interesting question is
    whether the model works going forward, and the second-most-recent are validation, where
    every tuning decision has to be made.
    """
    ordered = sorted(set(int(s) for s in seasons))
    if len(ordered) < test_seasons + validation_seasons + 1:
        raise ValueError(
            f"need at least {test_seasons + validation_seasons + 1} seasons to build a "
            f"train/validation/test split; got {len(ordered)}")
    test = tuple(ordered[-test_seasons:])
    validation = tuple(ordered[-(test_seasons + validation_seasons):-test_seasons])
    train = tuple(ordered[:-(test_seasons + validation_seasons)])
    return Split(train=train, validation=validation, test=test)


def eligible_checkpoints(hours_before_kickoff: Optional[float]) -> Tuple[float, ...]:
    """Which checkpoints this forecast was already available at.

    `hours_before_kickoff` counts DOWN to kickoff, so a bigger number means earlier. A
    forecast made 30 hours out exists at the 24-hour checkpoint and at the 1-hour one, but
    not at the one-week checkpoint, because a week before kickoff it had not been made yet.

    Getting this backwards is easy and quietly fatal: it would let a forecast made 20
    hours before kickoff be scored as the model's 24-hours-out opinion, crediting it with
    information it did not have.
    """
    if hours_before_kickoff is None or hours_before_kickoff < 0:
        return ()
    return tuple(sorted((c for c in CHECKPOINTS_HOURS
                         if hours_before_kickoff >= c), reverse=True))


def latest_checkpoint(hours_before_kickoff: Optional[float]) -> Optional[float]:
    """The earliest checkpoint this forecast qualifies for, or None."""
    eligible = eligible_checkpoints(hours_before_kickoff)
    return max(eligible) if eligible else None


def forecast_key(row: Dict[str, Any]) -> str:
    """Identity of the thing being forecast, independent of when it was refreshed.

    Two rows sharing this key are the same claim about the same contract, recorded twice.
    """
    return "|".join(str(row.get(part) or "") for part in
                    ("game_id", "strategy", "market_type", "selection"))


def cluster_key(row: Dict[str, Any]) -> str:
    """Identity of the independent unit of evidence: the game.

    Every contract on one game resolves off one scoreline, so they share a cluster.
    """
    return str(row.get("game_id") or "")


def deduplicate(rows: Sequence[Dict[str, Any]], *,
                checkpoint: Optional[float] = None) -> List[Dict[str, Any]]:
    """Collapse repeated refreshes of the same forecast to one row.

    Which row survives is not arbitrary. For a checkpoint C hours before kickoff, only
    forecasts made at least C hours out are eligible, and the LATEST of those wins — that
    is the model's best opinion as of the checkpoint. With no checkpoint we keep the last
    forecast before kickoff, the model's final word.

    The eligibility direction matters. `horizon_hours` counts down to kickoff, so keeping
    rows with `horizon <= C` would score a forecast made 20 hours out as the 24-hour
    opinion and credit it with four hours of information it did not have.

    This is the single largest correction in the evaluation. Without it the live
    performance page was reporting 4,077 observations drawn from 16 games.
    """
    best: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        hours = row.get("horizon_hours")
        if checkpoint is not None:
            if hours is None or float(hours) < checkpoint:
                continue
        key = forecast_key(row)
        current = best.get(key)
        if current is None:
            best[key] = row
            continue
        if _ordering(row) > _ordering(current):
            best[key] = row
    return list(best.values())


def _ordering(row: Dict[str, Any]) -> float:
    """Later forecasts sort higher. Prefers wall-clock time, falls back to horizon."""
    created = row.get("created_at")
    if created is not None:
        return float(created)
    hours = row.get("horizon_hours")
    return -float(hours) if hours is not None else 0.0


def clusters(rows: Sequence[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(cluster_key(row), []).append(row)
    return grouped


def clustered_mean(rows: Sequence[Dict[str, Any]], value: str
                   ) -> Optional[Dict[str, Any]]:
    """Mean of `value` with a standard error computed BETWEEN games, not between rows.

    The row-level standard error answers "how precisely do I know the average of these
    numbers", which nobody asked. The clustered one answers "how precisely do I know this
    model's skill", and it is the larger, honest number — typically several times larger
    once six contracts per game are collapsed into one.
    """
    grouped = clusters([r for r in rows if r.get(value) is not None])
    if not grouped:
        return None
    per_cluster = [
        sum(float(r[value]) for r in group) / len(group)
        for group in grouped.values()
    ]
    n_clusters = len(per_cluster)
    mean = sum(per_cluster) / n_clusters
    if n_clusters < 2:
        return {"mean": mean, "standard_error": None, "clusters": n_clusters,
                "observations": sum(len(g) for g in grouped.values()),
                "note": "One game is not a sample; no interval can be computed."}
    variance = sum((x - mean) ** 2 for x in per_cluster) / (n_clusters - 1)
    se = math.sqrt(variance / n_clusters)
    return {
        "mean": mean,
        "standard_error": se,
        "ci95": [mean - 1.96 * se, mean + 1.96 * se],
        "clusters": n_clusters,
        "observations": sum(len(g) for g in grouped.values()),
    }


def paired_difference(rows: Sequence[Dict[str, Any]], a: str, b: str
                      ) -> Optional[Dict[str, Any]]:
    """Is A better than B on the SAME observations? Clustered, paired, per game.

    Comparing two models on different row sets is the easiest way to manufacture an
    improvement — drop the hard games from one and its Brier score falls. Everything here
    is computed only on rows where both scores exist, and the test is on the per-game
    difference, which also removes the game-to-game variance that dominates the raw scores.
    """
    usable = [r for r in rows if r.get(a) is not None and r.get(b) is not None]
    if not usable:
        return None
    grouped = clusters(usable)
    diffs = [
        sum(float(r[a]) - float(r[b]) for r in group) / len(group)
        for group in grouped.values()
    ]
    n = len(diffs)
    mean = sum(diffs) / n
    if n < 2:
        return {"difference": mean, "clusters": n, "significant": False,
                "note": "A single game cannot establish a difference."}
    variance = sum((d - mean) ** 2 for d in diffs) / (n - 1)
    se = math.sqrt(variance / n)
    t = mean / se if se > 0 else 0.0
    return {
        "difference": mean,
        "standard_error": se,
        "ci95": [mean - 1.96 * se, mean + 1.96 * se],
        "t_statistic": t,
        "clusters": n,
        "observations": len(usable),
        # Two-sided, normal approximation. With a few hundred game clusters the t and z
        # critical values agree to the second decimal, and the model error dwarfs both.
        "significant": abs(t) > 1.96,
        "better": a if mean < 0 else b,
        "note": (
            f"Paired by game on {n} games where both had a forecast. Negative means "
            f"{a} scores lower, and for Brier and log loss lower is better."),
    }


@dataclass
class EvaluationGuard:
    """Records what each fitted object was allowed to see, so the claim is checkable.

    An assertion in a docstring is not a guarantee. This carries, for every scored season,
    the exact seasons its artifact was fitted on, and re-derives the verdict rather than
    trusting it.

    The check is PER SCORED SEASON, which matters. Season 2025's artifact is legitimately
    fitted on 2022-2024, and 2022 is itself a scored season elsewhere in the same run —
    scored there by its own artifact, fitted on seasons before 2022. Comparing the union of
    everything fitted against the union of everything scored flags that as a leak when it
    is not one. The question is only ever whether a given season's predictions saw that
    season, or any later one.
    """

    as_of_season: int
    ratings_max_week: Dict[Tuple[int, int], int] = field(default_factory=dict)
    # scored season -> the seasons its artifact was fitted on
    fits_by_season: Dict[int, Tuple[int, ...]] = field(default_factory=dict)
    scored_seasons: Tuple[int, ...] = ()

    @property
    def fitted_seasons(self) -> Tuple[int, ...]:
        """Every season that fed any artifact, for reporting. Not the leak test."""
        union: set = set()
        for seasons in self.fits_by_season.values():
            union.update(seasons)
        return tuple(sorted(union))

    def violations(self) -> List[str]:
        problems: List[str] = []
        for scored_season, fitted in sorted(self.fits_by_season.items()):
            leaked = sorted(s for s in fitted if s >= scored_season)
            if leaked:
                problems.append(
                    f"predictions for {scored_season} used coefficients fitted on season(s) "
                    f"{leaked}, which are not strictly earlier")
        for (season, week), max_week in sorted(self.ratings_max_week.items()):
            if max_week >= week:
                problems.append(
                    f"ratings for {season} week {week} were built with data through week "
                    f"{max_week}, which includes the week being predicted")
        return problems

    def to_dict(self) -> Dict[str, Any]:
        problems = self.violations()
        return {
            "as_of_season": self.as_of_season,
            "fits_by_scored_season": {str(k): list(v)
                                      for k, v in sorted(self.fits_by_season.items())},
            "fitted_seasons": list(self.fitted_seasons),
            "scored_seasons": list(self.scored_seasons),
            "weeks_checked": len(self.ratings_max_week),
            "violations": problems,
            "clean": not problems,
            "statement": (
                "Every fitted component — ratings, second-stage coefficients, residual "
                "sigmas, margin/total correlation and key-number profiles — used only "
                "seasons strictly before the season being scored, and ratings for week N "
                "used only weeks before N. Checked per scored season, not asserted."
                if not problems else
                f"LOOK-AHEAD DETECTED: {len(problems)} violation(s). These results are not "
                "evidence of anything."),
        }
