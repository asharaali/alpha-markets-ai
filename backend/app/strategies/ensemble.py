"""The ensemble — one number per market side, from several strategies that disagree.

Averaging every strategy equally would be the wrong answer for two reasons. Strategies do
not have equal claim on a market (the matchup model has an opinion on a spread; the totals
model does not), and they do not have equal track records.

So the combination is:

  * A weighted average in LOG-ODDS space, not probability space. Averaging 5% and 45%
    arithmetically gives 25%, which badly overstates a long shot; in log-odds it gives
    about 15%, which is the sane blend of those two beliefs.
  * Weights that start from a declared prior per strategy and then MOVE with measured
    performance — specifically each strategy's log loss against the market's log loss on
    the same predictions. A strategy that has demonstrably beaten the price gets more say;
    one that has not gets less. With fewer than 30 settled predictions the prior stands, and
    the response says so rather than implying the weights were earned.
  * Explicit disagreement reporting. When two strategies differ sharply, that is published
    as reduced confidence, not smoothed away — a market where the models fight is exactly
    where you should size down.
"""
from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.config import settings
from app.core.logging import get_logger
from app.core.types import CONFIDENCE_ORDER, Confidence, MarketType, Signal
from app.strategies import line_movement, pricing
from app.tracking import metrics, store

log = get_logger(__name__)

# Prior weight per strategy before any track record exists. The core game model carries
# most of the weight; the matchup view tilts; movement corroborates but does not predict.
PRIOR_WEIGHTS: Dict[str, float] = {
    "moneyline": 1.0,
    "spread": 1.0,
    "totals": 1.0,
    "win_margin": 0.8,
    "matchup": 0.5,
    "player_props": 0.6,
    "line_movement": 0.0,      # corroboration only — never contributes a probability
    "mispricing": 0.0,
}
DEFAULT_PRIOR = 0.4

# Track record only starts moving the weights past this many settled predictions.
MIN_SETTLED_FOR_WEIGHTING = metrics.MEANINGFUL_SAMPLE
# The most a track record may multiply or divide a strategy's prior weight.
MAX_WEIGHT_MULTIPLIER = 2.0

# How much corroborating or contradicting line movement nudges the blended probability.
MOVEMENT_INFLUENCE = 0.25


