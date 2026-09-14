"""The model's published numbers must agree with each other.

Coherence does not make a model right. It makes a model checkable: if the moneyline and the
pick'em spread disagree, one of them is definitely wrong, and the old code published both
without ever comparing them.
"""
from __future__ import annotations

import pytest

from app.core.types import MarketType
from app.models import coherence, distributions as D

from tests.conftest import make_game, make_projection, make_signal


class TestProjectionCoherence:
    def test_a_normal_projection_is_coherent(self):
        projection = make_projection(make_game(), margin=3.0, total=45.0)
        result = coherence.check_projection(
            projection,
            spread_lines=[-7.5, -3.5, -0.5, 2.5, 6.5],
            total_lines=[41.5, 44.5, 47.5, 50.5])
        assert result.coherent, result.failures()

    def test_moneyline_matches_the_margin_distribution(self):
        projection = make_projection(make_game(), margin=6.0, total=45.0)
        result = coherence.check_projection(projection)
        check = next(c for c in result.checks if c["check"] == "moneyline_matches_margin")
        assert check["ok"], check["detail"]

    def test_the_three_moneyline_outcomes_sum_to_one(self):
        projection = make_projection(make_game())
        check = next(c for c in coherence.check_projection(projection).checks
                     if c["check"] == "moneyline_sums_to_one")
        assert check["ok"]

    def test_spread_probabilities_fall_as_the_line_rises(self):
        projection = make_projection(make_game(), margin=3.0, total=45.0)
        result = coherence.check_projection(
            projection, spread_lines=[-10.5, -6.5, -3.5, -1.5, 0.5, 3.5, 7.5])
        check = next(c for c in result.checks if c["check"] == "spread_monotone")
        assert check["ok"], check["detail"]

    def test_total_probabilities_fall_as_the_line_rises(self):
        projection = make_projection(make_game(), margin=3.0, total=45.0)
        result = coherence.check_projection(
            projection, total_lines=[38.5, 42.5, 45.5, 48.5, 52.5])
        check = next(c for c in result.checks if c["check"] == "total_monotone")
        assert check["ok"], check["detail"]

    def test_team_totals_reconstruct_the_game(self):
        projection = make_projection(make_game(), margin=7.0, total=48.0)
        check = next(c for c in coherence.check_projection(projection).checks
                     if c["check"] == "team_totals_reconstruct_the_game")
        assert check["ok"], check["detail"]

    def test_a_broken_distribution_is_detected(self):
        """Half the mass removed: the checker must notice rather than normalise it away."""
        projection = make_projection(make_game())
        projection.margin = D.DiscreteDistribution(
            low=projection.margin.low,
            mass=[p * 0.5 for p in projection.margin.mass])
        result = coherence.check_projection(projection)
        assert not result.coherent
        assert any(c["check"] == "margin_is_a_distribution" for c in result.failures())

    def test_an_inconsistent_moneyline_is_detected(self):
        projection = make_projection(make_game(), margin=3.0)
        projection.home_win = 0.95        # nothing like P(margin > 0)
        result = coherence.check_projection(projection)
        assert not result.coherent
        failures = {c["check"] for c in result.failures()}
        assert "moneyline_matches_margin" in failures

    def test_the_summary_says_not_to_trade_an_incoherent_game(self):
        projection = make_projection(make_game())
        projection.home_win = 0.99
        summary = coherence.check_projection(projection).to_dict()["summary"]
        assert "should be traded" in summary or "none of them should be traded" in summary


class TestPublishedSignalCoherence:
    """Blending happens per market type, so published numbers can drift apart even when
    the projection behind them agreed."""

    def test_ordered_spread_rungs_pass(self):
        signals = [
            make_signal("s1", market_type=MarketType.SPREAD, team="KC", line=-3.5,
                        model_prob=0.68, market_prob=0.68, cost=0.68),
            make_signal("s2", market_type=MarketType.SPREAD, team="KC", line=0.5,
                        model_prob=0.58, market_prob=0.58, cost=0.58),
            make_signal("s3", market_type=MarketType.SPREAD, team="KC", line=6.5,
                        model_prob=0.41, market_prob=0.41, cost=0.41),
        ]
        assert coherence.check_signals(signals).coherent

    def test_out_of_order_published_rungs_are_caught(self):
        signals = [
            make_signal("s1", market_type=MarketType.SPREAD, team="KC", line=-3.5,
                        model_prob=0.55, market_prob=0.55, cost=0.55),
            make_signal("s2", market_type=MarketType.SPREAD, team="KC", line=6.5,
                        model_prob=0.72, market_prob=0.72, cost=0.72),
        ]
        result = coherence.check_signals(signals)
        assert not result.coherent
        assert any("out of order" in c["detail"] for c in result.failures())

    def test_total_complements_must_sum_to_one(self):
        signals = [
            make_signal("o", market_type=MarketType.TOTAL, line=44.5,
                        model_prob=0.55, market_prob=0.55, cost=0.55),
            make_signal("u", market_type=MarketType.TOTAL, line=44.5,
                        model_prob=0.45, market_prob=0.45, cost=0.45),
        ]
        assert coherence.check_signals(signals).coherent

    def test_total_complements_that_do_not_sum_are_caught(self):
        signals = [
            make_signal("o", market_type=MarketType.TOTAL, line=44.5,
                        model_prob=0.62, market_prob=0.62, cost=0.62),
            make_signal("u", market_type=MarketType.TOTAL, line=44.5,
                        model_prob=0.55, market_prob=0.55, cost=0.55),
        ]
        result = coherence.check_signals(signals)
        assert not result.coherent

    def test_moneyline_complements_may_leave_room_for_a_tie(self):
        signals = [
            make_signal("h", market_type=MarketType.MONEYLINE, team="KC",
                        model_prob=0.58, market_prob=0.58, cost=0.58),
            make_signal("a", market_type=MarketType.MONEYLINE, team="BUF",
                        model_prob=0.415, market_prob=0.415, cost=0.415),
        ]
        assert coherence.check_signals(signals).coherent
