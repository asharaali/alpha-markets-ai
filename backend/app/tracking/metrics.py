"""Scoring a track record honestly.

Hit rate is close to meaningless on its own. A model that only ever predicts 90% favourites
will show a gorgeous 88% hit rate and be losing money; a model that finds edges on 40% dogs
will show a terrible one and be printing. So the metrics that matter here are:

  * BRIER SCORE and LOG LOSS — is the probability itself any good? Both are compared to the
    market's own probability on the same predictions, because beating the market is the
    only benchmark that means anything.
  * CALIBRATION — when it says 60%, does it happen 60% of the time? Broken out by bucket,
    with the sample size in each, so a bucket holding four predictions is visibly a bucket
    holding four predictions.
  * ROI and expected-vs-actual value — did the edge the model claimed actually show up?
  * CLOSING LINE VALUE — did the market move toward our price after we predicted? This is
    the fastest-converging evidence available that a model is finding real information,
    because it is measurable long before enough games have settled to trust a win rate.
"""
from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Dict, List, Optional, Sequence

# Probability buckets for the calibration table.
BUCKETS = [(0.0, 0.1), (0.1, 0.2), (0.2, 0.3), (0.3, 0.4), (0.4, 0.5),
           (0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.0)]

# Below this, a metric is reported but explicitly marked as not yet meaningful.
MEANINGFUL_SAMPLE = 30


def brier(probs: Sequence[float], outcomes: Sequence[int]) -> Optional[float]:
    """Mean squared error of the probability. Lower is better; 0.25 is a coin flip."""
    if not probs:
        return None
    return sum((p - o) ** 2 for p, o in zip(probs, outcomes)) / len(probs)


def log_loss(probs: Sequence[float], outcomes: Sequence[int]) -> Optional[float]:
    """Mean negative log likelihood. Punishes confident mistakes far harder than Brier."""
    if not probs:
        return None
    total = 0.0
    for p, o in zip(probs, outcomes):
        clipped = min(max(p, 1e-6), 1 - 1e-6)
        total += -(math.log(clipped) if o else math.log(1 - clipped))
    return total / len(probs)


def calibration(probs: Sequence[float], outcomes: Sequence[int]) -> List[Dict[str, Any]]:
    """Predicted vs. observed frequency, bucketed. Sample size is always reported."""
    rows: List[Dict[str, Any]] = []
    for low, high in BUCKETS:
        picked = [(p, o) for p, o in zip(probs, outcomes) if low <= p < high or
                  (high == 1.0 and p == 1.0)]
        if not picked:
            rows.append({"bucket": f"{int(low*100)}-{int(high*100)}%", "n": 0,
                         "predicted": None, "observed": None, "gap": None})
            continue
        predicted = sum(p for p, _ in picked) / len(picked)
        observed = sum(o for _, o in picked) / len(picked)
        rows.append({
            "bucket": f"{int(low*100)}-{int(high*100)}%",
            "n": len(picked),
            "predicted": round(predicted, 4),
            "observed": round(observed, 4),
            "gap": round(observed - predicted, 4),
        })
    return rows


def calibration_error(rows: Sequence[Dict[str, Any]]) -> Optional[float]:
    """Sample-weighted mean absolute calibration gap (expected calibration error)."""
    total_n = sum(r["n"] for r in rows)
    if not total_n:
        return None
    return sum(abs(r["gap"]) * r["n"] for r in rows if r["gap"] is not None) / total_n


def roi(predictions: Sequence[Dict[str, Any]]) -> Optional[Dict[str, float]]:
    """Flat-stake return if every settled prediction had been backed for $1.

    Deliberately flat-staked: a Kelly-weighted ROI mixes the quality of the model with the
    quality of the staking plan, and we want to know about the model.
    """
    staked = 0.0
    returned = 0.0
    for row in predictions:
        cost = row.get("cost")
        outcome = row.get("outcome")
        if cost is None or outcome is None or cost <= 0:
            continue
        staked += 1.0
        if outcome:
            returned += 1.0 / cost      # $1 buys 1/cost contracts, each paying $1
    if staked <= 0:
        return None
    return {"staked": round(staked, 2), "returned": round(returned, 2),
            "net": round(returned - staked, 2),
            "roi": round((returned - staked) / staked, 4)}


def expected_vs_actual(predictions: Sequence[Dict[str, Any]]) -> Optional[Dict[str, float]]:
    """Claimed EV against realised EV — did the edge the model advertised turn up?"""
    claimed = [r["ev_per_dollar"] for r in predictions if r.get("ev_per_dollar") is not None]
    money = roi(predictions)
    if not claimed or money is None:
        return None
    return {
        "expected_ev_per_dollar": round(sum(claimed) / len(claimed), 4),
        "actual_roi": money["roi"],
        "gap": round(money["roi"] - (sum(claimed) / len(claimed)), 4),
        "n": len(claimed),
    }


