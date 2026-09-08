"""Tests for outcome resolution, performance metrics, and risk sizing.

Grading is the one place where a bug quietly rewrites history: a contract settled the wrong
way makes the track record say the opposite of the truth. Every market type is pinned
against a concrete scoreline.
"""
from __future__ import annotations

import pytest

from app.core.types import MarketType
from app.tracking import metrics
from app.tracking.grading import _resolve


def resolve(market, selection, team, line, home_score, away_score, home="KC"):
    return _resolve(market, selection, team, line, home, home_score, away_score)


class TestMoneylineResolution:
    def test_home_win(self):
        assert resolve("moneyline", "KC ML", "KC", None, 24, 20) is True

    def test_home_loss(self):
        assert resolve("moneyline", "KC ML", "KC", None, 20, 24) is False

    def test_away_side_is_the_mirror(self):
        assert resolve("moneyline", "BUF ML", "BUF", None, 20, 24) is True

    def test_a_tie_voids_rather_than_losing(self):
        """Kalshi refunds an NFL moneyline on a tie. Grading it as a loss would be theft."""
        assert resolve("moneyline", "KC ML", "KC", None, 20, 20) is None


class TestSpreadResolution:
    def test_home_covers_when_margin_exceeds_the_line(self):
        assert resolve("spread", "KC -6.5", "KC", 6.5, 28, 20) is True

    def test_home_fails_by_half_a_point(self):
        assert resolve("spread", "KC -6.5", "KC", 6.5, 27, 21) is False

    def test_away_spread_reads_the_negative_margin(self):
        assert resolve("spread", "BUF -3.5", "BUF", 3.5, 20, 27) is True

    def test_exact_whole_number_does_not_win_an_over_contract(self):
        """Kalshi writes 'wins by over N', so landing exactly on N resolves NO."""
        assert resolve("spread", "KC -7", "KC", 7.0, 27, 20) is False


class TestTotalResolution:
    def test_over_hits(self):
        assert resolve("total", "Over 44.5", None, 44.5, 24, 21) is True

    def test_over_misses(self):
        assert resolve("total", "Over 44.5", None, 44.5, 21, 21) is False

    def test_team_total_reads_only_that_team(self):
        assert resolve("team_total", "KC Over 23.5", "KC", 23.5, 24, 45) is True
        assert resolve("team_total", "KC Over 23.5", "KC", 23.5, 23, 45) is False

    def test_away_team_total(self):
        assert resolve("team_total", "BUF Over 23.5", "BUF", 23.5, 10, 24) is True


class TestWinMarginResolution:
    @pytest.mark.parametrize("selection,home,away,expected", [
        ("Chiefs 1-6", 24, 20, True),
        ("Chiefs 1-6", 28, 20, False),
        ("Chiefs 7-14", 28, 20, True),
        ("Chiefs 15+", 38, 20, True),
        ("Chiefs 15+", 30, 20, False),
        ("Tie", 20, 20, True),
        ("Tie", 21, 20, False),
    ])
    def test_bands(self, selection, home, away, expected):
        assert resolve("win_margin", selection, "KC", 0.0, home, away) is expected

    def test_bands_flip_for_the_away_team(self):
        assert resolve("win_margin", "Bills 7-14", "BUF", 0.0, 20, 30) is True


class TestUnsettleableMarkets:
    def test_player_props_cannot_be_graded_from_a_final_score(self):
        assert resolve("pass_yards", "Mahomes 250+", None, 250, 24, 20) is None

    def test_half_markets_cannot_be_graded_from_a_final_score(self):
        assert resolve("first_half_total", "1H Over 21.5", None, 21.5, 24, 20) is None


class TestMetrics:
    def rows(self, pairs, cost=0.5):
        return [{"model_prob": p, "fair_prob": p, "market_prob": p, "outcome": o,
                 "cost": cost, "strategy": "s", "market_type": "m", "confidence": "high",
                 "created_at": i, "settled_at": i, "ev_per_dollar": 0.0}
                for i, (p, o) in enumerate(pairs)]

    def test_brier_of_a_perfect_forecaster_is_zero(self):
        assert metrics.brier([1.0, 0.0], [1, 0]) == pytest.approx(0.0)

    def test_brier_of_a_coin_flip_is_a_quarter(self):
        assert metrics.brier([0.5, 0.5], [1, 0]) == pytest.approx(0.25)

    def test_brier_punishes_confident_error(self):
        assert metrics.brier([0.9], [0]) > metrics.brier([0.6], [0])

    def test_log_loss_punishes_confident_error_far_harder(self):
        confident = metrics.log_loss([0.99], [0])
        mild = metrics.log_loss([0.6], [0])
        assert confident > mild * 4

    def test_log_loss_never_returns_infinity(self):
        assert metrics.log_loss([1.0], [0]) < 100

    def test_calibration_reports_sample_size_per_bucket(self):
        rows = metrics.calibration([0.65, 0.65, 0.15], [1, 0, 0])
        bucket = next(r for r in rows if r["bucket"] == "60-70%")
        assert bucket["n"] == 2 and bucket["predicted"] == pytest.approx(0.65)
        assert bucket["observed"] == pytest.approx(0.5)

    def test_calibration_error_is_sample_weighted(self):
        rows = metrics.calibration([0.5] * 10 + [0.9], [1, 0] * 5 + [0])
        assert 0 <= metrics.calibration_error(rows) <= 1

    def test_roi_at_even_money(self):
        result = metrics.roi(self.rows([(0.5, 1), (0.5, 0)], cost=0.5))
        assert result["staked"] == 2.0 and result["returned"] == pytest.approx(2.0)
        assert result["roi"] == pytest.approx(0.0)

    def test_roi_is_positive_when_a_longshot_lands(self):
        result = metrics.roi(self.rows([(0.2, 1), (0.2, 0), (0.2, 0), (0.2, 0)], cost=0.2))
        assert result["roi"] == pytest.approx(0.25)

    def test_summary_flags_a_small_sample_as_not_meaningful(self):
        summary = metrics.summarise(self.rows([(0.6, 1)] * 5))
        assert summary["n"] == 5 and summary["meaningful"] is False
        assert "too few" in summary["note"]

    def test_summary_on_no_data_explains_itself(self):
        summary = metrics.summarise([])
        assert summary["n"] == 0 and "No settled predictions" in summary["note"]

    def test_summary_compares_against_the_market_column(self):
        summary = metrics.summarise(self.rows([(0.6, 1), (0.6, 0)] * 20))
        assert summary["brier"] is not None and summary["brier_market"] is not None

    def test_equity_curve_is_cumulative_and_ordered(self):
        curve = metrics.equity_curve(self.rows([(0.5, 1), (0.5, 0), (0.5, 1)], cost=0.5))
        assert [round(p["cumulative"], 2) for p in curve] == [1.0, 0.0, 1.0]

    def test_drawdown_measures_peak_to_trough(self):
        dd = metrics.drawdown([0, 5, 2, 8, 1])
        assert dd["max_drawdown"] == pytest.approx(-7.0)

    def test_grouping_splits_and_orders_by_sample(self):
        rows = self.rows([(0.6, 1)] * 3) + [
            {**self.rows([(0.6, 0)])[0], "strategy": "other"}]
        groups = metrics.by_group(rows, "strategy")
        assert groups[0]["n"] == 3


