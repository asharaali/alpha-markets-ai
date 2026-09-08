"""Tests for the money math: odds conversions, expected value, Kelly, vig removal.

These are the calculations where a sign error or an off-by-one silently costs real money
rather than throwing an exception, so they are pinned against hand-computed values.
"""
from __future__ import annotations

import math

import pytest

from app.core.types import MarketQuote, MarketType, Side
from app.strategies import pricing


def quote(bid=None, ask=None, depth=1000.0, side=Side.YES, ticker="T",
          market_type=MarketType.MONEYLINE):
    return MarketQuote(venue="kalshi", ticker=ticker, event_ticker="E",
                       market_type=market_type, label="L", side=side,
                       yes_bid=bid, yes_ask=ask, depth_usd=depth)


class TestQuoteArithmetic:
    def test_mid_is_the_average_of_bid_and_ask(self):
        assert quote(0.40, 0.44).mid == pytest.approx(0.42)

    def test_cost_to_buy_yes_is_the_ask_not_the_mid(self):
        # Paying the mid is not a thing you can do; you pay the ask.
        assert quote(0.40, 0.44).cost == pytest.approx(0.44)

    def test_cost_to_buy_no_is_one_minus_the_yes_bid(self):
        q = quote(0.40, 0.44, side=Side.NO)
        assert q.cost == pytest.approx(0.60)

    def test_no_side_implied_probability_is_the_complement(self):
        q = quote(0.40, 0.44, side=Side.NO)
        assert q.implied_prob() == pytest.approx(0.58)

    def test_missing_book_yields_no_price_rather_than_zero(self):
        q = quote(None, None)
        assert q.mid is None and q.cost is None and q.implied_prob() is None

    def test_one_sided_book_still_reports_a_mid(self):
        assert quote(0.30, None).mid == pytest.approx(0.30)


class TestExpectedValue:
    def test_ev_is_zero_when_probability_equals_cost(self):
        assert pricing.expected_value(0.50, 0.50) == pytest.approx(0.0)

    def test_ev_per_dollar_on_a_favourable_price(self):
        # $1 buys 1/0.40 = 2.5 contracts. 50% of the time they pay $2.50.
        assert pricing.expected_value(0.50, 0.40) == pytest.approx(0.25)

    def test_ev_is_negative_when_overpaying(self):
        assert pricing.expected_value(0.40, 0.50) == pytest.approx(-0.20)

    def test_ev_is_undefined_at_impossible_prices(self):
        assert pricing.expected_value(0.5, 0.0) is None
        assert pricing.expected_value(0.5, 1.0) is None

    def test_decimal_and_american_odds_round_trip(self):
        assert pricing.decimal_odds(0.50) == pytest.approx(2.0)
        assert pricing.american_odds(0.50) == 100
        assert pricing.american_odds(0.80) == -400
        assert pricing.american_odds(0.25) == 300


class TestKelly:
    def test_kelly_is_zero_on_a_break_even_bet(self):
        assert pricing.kelly_fraction(0.50, 0.50) == pytest.approx(0.0)

    def test_kelly_is_zero_on_a_losing_bet(self):
        assert pricing.kelly_fraction(0.40, 0.50) == 0.0

    def test_kelly_matches_the_closed_form(self):
        # b = 1/0.4 - 1 = 1.5; f* = (1.5*0.5 - 0.5)/1.5 = 1/6
        assert pricing.kelly_fraction(0.50, 0.40) == pytest.approx(1 / 6)

    def test_kelly_never_exceeds_one(self):
        assert pricing.kelly_fraction(0.99, 0.10) <= 1.0


class TestVigRemoval:
    def test_two_sided_market_normalises_to_one(self):
        quotes = [quote(0.62, 0.64, ticker="A"), quote(0.37, 0.39, ticker="B")]
        free = pricing.vig_free(quotes)
        assert sum(free.values()) == pytest.approx(1.0)
        # 0.63 and 0.38 sum to 1.01, so each is scaled down proportionally.
        assert free["A"] == pytest.approx(0.63 / 1.01)

    def test_a_fair_market_is_left_alone(self):
        quotes = [quote(0.59, 0.61, ticker="A"), quote(0.39, 0.41, ticker="B")]
        free = pricing.vig_free(quotes)
        assert free["A"] == pytest.approx(0.60)

    def test_unpriced_quotes_are_excluded_not_zeroed(self):
        quotes = [quote(0.60, 0.60, ticker="A"), quote(None, None, ticker="B")]
        free = pricing.vig_free(quotes)
        assert "B" not in free and free["A"] == pytest.approx(1.0)


class TestBlending:
    def test_blend_sits_between_model_and_market(self):
        blended = pricing.blend(0.70, 0.60, 0.40)
        assert 0.60 < blended < 0.70
        assert blended == pytest.approx(0.64)

    def test_extreme_disagreement_is_shrunk_harder(self):
        """A 40-point gap must not survive as a 40-point gap."""
        blended = pricing.blend(0.90, 0.50, 1.0)
        gap = blended - 0.50
        assert gap < 0.40, "extreme disagreement was not shrunk at all"
        expected = pricing.DISAGREEMENT_TOLERANCE + (0.40 - pricing.DISAGREEMENT_TOLERANCE) * pricing.EXCESS_TRUST
        assert gap == pytest.approx(expected)

    def test_shrinkage_is_symmetric_below_the_market(self):
        low = pricing.blend(0.10, 0.50, 1.0)
        high = pricing.blend(0.90, 0.50, 1.0)
        assert (0.50 - low) == pytest.approx(high - 0.50)

    def test_blend_never_returns_an_impossible_probability(self):
        assert 0 < pricing.blend(0.0, 0.0, 1.0) < 1
        assert 0 < pricing.blend(1.0, 1.0, 1.0) < 1


class TestValueGate:
    def test_illiquid_markets_never_qualify(self):
        assert not pricing.is_value(edge=0.20, ev=0.50, market_prob=0.5, liquid=False)

    def test_extreme_longshots_are_excluded(self):
        assert not pricing.is_value(edge=0.20, ev=0.90, market_prob=0.02, liquid=True)

    def test_near_locks_are_excluded(self):
        assert not pricing.is_value(edge=0.20, ev=0.10, market_prob=0.97, liquid=True)

    def test_small_ev_is_rejected_even_with_a_big_edge(self):
        assert not pricing.is_value(edge=0.20, ev=0.001, market_prob=0.5, liquid=True)

    def test_a_genuine_edge_passes(self):
        assert pricing.is_value(edge=0.05, ev=0.12, market_prob=0.40, liquid=True)

    def test_longshot_with_small_absolute_edge_but_large_return_passes(self):
        """The reason the gate is EV-based: 2 points of edge on a 15c contract is a real bet."""
        assert pricing.is_value(edge=0.02, ev=0.13, market_prob=0.15, liquid=True)
