"""Recommendations: complete, honest, and ordered by value rather than by hope."""
from __future__ import annotations

import pytest

from app.core.types import Confidence, MarketType
from app.risk import fees
from app.strategies import recommendation as rec

from tests.conftest import make_signal


def build_one(**kwargs):
    signal = make_signal(kwargs.pop("ticker", "NFL-KC"), **kwargs)
    return rec.build(signal, matchup="Bills at Chiefs", kickoff="2026-09-13T17:00:00Z",
                     sizing={"stake": 50.0, "basis": "25% Kelly",
                             "limits_applied": ["per-game cap"]},
                     quote_age=12.0)


class TestCompleteness:
    """Requirement: every field a person needs to evaluate the bet, not just place it."""

    def test_every_required_field_is_present(self):
        card = build_one().to_dict()
        assert card["game_id"] and card["ticker"] and card["selection"]
        assert card["side"] in ("yes", "no")
        assert card["settles"]
        assert card["probability"]["raw_model"] is not None
        assert card["probability"]["final"] is not None
        assert card["probability"]["market"] is not None
        assert card["price"]["cost"] is not None
        assert card["price"]["quote_age_seconds"] == 12.0
        assert card["price"]["depth_usd"] > 0
        assert card["economics"]["ev_after_fees"] is not None
        assert card["price"]["max_entry_price"] > 0
        assert card["sizing"]["recommended_stake"] == 50.0
        assert card["sizing"]["limits_applied"] == ["per-game cap"]
        assert isinstance(card["reasoning"]["supporting"], list)
        assert isinstance(card["reasoning"]["missing"], list)

    def test_raw_and_final_probability_are_both_shown(self):
        card = build_one(model_prob=0.64, market_prob=0.56, cost=0.56).to_dict()
        assert card["probability"]["raw_model"] == pytest.approx(0.64, abs=1e-4)
        assert card["probability"]["final"] is not None

    def test_settlement_condition_is_written_out(self):
        card = build_one(market_type=MarketType.SPREAD, team="KC", line=3.5).to_dict()
        assert "3.5" in card["settles"]
        assert "more than" in card["settles"].lower()

    def test_moneyline_settlement_mentions_the_tie(self):
        card = build_one(market_type=MarketType.MONEYLINE, team="KC").to_dict()
        assert "tie" in card["settles"].lower()


class TestEconomicsAreAfterFees:
    def test_net_ev_is_below_gross(self):
        card = build_one(model_prob=0.62, market_prob=0.56, cost=0.56).to_dict()
        assert card["economics"]["ev_after_fees"] < card["economics"]["ev_gross"]

    def test_breakeven_probability_exceeds_the_price(self):
        card = build_one(cost=0.50).to_dict()
        assert card["economics"]["breakeven_probability"] > 0.50

    def test_an_edge_that_only_exists_before_fees_is_flagged(self):
        card = build_one(model_prob=0.505, market_prob=0.50, cost=0.50).to_dict()
        assert card["economics"]["ev_after_fees"] <= 0
        assert any("disappears after" in w for w in card["reasoning"]["warnings"])


class TestValueVersusLikelihood:
    """The distinction that stops a bankroll dying on heavy favourites."""

    def test_a_heavy_favourite_at_a_bad_price_is_likely_but_not_valuable(self):
        card = build_one(model_prob=0.86, market_prob=0.90, cost=0.90).to_dict()
        assert card["assessment"]["win_likelihood"] == "likely"
        assert card["assessment"]["value_rating"] == "negative"

    def test_a_longshot_at_a_good_price_is_unlikely_but_valuable(self):
        card = build_one(model_prob=0.35, market_prob=0.25, cost=0.25).to_dict()
        assert card["assessment"]["win_likelihood"] == "unlikely"
        assert card["assessment"]["value_rating"] in ("strong", "moderate")

    def test_ranking_does_not_put_the_favourite_first(self):
        favourite = build_one(ticker="FAV", model_prob=0.86, market_prob=0.88, cost=0.88)
        underdog = build_one(ticker="DOG", model_prob=0.35, market_prob=0.25, cost=0.25)
        ordered = rec.rank([favourite, underdog])
        assert ordered[0].ticker == "DOG"

    def test_ranking_prefers_a_robust_edge_over_a_bigger_fragile_one(self):
        fragile = build_one(ticker="FRAGILE", model_prob=0.525, market_prob=0.50,
                            cost=0.50)
        robust = build_one(ticker="ROBUST", model_prob=0.40, market_prob=0.30, cost=0.30)
        ordered = rec.rank([fragile, robust])
        assert ordered[0].ticker == "ROBUST"


class TestRobustness:
    def test_an_edge_inside_the_model_error_is_not_robust(self):
        assert rec.is_robust(final_prob=0.52, cost=0.50, contracts=200) is False

    def test_a_wide_edge_is_robust(self):
        assert rec.is_robust(final_prob=0.65, cost=0.50, contracts=200) is True

    def test_a_fragile_recommendation_says_so(self):
        card = build_one(model_prob=0.52, market_prob=0.50, cost=0.50).to_dict()
        assert card["assessment"]["robust"] is False
        assert "unproven" in card["assessment"]["robustness_note"]


class TestMaximumEntryPrice:
    def test_max_entry_is_above_the_current_price_on_a_real_edge(self):
        card = build_one(model_prob=0.65, market_prob=0.55, cost=0.55).to_dict()
        assert card["price"]["max_entry_price"] > card["price"]["cost"]

    def test_max_entry_never_reaches_the_model_probability(self):
        """At a price equal to the model's probability the edge is exactly zero."""
        price = rec.max_entry_price(0.65, contracts=200)
        assert price < 0.65


class TestLiquidityAndStaleness:
    def test_thin_depth_against_the_recommended_stake_warns(self):
        signal = make_signal("NFL-KC", depth=20.0)
        card = rec.build(signal, matchup="m", kickoff=None,
                         sizing={"stake": 100.0, "basis": "flat"},
                         quote_age=5.0).to_dict()
        assert card["price"]["depth_covers_stake"] is False
        assert any("resting at this price" in w for w in card["reasoning"]["warnings"])

    def test_a_stale_quote_is_marked_stale(self):
        card = build_one(ticker="X")
        stale = rec.build(make_signal("X"), matchup="m", kickoff=None,
                          sizing={"stake": 10.0, "basis": "flat"},
                          quote_age=300.0).to_dict()
        assert stale["price"]["stale"] is True
        assert card.to_dict()["price"]["stale"] is False


class TestEmptyIsANormalAnswer:
    def test_empty_reason_explains_rather_than_apologises(self):
        message = rec.empty_reason(42, {"failed the EV floor": 30,
                                        "too thin to trade": 12})
        assert "42" in message
        assert "normal result" in message
        assert "not lowered" in message

    def test_no_priced_contracts_says_so_plainly(self):
        assert "have not opened yet" in rec.empty_reason(0, {})


class TestReferenceSignalsAreNotRecommended:
    def test_a_reference_signal_produces_no_recommendation(self):
        signal = make_signal("PROP", confidence=Confidence.REFERENCE)
        assert rec.build(signal, matchup="m", kickoff=None) is None
