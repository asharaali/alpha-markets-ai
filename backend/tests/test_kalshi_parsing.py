"""Tests for Kalshi contract parsing and game mapping.

A contract that parses wrong is worse than one that fails to parse: reading
"Seattle wins by over 13.5" as a moneyline would attach a completely wrong probability to
a real tradeable market. Every parser must return None rather than guess.
"""
from __future__ import annotations

import pytest

from app.core.types import Game, MarketType
from app.data import teams
from app.data.kalshi import markets as km
from app.data.kalshi import series as ks


class TestTeamResolution:
    @pytest.mark.parametrize("name,expected", [
        ("KC", "KC"), ("Chiefs", "KC"), ("Kansas City Chiefs", "KC"),
        ("LAR", "LA"), ("Los Angeles R", "LA"), ("Los Angeles Rams", "LA"),
        ("Los Angeles C", "LAC"), ("LAC", "LAC"),
        ("JAC", "JAX"), ("Jacksonville", "JAX"),
        ("WSH", "WAS"), ("Washington Commanders", "WAS"),
        ("New York G", "NYG"), ("New York J", "NYJ"),
        ("OAK", "LV"), ("SD", "LAC"), ("STL", "LA"),
    ])
    def test_known_spellings_resolve(self, name, expected):
        assert teams.resolve(name) == expected

    def test_unknown_names_return_none_rather_than_guessing(self):
        assert teams.resolve("Toronto Maple Leafs") is None
        assert teams.resolve("") is None
        assert teams.resolve(None) is None

    def test_la_and_lac_are_never_confused(self):
        """Both play in the same stadium; conflating them would swap two whole teams."""
        assert teams.resolve("Los Angeles R") != teams.resolve("Los Angeles C")

    def test_registry_has_all_32_teams(self):
        assert len(teams.TEAMS) == 32
        assert len({t.division for t in teams.TEAMS.values()}) == 8

    def test_divisional_detection(self):
        assert teams.is_divisional("KC", "LV")
        assert not teams.is_divisional("KC", "SEA")

    def test_travel_is_symmetric_and_zero_at_home(self):
        assert teams.travel_miles("KC", "KC") == 0.0
        assert teams.travel_miles("KC", "SEA") == teams.travel_miles("SEA", "KC")

    def test_timezone_shift_is_signed_eastward_positive(self):
        assert teams.timezone_shift("SEA", "KC") > 0    # travelling east
        assert teams.timezone_shift("KC", "SEA") < 0    # travelling west
        assert teams.timezone_shift("NYJ", "NE") == 0


class TestContractParsing:
    AWAY, HOME = "NE", "SEA"

    def parse(self, series, subtitle):
        spec = ks.spec(series)
        assert spec is not None, f"{series} is not registered"
        return spec.parse(subtitle, self.AWAY, self.HOME)

    def test_moneyline(self):
        c = self.parse("KXNFLGAME", "Seattle")
        assert c.market_type is MarketType.MONEYLINE and c.team == "SEA" and c.line is None

    def test_spread_reads_team_and_line(self):
        c = self.parse("KXNFLSPREAD", "Seattle wins by over 13.5 points")
        assert c.market_type is MarketType.SPREAD
        assert c.team == "SEA" and c.line == 13.5

    def test_spread_is_not_mistaken_for_a_moneyline(self):
        assert self.parse("KXNFLGAME", "Seattle wins by over 13.5 points") is None

    def test_total(self):
        c = self.parse("KXNFLTOTAL", "Over 44.5 points scored")
        assert c.market_type is MarketType.TOTAL and c.line == 44.5 and c.team is None

    def test_team_total_keeps_the_team(self):
        c = self.parse("KXNFLTEAMTOTAL", "SEA Seahawks over 23.5 points scored")
        assert c.market_type is MarketType.TEAM_TOTAL
        assert c.team == "SEA" and c.line == 23.5

    def test_team_total_is_distinguished_from_game_total(self):
        assert self.parse("KXNFLTOTAL", "SEA Seahawks over 23.5 points scored") is None

    def test_win_margin_bands(self):
        tie = self.parse("KXNFLWINMARGIN", "Tie")
        assert tie.market_type is MarketType.WIN_MARGIN and tie.team is None
        band = self.parse("KXNFLWINMARGIN", "Seattle wins by 7 to 14 points")
        assert band.team == "SEA" and "7-14" in band.selection
        plus = self.parse("KXNFLWINMARGIN", "Seattle wins by 15 or more points")
        assert "15+" in plus.selection

    def test_player_props_capture_player_and_line(self):
        c = self.parse("KXNFLPASSYDS", "Sam Darnold: 225+")
        assert c.market_type is MarketType.PASS_YARDS
        assert c.player == "Sam Darnold" and c.line == 225.0

    def test_receptions_prop(self):
        c = self.parse("KXNFLREC", "Cooper Kupp: 5+")
        assert c.market_type is MarketType.RECEPTIONS and c.line == 5.0

    def test_a_team_not_in_this_game_is_rejected(self):
        """A market mentioning a third team means we matched the wrong event."""
        assert self.parse("KXNFLGAME", "Kansas City") is None

    def test_unparseable_subtitles_return_none(self):
        for junk in ["", "???", "Something entirely different", "Over points scored"]:
            assert self.parse("KXNFLTOTAL", junk) is None

    def test_every_registered_series_has_a_parser_and_type(self):
        for spec in ks.all_specs():
            assert callable(spec.parse)
            assert isinstance(spec.market_type, MarketType)
            assert spec.category in {"Game Line", "Game Prop", "Player Prop", "Segment"}

    def test_all_player_prop_series_are_reference_only_by_default(self):
        for ticker in ks.PLAYER_PROP_SERIES:
            assert ks.spec(ticker).reference_only


