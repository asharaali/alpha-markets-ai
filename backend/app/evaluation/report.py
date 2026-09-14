"""The evaluation report, and the rule for when a model change is allowed to ship.

The promotion rule is deliberately hard to satisfy:

    A change ships only if it improves HELD-OUT performance on matched observations, with
    the improvement larger than its own clustered standard error, or if it fixes a
    demonstrated correctness defect.

"Fixes a defect" is a separate door on purpose. The simulation parity fix made no
measurable difference to Brier score — it barely could, it affects joint parlay pricing —
but it was wrong and is now right. Correctness does not need to win a bake-off.

Everything else does. And the honest expected outcome is that most of it loses: closing
sportsbook lines are the most efficient prices in sports betting, and a model that ties
them is already unusual. This module is built to report that plainly when it happens, and
to retain the baseline rather than shipping a change that merely looks sophisticated.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Sequence

from app.core.logging import get_logger
from app.evaluation import baselines, protocol, walkforward

log = get_logger(__name__)

MARKETS = ("moneyline", "spread", "total")


async def run(*, seasons: Sequence[int], test_seasons: int = 1,
              validation_seasons: int = 1, start_week: int = 1,
              blend_weight: Optional[float] = None) -> Dict[str, Any]:
    """The full chronological evaluation: split, walk forward, compare, decide."""
    started = time.time()
    split = protocol.chronological_split(seasons, test_seasons=test_seasons,
                                         validation_seasons=validation_seasons)

    rows, guard = await walkforward.generate(
        seasons, start_week=start_week, blend_weight=blend_weight)
    scored = baselines.score_all(rows)
    matched_rows = baselines.matched(scored)

    by_period = {
        "train": [r for r in matched_rows if r["season"] in split.train],
        "validation": [r for r in matched_rows if r["season"] in split.validation],
        "test": [r for r in matched_rows if r["season"] in split.test],
    }

    report: Dict[str, Any] = {
        "generated_at": time.time(),
        "elapsed_seconds": round(time.time() - started, 1),
        "split": split.describe(),
        "look_ahead": guard.to_dict(),
        "totals": {
            "forecast_rows": len(rows),
            "matched_rows": len(matched_rows),
            "games": len({r["game_id"] for r in matched_rows}),
            "note": ("Rows are contracts; games are the independent unit of evidence. "
                     "Every interval below is computed between games, never between "
                     "rows."),
        },
        "periods": {},
        "by_market": {},
        "comparisons": {},
        "returns": {},
    }

    for period, period_rows in by_period.items():
        if not period_rows:
            report["periods"][period] = {"games": 0, "note": "No games in this period."}
            continue
        report["periods"][period] = {
            "seasons": sorted({r["season"] for r in period_rows}),
            "games": len({r["game_id"] for r in period_rows}),
            "rows": len(period_rows),
            "baselines": [baselines.summarise_baseline(period_rows, b, label=period)
                          for b in baselines.BASELINES],
        }

    test_rows = by_period["test"]
    if test_rows:
        report["by_market"] = {
            market: {
                "games": len({r["game_id"] for r in test_rows
                              if r["market_type"] == market}),
                "baselines": [
                    baselines.summarise_baseline(
                        [r for r in test_rows if r["market_type"] == market], b,
                        label=market)
                    for b in baselines.BASELINES],
            }
            for market in MARKETS
            if any(r["market_type"] == market for r in test_rows)
        }
        report["comparisons"] = _comparisons(test_rows)
        report["returns"] = {
            baseline.key: walkforward.flat_stake_return(
                test_rows, baseline.column, min_edge=0.02)
            for baseline in baselines.BASELINES
        }
        report["ablations"] = _ablations(test_rows)

    report["verdict"] = _verdict(report)
    report["limitations"] = _limitations()
    return report


def _comparisons(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Every candidate against the market, paired by game on matched observations."""
    out: Dict[str, Any] = {}
    for baseline in baselines.BASELINES:
        if baseline.is_reference:
            continue
        for metric in ("brier", "logloss"):
            key = f"{baseline.key}_vs_market_{metric}"
            out[key] = protocol.paired_difference(
                rows, f"{metric}_{baseline.key}", f"{metric}_market")
    return out


