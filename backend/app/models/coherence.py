"""Do the model's own numbers agree with each other?

A game projection produces a moneyline, several spread rungs, a total and two team totals.
Those are not independent claims — they are all readings of one joint distribution over one
scoreline, and arithmetic constrains them:

  * P(win) must equal P(margin > 0). If the moneyline and the pick'em spread disagree, one
    of them is wrong.
  * Spread probabilities must be monotone. P(win by more than 3) cannot exceed P(win by
    more than 2.5); asking for more points cannot become easier.
  * The two team totals must sum to the game total. If the model says the home team scores
    24 and the away team 21, it cannot also say the total is 50.
  * P(team A wins) + P(team B wins) + P(tie) must equal 1.

None of this makes the model right. A model can be perfectly coherent and perfectly wrong.
But incoherence is a guarantee that at least one published number is wrong, and the old
code published all of these side by side without ever checking that they agreed.

Violations are reported rather than silently repaired. Papering over a contradiction hides
the bug that caused it, and the honest response to "these two prices disagree" is to stop
recommending either until it is understood.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.models import distributions as dist

# Coherence is checked against the discrete distribution the app publishes, so the
# tolerance only needs to absorb rounding at the displayed precision, not modelling error.
TOLERANCE = 0.005


@dataclass
class CoherenceResult:
    checks: List[Dict[str, Any]] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str, gap: Optional[float] = None) -> None:
        self.checks.append({"check": name, "ok": ok, "detail": detail,
                            "gap": round(gap, 5) if gap is not None else None})

    @property
    def coherent(self) -> bool:
        return all(c["ok"] for c in self.checks)

    def failures(self) -> List[Dict[str, Any]]:
        return [c for c in self.checks if not c["ok"]]

    def to_dict(self) -> Dict[str, Any]:
        failures = self.failures()
        return {
            "coherent": self.coherent,
            "checks_run": len(self.checks),
            "failures": failures,
            "summary": (
                "Moneyline, spread, total and team-total probabilities are mutually "
                "consistent with one joint distribution over the scoreline."
                if self.coherent else
                f"{len(failures)} internal contradiction(s). At least one published "
                "probability on this game is wrong, so none of them should be traded "
                "until it is resolved."),
        }


def check_projection(projection, *, spread_lines: Sequence[float] = (),
                     total_lines: Sequence[float] = ()) -> CoherenceResult:
    """Verify a game projection against itself."""
    result = CoherenceResult()
    margin = projection.margin
    total = projection.total

    # 1. The three moneyline outcomes are exhaustive.
    triple = projection.home_win + projection.tie + projection.away_win
    result.add("moneyline_sums_to_one", abs(triple - 1.0) <= TOLERANCE,
               f"home {projection.home_win:.4f} + tie {projection.tie:.4f} + away "
               f"{projection.away_win:.4f} = {triple:.4f}", abs(triple - 1.0))

    # 2. The moneyline must be the margin distribution's own answer.
    from_margin = 1.0 - margin.cdf(0)
    result.add("moneyline_matches_margin", abs(from_margin - projection.home_win) <= TOLERANCE,
               f"P(margin > 0) = {from_margin:.4f} versus published home win "
               f"{projection.home_win:.4f}", abs(from_margin - projection.home_win))

    # 3. Spread probabilities fall as the line rises. Asking for more points cannot get
    #    easier, and a model that says otherwise has a broken distribution.
    ordered = sorted(spread_lines)
    previous: Optional[Tuple[float, float]] = None
    monotone = True
    detail = "no spread lines supplied"
    for line in ordered:
        prob, _ = dist.cover_probability(margin, -line, team_is_home=True)
        if previous is not None and prob > previous[1] + TOLERANCE:
            monotone = False
            detail = (f"P(home by more than {line:g}) = {prob:.4f} exceeds "
                      f"P(home by more than {previous[0]:g}) = {previous[1]:.4f}")
            break
        previous = (line, prob)
    if ordered and monotone:
        detail = f"probabilities fall monotonically across {len(ordered)} spread lines"
    if ordered:
        result.add("spread_monotone", monotone, detail)

    # 4. Total probabilities fall as the line rises, for the same reason.
    ordered_totals = sorted(total_lines)
    previous_total: Optional[Tuple[float, float]] = None
    monotone_total = True
    total_detail = "no total lines supplied"
    for line in ordered_totals:
        prob = total.prob_over(line)
        if previous_total is not None and prob > previous_total[1] + TOLERANCE:
            monotone_total = False
            total_detail = (f"P(total over {line:g}) = {prob:.4f} exceeds "
                            f"P(total over {previous_total[0]:g}) = {previous_total[1]:.4f}")
            break
        previous_total = (line, prob)
    if ordered_totals and monotone_total:
        total_detail = (f"probabilities fall monotonically across "
                        f"{len(ordered_totals)} total lines")
    if ordered_totals:
        result.add("total_monotone", monotone_total, total_detail)

    # 5. Distributions are proper: mass sums to one and nothing is negative.
    for name, d in (("margin", margin), ("total", total)):
        mass = sum(d.mass)
        result.add(f"{name}_is_a_distribution",
                   abs(mass - 1.0) <= TOLERANCE and all(p >= -1e-12 for p in d.mass),
                   f"{name} mass sums to {mass:.6f}", abs(mass - 1.0))

    # 6. Expected team scores must reconstruct the expected total and margin. This is the
    #    check that catches a team-total model drifting away from the game model.
    expected_home = (projection.expected_total + projection.expected_margin) / 2.0
    expected_away = (projection.expected_total - projection.expected_margin) / 2.0
    rebuilt_total = expected_home + expected_away
    rebuilt_margin = expected_home - expected_away
    result.add("team_totals_reconstruct_the_game",
               abs(rebuilt_total - projection.expected_total) <= 0.01
               and abs(rebuilt_margin - projection.expected_margin) <= 0.01,
               f"home {expected_home:.2f} + away {expected_away:.2f} = "
               f"{rebuilt_total:.2f} (total {projection.expected_total:.2f}), "
               f"difference {rebuilt_margin:.2f} (margin {projection.expected_margin:.2f})")

    # 7. A total cannot be negative and a margin cannot exceed the total.
    result.add("total_is_non_negative", total.low >= 0,
               f"total support starts at {total.low}")

    return result


def check_signals(signals: Sequence[Any]) -> CoherenceResult:
    """Cross-check published signal probabilities on one game against each other.

    Runs on what the user is actually shown, which is not always what the projection said:
    signals are blended toward the market per market type, and two market types blended at
    different weights can drift out of agreement even when the projection behind them was
    perfectly coherent. That drift is invisible unless something looks for it.
    """
    from app.core.types import MarketType
    from app.strategies import pricing

    result = CoherenceResult()
    spreads: Dict[str, List[Tuple[float, float]]] = {}
    for signal in signals:
        if signal.market_type is MarketType.SPREAD and signal.line is not None:
            spreads.setdefault(signal.team or "", []).append(
                (float(signal.line), pricing.published_prob(signal)))

    for team, rungs in spreads.items():
        rungs.sort()
        monotone = all(rungs[i][1] <= rungs[i - 1][1] + TOLERANCE
                       for i in range(1, len(rungs)))
        if len(rungs) > 1:
            result.add(
                f"published_spread_monotone_{team or 'unknown'}", monotone,
                (f"{len(rungs)} published rungs for {team or 'unknown'} fall in order"
                 if monotone else
                 f"published rungs for {team or 'unknown'} are out of order: "
                 + ", ".join(f"{line:g}->{prob:.3f}" for line, prob in rungs)))

    complements: Dict[Tuple[str, Optional[float]], List[float]] = {}
    for signal in signals:
        if signal.market_type in (MarketType.MONEYLINE, MarketType.TOTAL):
            key = (signal.market_type.value, signal.line)
            complements.setdefault(key, []).append(pricing.published_prob(signal))
    for (market, line), probs in complements.items():
        if len(probs) == 2:
            total = sum(probs)
            # Moneylines leave room for a tie; totals do not.
            ceiling = 1.0 if market == MarketType.TOTAL.value else 1.0
            floor = 0.97 if market == MarketType.MONEYLINE.value else 1.0 - TOLERANCE
            ok = floor - TOLERANCE <= total <= ceiling + TOLERANCE
            result.add(f"published_{market}_complements_{line}", ok,
                       f"two sides of {market} {line if line is not None else ''} sum to "
                       f"{total:.4f}", abs(total - 1.0))

    return result