class TestRiskSizing:
    def make(self, prob, cost, bankroll=1000.0, mode="kelly"):
        from app.core.types import Confidence, MarketQuote, Side, Signal
        from app.risk.bankroll import Exposure, RiskSettings, size_bet
        quote = MarketQuote(venue="kalshi", ticker="T", event_ticker="E",
                            market_type=MarketType.MONEYLINE, label="L",
                            yes_bid=cost - 0.01, yes_ask=cost, depth_usd=5000)
        signal = Signal(strategy="ensemble", game_id="2026_01_NE_SEA",
                        market_type=MarketType.MONEYLINE, label="L", selection="S",
                        model_prob=prob, confidence=Confidence.HIGH)
        signal.quote = quote
        signal.features.update({"fair_prob": prob, "cost": cost, "liquid": True})
        risk = RiskSettings(mode=mode, bankroll=bankroll)
        return size_bet(signal, risk, Exposure())

    def test_a_negative_edge_is_sized_at_zero(self):
        result = self.make(0.40, 0.50)
        assert result["stake"] == 0.0
        assert any("Kelly says not to bet" in w for w in result["warnings"])

    def test_a_real_edge_produces_a_stake(self):
        result = self.make(0.60, 0.50)
        assert result["stake"] > 0

    def test_fractional_kelly_stakes_less_than_full_kelly(self):
        result = self.make(0.70, 0.50)
        full = result["full_kelly_pct"] / 100 * 1000
        assert result["stake"] < full

    def test_the_per_bet_cap_binds_before_kelly_does(self):
        """Kelly on a huge edge would recommend an irresponsible fraction."""
        result = self.make(0.95, 0.30)
        assert result["stake"] <= 1000 * 0.05 + 1e-9
        assert result["capped_by"] is not None

    def test_exposure_already_taken_reduces_the_next_stake(self):
        from app.core.types import Confidence, MarketQuote, Signal
        from app.risk.bankroll import Exposure, RiskSettings, size_bet
        quote = MarketQuote(venue="kalshi", ticker="T", event_ticker="E",
                            market_type=MarketType.MONEYLINE, label="L",
                            yes_bid=0.49, yes_ask=0.50, depth_usd=5000)
        signal = Signal(strategy="ensemble", game_id="2026_01_NE_SEA",
                        market_type=MarketType.MONEYLINE, label="L", selection="S",
                        model_prob=0.65, confidence=Confidence.HIGH)
        signal.quote = quote
        signal.features.update({"fair_prob": 0.65, "cost": 0.50, "liquid": True})
        risk = RiskSettings(bankroll=1000.0)
        fresh = size_bet(signal, risk, Exposure())
        loaded = size_bet(signal, risk, Exposure(total=95, today=95,
                                                 by_game={"2026_01_NE_SEA": 95}))
        assert loaded["stake"] < fresh["stake"]
        assert any("already have" in w for w in loaded["warnings"])

    def test_flat_mode_ignores_the_edge_size(self):
        small = self.make(0.55, 0.50, mode="flat")
        large = self.make(0.80, 0.50, mode="flat")
        assert small["stake"] == large["stake"]

    def test_an_unpriced_signal_is_not_sized(self):
        from app.core.types import Confidence, Signal
        from app.risk.bankroll import Exposure, RiskSettings, size_bet
        signal = Signal(strategy="e", game_id="G", market_type=MarketType.MONEYLINE,
                        label="L", selection="S", model_prob=0.6,
                        confidence=Confidence.HIGH)
        result = size_bet(signal, RiskSettings(bankroll=1000.0), Exposure())
        assert result["stake"] == 0.0 and result["capped_by"] == "unpriced"
