"""The evaluation protocol: no look-ahead, no inflated evidence, matched comparisons.

These tests guard the claims the performance page makes. Each one corresponds to a way the
old pipeline overstated what it knew.
"""
from __future__ import annotations

import math

import pytest

from app.evaluation import baselines, protocol


def forecast(game_id, *, selection="KC ML", created_at=0.0, horizon=None,
             market_type="moneyline", strategy="ensemble", **extra):
    row = {"game_id": game_id, "strategy": strategy, "market_type": market_type,
           "selection": selection, "created_at": created_at,
           "horizon_hours": horizon}
    row.update(extra)
    return row


class TestChronologicalSplitting:
    def test_test_set_is_the_most_recent_seasons(self):
        split = protocol.chronological_split([2019, 2020, 2021, 2022, 2023])
        assert split.test == (2023,)
        assert split.validation == (2022,)
        assert split.train == (2019, 2020, 2021)

    def test_periods_never_overlap(self):
        split = protocol.chronological_split(range(2015, 2026), test_seasons=2,
                                             validation_seasons=2)
        assert not set(split.train) & set(split.validation)
        assert not set(split.validation) & set(split.test)
        assert not set(split.train) & set(split.test)

    def test_every_training_season_precedes_every_test_season(self):
        split = protocol.chronological_split(range(2015, 2026), test_seasons=2)
        assert max(split.train) < min(split.test)
        assert max(split.validation) < min(split.test)

    def test_too_few_seasons_is_refused_rather_than_fudged(self):
        with pytest.raises(ValueError):
            protocol.chronological_split([2024, 2025])


class TestCheckpointEligibility:
    """A forecast cannot be scored at a checkpoint that predates it."""

    def test_a_forecast_is_eligible_only_for_later_checkpoints(self):
        # Made 30 hours out: exists at the 24h and 1h checkpoints, not at one week.
        assert protocol.eligible_checkpoints(30.0) == (24.0, 1.0)

    def test_a_late_forecast_qualifies_for_nothing_earlier(self):
        assert protocol.eligible_checkpoints(0.5) == ()

    def test_an_early_forecast_qualifies_for_everything(self):
        assert protocol.eligible_checkpoints(200.0) == (168.0, 24.0, 1.0)

    def test_checkpoint_selection_does_not_credit_unavailable_information(self):
        """The direction test. Scoring a 20-hours-out forecast as the 24-hour opinion
        would credit the model with four hours of information it did not have."""
        rows = [forecast("G1", created_at=1, horizon=150.0),
                forecast("G1", created_at=2, horizon=20.0)]
        kept = protocol.deduplicate(rows, checkpoint=24.0)
        assert len(kept) == 1
        assert kept[0]["horizon_hours"] == 150.0


class TestRepeatedForecastsAreCollapsed:
    """The 250x evidence inflation, pinned."""

    def test_forty_three_refreshes_of_one_view_count_once(self):
        rows = [forecast("G1", created_at=float(i), horizon=100.0 - i)
                for i in range(43)]
        assert len(protocol.deduplicate(rows)) == 1

    def test_the_final_forecast_is_the_one_kept(self):
        rows = [forecast("G1", created_at=1.0, horizon=100.0, model_prob=0.51),
                forecast("G1", created_at=2.0, horizon=50.0, model_prob=0.57),
                forecast("G1", created_at=3.0, horizon=2.0, model_prob=0.62)]
        kept = protocol.deduplicate(rows)
        assert kept[0]["model_prob"] == 0.62

    def test_different_contracts_on_one_game_stay_separate(self):
        rows = [forecast("G1", selection="KC ML"),
                forecast("G1", selection="KC -3.5", market_type="spread"),
                forecast("G1", selection="Over 44.5", market_type="total")]
        assert len(protocol.deduplicate(rows)) == 3

    def test_but_they_share_one_cluster(self):
        """Six contracts on one game resolve off one scoreline. One unit of evidence."""
        rows = [forecast("G1", selection=f"S{i}") for i in range(6)]
        assert len(protocol.clusters(rows)) == 1


