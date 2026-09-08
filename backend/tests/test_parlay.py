"""Tests for the parlay engine: correlation, conflicts, and the arithmetic of a combination.

The central claim this suite defends is that same-game legs are NOT priced by multiplying
probabilities, and that the engine refuses combinations that cannot or should not be made.
"""
from __future__ import annotations

import pytest

from app.core.types import Confidence, MarketType
from app.parlay import conflicts
from app.parlay.builder import CATEGORIES, build, candidates, evaluate
from app.parlay.simulation import SlateSimulator, leg_predicate
from tests.conftest import make_game, make_projection, make_signal

GAME_A = "2026_01_AAA_BBB"
GAME_B = "2026_01_CCC_DDD"


@pytest.fixture
def simulator():
    game_a = make_game(GAME_A, home="KC", away="BUF")
    game_b = make_game(GAME_B, home="SF", away="LA")
    sim = SlateSimulator(correlation=0.03, draws=20000)
    sim.register(GAME_A, make_projection(game_a, margin=3.0, total=45.0), "KC")
    sim.register(GAME_B, make_projection(game_b, margin=-2.0, total=48.0), "SF")
    return sim


@pytest.fixture
def homes():
    return {GAME_A: "KC", GAME_B: "SF"}


class TestLegPredicates:
    def test_home_moneyline_reads_the_scoreline(self):
        signal = make_signal("ml", team="KC", market_type=MarketType.MONEYLINE)
        test = leg_predicate(signal, "KC")
        assert test(24, 20) and not test(20, 24)

    def test_away_moneyline_is_the_mirror(self):
        signal = make_signal("ml", team="BUF", market_type=MarketType.MONEYLINE)
        test = leg_predicate(signal, "KC")
        assert test(20, 24) and not test(24, 20)

    def test_home_spread_requires_winning_by_more_than_the_line(self):
        signal = make_signal("sp", team="KC", market_type=MarketType.SPREAD, line=6.5)
        test = leg_predicate(signal, "KC")
        assert test(28, 20) and not test(27, 21)

    def test_total_reads_the_sum(self):
        signal = make_signal("tot", market_type=MarketType.TOTAL, line=44.5)
        test = leg_predicate(signal, "KC")
        assert test(24, 21) and not test(21, 21)

    def test_team_total_reads_only_that_team(self):
        signal = make_signal("tt", team="BUF", market_type=MarketType.TEAM_TOTAL, line=20.5)
        test = leg_predicate(signal, "KC")
        assert test(10, 24) and not test(45, 20)

    def test_player_props_cannot_be_simulated_from_a_scoreline(self):
        signal = make_signal("prop", market_type=MarketType.PASS_YARDS, line=250)
        assert leg_predicate(signal, "KC") is None


class TestCorrelation:
    """The heart of it: same-game legs must not be multiplied."""

    def test_positively_correlated_same_game_legs_beat_the_naive_product(self, simulator):
        ml = make_signal("a-ml", game_id=GAME_A, team="KC", market_type=MarketType.MONEYLINE)
        spread = make_signal("a-sp", game_id=GAME_A, team="KC",
                             market_type=MarketType.SPREAD, line=-6.5)
        result = simulator.joint_probability([ml, spread])
        assert result["combined_prob"] > result["naive_prob"] * 1.2, (
            "winning outright and covering a negative line move together; multiplying "
            "them badly understates the parlay")

    def test_mutually_exclusive_legs_have_zero_joint_probability(self, simulator):
        kc = make_signal("a-kc", game_id=GAME_A, team="KC", market_type=MarketType.MONEYLINE)
        buf = make_signal("a-buf", game_id=GAME_A, team="BUF", market_type=MarketType.MONEYLINE)
        result = simulator.joint_probability([kc, buf])
        assert result["combined_prob"] == 0.0

    def test_different_games_are_independent(self, simulator):
        a = make_signal("a-ml", game_id=GAME_A, team="KC", market_type=MarketType.MONEYLINE)
        b = make_signal("b-ml", game_id=GAME_B, team="SF", market_type=MarketType.MONEYLINE)
        result = simulator.joint_probability([a, b])
        assert result["combined_prob"] == pytest.approx(result["naive_prob"], abs=0.02)

    def test_nested_legs_collapse_to_the_harder_one(self, simulator):
        easy = make_signal("e", game_id=GAME_A, team="KC",
                           market_type=MarketType.SPREAD, line=1.5)
        hard = make_signal("h", game_id=GAME_A, team="KC",
                           market_type=MarketType.SPREAD, line=10.5)
        joint = simulator.joint_probability([easy, hard])["combined_prob"]
        alone = simulator.single_probability(hard)
        assert joint == pytest.approx(alone, abs=0.01)

    def test_simulation_matches_the_analytic_probability_for_one_leg(self, simulator):
        signal = make_signal("a-ml", game_id=GAME_A, team="KC",
                             market_type=MarketType.MONEYLINE)
        simulated = simulator.single_probability(signal)
        # The analytic model has KC favoured by 3 with sigma 13.
        assert 0.52 < simulated < 0.62

    def test_results_are_deterministic_across_runs(self):
        game = make_game(GAME_A, home="KC", away="BUF")
        signal = make_signal("a-ml", game_id=GAME_A, team="KC")
        values = []
        for _ in range(2):
            sim = SlateSimulator(correlation=0.03, draws=5000)
            sim.register(GAME_A, make_projection(game), "KC")
            values.append(sim.single_probability(signal))
        assert values[0] == values[1], "a recommendation that changes on refresh is not one"


