"""Missing data must read as unknown, never as good news.

The failure these tests pin: `injuries.get(team, [])` returns an empty list both for a team
that filed a clean report and for a team whose data never arrived. The model treated them
identically, so a team with a broken feed was projected as fully healthy, with full
confidence, and nothing on the card said otherwise.
"""
from __future__ import annotations

import time

import pytest

from app.data import availability
from app.data.nflverse import InjuryReport
from app.models.ratings import RatingSet
from app.strategies import injury_impact

from tests.conftest import make_game


def report(player="P. Mahomes", position="QB", status="Out", team="KC"):
    return InjuryReport(season=2026, week=5, team=team, player=player,
                        player_id=f"id_{player}", position=position,
                        report_status=status, practice_status=None, injury="knee")


def empty_ratings():
    return RatingSet(season=2026, week=5, teams={}, league_mean={}, home_field={},
                     sample_games=40, effective_games_per_team=10.0,
                     seasons_used=[2025, 2026])


class TestUnknownIsNotHealthy:
    def test_a_missing_team_is_not_treated_as_clean(self):
        game = make_game(home="KC", away="BUF")
        # BUF filed a clean report; KC's data never arrived.
        only_away = injury_impact.adjust(
            game, empty_ratings(), injuries={"BUF": []}, depth_chart={}, weather=None)
        both_clean = injury_impact.adjust(
            game, empty_ratings(), injuries={"KC": [], "BUF": []}, depth_chart={},
            weather=None)

        assert only_away.sigma_add > both_clean.sigma_add, (
            "a team with no injury data must be MORE uncertain than one with a clean "
            "report, not identical to it")
        assert "KC" in only_away.detail["unknown_teams"]
        assert both_clean.detail["unknown_teams"] == []

    def test_missing_data_says_so_in_the_reasoning(self):
        game = make_game(home="KC", away="BUF")
        result = injury_impact.adjust(game, empty_ratings(), injuries={"BUF": []},
                                      depth_chart={}, weather=None)
        assert any("UNKNOWN, not assumed healthy" in r for r in result.reasons)

    def test_a_clean_report_is_reported_as_a_clean_report(self):
        game = make_game(home="KC", away="BUF")
        result = injury_impact.adjust(
            game, empty_ratings(), injuries={"KC": [], "BUF": []}, depth_chart={},
            weather=None)
        assert any("clean injury report" in r for r in result.reasons)
        assert result.detail["data_quality"] == "complete"

    def test_an_unknown_team_contributes_no_margin_shift(self):
        """Guessing zero impact is wrong in the other direction, so the shift stays put
        and the uncertainty carries the doubt instead."""
        game = make_game(home="KC", away="BUF")
        result = injury_impact.adjust(game, empty_ratings(), injuries={"BUF": []},
                                      depth_chart={}, weather=None)
        assert result.margin_shift == pytest.approx(0.0)
        assert result.sigma_add > 0

    def test_a_real_injury_still_moves_the_margin(self):
        game = make_game(home="KC", away="BUF")
        result = injury_impact.adjust(
            game, empty_ratings(),
            injuries={"KC": [report(team="KC")], "BUF": []},
            depth_chart={}, weather=None)
        # Home quarterback out: the home margin falls, so the shift is negative.
        assert result.margin_shift < -3.0


class TestSourceStates:
    def test_a_failed_fetch_is_unknown_not_empty(self):
        state = availability.classify("injuries", present=False)
        assert state.status == availability.UNKNOWN
        assert state.usable is False

    def test_a_successful_empty_fetch_is_fresh(self):
        state = availability.classify("injuries", present=True,
                                      fetched_at=time.time(), rows=0)
        assert state.status == availability.FRESH
        assert state.usable is True

    def test_old_data_is_stale_but_usable(self):
        state = availability.classify("injuries", present=True,
                                      fetched_at=time.time() - 48 * 3600, rows=12)
        assert state.status == availability.STALE
        assert state.usable is True

    def test_age_is_reported_in_readable_units(self):
        state = availability.classify("weather", present=True,
                                      fetched_at=time.time() - 7200, rows=1)
        assert "h ago" in state.to_dict()["age_text"]


class TestDataHealth:
    def test_missing_sources_produce_reader_facing_notes(self):
        health = availability.DataHealth()
        health.add(availability.classify("injuries", present=False))
        health.add(availability.classify("weather", present=True,
                                         fetched_at=time.time(), rows=1))
        assert health.missing() == ["injuries"]
        notes = health.missing_notes()
        assert any("not assuming both teams are healthy" in n for n in notes)

    def test_unknown_sources_add_uncertainty_in_quadrature(self):
        health = availability.DataHealth()
        health.add(availability.classify("injuries", present=False))
        health.add(availability.classify("weather", present=False))
        expected = (availability.UNKNOWN_SIGMA_POINTS["injuries"] ** 2
                    + availability.UNKNOWN_SIGMA_POINTS["weather"] ** 2) ** 0.5
        assert availability.uncertainty_for(health) == pytest.approx(expected)
        assert availability.uncertainty_for(health) < (
            availability.UNKNOWN_SIGMA_POINTS["injuries"]
            + availability.UNKNOWN_SIGMA_POINTS["weather"]), (
            "two unknowns must not make a game twice as unknowable")

    def test_fully_fresh_health_adds_nothing(self):
        health = availability.DataHealth()
        for name in ("injuries", "weather", "depth_chart"):
            health.add(availability.classify(name, present=True,
                                             fetched_at=time.time(), rows=3))
        assert health.to_dict()["all_fresh"] is True
        assert availability.uncertainty_for(health) == pytest.approx(0.0)

    def test_stale_data_is_flagged_without_being_discarded(self):
        health = availability.DataHealth()
        health.add(availability.classify("injuries", present=True,
                                         fetched_at=time.time() - 86400, rows=5))
        assert health.stale() == ["injuries"]
        assert health.is_known("injuries") is True
        assert any("may have moved since" in n for n in health.missing_notes())