class TestEventMapping:
    def game(self, gid, away, home, kickoff):
        return Game(game_id=gid, season=2026, week=1, game_type="REG",
                    kickoff=kickoff, home=home, away=away)

    def test_subtitle_is_read_as_away_at_home(self):
        assert km._parse_matchup({"sub_title": "NE vs SEA (Sep 9)"}) == ("NE", "SEA")

    def test_full_names_in_the_title_also_work(self):
        assert km._parse_matchup(
            {"sub_title": "", "title": "New England vs Seattle: Total Points"}) == ("NE", "SEA")

    def test_ambiguous_la_teams_map_correctly(self):
        assert km._parse_matchup({"sub_title": "SF vs LAR (Sep 10)"}) == ("SF", "LA")
        assert km._parse_matchup({"sub_title": "SF vs LAC (Sep 10)"}) == ("SF", "LAC")

    def test_unreadable_matchups_return_none(self):
        assert km._parse_matchup({"sub_title": "Championship Winner"}) is None
        assert km._parse_matchup({"sub_title": "", "title": ""}) is None

    def test_repeat_matchups_are_split_by_close_time(self):
        """Two teams meet twice a season; the market must land on the right one."""
        early = self.game("2026_02_NE_SEA", "NE", "SEA", "2026-09-14T17:00:00+00:00")
        late = self.game("2026_14_NE_SEA", "NE", "SEA", "2026-12-07T17:00:00+00:00")
        index = km.build_game_index([early, late])
        picked = km._match_game("NE", "SEA", "2026-12-07T21:00:00Z", index)
        assert picked.game_id == "2026_14_NE_SEA"

    def test_a_matchup_not_on_the_schedule_maps_to_nothing(self):
        index = km.build_game_index([self.game("2026_01_NE_SEA", "NE", "SEA", "")])
        assert km._match_game("KC", "BUF", None, index) is None


class TestLadderSelection:
    """Pricing every rung of every ladder is 800 HTTP calls; pricing near the projection
    is 300 and covers everything anyone would bet."""

    def market(self, mtype, line, team=None, game_id="G1"):
        return km.DiscoveredMarket(
            series="S", event_ticker="E", ticker=f"{mtype}-{line}-{team}",
            game_id=game_id, away="NE", home="SEA", market_type=mtype,
            category="Game Line", label="L", selection="S", team=team, line=line)

    def test_moneylines_are_always_priced(self):
        markets = [self.market(MarketType.MONEYLINE, None, "SEA")]
        assert len(km.select_for_pricing(markets, {})) == 1

    def test_ladder_is_trimmed_around_the_projection(self):
        rungs = [self.market(MarketType.TOTAL, line) for line in range(20, 70, 3)]
        picked = km.select_for_pricing(
            rungs, {"G1": {"total": 44.0, "margin": 3.0, "home_score": 24, "away_score": 21,
                           "home_team": "SEA", "away_team": "NE"}}, window=5)
        assert len(picked) == 5
        lines = sorted(m.line for m in picked)
        assert lines[0] >= 35 and lines[-1] <= 53, f"kept {lines}, too far from 44"

    def test_price_all_overrides_the_window(self):
        rungs = [self.market(MarketType.TOTAL, line) for line in range(20, 70, 3)]
        assert len(km.select_for_pricing(rungs, {}, price_all=True)) == len(rungs)

    def test_spread_ladder_centres_on_that_team_s_expected_margin(self):
        rungs = [self.market(MarketType.SPREAD, line, "SEA") for line in range(1, 30, 3)]
        picked = km.select_for_pricing(
            rungs, {"G1": {"margin": 14.0, "total": 44.0, "home_score": 29, "away_score": 15,
                           "home_team": "SEA", "away_team": "NE"}}, window=3)
        lines = sorted(m.line for m in picked)
        assert min(lines) >= 7 and max(lines) <= 22, f"kept {lines}, expected around 14"
