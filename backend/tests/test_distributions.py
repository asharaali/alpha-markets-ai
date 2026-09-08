"""Tests for the score distributions — where key numbers and push handling live."""
from __future__ import annotations

import math

import pytest

from app.models import distributions as D


class TestNormalPrimitives:
    def test_cdf_is_a_half_at_the_mean(self):
        assert D.normal_cdf(0.0, 0.0, 1.0) == pytest.approx(0.5)

    def test_cdf_matches_known_quantiles(self):
        assert D.normal_cdf(1.0, 0.0, 1.0) == pytest.approx(0.8413, abs=1e-4)
        assert D.normal_cdf(-1.96, 0.0, 1.0) == pytest.approx(0.0250, abs=1e-4)

    def test_pmf_integrates_to_one_over_the_support(self):
        total = sum(D.normal_pmf(k, 0.0, 3.0) for k in range(-30, 31))
        assert total == pytest.approx(1.0, abs=1e-6)

    def test_degenerate_sigma_does_not_divide_by_zero(self):
        assert D.normal_cdf(1.0, 0.0, 0.0) == 1.0
        assert D.normal_pdf(1.0, 0.0, 0.0) == 0.0


class TestDiscreteDistribution:
    def setup_method(self):
        self.dist = D.build_distribution(3.0, 13.0)

    def test_mass_sums_to_one(self):
        assert sum(self.dist.mass) == pytest.approx(1.0)

    def test_mean_recovers_the_input(self):
        assert self.dist.mean() == pytest.approx(3.0, abs=0.05)

    def test_stdev_recovers_the_input(self):
        assert self.dist.stdev() == pytest.approx(13.0, abs=0.15)

    def test_over_and_under_and_push_partition_the_space(self):
        for line in (3.0, 3.5, 0.0, -7.0):
            total = (self.dist.prob_over(line) + self.dist.prob_under(line)
                     + self.dist.prob_push(line))
            assert total == pytest.approx(1.0, abs=1e-9), f"line {line} does not partition"

    def test_half_point_lines_never_push(self):
        assert self.dist.prob_push(3.5) == 0.0

    def test_whole_number_lines_do_push(self):
        assert self.dist.prob_push(3.0) > 0.0

    def test_cdf_is_monotonic(self):
        values = [self.dist.cdf(k) for k in range(-40, 41)]
        assert all(b >= a for a, b in zip(values, values[1:]))

    def test_probability_outside_support_is_bounded(self):
        assert self.dist.prob_over(500) == 0.0
        assert self.dist.prob_under(-500) == 0.0


class TestKeyNumbers:
    """The reason a -2.5 and a -3.5 must not be priced the same."""

    PROFILE = {3: 2.5, -3: 2.5, 7: 1.7, -7: 1.7, 0: 0.2}

    def test_profile_raises_the_mass_on_key_margins(self):
        plain = D.build_distribution(0.0, 13.0)
        shaped = D.build_distribution(0.0, 13.0, profile=self.PROFILE)
        assert shaped.pmf(3) > plain.pmf(3) * 2.0

    def test_profile_still_normalises(self):
        shaped = D.build_distribution(0.0, 13.0, profile=self.PROFILE)
        assert sum(shaped.mass) == pytest.approx(1.0)

    def test_crossing_a_key_number_costs_more_than_a_neutral_number(self):
        """Moving a line through 3 must move the probability more than moving through 5."""
        shaped = D.build_distribution(0.0, 13.0, profile=self.PROFILE)
        through_three = shaped.prob_over(2.5) - shaped.prob_over(3.5)
        through_five = shaped.prob_over(4.5) - shaped.prob_over(5.5)
        assert through_three > through_five * 1.5

    def test_tie_suppression_lowers_the_tie_probability(self):
        shaped = D.build_distribution(0.0, 13.0, profile=self.PROFILE)
        plain = D.build_distribution(0.0, 13.0)
        assert shaped.pmf(0) < plain.pmf(0)