class TestConflicts:
    def test_both_moneylines_is_impossible(self, homes):
        legs = [make_signal("a", game_id=GAME_A, team="KC"),
                make_signal("b", game_id=GAME_A, team="BUF")]
        allowed, found = conflicts.is_allowed(legs, homes)
        assert not allowed and found[0].kind == "impossible"

    def test_same_ticker_twice_is_impossible(self, homes):
        legs = [make_signal("same", game_id=GAME_A, team="KC"),
                make_signal("same", game_id=GAME_A, team="KC")]
        allowed, _ = conflicts.is_allowed(legs, homes)
        assert not allowed

    def test_two_rungs_of_one_ladder_are_nested(self, homes):
        legs = [make_signal("a", game_id=GAME_A, team="KC",
                            market_type=MarketType.SPREAD, line=3.5),
                make_signal("b", game_id=GAME_A, team="KC",
                            market_type=MarketType.SPREAD, line=7.5)]
        allowed, found = conflicts.is_allowed(legs, homes)
        assert not allowed and found[0].kind == "nested"

    def test_two_total_rungs_are_nested(self, homes):
        legs = [make_signal("a", game_id=GAME_A, market_type=MarketType.TOTAL, line=41.5),
                make_signal("b", game_id=GAME_A, market_type=MarketType.TOTAL, line=47.5)]
        allowed, _ = conflicts.is_allowed(legs, homes)
        assert not allowed

    def test_moneyline_plus_own_positive_spread_is_nested(self, homes):
        legs = [make_signal("ml", game_id=GAME_A, team="KC"),
                make_signal("sp", game_id=GAME_A, team="KC",
                            market_type=MarketType.SPREAD, line=6.5)]
        allowed, found = conflicts.is_allowed(legs, homes)
        assert not allowed and found[0].kind == "nested"

    def test_moneyline_plus_opposing_spread_is_impossible(self, homes):
        legs = [make_signal("ml", game_id=GAME_A, team="KC"),
                make_signal("sp", game_id=GAME_A, team="BUF",
                            market_type=MarketType.SPREAD, line=6.5)]
        allowed, found = conflicts.is_allowed(legs, homes)
        assert not allowed and found[0].kind == "impossible"

    def test_legs_in_different_games_never_conflict(self, homes):
        legs = [make_signal("a", game_id=GAME_A, team="KC"),
                make_signal("b", game_id=GAME_B, team="SF")]
        allowed, found = conflicts.is_allowed(legs, homes)
        assert allowed and not found

    def test_total_and_moneyline_in_one_game_coexist(self, homes):
        legs = [make_signal("ml", game_id=GAME_A, team="KC"),
                make_signal("tot", game_id=GAME_A, market_type=MarketType.TOTAL, line=44.5)]
        allowed, _ = conflicts.is_allowed(legs, homes)
        assert allowed

    def test_coexistence_notes_call_out_same_game_legs(self, homes):
        legs = [make_signal("ml", game_id=GAME_A, team="KC"),
                make_signal("tot", game_id=GAME_A, market_type=MarketType.TOTAL, line=44.5)]
        notes = conflicts.coexistence_notes(legs, homes)
        assert any("share" in note for note in notes)


class TestEvaluation:
    def test_evaluate_prices_from_real_costs(self, simulator, homes):
        legs = [make_signal("a", game_id=GAME_A, team="KC", cost=0.50, model_prob=0.60),
                make_signal("b", game_id=GAME_B, team="SF", cost=0.50, model_prob=0.60)]
        parlay = evaluate(legs, simulator, "balanced", homes)
        assert parlay is not None
        assert parlay.combined_cost == pytest.approx(0.25)
        assert parlay.payout_multiple == pytest.approx(4.0)

    def test_evaluate_refuses_a_conflicting_combination(self, simulator, homes):
        legs = [make_signal("a", game_id=GAME_A, team="KC"),
                make_signal("b", game_id=GAME_A, team="BUF")]
        assert evaluate(legs, simulator, "balanced", homes) is None

    def test_evaluate_reports_the_correlation_effect(self, simulator, homes):
        legs = [make_signal("ml", game_id=GAME_A, team="KC", cost=0.55),
                make_signal("tt", game_id=GAME_A, team="KC",
                            market_type=MarketType.TEAM_TOTAL, line=23.5, cost=0.5)]
        parlay = evaluate(legs, simulator, "balanced", homes)
        assert parlay is not None
        assert parlay.correlation_effect != 0.0
        assert any("orrelation" in line for line in parlay.explanation)

    def test_ev_matches_the_definition(self, simulator, homes):
        legs = [make_signal("a", game_id=GAME_A, team="KC", cost=0.50),
                make_signal("b", game_id=GAME_B, team="SF", cost=0.50)]
        parlay = evaluate(legs, simulator, "balanced", homes)
        expected = (parlay.combined_prob / parlay.combined_cost) - 1.0
        assert parlay.ev_per_dollar == pytest.approx(expected)

    def test_a_single_leg_is_not_a_parlay(self, simulator, homes):
        legs = [make_signal("a", game_id=GAME_A, team="KC")]
        assert evaluate(legs, simulator, "balanced", homes) is None


