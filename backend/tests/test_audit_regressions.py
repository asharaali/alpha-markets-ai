"""Regression tests for the correctness audit.

Every test in this file was written to FAIL against the pre-audit code and pass after the
fix. Each one names the defect it pins down, so a future change that reintroduces the bug
fails with an explanation rather than a bare assertion.

The audit findings, in the order they appear below:

  1. Simulated scores rounded home and away independently, turning a sampled margin of 3
     into a realised margin of 4 whenever the total's parity disagreed. Key numbers are the
     entire point of the margin distribution, so this corrupted the thing it exists to model.
  2. Per-game simulation seeds came from Python's built-in string hash, which is salted per
     process, so the same slate priced differently after a restart.
  3. Position settlement passed team=None and line=None into the resolver, grading every
     home moneyline against the away team and leaving spreads and totals unsettled forever.
  4. The parlay builder multiplied leg costs (an all-or-nothing product) while execution
     split the stake across singles (an additive product).
  5. Closing-line value read the last snapshot with no pregame cutoff, so a snapshot taken
     after kickoff counted as the close.
  6. Expected value and staking ignored Kalshi's trading fee.
  7. The joint simulator scaled market-blended marginals by a raw-model correlation ratio
     without enforcing the Frechet bounds a joint probability must obey.
"""
from __future__ import annotations

import math

import pytest

from app.core.types import MarketType, Side
from app.models import distributions as D
from app.parlay import simulation
from app.tracking import grading

from tests.conftest import make_game, make_projection, make_signal


# The margin key-number profile actually fitted from 4,363 historical games. Margins of
# exactly 3 occur about 2.5x more often than a smooth Normal implies, and 4 slightly less
# often. That inversion is what the old sampler destroyed, so the test has to run against a
# distribution that really has the spike rather than the smooth default.
FITTED_MARGIN_PROFILE = {
    0: 0.20452, 1: 0.78731, 2: 0.89688, 3: 2.52393, 4: 0.93927, 5: 0.86400,
    6: 1.32176, 7: 1.63188, 8: 0.95239, 9: 0.88100, 10: 1.16077, 11: 0.90300,
    13: 1.09000, 14: 1.29673, 17: 1.21000,
    -1: 0.78731, -2: 0.89688, -3: 2.52393, -4: 0.93927, -5: 0.86400,
    -6: 1.32176, -7: 1.63188, -8: 0.95239, -9: 0.88100, -10: 1.16077,
    -11: 0.90300, -13: 1.09000, -14: 1.29673, -17: 1.21000,
}


def keyed_projection(margin=3.0, total=44.0, sigma=13.0):
    """A projection whose margin carries the real fitted key-number spikes."""
    game = make_game()
    projection = make_projection(game, margin=margin, total=total, sigma=sigma)
    projection.margin = D.margin_distribution(margin, sigma,
                                              profile=FITTED_MARGIN_PROFILE)
    return projection


