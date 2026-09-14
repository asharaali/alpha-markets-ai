"""A parlay payout must correspond to a real quote or be unmistakably hypothetical.

The original defect in one sentence: the board multiplied leg costs and published the
reciprocal as a payout multiple, while the place endpoint split the stake across separate
single contracts. The number shown belonged to the product that was not being bought.
"""
from __future__ import annotations

import pytest

from app.parlay import products
from app.risk import fees


LEGS = [
    {"cost": 0.50, "prob": 0.60, "ticker": "A"},
    {"cost": 0.40, "prob": 0.50, "ticker": "B"},
]


class TestTheTwoProductsAreDifferentBets:
    def test_basket_and_parlay_pay_different_amounts(self):
        basket = products.price_basket(LEGS, stake=100.0)
        parlay = products.price_hypothetical_parlay(LEGS, joint_prob=0.33, stake=100.0)
        assert basket["max_payout"] == pytest.approx(225.0, abs=1.0)
        assert parlay["payout_multiple"] * 100.0 == pytest.approx(500.0, abs=1.0)

    def test_only_the_basket_is_executable(self):
        assert products.price_basket(LEGS, stake=100.0)["executable"] is True
        assert products.price_hypothetical_parlay(
            LEGS, joint_prob=0.33)["executable"] is False

    def test_the_hypothetical_says_no_venue_quoted_it(self):
        note = products.price_hypothetical_parlay(LEGS, joint_prob=0.33)["note"]
        assert "HYPOTHETICAL" in note
        assert "cannot be bought" in note

    def test_basket_partial_wins_pay(self):
        basket = products.price_basket(LEGS, stake=100.0)
        assert basket["partial_wins_pay"] is True
        assert basket["payout_is_additive"] is True
        # Each leg's own payout is stated, because in a basket one leg can pay alone.
        assert all("payout_if_this_leg_wins" in leg for leg in basket["legs"])

    def test_parlay_payout_is_not_additive(self):
        parlay = products.price_hypothetical_parlay(LEGS, joint_prob=0.33)
        assert parlay["payout_is_additive"] is False


class TestBasketArithmetic:
    def test_contracts_are_whole_and_priced_at_the_book(self):
        basket = products.price_basket(LEGS, stake=100.0)
        a, b = basket["legs"]
        assert a["contracts"] == 100     # $50 at 50c
        assert b["contracts"] == 125     # $50 at 40c
        assert basket["max_payout"] == pytest.approx(225.0)

    def test_fees_are_charged_per_leg(self):
        basket = products.price_basket(LEGS, stake=100.0)
        expected = (fees.trading_fee(contracts=100, price=0.50)
                    + fees.trading_fee(contracts=125, price=0.40))
        assert basket["fees"] == pytest.approx(expected, abs=0.01)
        assert basket["fees"] > 0

    def test_net_result_is_after_fees(self):
        basket = products.price_basket(LEGS, stake=100.0)
        assert basket["net_if_all_legs_win"] == pytest.approx(
            basket["max_payout"] - basket["stake"] - basket["fees"], abs=0.01)

    def test_uneven_weights_are_respected(self):
        basket = products.price_basket(LEGS, stake=100.0, weights=[0.75, 0.25])
        a, b = basket["legs"]
        assert a["contracts"] == 150     # $75 at 50c
        assert b["contracts"] == 62      # $25 at 40c

    def test_an_empty_basket_is_not_an_error(self):
        basket = products.price_basket([], stake=100.0)
        assert basket["max_payout"] == 0.0


class TestComboQuotesAreTheOnlyExecutableMultiLeg:
    def test_a_real_quote_is_executable_and_uses_the_venue_price(self):
        combo = products.price_combo_quote(
            LEGS, ticker="KXNFLCOMBO-ABC", yes_ask=0.22, yes_bid=0.19,
            depth_usd=800.0, joint_prob=0.33, stake=100.0, quote_age_seconds=4.0)
        assert combo["executable"] is True
        assert combo["pricing_basis"] == "live_combo_order_book"
        assert combo["quoted_cost"] == pytest.approx(0.22)
        # The venue's price, not the multiplied one (0.50 x 0.40 = 0.20).
        assert combo["payout_multiple"] == pytest.approx(1 / 0.22, abs=0.01)

    def test_a_combo_with_no_ask_is_not_executable(self):
        combo = products.price_combo_quote(
            LEGS, ticker="KXNFLCOMBO-ABC", yes_ask=None, yes_bid=0.19,
            depth_usd=0.0, joint_prob=0.33)
        assert combo["executable"] is False
        assert "nothing to buy" in combo["note"]

    def test_combo_economics_are_after_fees(self):
        combo = products.price_combo_quote(
            LEGS, ticker="T", yes_ask=0.22, yes_bid=0.19, depth_usd=800.0,
            joint_prob=0.33, stake=100.0)
        assert combo["ev_per_dollar_after_fees"] is not None
        gross = (0.33 / 0.22) - 1.0
        assert combo["ev_per_dollar_after_fees"] < gross
        assert combo["breakeven_probability"] > 0.22

    def test_quote_age_is_carried_through(self):
        combo = products.price_combo_quote(
            LEGS, ticker="T", yes_ask=0.22, yes_bid=0.19, depth_usd=800.0,
            joint_prob=0.33, quote_age_seconds=12.5)
        assert combo["quote_age_seconds"] == 12.5


class TestSimulationUncertaintyIsShown:
    def test_a_probability_range_accompanies_the_point_estimate(self):
        parlay = products.price_hypothetical_parlay(
            LEGS, joint_prob=0.33, standard_error=0.004)
        assert parlay["probability_range"][0] < 0.33 < parlay["probability_range"][1]

    def test_no_range_when_no_standard_error_was_measured(self):
        parlay = products.price_hypothetical_parlay(LEGS, joint_prob=0.33)
        assert "probability_range" not in parlay


class TestTheComparisonExplainsTheDifference:
    def test_comparison_names_the_reason_the_numbers_differ(self):
        basket = products.price_basket(LEGS, stake=100.0)
        parlay = products.price_hypothetical_parlay(LEGS, joint_prob=0.33)
        comparison = products.compare(basket, parlay)
        assert "every leg lands" in comparison["difference_explained"]
        assert comparison["basket_max_payout"] < comparison["parlay_payout_multiple"] * 100


class TestUnusablePricesAreRefused:
    @pytest.mark.parametrize("cost", [0.0, 1.0, -0.2, 1.5])
    def test_a_leg_with_an_impossible_price_cannot_be_combined(self, cost):
        legs = [{"cost": cost, "prob": 0.5}, {"cost": 0.4, "prob": 0.5}]
        result = products.price_hypothetical_parlay(legs, joint_prob=0.2)
        assert result["executable"] is False
        assert "no usable price" in result["note"]