class TestBuild:
    def test_empty_board_returns_an_explanation_not_a_parlay(self, simulator, homes):
        result = build([], simulator, homes, category_key="balanced")
        assert result["parlays"] == []
        assert "clear the value gate" in result["note"]

    def test_non_value_legs_are_never_used(self, simulator, homes):
        legs = [make_signal("a", game_id=GAME_A, team="KC", value=False),
                make_signal("b", game_id=GAME_B, team="SF", value=False)]
        result = build(legs, simulator, homes, category_key="balanced")
        assert result["parlays"] == []

    def test_a_genuine_board_produces_a_priced_parlay(self, simulator, homes):
        legs = [
            make_signal("a", game_id=GAME_A, team="KC", model_prob=0.66,
                        market_prob=0.55, cost=0.55),
            make_signal("b", game_id=GAME_B, team="SF", model_prob=0.64,
                        market_prob=0.54, cost=0.54),
        ]
        result = build(legs, simulator, homes, category_key="conservative")
        assert result["parlays"], result["note"]
        parlay = result["parlays"][0]
        assert parlay["leg_count"] == 2
        assert parlay["ev_per_dollar"] > 0
        assert parlay["risk_rating"] in {"moderate", "elevated", "high", "very high"}

    def test_conservative_rejects_low_probability_legs(self, simulator, homes):
        legs = [make_signal("a", game_id=GAME_A, team="KC", model_prob=0.30, cost=0.25),
                make_signal("b", game_id=GAME_B, team="SF", model_prob=0.30, cost=0.25)]
        pool = candidates(legs, CATEGORIES["conservative"])
        assert pool == []

    def test_categories_are_ordered_by_risk(self):
        assert (CATEGORIES["conservative"].min_leg_prob
                > CATEGORIES["balanced"].min_leg_prob
                > CATEGORIES["aggressive"].min_leg_prob)


class TestMarginalConsistency:
    """The combined probability must be consistent with the leg probabilities displayed."""

    def test_independent_legs_combine_to_the_published_product(self, simulator, homes):
        legs = [make_signal("a", game_id=GAME_A, team="KC", model_prob=0.66, cost=0.55),
                make_signal("b", game_id=GAME_B, team="SF", model_prob=0.64, cost=0.54)]
        result = simulator.joint_probability(legs, marginals=[0.66, 0.64])
        assert result["combined_prob"] == pytest.approx(0.66 * 0.64, abs=0.02)

    def test_published_marginals_survive_the_simulation(self, simulator, homes):
        """A parlay must not be priced off numbers different from the ones on the cards."""
        legs = [make_signal("a", game_id=GAME_A, team="KC", model_prob=0.70, cost=0.55),
                make_signal("b", game_id=GAME_B, team="SF", model_prob=0.70, cost=0.54)]
        parlay = evaluate(legs, simulator, "balanced", homes)
        assert parlay is not None
        assert parlay.naive_prob == pytest.approx(0.49, abs=0.01)

    def test_correlation_still_applies_to_same_game_legs(self, simulator, homes):
        legs = [make_signal("ml", game_id=GAME_A, team="KC", model_prob=0.60, cost=0.55),
                make_signal("sp", game_id=GAME_A, team="KC",
                            market_type=MarketType.SPREAD, line=-6.5,
                            model_prob=0.40, cost=0.38)]
        result = simulator.joint_probability(legs, marginals=[0.60, 0.40])
        assert result["combined_prob"] > 0.60 * 0.40

    def test_parlay_never_exceeds_its_weakest_leg(self, simulator, homes):
        legs = [make_signal("ml", game_id=GAME_A, team="KC", model_prob=0.90, cost=0.85),
                make_signal("sp", game_id=GAME_A, team="KC",
                            market_type=MarketType.SPREAD, line=-1.5,
                            model_prob=0.55, cost=0.52)]
        result = simulator.joint_probability(legs, marginals=[0.90, 0.55])
        assert result["combined_prob"] <= 0.55 + 1e-9

    def test_impossible_legs_stay_impossible_under_calibration(self, simulator, homes):
        legs = [make_signal("kc", game_id=GAME_A, team="KC", model_prob=0.6),
                make_signal("buf", game_id=GAME_A, team="BUF", model_prob=0.4)]
        result = simulator.joint_probability(legs, marginals=[0.6, 0.4])
        assert result["combined_prob"] == 0.0