class TestSimulatedScoresPreserveTheSampledMargin:
    """Finding 1: independent rounding moved margins off their key numbers."""

    def test_every_simulated_scoreline_is_a_valid_integer_pair(self):
        projection = make_projection(make_game(), margin=3.0, total=44.0)
        draws = simulation.simulate_game(projection, correlation=0.18, draws=4000, seed=7)
        assert draws, "simulation produced no scorelines"
        for home, away in draws:
            assert isinstance(home, int) and isinstance(away, int)
            assert home >= 0 and away >= 0

    def test_margin_of_three_is_not_leaked_into_four(self):
        """The distribution's spike at 3 must survive the trip through the sampler.

        The fitted profile puts 2.5x weight on a 3-point margin and 0.94x on a 4-point
        one. Independent rounding inverted that: every margin-3 draw paired with an even
        total came out as a 4.
        """
        projection = keyed_projection(margin=3.0, total=44.0)
        draws = simulation.simulate_game(projection, correlation=0.0, draws=40000, seed=11)
        margins = [h - a for h, a in draws]
        share_three = sum(1 for m in margins if m == 3) / len(margins)
        share_four = sum(1 for m in margins if m == 4) / len(margins)
        expected_three = projection.margin.pmf(3)
        expected_four = projection.margin.pmf(4)

        assert expected_three > 2 * expected_four, "fixture lost its key-number spike"
        assert share_three > share_four, (
            f"margin 3 came out at {share_three:.4f} versus margin 4 at {share_four:.4f}; "
            "the key-number spike was destroyed in sampling")
        assert share_three == pytest.approx(expected_three, abs=0.006)

    def test_sampled_margin_distribution_tracks_the_model(self):
        projection = keyed_projection(margin=-2.5, total=47.0)
        draws = simulation.simulate_game(projection, correlation=0.0, draws=40000, seed=3)
        margins = [h - a for h, a in draws]
        for key in (-7, -3, 0, 3, 7):
            observed = sum(1 for m in margins if m == key) / len(margins)
            assert observed == pytest.approx(projection.margin.pmf(key), abs=0.006), (
                f"sampled share at margin {key} does not match the model")

    def test_no_draws_are_silently_discarded(self):
        projection = make_projection(make_game(), margin=3.0, total=44.0)
        draws = simulation.simulate_game(projection, correlation=0.1, draws=5000, seed=5)
        assert len(draws) == 5000, (
            "draws were dropped rather than resampled, which biases the sample and "
            "quietly shrinks the Monte Carlo error estimate")


class TestSimulationSeedsAreStableAcrossProcesses:
    """Finding 2: abs(hash(game_id)) is salted per process."""

    def test_seed_is_deterministic_and_distinguishes_games(self):
        first = simulation.game_seed("2026_01_KC_BUF")
        assert first == simulation.game_seed("2026_01_KC_BUF")
        assert first != simulation.game_seed("2026_01_BUF_KC")
        # Pinned literal: if the derivation changes, every previously published parlay
        # number changes with it, so that has to be a deliberate edit.
        assert first == 61460873

    def test_seed_survives_a_different_process_hash_salt(self):
        """The real property, tested the only way it can be: in another process.

        PYTHONHASHSEED is fixed at interpreter start, so an in-process assertion cannot
        catch this. Two subprocesses with different salts must agree.
        """
        import os
        import subprocess
        import sys

        script = (
            "import sys; sys.path.insert(0, '.');"
            "from app.parlay import simulation;"
            "print(simulation.game_seed('2026_01_KC_BUF'))"
        )
        seeds = []
        for salt in ("0", "1", "987654"):
            env = {**os.environ, "PYTHONHASHSEED": salt}
            out = subprocess.run([sys.executable, "-c", script], env=env,
                                 capture_output=True, text=True, check=True)
            seeds.append(out.stdout.strip())
        assert len(set(seeds)) == 1, (
            f"seed changed with the process hash salt: {seeds}; parlay prices would move "
            "on every restart")


class TestPositionSettlementUsesRealContractMetadata:
    """Finding 3: team and line were dropped on the way into the resolver."""

    def test_home_moneyline_resolves_to_the_home_team(self):
        assert grading.resolve_contract(
            market_type=MarketType.MONEYLINE.value, selection="KC ML", team="KC",
            line=None, home="KC", home_score=27, away_score=20) is True

    def test_home_moneyline_without_a_team_is_unresolvable_not_guessed(self):
        """The old code let team=None fall through to 'not home', silently grading a
        winning home bet as a loss."""
        assert grading.resolve_contract(
            market_type=MarketType.MONEYLINE.value, selection="KC ML", team=None,
            line=None, home="KC", home_score=27, away_score=20) is None

    def test_spread_needs_its_line(self):
        assert grading.resolve_contract(
            market_type=MarketType.SPREAD.value, selection="KC -3.5", team="KC",
            line=3.5, home="KC", home_score=27, away_score=20) is True
        assert grading.resolve_contract(
            market_type=MarketType.SPREAD.value, selection="KC -3.5", team="KC",
            line=None, home="KC", home_score=27, away_score=20) is None

    def test_no_side_positions_invert_the_outcome(self):
        yes = grading.resolve_contract(
            market_type=MarketType.MONEYLINE.value, selection="KC ML", team="KC",
            line=None, home="KC", home_score=27, away_score=20, side=Side.NO.value)
        assert yes is False, "holding NO on a winning YES contract is a loss"