def _logit(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def _sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def performance_weights() -> Tuple[Dict[str, float], Dict[str, Any]]:
    """Weight multipliers earned from each strategy's measured record against the market."""
    multipliers: Dict[str, float] = {}
    detail: Dict[str, Any] = {}
    try:
        settled = store.settled_predictions()
    except Exception as exc:  # noqa: BLE001 - the ensemble must survive a database problem
        log.warning("could not read settled predictions for weighting: %s", exc)
        return {}, {"available": False, "reason": str(exc)[:120]}

    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in settled:
        grouped[row["strategy"]].append(row)

    for strategy, rows in grouped.items():
        if len(rows) < MIN_SETTLED_FOR_WEIGHTING:
            detail[strategy] = {"n": len(rows), "weighted": False,
                                "reason": "not enough settled predictions yet"}
            continue
        score = metrics.summarise(rows, label=strategy)
        model_ll = score.get("log_loss")
        market_ll = score.get("log_loss_market")
        if not model_ll or not market_ll:
            detail[strategy] = {"n": len(rows), "weighted": False,
                                "reason": "no market benchmark recorded"}
            continue
        # Beating the market's log loss earns weight; losing to it costs weight.
        ratio = market_ll / model_ll if model_ll > 0 else 1.0
        multiplier = min(max(ratio, 1.0 / MAX_WEIGHT_MULTIPLIER), MAX_WEIGHT_MULTIPLIER)
        multipliers[strategy] = multiplier
        detail[strategy] = {"n": len(rows), "weighted": True,
                            "log_loss": model_ll, "log_loss_market": market_ll,
                            "multiplier": round(multiplier, 3)}
    return multipliers, {"available": True, "strategies": detail,
                         "min_sample": MIN_SETTLED_FOR_WEIGHTING}


def _weight_for(strategy: str, multipliers: Dict[str, float]) -> float:
    return PRIOR_WEIGHTS.get(strategy, DEFAULT_PRIOR) * multipliers.get(strategy, 1.0)


def _group_key(signal: Signal) -> str:
    """Signals about the exact same tradeable side combine; nothing else does."""
    ticker = signal.quote.ticker if signal.quote else "unpriced"
    return f"{signal.game_id}|{ticker}|{signal.selection}"


def combine(signals: Sequence[Signal], *,
            multipliers: Optional[Dict[str, float]] = None,
            use_movement: bool = True) -> List[Signal]:
    """Fold per-strategy signals into one ensemble signal per market side."""
    multipliers = multipliers if multipliers is not None else performance_weights()[0]
    groups: Dict[str, List[Signal]] = defaultdict(list)
    for signal in signals:
        if signal.strategy in ("line_movement", "mispricing"):
            continue
        groups[_group_key(signal)].append(signal)

    out: List[Signal] = []
    for members in groups.values():
        contributors = [s for s in members if _weight_for(s.strategy, multipliers) > 0]
        if not contributors:
            continue
        blended = _blend(contributors, multipliers)
        if blended is None:
            continue
        if use_movement:
            _apply_movement(blended)
        out.append(blended)
    return out


def _blend(members: List[Signal], multipliers: Dict[str, float]) -> Optional[Signal]:
    weights: List[float] = []
    logits: List[float] = []
    for signal in members:
        weight = _weight_for(signal.strategy, multipliers)
        # A strategy's own confidence scales its say within its family.
        weight *= 0.4 + 0.2 * CONFIDENCE_ORDER.get(signal.confidence, 1)
        if weight <= 0:
            continue
        weights.append(weight)
        logits.append(_logit(pricing.published_prob(signal)))
    if not weights:
        return None

    total = sum(weights)
    mean_logit = sum(w * l for w, l in zip(weights, logits)) / total
    combined = _sigmoid(mean_logit)

    # Disagreement, measured in probability terms across the contributing strategies.
    probs = [pricing.published_prob(s) for s in members]
    spread = max(probs) - min(probs) if len(probs) > 1 else 0.0

    primary = max(members, key=lambda s: _weight_for(s.strategy, multipliers))
    quote = primary.quote
    reasoning: List[str] = []
    for signal in sorted(members, key=lambda s: -_weight_for(s.strategy, multipliers)):
        head = signal.reasoning[0] if signal.reasoning else ""
        reasoning.append(f"[{signal.strategy}] {pricing.published_prob(signal):.1%}"
                         + (f" — {head}" if head else ""))
    if spread > 0.06:
        reasoning.append(
            f"Strategies disagree by {spread:.1%} on this market — confidence is reduced "
            "accordingly rather than averaging the disagreement away")

    ensemble = Signal(
        strategy="ensemble",
        game_id=primary.game_id,
        market_type=primary.market_type,
        label=primary.label,
        selection=primary.selection,
        model_prob=combined,
        confidence=Confidence.LOW,
        line=primary.line, team=primary.team, player=primary.player,
        reasoning=reasoning,
    )
    if quote is not None:
        sample_confidence = max(
            (s.features.get("model_weight", 0.3) for s in members), default=0.3)
        pricing.price_signal(ensemble, quote,
                             sample_confidence=min(sample_confidence * 2.4, 1.0))
    ensemble.features.update({
        "contributors": [
            {"strategy": s.strategy,
             "prob": round(pricing.published_prob(s), 4),
             "weight": round(_weight_for(s.strategy, multipliers), 3),
             "confidence": s.confidence.value}
            for s in members
        ],
        "disagreement": round(spread, 4),
        "member_count": len(members),
    })
    ensemble.confidence = _grade(ensemble, members, spread)
    return ensemble


def _apply_movement(signal: Signal) -> None:
    """Nudge the ensemble by whether the market has been moving with us or against us."""
    corroboration = line_movement.corroboration(signal)
    if corroboration is None or corroboration == 0.0 or signal.market_prob is None:
        return
    fair = signal.features.get("fair_prob")
    if fair is None:
        return
    gap = float(fair) - signal.market_prob
    # Movement toward us keeps more of the gap; movement against us keeps less.
    adjusted = signal.market_prob + gap * (1.0 + MOVEMENT_INFLUENCE * corroboration)
    adjusted = min(max(adjusted, 1e-4), 1 - 1e-4)
    signal.features["fair_prob"] = round(adjusted, 4)
    signal.features["movement_corroboration"] = round(corroboration, 3)
    signal.edge = adjusted - signal.market_prob
    cost = signal.quote.cost if signal.quote else None
    signal.ev_per_dollar = pricing.expected_value(adjusted, cost) if cost else None
    signal.features["value"] = pricing.is_value(
        edge=signal.edge, ev=signal.ev_per_dollar,
        market_prob=signal.market_prob,
        liquid=bool(signal.features.get("liquid")))
    signal.reasoning.append(
        "Market has been moving " + ("toward" if corroboration > 0 else "away from")
        + " this side over the last day"
        + (" — edge kept" if corroboration > 0 else " — edge trimmed"))


def _grade(ensemble: Signal, members: List[Signal], spread: float) -> Confidence:
    """Ensemble confidence: the members' own grades, tempered by how much they disagree."""
    if any(m.confidence is Confidence.REFERENCE for m in members) and len(members) == 1:
        return Confidence.REFERENCE
    scores = [CONFIDENCE_ORDER.get(m.confidence, 1) for m in members]
    average = sum(scores) / len(scores)
    if spread > 0.12:
        average -= 1.0
    elif spread > 0.06:
        average -= 0.5
    if len(members) > 1 and spread <= 0.03:
        average += 0.5          # independent strategies agreeing is real evidence
    if not ensemble.features.get("liquid", True):
        return Confidence.LOW
    if average >= 2.6:
        return Confidence.HIGH
    if average >= 1.6:
        return Confidence.MEDIUM
    if average >= 0.5:
        return Confidence.LOW
    return Confidence.REFERENCE


def explain_weights() -> Dict[str, Any]:
    """What the ensemble is currently weighting and why — for the Model Lab."""
    multipliers, detail = performance_weights()
    rows = []
    for strategy, prior in sorted(PRIOR_WEIGHTS.items()):
        rows.append({
            "strategy": strategy,
            "prior_weight": prior,
            "performance_multiplier": round(multipliers.get(strategy, 1.0), 3),
            "effective_weight": round(_weight_for(strategy, multipliers), 3),
            "record": detail.get("strategies", {}).get(strategy),
        })
    return {
        "weights": rows,
        "performance_weighting_active": bool(multipliers),
        "min_settled_for_weighting": MIN_SETTLED_FOR_WEIGHTING,
        "note": (
            "Weights are currently the declared priors — no strategy has enough settled "
            "predictions to have earned a change yet."
            if not multipliers else
            "Weights have been adjusted by measured log loss against the market on the "
            "same predictions."
        ),
    }
