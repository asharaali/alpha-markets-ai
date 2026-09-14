"""The competitors every model change has to beat before it ships.

A model is not good because its Brier score is 0.24. It is good only if 0.24 beats what you
would have got for free, on the same games, at the same moment. So every forecast produced
by the walk-forward engine carries all of these probabilities side by side on the same row:

  MARKET      the de-vigged closing price, unchanged. This is the benchmark, and it is a
              hard one: closing lines are the most efficient prices in sports betting.
  MODEL       the NFL model alone, market never consulted.
  BLEND       model and market combined at the configured weight.
  ENSEMBLE    the full strategy stack.
  ABLATIONS   the ensemble with one component removed, to see what that component is
              actually worth rather than what it is assumed to be worth.

Scoring them on the same row is the whole point. Two models evaluated on different subsets
are not comparable, and the easiest way to invent an improvement is to quietly drop the
games one of them found hard.

Brier and log loss are both reported because they disagree in a useful way: Brier is
forgiving of confident errors, log loss is not. A model that beats the market on Brier and
loses on log loss is making a small number of very confident mistakes, which is exactly the
failure mode that empties a bankroll.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

# Every probability is clamped away from 0 and 1 before scoring. An unclamped log loss
# becomes infinite on a single confident miss, which then dominates every average it
# appears in and makes the metric useless rather than informative.
EPSILON = 1e-6


def clamp(p: float) -> float:
    return min(max(float(p), EPSILON), 1.0 - EPSILON)


def brier_score(prob: float, outcome: int) -> float:
    return (clamp(prob) - outcome) ** 2


def log_loss_score(prob: float, outcome: int) -> float:
    p = clamp(prob)
    return -(math.log(p) if outcome else math.log(1.0 - p))


@dataclass
class Baseline:
    """One competitor: a name and the probability column it reads."""

    key: str
    name: str
    column: str
    description: str
    is_reference: bool = False


BASELINES: List[Baseline] = [
    Baseline("market", "Market only", "market_prob",
             "The de-vigged closing price. Free, and very hard to beat.",
             is_reference=True),
    Baseline("model", "NFL model only", "model_prob",
             "Opponent-adjusted ratings through the fitted game model. The market is "
             "never consulted."),
    Baseline("blend", "Model + market", "blend_prob",
             "The model shaded toward the market by the configured weight."),
    Baseline("ensemble", "Full ensemble", "ensemble_prob",
             "Every strategy combined, which is what the app actually publishes."),
]


def score_rows(rows: Sequence[Dict[str, Any]], baseline: Baseline) -> List[Dict[str, Any]]:
    """Attach per-row Brier and log loss for one baseline, skipping rows it cannot price.

    The row is left without scores rather than defaulted, so the matched-observation
    filter downstream can drop it from every comparison at once instead of letting one
    model be scored on games another never saw.
    """
    out: List[Dict[str, Any]] = []
    for row in rows:
        prob = row.get(baseline.column)
        outcome = row.get("outcome")
        if prob is None or outcome is None:
            out.append(row)
            continue
        scored = dict(row)
        scored[f"brier_{baseline.key}"] = brier_score(float(prob), int(outcome))
        scored[f"logloss_{baseline.key}"] = log_loss_score(float(prob), int(outcome))
        out.append(scored)
    return out


def score_all(rows: Sequence[Dict[str, Any]],
              baselines: Optional[Sequence[Baseline]] = None) -> List[Dict[str, Any]]:
    scored = list(rows)
    for baseline in (baselines or BASELINES):
        scored = score_rows(scored, baseline)
    return scored


def matched(rows: Sequence[Dict[str, Any]],
            baselines: Optional[Sequence[Baseline]] = None) -> List[Dict[str, Any]]:
    """Only rows every baseline could price. Comparisons run on these and nothing else."""
    keys = [b.key for b in (baselines or BASELINES)]
    return [r for r in rows
            if all(r.get(f"brier_{k}") is not None for k in keys)]


def calibration_table(rows: Sequence[Dict[str, Any]], column: str, *,
                      bins: int = 10) -> List[Dict[str, Any]]:
    """Predicted versus observed frequency, bucketed.

    Calibration is the property that matters most for betting and is invisible in an
    accuracy number: a model that says 60% and is right 60% of the time is useful even if
    it is rarely confident, and a model that says 80% and is right 60% of the time will
    lose money on every bet it feels best about.
    """
    buckets: List[Dict[str, Any]] = []
    for i in range(bins):
        low, high = i / bins, (i + 1) / bins
        members = [r for r in rows
                   if r.get(column) is not None and r.get("outcome") is not None
                   and (low <= float(r[column]) < high
                        or (i == bins - 1 and float(r[column]) == 1.0))]
        if not members:
            buckets.append({"bin": f"{low:.0%}-{high:.0%}", "n": 0,
                            "predicted": None, "observed": None, "gap": None})
            continue
        predicted = sum(float(r[column]) for r in members) / len(members)
        observed = sum(int(r["outcome"]) for r in members) / len(members)
        buckets.append({
            "bin": f"{low:.0%}-{high:.0%}",
            "n": len(members),
            "games": len({r.get("game_id") for r in members}),
            "predicted": round(predicted, 4),
            "observed": round(observed, 4),
            "gap": round(observed - predicted, 4),
        })
    return buckets


def expected_calibration_error(table: Sequence[Dict[str, Any]]) -> Optional[float]:
    """Average |predicted - observed|, weighted by how many forecasts landed in each bin."""
    populated = [b for b in table if b["n"] and b["gap"] is not None]
    if not populated:
        return None
    total = sum(b["n"] for b in populated)
    return sum(abs(b["gap"]) * b["n"] for b in populated) / total


@dataclass
class Ablation:
    """One component removed, to measure what it contributes rather than assume it."""

    key: str
    name: str
    removes: str
    description: str


ABLATIONS: List[Ablation] = [
    Ablation("no_market_blend", "Without market blending", "blend",
             "The model's own probability, unshaded. Tests whether blending toward the "
             "market helps or merely hides the model."),
    Ablation("no_key_numbers", "Without key-number profile", "key_profile",
             "A smooth Normal margin distribution. Tests whether the 2.5x spike at 3 is "
             "worth the complexity it adds."),
    Ablation("no_injury_adjustment", "Without injury adjustment", "injuries",
             "Projections unadjusted for availability. Tests whether the injury feed "
             "improves forecasts or just adds noise."),
    Ablation("no_rest_travel", "Without rest and travel", "rest",
             "Drops the rest-days and travel terms from the margin model."),
    Ablation("no_weather", "Without weather", "weather",
             "Drops wind and venue effects from the total model."),
]


def summarise_baseline(rows: Sequence[Dict[str, Any]], baseline: Baseline,
                       *, label: str = "") -> Dict[str, Any]:
    """The scorecard for one competitor on one set of matched rows."""
    from app.evaluation import protocol

    brier = protocol.clustered_mean(rows, f"brier_{baseline.key}")
    logloss = protocol.clustered_mean(rows, f"logloss_{baseline.key}")
    table = calibration_table(rows, baseline.column)
    ece = expected_calibration_error(table)

    return {
        "key": baseline.key,
        "name": baseline.name,
        "label": label,
        "description": baseline.description,
        "is_reference": baseline.is_reference,
        "brier": round(brier["mean"], 5) if brier else None,
        "brier_se": round(brier["standard_error"], 5)
                    if brier and brier.get("standard_error") else None,
        "log_loss": round(logloss["mean"], 5) if logloss else None,
        "log_loss_se": round(logloss["standard_error"], 5)
                       if logloss and logloss.get("standard_error") else None,
        "calibration": table,
        "calibration_error": round(ece, 5) if ece is not None else None,
        # Both numbers, always. The row count is what the old page reported and it is the
        # misleading one; the game count is the real sample size.
        "observations": brier["observations"] if brier else 0,
        "games": brier["clusters"] if brier else 0,
    }