class TestParlayPricingMatchesTheProductThatExecutes:
    """Finding 4: multiplied costs describe a product the venue does not sell here."""

    def test_basket_of_singles_pays_additively(self):
        from app.parlay import products

        legs = [{"cost": 0.50, "prob": 0.60}, {"cost": 0.40, "prob": 0.50}]
        basket = products.price_basket(legs, stake=100.0)
        # $50 buys 100 contracts at 50c; $50 buys 125 at 40c. Every leg winning returns
        # $225, not the $500 that multiplying the costs implies.
        assert basket["max_payout"] == pytest.approx(225.0, abs=1.0)
        assert basket["all_legs_win_payout"] == pytest.approx(225.0, abs=1.0)
        assert basket["product"] == "basket_of_singles"
        assert basket["payout_is_additive"] is True

    def test_true_parlay_payout_is_never_presented_as_available_without_a_quote(self):
        from app.parlay import products

        legs = [{"cost": 0.50, "prob": 0.60}, {"cost": 0.40, "prob": 0.50}]
        hypothetical = products.price_hypothetical_parlay(legs, joint_prob=0.33)
        assert hypothetical["executable"] is False
        assert hypothetical["pricing_basis"] == "hypothetical"
        assert hypothetical["payout_multiple"] == pytest.approx(5.0, abs=0.01)

    def test_the_two_products_report_different_payouts(self):
        from app.parlay import products

        legs = [{"cost": 0.50, "prob": 0.60}, {"cost": 0.40, "prob": 0.50}]
        basket = products.price_basket(legs, stake=100.0)
        parlay = products.price_hypothetical_parlay(legs, joint_prob=0.33)
        assert basket["max_payout"] < parlay["payout_multiple"] * 100.0, (
            "a basket of singles cannot pay what an all-or-nothing parlay pays; showing "
            "one number for both is the defect")


class TestClosingLineValueRespectsKickoff:
    """Finding 5: the last snapshot was taken as the close regardless of when it landed."""

    def test_snapshot_after_kickoff_is_not_the_closing_price(self):
        history = [
            {"captured_at": 1000.0, "mid": 0.50},
            {"captured_at": 1900.0, "mid": 0.56},   # last pregame read
            {"captured_at": 2600.0, "mid": 0.97},   # in-game, team is winning
        ]
        close = grading.closing_price_from(history, kickoff_ts=2000.0)
        assert close == pytest.approx(0.56), (
            "an in-game price was used as the close, which manufactures enormous CLV out "
            "of nothing")

    def test_no_pregame_snapshot_yields_no_clv(self):
        history = [{"captured_at": 2600.0, "mid": 0.97}]
        assert grading.closing_price_from(history, kickoff_ts=2000.0) is None

    def test_unknown_kickoff_yields_no_clv(self):
        history = [{"captured_at": 1000.0, "mid": 0.5}]
        assert grading.closing_price_from(history, kickoff_ts=None) is None