def _ablations(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """What each component is actually worth, measured by removing it.

    A component that does not change the score is not earning its complexity, and saying
    so is more useful than listing it as a feature.
    """
    out: Dict[str, Any] = {}
    for ablation in baselines.ABLATIONS:
        column = f"{ablation.key}_prob"
        if not any(r.get(column) is not None for r in rows):
            out[ablation.key] = {
                "name": ablation.name,
                "measured": False,
                "note": ("Not measurable from this backtest — the component does not "
                         "affect the markets scored here."),
            }
            continue
        scored = baselines.score_rows(
            rows, baselines.Baseline(ablation.key, ablation.name, column,
                                     ablation.description))
        comparison = protocol.paired_difference(
            scored, f"brier_{ablation.key}", "brier_ensemble")
        if comparison is None:
            out[ablation.key] = {"name": ablation.name, "measured": False}
            continue
        # comparison["difference"] is (ablated - full). Positive means removing the
        # component made the score WORSE, so the component helps.
        difference = comparison["difference"]
        out[ablation.key] = {
            "name": ablation.name,
            "description": ablation.description,
            "measured": True,
            "brier_change_when_removed": round(difference, 5),
            "standard_error": round(comparison["standard_error"], 5)
                              if comparison.get("standard_error") else None,
            "games": comparison["clusters"],
            "component_helps": difference > 0 and comparison["significant"],
            "verdict": (
                "Removing it makes forecasts measurably worse, so it is earning its place."
                if difference > 0 and comparison["significant"] else
                "Removing it makes forecasts measurably better; this component is hurting."
                if difference < 0 and comparison["significant"] else
                "Removing it changes nothing detectable at this sample size. The component "
                "is unproven, not proven useless."),
        }
    return out


def _verdict(report: Dict[str, Any]) -> Dict[str, Any]:
    """A plain answer to 'does any of this beat just taking the price?'"""
    look_ahead = report.get("look_ahead") or {}
    if not look_ahead.get("clean"):
        return {
            "promotable": False,
            "summary": ("Look-ahead was detected in this run, so none of these numbers "
                        "are evidence. Fix the leak before reading anything else."),
            "violations": look_ahead.get("violations"),
        }

    test = (report.get("periods") or {}).get("test") or {}
    if not test.get("games"):
        return {"promotable": False,
                "summary": "No held-out games were scored, so nothing can be promoted."}

    comparisons = report.get("comparisons") or {}
    winners: List[str] = []
    losers: List[str] = []
    for baseline in baselines.BASELINES:
        if baseline.is_reference:
            continue
        result = comparisons.get(f"{baseline.key}_vs_market_brier")
        if not result:
            continue
        # Negative difference means the candidate's Brier is lower, which is better.
        beats = result["difference"] < 0 and result.get("significant")
        (winners if beats else losers).append(baseline.name)

    market_score = None
    for entry in test.get("baselines") or []:
        if entry["key"] == "market":
            market_score = entry.get("brier")

    return {
        "promotable": bool(winners),
        "beats_market": winners,
        "does_not_beat_market": losers,
        "market_brier": market_score,
        "held_out_games": test.get("games"),
        "summary": (
            f"On {test.get('games')} held-out games, {', '.join(winners)} beat the closing "
            f"line by more than the clustered standard error."
            if winners else
            f"On {test.get('games')} held-out games, nothing beat the closing line by "
            "more than its own standard error. The baseline is retained. That is the "
            "expected result against closing sportsbook prices and it is not a failure of "
            "the pipeline — it is the pipeline working."),
        "rule": (
            "A model change ships only if it improves held-out performance on matched "
            "observations by more than the clustered standard error, or if it fixes a "
            "demonstrated correctness defect. Feature count and passing tests are not "
            "evidence."),
    }


def _limitations() -> List[str]:
    return [
        "The 'full ensemble' column in this backtest is IDENTICAL to the model+market "
        "blend. The live ensemble also reads injury reports, depth charts, line movement "
        "and sportsbook consensus, none of which nflverse publishes as a point-in-time "
        "history — reconstructing what the injury report said on the Thursday of week 7 "
        "of 2023 is not possible from the data available. So the ensemble's incremental "
        "value over the blend is UNMEASURED here, not measured and found to be zero. Any "
        "claim that the ensemble helps rests on the live tracked record, which is small.",

        "For the same reason the injury, rest/travel and weather ablations report "
        "'not measurable' rather than a number. Those components are not validated.",

        "The benchmark is the closing SPORTSBOOK line, not Kalshi. Kalshi publishes no "
        "usable price history, so this measures forecast quality against an efficient "
        "reference price. It does not establish that an edge was executable on Kalshi "
        "days before kickoff, and no number here should be quoted as if it did.",

        "Closing-line backtests flatter any model that trades earlier than the close. A "
        "prediction scored against a price set hours later is not the price that was "
        "available when the bet would have been placed.",

        "Returns apply Kalshi's fee to sportsbook prices. That answers 'would this edge "
        "survive the fee', not 'this is what Kalshi would have paid'.",

        "The as-of artifact is refitted once per season, not once per week. Within a "
        "season the coefficients are fixed while the ratings advance weekly. No season "
        "sees itself, which is the guarantee that matters, but a weekly refit would be "
        "marginally stricter.",

        "Player props are excluded. They cannot be settled from a final score and the "
        "data quality behind them has not been validated, so they remain reference-only.",

        "Prospective Kalshi results are tracked separately in the live record. Nothing in "
        "this report is a claim about them.",
    ]
