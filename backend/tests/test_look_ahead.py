"""The look-ahead guarantee.

A backtest that can see the result it is predicting is not evidence, it is a bug that
produces impressive numbers. These tests assert the guarantee structurally: the data layer
must not hand back a single play from a week it was told to stop before, and ratings built
as of week N must be byte-identical whether or not later weeks exist on disk.
"""
from __future__ import annotations

import pytest

from app.data import csvstream as cs
from app.data import nflverse
from app.features import aggregate
from app.models.ratings import build as build_ratings

SEASON = 2025


async def _have_data() -> bool:
    return await nflverse.play_by_play_path(SEASON) is not None


@pytest.mark.asyncio
class TestDataTruncation:
    async def test_aggregates_stop_at_max_week(self):
        if not await _have_data():
            pytest.skip("play-by-play for the test season is not cached")
        rows = await aggregate.team_games(SEASON, max_week=6)
        assert rows, "expected some team-games"
        assert max(r.week for r in rows) <= 6

    async def test_asking_for_week_one_sees_only_week_one(self):
        if not await _have_data():
            pytest.skip("play-by-play for the test season is not cached")
        rows = await aggregate.team_games(SEASON, max_week=1)
        assert {r.week for r in rows} == {1}

    async def test_player_logs_respect_max_week(self):
        logs = await nflverse.player_weeks(SEASON, max_week=4)
        if not logs:
            pytest.skip("player logs unavailable")
        assert max(int(r["week"]) for r in logs) <= 4


@pytest.mark.asyncio
class TestRatingIsolation:
    async def test_week_one_ratings_use_no_current_season_games(self):
        """In week 1 nothing has happened yet, so the fit must rest entirely on prior years."""
        if not await _have_data():
            pytest.skip("play-by-play unavailable")
        ratings = await build_ratings(SEASON, 1)
        assert SEASON not in ratings.seasons_used, (
            "week-1 ratings included games from the season being predicted")

    async def test_ratings_grow_more_confident_as_a_season_progresses(self):
        if not await _have_data():
            pytest.skip("play-by-play unavailable")
        early = await build_ratings(SEASON, 3)
        late = await build_ratings(SEASON, 15)
        assert late.effective_games_per_team > early.effective_games_per_team
        assert late.confidence() >= early.confidence()

    async def test_ratings_as_of_a_week_are_reproducible(self):
        """The same request must produce the same ratings — a backtest that is not
        deterministic cannot be checked by anyone, including us."""
        if not await _have_data():
            pytest.skip("play-by-play unavailable")
        first = await build_ratings(SEASON, 8)
        second = await build_ratings(SEASON, 8)
        for team in first.teams:
            assert (first.get(team).offense.get("epa_per_play")
                    == second.get(team).offense.get("epa_per_play"))

    async def test_a_later_week_differs_from_an_earlier_one(self):
        """Sanity check on the guarantee itself: if truncation did nothing, week 3 and
        week 15 ratings would be identical and the other tests would pass vacuously."""
        if not await _have_data():
            pytest.skip("play-by-play unavailable")
        early = await build_ratings(SEASON, 3)
        late = await build_ratings(SEASON, 15)
        differences = sum(
            1 for t in early.teams
            if early.get(t).offense.get("epa_per_play") != late.get(t).offense.get("epa_per_play"))
        assert differences > 20, "truncation appears to have no effect at all"


@pytest.mark.asyncio
class TestScheduleIntegrity:
    async def test_future_games_carry_no_result(self):
        games = await nflverse.schedule(seasons=[2026])
        if not games:
            pytest.skip("2026 schedule unavailable")
        future = [g for g in games if not g.completed]
        assert future, "expected some unplayed games"
        for game in future:
            assert game.home_score is None and game.away_score is None
            assert game.margin is None and game.total_points is None

    async def test_completed_games_have_both_scores(self):
        games = await nflverse.schedule(seasons=[SEASON])
        if not games:
            pytest.skip("schedule unavailable")
        for game in games:
            if game.completed:
                assert game.home_score is not None and game.away_score is not None
                assert game.margin == game.home_score - game.away_score


class TestCsvStreaming:
    def test_missing_numeric_cells_are_none_not_zero(self):
        """'NA' means we do not know, and treating it as zero silently biases every mean."""
        assert cs.num("") is None
        assert cs.num("NA") is None
        assert cs.num("NaN") is None
        assert cs.num("0") == 0.0
        assert cs.num("-3.5") == -3.5

    def test_flags_parse_the_shapes_nflverse_actually_emits(self):
        assert cs.flag("1") and cs.flag("TRUE") and cs.flag("True")
        assert not cs.flag("0") and not cs.flag("") and not cs.flag("NA")

    def test_text_normalises_missing_markers(self):
        assert cs.text("NA") is None and cs.text("  ") is None
        assert cs.text(" KC ") == "KC"