class TestFeesEnterExpectedValueAndStaking:
    """Finding 6: Kalshi's trading fee was absent from every calculation."""

    def test_fee_matches_the_published_formula(self):
        from app.risk import fees

        # Kalshi: fee = ceil(0.07 x contracts x price x (1 - price)), in cents.
        assert fees.trading_fee(contracts=100, price=0.50) == pytest.approx(1.75, abs=0.01)
        assert fees.trading_fee(contracts=1, price=0.50) == pytest.approx(0.02, abs=0.001)

    def test_fee_is_zero_at_the_extremes(self):
        from app.risk import fees

        assert fees.trading_fee(contracts=100, price=1.0) == 0.0
        assert fees.trading_fee(contracts=100, price=0.0) == 0.0

    def test_expected_value_after_fees_is_below_gross(self):
        from app.risk import fees
        from app.strategies import pricing

        gross = pricing.expected_value(0.60, 0.55)
        net = fees.expected_value_after_fees(prob=0.60, cost=0.55, contracts=100)
        assert net < gross, "fees must reduce expected value"

    def test_a_thin_edge_can_be_erased_by_fees(self):
        from app.risk import fees

        # A one-cent edge at even money does not survive the round trip.
        net = fees.expected_value_after_fees(prob=0.51, cost=0.50, contracts=100)
        assert net < 0.0, (
            "a 1c edge at 50c is smaller than the fee; calling it +EV is the defect")


class TestJointProbabilityObeysFrechetBounds:
    """Finding 7: a raw-model ratio was applied to blended marginals unchecked."""

    def test_joint_never_exceeds_the_smallest_marginal(self):
        game = make_game()
        projection = make_projection(game, margin=6.0, total=45.0)
        sim = simulation.SlateSimulator(correlation=0.2, draws=6000)
        sim.register(game.game_id, projection, game.home)

        ml = make_signal("ML", market_type=MarketType.MONEYLINE, team="KC")
        spread = make_signal("SPREAD", market_type=MarketType.SPREAD, team="KC", line=3.5)
        result = sim.joint_probability([ml, spread], marginals=[0.70, 0.55])
        assert result is not None
        assert result["combined_prob"] <= 0.55 + 1e-9

    def test_joint_never_falls_below_the_frechet_lower_bound(self):
        game = make_game()
        projection = make_projection(game, margin=6.0, total=45.0)
        sim = simulation.SlateSimulator(correlation=0.2, draws=6000)
        sim.register(game.game_id, projection, game.home)

        ml = make_signal("ML", market_type=MarketType.MONEYLINE, team="KC")
        spread = make_signal("SPREAD", market_type=MarketType.SPREAD, team="KC", line=3.5)
        marginals = [0.90, 0.85]
        result = sim.joint_probability([ml, spread], marginals=marginals)
        assert result is not None
        lower = max(0.0, sum(marginals) - (len(marginals) - 1))
        assert result["combined_prob"] >= lower - 1e-9, (
            f"P(A and B) cannot be below {lower:.4f} when the marginals are {marginals}")

    def test_bounds_hold_across_many_marginal_combinations(self):
        game = make_game()
        projection = make_projection(game, margin=3.0, total=45.0)
        sim = simulation.SlateSimulator(correlation=0.25, draws=4000)
        sim.register(game.game_id, projection, game.home)
        ml = make_signal("ML", market_type=MarketType.MONEYLINE, team="KC")
        total = make_signal("TOT", market_type=MarketType.TOTAL, line=44.5)

        for pa in (0.15, 0.35, 0.55, 0.75, 0.95):
            for pb in (0.15, 0.35, 0.55, 0.75, 0.95):
                result = sim.joint_probability([ml, total], marginals=[pa, pb])
                assert result is not None
                joint = result["combined_prob"]
                assert joint <= min(pa, pb) + 1e-9
                assert joint >= max(0.0, pa + pb - 1.0) - 1e-9

    def test_simulation_uncertainty_is_reported(self):
        game = make_game()
        projection = make_projection(game, margin=3.0, total=45.0)
        sim = simulation.SlateSimulator(correlation=0.2, draws=4000)
        sim.register(game.game_id, projection, game.home)
        ml = make_signal("ML", market_type=MarketType.MONEYLINE, team="KC")
        total = make_signal("TOT", market_type=MarketType.TOTAL, line=44.5)
        result = sim.joint_probability([ml, total], marginals=[0.60, 0.55])
        assert result is not None
        assert result["standard_error"] > 0.0
        assert "draws" in result