class TestWinAndCover:
    def test_win_tie_and_loss_sum_to_one(self):
        dist = D.margin_distribution(3.5, 13.0)
        home, tie, away = D.win_probability(dist)
        assert home + tie + away == pytest.approx(1.0)

    def test_favourite_wins_more_often(self):
        home, _, away = D.win_probability(D.margin_distribution(7.0, 13.0))
        assert home > away

    def test_pick_em_is_symmetric(self):
        home, _, away = D.win_probability(D.margin_distribution(0.0, 13.0))
        assert home == pytest.approx(away, abs=1e-9)

    def test_home_and_away_cover_are_complementary_on_a_half_point(self):
        dist = D.margin_distribution(2.0, 13.0)
        home_cover, home_push = D.cover_probability(dist, -3.5, team_is_home=True)
        away_cover, away_push = D.cover_probability(dist, 3.5, team_is_home=False)
        assert home_push == 0.0 and away_push == 0.0
        assert home_cover + away_cover == pytest.approx(1.0)

    def test_laying_more_points_is_always_harder(self):
        dist = D.margin_distribution(6.0, 13.0)
        easier, _ = D.cover_probability(dist, -3.5, team_is_home=True)
        harder, _ = D.cover_probability(dist, -10.5, team_is_home=True)
        assert harder < easier


class TestTotals:
    def test_total_distribution_never_goes_negative(self):
        dist = D.total_distribution(44.0, 13.0)
        assert dist.low >= 0

    def test_over_and_under_are_complementary_on_a_half_point(self):
        dist = D.total_distribution(44.0, 13.0)
        assert dist.prob_over(44.5) + dist.prob_under(44.5) == pytest.approx(1.0)

    def test_higher_lines_are_less_likely(self):
        dist = D.total_distribution(44.0, 13.0)
        assert dist.prob_over(51.5) < dist.prob_over(44.5) < dist.prob_over(37.5)


class TestPoisson:
    def test_pmf_sums_to_one(self):
        assert sum(D.poisson_pmf(k, 2.5) for k in range(0, 60)) == pytest.approx(1.0)

    def test_at_least_one_matches_the_complement_of_zero(self):
        assert D.poisson_at_least_one(0.7) == pytest.approx(1 - D.poisson_pmf(0, 0.7))

    def test_zero_rate_means_it_never_happens(self):
        assert D.poisson_at_least_one(0.0) == 0.0


class TestProfileFitting:
    def test_a_clearly_rare_outcome_stays_rare(self):
        """Exact ties: expected ~110 by a smooth model, observed 11. Must not shrink to 1."""
        profile = D.fit_profile({0: 11.0}, {0: 110.0})
        assert profile[0] < 0.35

    def test_a_thinly_observed_outcome_is_pulled_toward_one(self):
        profile = D.fit_profile({77: 0.0}, {77: 1.5})
        assert profile[77] > 0.6

    def test_a_well_supported_spike_survives(self):
        profile = D.fit_profile({3: 300.0}, {3: 120.0})
        assert profile[3] > 1.8

    def test_multipliers_are_clipped_to_a_sane_range(self):
        profile = D.fit_profile({5: 100000.0}, {5: 1.0})
        assert profile[5] <= 3.0


class TestCoverSignConvention:
    """Pinning the spread convention. A sign error here silently inverts every spread bet."""

    def test_favoured_home_team_covers_less_often_than_it_wins(self):
        dist = D.margin_distribution(5.0, 13.0)
        win, _, _ = D.win_probability(dist)
        cover, _ = D.cover_probability(dist, -3.5, team_is_home=True)
        assert cover < win

    def test_underdog_away_team_covers_more_often_than_it_wins(self):
        dist = D.margin_distribution(5.0, 13.0)
        _, _, away_win = D.win_probability(dist)
        cover, _ = D.cover_probability(dist, 3.5, team_is_home=False)
        assert cover > away_win

    def test_favoured_away_team_uses_a_negative_line(self):
        # Away favoured by 5 => home margin centred on -5. Away laying 3.5 should be > 50%.
        dist = D.margin_distribution(-5.0, 13.0)
        cover, _ = D.cover_probability(dist, -3.5, team_is_home=False)
        assert cover > 0.5

    def test_whole_number_line_pushes_are_shared_by_both_sides(self):
        dist = D.margin_distribution(1.0, 13.0)
        home_cover, home_push = D.cover_probability(dist, -3.0, team_is_home=True)
        away_cover, away_push = D.cover_probability(dist, 3.0, team_is_home=False)
        assert home_push == pytest.approx(away_push)
        assert home_cover + away_cover + home_push == pytest.approx(1.0)
