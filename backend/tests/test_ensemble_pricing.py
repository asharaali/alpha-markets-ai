"""Tests for how the ensemble prices a combination.

The bug these exist to prevent: every strategy blends its raw model output toward the
market before publishing. The ensemble then combined those already-blended numbers and
blended the result toward the market a SECOND time. Three contributors each sitting three
points off the market produced an ensemble sitting 0.3 points off it — which silently
suppressed every edge the application was capable of finding, and looked exactly like a
model that simply never disagreed with the price.
"""
from __future__ import annotations

import pytest

from app.core.types import Confidence, MarketQuote, MarketType, Side, Signal
from app.strategies import ensemble, pricing
from tests.conftest import make_quote


def contributor(strategy, published, market=0.50, cost=0.51, ticker="T"):
    """A strategy signal whose probability has ALREADY been blended toward the market."""
    quote = make_quote(ticker, bid=cost - 0.01, ask=cost)
    s = Signal(strategy=strategy, game_id="2026_01_AAA_BBB",
               market_type=MarketType.SPREAD, label="L", selection="L",
               model_prob=published, confidence=Confidence.MEDIUM, team="KC", line=3.5)
    s.quote = quote
    s.market_prob = market
    s.features.update({"fair_prob": published, "cost": cost, "liquid": True,
                       "depth_usd": 5000, "model_weight": 0.18})
    return s


class TestAttachQuoteDoesNotBlend:
    def test_a_final_probability_survives_intact(self):
        quote = make_quote("T", bid=0.49, ask=0.50)
        s = Signal(strategy="ensemble", game_id="G", market_type=MarketType.SPREAD,
                   label="L", selection="L", model_prob=0.62, confidence=Confidence.HIGH)
        pricing.attach_quote(s, quote, 0.62)
        assert s.features["fair_prob"] == pytest.approx(0.62)
        assert s.features["blended_here"] is False

    def test_price_signal_by_contrast_does_blend(self):
        quote = make_quote("T", bid=0.49, ask=0.50)
        s = Signal(strategy="spread", game_id="G", market_type=MarketType.SPREAD,
                   label="L", selection="L", model_prob=0.62, confidence=Confidence.HIGH)
        pricing.price_signal(s, quote, sample_confidence=1.0)
        assert s.features["fair_prob"] < 0.62, "price_signal must shrink toward market"
        assert s.features["blended_here"] is True

    def test_edge_and_ev_follow_the_given_probability(self):
        quote = make_quote("T", bid=0.49, ask=0.50)
        s = Signal(strategy="ensemble", game_id="G", market_type=MarketType.SPREAD,
                   label="L", selection="L", model_prob=0.60, confidence=Confidence.HIGH)
        pricing.attach_quote(s, quote, 0.60)
        assert s.edge == pytest.approx(0.60 - s.market_prob)
        assert s.ev_per_dollar == pytest.approx((0.60 / 0.50) - 1)


class TestEnsembleDoesNotDoubleShrink:
    def test_agreeing_contributors_survive_combination(self):
        """Three strategies all 4 points above market must not average to the market."""
        members = [contributor("spread", 0.54), contributor("matchup", 0.54),
                   contributor("book_consensus", 0.54)]
        combined = ensemble.combine(members, multipliers={}, use_movement=False)
        assert len(combined) == 1
        fair = combined[0].features["fair_prob"]
        assert fair == pytest.approx(0.54, abs=0.005), (
            f"three contributors at 54% produced {fair:.1%} — the combination was "
            "re-shrunk toward the 50% market price")

    def test_combination_lands_between_its_contributors(self):
        members = [contributor("spread", 0.52), contributor("book_consensus", 0.60)]
        combined = ensemble.combine(members, multipliers={}, use_movement=False)
        fair = combined[0].features["fair_prob"]
        assert 0.52 - 1e-6 <= fair <= 0.60 + 1e-6

    def test_the_heaviest_contributor_pulls_hardest(self):
        """book_consensus carries the largest prior, so it should dominate a disagreement."""
        members = [contributor("spread", 0.52), contributor("book_consensus", 0.60)]
        combined = ensemble.combine(members, multipliers={}, use_movement=False)
        fair = combined[0].features["fair_prob"]
        midpoint = 0.56
        assert fair > midpoint, f"{fair:.3f} should sit above the midpoint {midpoint}"

    def test_an_edge_survives_end_to_end(self):
        """The whole point: a real disagreement must still be a positive-EV signal."""
        members = [contributor("spread", 0.56, market=0.50, cost=0.51),
                   contributor("book_consensus", 0.58, market=0.50, cost=0.51)]
        combined = ensemble.combine(members, multipliers={}, use_movement=False)
        signal = combined[0]
        assert signal.ev_per_dollar > 0.05, (
            f"contributors 6-8 points above a 51c price yielded {signal.ev_per_dollar:+.1%} EV")

    def test_disagreement_is_reported_not_hidden(self):
        members = [contributor("spread", 0.45), contributor("book_consensus", 0.65)]
        combined = ensemble.combine(members, multipliers={}, use_movement=False)
        assert combined[0].features["disagreement"] == pytest.approx(0.20, abs=0.001)
        assert any("disagree" in r for r in combined[0].reasoning)

    def test_wide_disagreement_lowers_confidence(self):
        tight = ensemble.combine([contributor("spread", 0.55),
                                  contributor("book_consensus", 0.56)],
                                 multipliers={}, use_movement=False)[0]
        wide = ensemble.combine([contributor("spread", 0.40),
                                 contributor("book_consensus", 0.70)],
                                multipliers={}, use_movement=False)[0]
        order = {"reference": 0, "low": 1, "medium": 2, "high": 3}
        assert order[wide.confidence.value] < order[tight.confidence.value]


class TestLogOddsAveraging:
    def test_longshots_are_averaged_in_log_odds_not_arithmetically(self):
        """Averaging 5% and 45% arithmetically gives 25%, which badly overstates a longshot."""
        members = [contributor("spread", 0.05), contributor("book_consensus", 0.45)]
        combined = ensemble.combine(members, multipliers={}, use_movement=False)
        fair = combined[0].features["fair_prob"]
        assert fair < 0.25, f"{fair:.1%} looks like an arithmetic mean, not a log-odds one"
        assert fair > 0.05