def closing_line_value(predictions: Sequence[Dict[str, Any]]) -> Optional[Dict[str, float]]:
    """Average CLV, and how often it was positive.

    Signed so that positive always means the market moved toward the side we took.
    """
    values: List[float] = []
    for row in predictions:
        clv = row.get("clv")
        if clv is None or row.get("market_prob") is None or row.get("fair_prob") is None:
            continue
        # We backed the side we thought was underpriced, so a market that rose afterwards
        # confirms us only if we were on the "yes" side of that move.
        direction = 1.0 if row["fair_prob"] >= row["market_prob"] else -1.0
        values.append(clv * direction)
    if not values:
        return None
    positive = sum(1 for v in values if v > 0)
    return {"mean_clv": round(sum(values) / len(values), 4),
            "positive_rate": round(positive / len(values), 4),
            "n": len(values)}


def drawdown(equity_curve: Sequence[float]) -> Dict[str, float]:
    """Worst peak-to-trough decline along a running equity curve."""
    peak = float("-inf")
    worst = 0.0
    worst_peak = 0.0
    for value in equity_curve:
        peak = max(peak, value)
        decline = value - peak
        if decline < worst:
            worst = decline
            worst_peak = peak
    return {"max_drawdown": round(worst, 2),
            "max_drawdown_pct": round(worst / worst_peak, 4) if worst_peak else 0.0}


def equity_curve(predictions: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Cumulative flat-stake profit over time, for the performance chart."""
    ordered = sorted((p for p in predictions if p.get("outcome") is not None
                      and p.get("cost")),
                     key=lambda p: p.get("settled_at") or p.get("created_at") or 0)
    running = 0.0
    out: List[Dict[str, Any]] = []
    for row in ordered:
        cost = float(row["cost"])
        running += ((1.0 / cost) - 1.0) if row["outcome"] else -1.0
        out.append({"at": row.get("settled_at") or row.get("created_at"),
                    "cumulative": round(running, 3),
                    "strategy": row.get("strategy"),
                    "selection": row.get("selection")})
    return out


def summarise(predictions: Sequence[Dict[str, Any]], *,
              label: str = "all") -> Dict[str, Any]:
    """The full scorecard for a set of settled predictions.

    Every model probability is scored against the market probability recorded at the same
    moment, so 'is this better than just taking the price?' is answerable directly.
    """
    settled = [p for p in predictions if p.get("outcome") is not None]
    n = len(settled)
    if n == 0:
        return {
            "label": label, "n": 0, "meaningful": False,
            "note": "No settled predictions yet. Metrics appear once games resolve.",
        }

    model_probs = [float(p.get("fair_prob") or p["model_prob"]) for p in settled]
    market_probs = [float(p["market_prob"]) for p in settled if p.get("market_prob") is not None]
    outcomes = [int(p["outcome"]) for p in settled]
    market_outcomes = [int(p["outcome"]) for p in settled if p.get("market_prob") is not None]

    cal = calibration(model_probs, outcomes)
    money = roi(settled)
    hits = sum(outcomes)

    return {
        "label": label,
        "n": n,
        "meaningful": n >= MEANINGFUL_SAMPLE,
        "hits": hits,
        "misses": n - hits,
        "hit_rate": round(hits / n, 4),
        "average_predicted": round(sum(model_probs) / n, 4),
        "brier": _round(brier(model_probs, outcomes)),
        "brier_market": _round(brier(market_probs, market_outcomes)) if market_probs else None,
        "log_loss": _round(log_loss(model_probs, outcomes)),
        "log_loss_market": _round(log_loss(market_probs, market_outcomes)) if market_probs else None,
        "calibration": cal,
        "calibration_error": _round(calibration_error(cal)),
        "roi": money,
        "expected_vs_actual": expected_vs_actual(settled),
        "clv": closing_line_value(settled),
        "drawdown": drawdown([p["cumulative"] for p in equity_curve(settled)]),
        "note": (
            f"Only {n} settled prediction(s) — too few to draw conclusions from. "
            f"{MEANINGFUL_SAMPLE} is the point at which these numbers start to mean "
            "something." if n < MEANINGFUL_SAMPLE else
            "Model probabilities are scored against the market probability recorded at "
            "the same moment; beating that column is the benchmark that matters."
        ),
    }


def _round(value: Optional[float], places: int = 4) -> Optional[float]:
    return round(value, places) if value is not None else None


def by_group(predictions: Sequence[Dict[str, Any]], key: str) -> List[Dict[str, Any]]:
    """Scorecards split by strategy, market type, or confidence level."""
    groups: Dict[Any, List[Dict[str, Any]]] = defaultdict(list)
    for row in predictions:
        groups[row.get(key)].append(row)
    out = [summarise(rows, label=str(name)) for name, rows in groups.items()]
    out.sort(key=lambda s: s["n"], reverse=True)
    return out