class TestClusteredUncertainty:
    def test_standard_error_is_computed_between_games_not_rows(self):
        # Ten games, six identical contracts each. The row-level SE would be tiny because
        # n=60; the clustered SE sees the ten games it actually has.
        rows = []
        for g in range(10):
            value = 0.20 + 0.02 * g
            for c in range(6):
                rows.append({"game_id": f"G{g}", "brier": value})
        result = protocol.clustered_mean(rows, "brier")
        assert result["clusters"] == 10
        assert result["observations"] == 60

        per_game = [0.20 + 0.02 * g for g in range(10)]
        mean = sum(per_game) / 10
        variance = sum((x - mean) ** 2 for x in per_game) / 9
        assert result["standard_error"] == pytest.approx(math.sqrt(variance / 10))

    def test_inflating_rows_does_not_shrink_the_interval(self):
        """The property that makes the number honest."""
        sparse = [{"game_id": f"G{g}", "brier": 0.2 + 0.02 * g} for g in range(10)]
        dense = []
        for g in range(10):
            for _ in range(50):
                dense.append({"game_id": f"G{g}", "brier": 0.2 + 0.02 * g})
        a = protocol.clustered_mean(sparse, "brier")
        b = protocol.clustered_mean(dense, "brier")
        assert a["standard_error"] == pytest.approx(b["standard_error"])
        assert b["observations"] == 500
        assert b["clusters"] == 10

    def test_one_game_yields_no_interval(self):
        result = protocol.clustered_mean([{"game_id": "G1", "brier": 0.2}], "brier")
        assert result["standard_error"] is None


class TestPairedComparisons:
    def test_comparison_runs_only_on_rows_both_models_priced(self):
        rows = [
            {"game_id": "G1", "brier_a": 0.20, "brier_b": 0.25},
            {"game_id": "G2", "brier_a": 0.18, "brier_b": None},
            {"game_id": "G3", "brier_a": 0.22, "brier_b": 0.24},
        ]
        result = protocol.paired_difference(rows, "brier_a", "brier_b")
        assert result["observations"] == 2, "the unmatched row must be dropped"
        assert result["clusters"] == 2

    def test_lower_score_is_reported_as_better(self):
        rows = [{"game_id": f"G{i}", "brier_a": 0.20, "brier_b": 0.25}
                for i in range(30)]
        result = protocol.paired_difference(rows, "brier_a", "brier_b")
        assert result["difference"] < 0
        assert result["better"] == "brier_a"

    def test_a_difference_swamped_by_game_to_game_noise_is_not_significant(self):
        """The realistic case: a tiny average edge, with per-game scatter far larger.

        The noise has to differ between the two columns. Identical noise in both would
        make the per-game difference a constant, and a perfectly consistent 0.0001 edge
        genuinely IS significant — that is the method working, not failing.
        """
        rows = []
        for i in range(40):
            rows.append({"game_id": f"G{i}",
                         "brier_a": 0.20 + 0.15 * math.sin(i),
                         "brier_b": 0.2001 + 0.15 * math.sin(i * 2.7)})
        result = protocol.paired_difference(rows, "brier_a", "brier_b")
        assert result["significant"] is False
        assert abs(result["difference"]) < result["standard_error"] * 1.96

    def test_zero_variance_does_not_claim_infinite_confidence(self):
        """A perfectly constant difference has a zero standard error, which would divide
        out to an infinite t. Real forecast data never does this, so seeing it means the
        input is degenerate — and the safe answer to degenerate input is "not proven"."""
        rows = [{"game_id": f"G{i}", "brier_a": 0.20, "brier_b": 0.2001}
                for i in range(40)]
        result = protocol.paired_difference(rows, "brier_a", "brier_b")
        assert result["standard_error"] == pytest.approx(0.0, abs=1e-12)
        assert result["significant"] is False

    def test_a_small_but_real_edge_is_detected(self):
        """Same tiny edge, with realistic per-game scatter around it."""
        rows = []
        for i in range(400):
            scatter = 0.01 * math.sin(i * 1.7)
            rows.append({"game_id": f"G{i}", "brier_a": 0.20 + scatter,
                         "brier_b": 0.205 + scatter * 0.98})
        result = protocol.paired_difference(rows, "brier_a", "brier_b")
        assert result["difference"] < 0
        assert result["significant"] is True


class TestLookAheadGuard:
    def test_clean_run_reports_clean(self):
        guard = protocol.EvaluationGuard(
            as_of_season=2024, scored_seasons=(2024, 2025),
            fits_by_season={2024: (2021, 2022, 2023), 2025: (2021, 2022, 2023, 2024)},
            ratings_max_week={(2024, 5): 4, (2024, 6): 5})
        assert guard.violations() == []
        assert guard.to_dict()["clean"] is True

    def test_a_season_used_to_fit_a_later_one_is_not_a_leak(self):
        """The case that made the first version of this guard cry wolf.

        Scoring 2022 and 2025 in one run: 2025's artifact legitimately includes 2022, and
        2022 is scored by its own artifact fitted on seasons before 2022. Comparing unions
        flags this; checking per scored season does not.
        """
        guard = protocol.EvaluationGuard(
            as_of_season=2022, scored_seasons=(2022, 2025),
            fits_by_season={2022: (2019, 2020, 2021),
                            2025: (2019, 2020, 2021, 2022, 2023, 2024)})
        assert guard.violations() == []

    def test_a_season_fitted_on_itself_is_caught(self):
        guard = protocol.EvaluationGuard(
            as_of_season=2024, scored_seasons=(2024,),
            fits_by_season={2024: (2022, 2023, 2024)})
        problems = guard.violations()
        assert any("2024" in p for p in problems)
        assert guard.to_dict()["clean"] is False

    def test_a_season_fitted_on_a_later_season_is_caught(self):
        guard = protocol.EvaluationGuard(
            as_of_season=2023, scored_seasons=(2023,),
            fits_by_season={2023: (2021, 2022, 2024, 2025)})
        assert any("2024" in p for p in guard.violations())

    def test_ratings_reaching_into_the_predicted_week_is_caught(self):
        guard = protocol.EvaluationGuard(
            as_of_season=2025, scored_seasons=(2025,),
            fits_by_season={2025: (2021,)},
            ratings_max_week={(2025, 5): 5})
        assert any("week 5" in p for p in guard.violations())

    def test_a_dirty_run_says_the_numbers_are_not_evidence(self):
        guard = protocol.EvaluationGuard(
            as_of_season=2025, scored_seasons=(2025,),
            fits_by_season={2025: (2025,)})
        assert "LOOK-AHEAD DETECTED" in guard.to_dict()["statement"]


class TestScoring:
    def test_log_loss_survives_a_confident_miss(self):
        """Unclamped, one confident error is infinite and poisons every average."""
        assert math.isfinite(baselines.log_loss_score(1.0, 0))
        assert math.isfinite(baselines.log_loss_score(0.0, 1))

    def test_brier_rewards_the_correct_direction(self):
        assert baselines.brier_score(0.9, 1) < baselines.brier_score(0.6, 1)

    def test_matched_filter_drops_rows_any_model_could_not_price(self):
        rows = [
            {"game_id": "G1", "market_prob": 0.5, "model_prob": 0.6,
             "blend_prob": 0.55, "ensemble_prob": 0.55, "outcome": 1},
            {"game_id": "G2", "market_prob": None, "model_prob": 0.6,
             "blend_prob": 0.55, "ensemble_prob": 0.55, "outcome": 1},
        ]
        scored = baselines.score_all(rows)
        assert len(baselines.matched(scored)) == 1

    def test_calibration_table_reports_games_not_just_rows(self):
        rows = [{"game_id": "G1", "model_prob": 0.55, "outcome": 1},
                {"game_id": "G1", "model_prob": 0.56, "outcome": 1},
                {"game_id": "G2", "model_prob": 0.58, "outcome": 0}]
        table = baselines.calibration_table(rows, "model_prob", bins=10)
        bucket = [b for b in table if b["n"]][0]
        assert bucket["n"] == 3
        assert bucket["games"] == 2

    def test_perfect_calibration_scores_zero_error(self):
        rows = []
        for i in range(100):
            rows.append({"game_id": f"G{i}", "model_prob": 0.30,
                         "outcome": 1 if i < 30 else 0})
        table = baselines.calibration_table(rows, "model_prob")
        assert baselines.expected_calibration_error(table) == pytest.approx(0.0, abs=0.01)
