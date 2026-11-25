"""Module 5 Sports: ESPN parsing, Elo, the incremental refresh, and the private read path.

Review focus:
1. Probabilities are pregame: a game's result never leaks into its own forecast record.
2. One league down -> only that league errors; the others are rebuilt.
3. Finished past units are never fetched again; backfill respects the per-run budget.
4. The request path reads files only, and is private.
"""
import asyncio
import json
import math
import time
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.data.sports import elo, espn, refresh
from app.data.sports.leagues import LEAGUES
from app.jobs import scheduler
from app.main import app

FIX = Path(__file__).parent / "fixtures" / "sports"
NBA, NFL, EPL = LEAGUES["nba"], LEAGUES["nfl"], LEAGUES["epl"]
DAY = 86400


def load(name):
    return json.loads((FIX / name).read_text())


# ---- ESPN parsing -------------------------------------------------------------------------

def test_american_odds():
    assert espn.american_to_prob("-300") == pytest.approx(0.75)
    assert espn.american_to_prob("+300") == pytest.approx(0.25)
    assert espn.american_to_prob("EVEN") == 0.5
    assert espn.american_to_prob("+50") is None and espn.american_to_prob("OFF") is None


def test_book_probs_remove_the_vig_including_draw():
    b = espn.book_probs([{"moneyline": {"home": {"close": {"odds": "+160"}}, "away": {"close": {"odds": "+175"}},
                                        "draw": {"close": {"odds": "+240"}}}, "details": "x"}])
    assert b["home"] + b["away"] + b["draw"] == pytest.approx(1.0)
    assert b["home"] > b["away"] > b["draw"]
    assert espn.book_probs([{"moneyline": {"home": {"close": {"odds": None}, "open": {"odds": "-150"}},
                                           "away": {"close": {"odds": "+130"}}}}])["home"] > 0.5   # open fallback
    assert espn.book_probs([]) is None


def test_parse_nba_scoreboard():
    games, meta = espn.parse_scoreboard(load("nba_scoreboard.json"))
    assert len(games) == 3 and meta["season"] == 2027
    assert [g["state"] for g in games] == ["in", "in", "pre"]
    pre = games[2]
    assert pre["book"]["home"] + pre["book"]["away"] == pytest.approx(1.0)
    assert games[0]["book"] is None                       # live games carry no line
    assert games[0]["home"]["score"] is not None and games[0]["home"]["record"]
    assert all(isinstance(c, str) and len(c) == 8 for c in meta["calendar"])


def test_parse_epl_has_a_draw_line():
    games, _ = espn.parse_scoreboard(load("epl_scoreboard.json"))
    assert games[0]["book"]["draw"] is not None


def test_nfl_calendar_is_weeks_without_preseason():
    _, meta = espn.parse_scoreboard(load("nfl_scoreboard.json"))
    assert meta["calendar"] and all(c[0] in (2, 3) for c in meta["calendar"])
    assert [c[1] for c in meta["calendar"] if c[0] == 2][:3] == [1, 2, 3]


def test_params_for_units():
    assert espn.params_for(NBA, "d:20261124")["dates"] == "20261124"
    p = espn.params_for(LEAGUES["ncaaf"], "w:2026:2:12")
    assert p == {"limit": 500, "groups": "80", "dates": "2026", "seasontype": "2", "week": "12"}


# ---- Elo ----------------------------------------------------------------------------------

def game(gid, t, home, away, hs, as_, season=2026, stype=2, neutral=False, book=None, conf=None, state="post"):
    side = lambda tid, s: {"id": tid, "abbr": tid, "name": tid, "short": tid, "score": s, "record": None,
                           "rank": None, "conf": conf, "winner": None}
    return {"id": gid, "start": float(t), "state": state, "status": "", "completed": state == "post",
            "detail": "", "season": season, "season_type": stype, "neutral": neutral,
            "home": side(home, hs), "away": side(away, as_), "book": book}


def test_winner_gains_exactly_what_loser_loses():
    r = elo.run(NBA, [game("1", 1, "A", "B", 110, 100)], current_season=2026, week_ago=0)
    assert r["ratings"]["A"] > 1500 > r["ratings"]["B"]
    assert r["ratings"]["A"] + r["ratings"]["B"] == pytest.approx(3000)


def test_home_advantage_and_neutral_site():
    assert elo.probs(NBA, 1500, 1500, neutral=False)["home"] > 0.5
    assert elo.probs(NBA, 1500, 1500, neutral=True)["home"] == pytest.approx(0.5)


def test_record_is_pregame_and_only_this_season():
    # Last season A beat B ten times; this season's first game is still forecast before it updates.
    old = [game(str(i), i, "A", "B", 110, 90, season=2025) for i in range(10)]
    new = [game("x", 100, "B", "A", 120, 100, season=2026, neutral=True)]
    r = elo.run(NBA, old + new, current_season=2026, week_ago=0)
    assert r["record"]["n"] == 1 and r["record"]["hit_rate"] == 0.0     # Elo picked A: wrong, and says so
    assert r["records"]["B"]["w"] == 1 and r["records"]["A"]["l"] == 1   # W-L counts this season only


def test_new_season_regresses_toward_the_mean():
    games = [game(str(i), i, "A", "B", 110, 90, season=2025) for i in range(20)]
    end = elo.run(NBA, games, current_season=2025, week_ago=0)["ratings"]["A"]
    nxt = elo.run(NBA, games + [game("n", 100, "C", "D", 100, 99, season=2026)], current_season=2026,
                  week_ago=0)["ratings"]["A"]
    assert nxt == pytest.approx(1500 + (1 - NBA.regress) * (end - 1500))


def test_preseason_and_unfinished_games_do_not_count():
    games = [game("p", 1, "A", "B", 130, 80, stype=1), game("s", 2, "A", "B", None, None, state="pre")]
    assert elo.run(NBA, games, current_season=2026, week_ago=0)["ratings"] == {}


def test_nfl_tie_is_unscored_and_leaves_ratings():
    r = elo.run(NFL, [game("t", 1, "A", "B", 20, 20)], current_season=2026, week_ago=0)
    assert r["record"]["n"] == 0 and r["ratings"]["A"] == r["ratings"]["B"] == 1500


def test_soccer_three_way_probs_and_draw_update():
    p = elo.probs(EPL, 1500, 1500, neutral=False)
    assert p["home"] + p["away"] + p["draw"] == pytest.approx(1.0) and p["home"] > p["away"]
    r = elo.run(EPL, [game("d", 1, "A", "B", 1, 1)], current_season=2026, week_ago=0)
    assert r["ratings"]["A"] < 1500        # home side was expected to win; a draw costs it
    assert r["records"]["A"]["d"] == 1 and r["records"]["A"]["pts"] == 1


def test_vs_book_scores_only_games_with_a_line():
    book = {"home": 0.8, "away": 0.2, "draw": None}
    games = [game("1", 1, "A", "B", 100, 90, book=book), game("2", 2, "A", "B", 100, 90)]
    rec = elo.run(NBA, games, current_season=2026, week_ago=0)["record"]
    assert rec["n"] == 2 and rec["vs_book"]["n"] == 1
    assert rec["vs_book"]["book_logloss"] == pytest.approx(-math.log(0.8), abs=1e-4)


def test_fcs_teams_start_low_and_are_not_ranked():
    ncaaf = LEAGUES["ncaaf"]
    g = game("1", 1, "UGA", "FCS1", 45, 3, conf="8")
    g["away"]["conf"] = "20"
    r = elo.run(ncaaf, [g], current_season=2026, week_ago=0)
    assert r["ratings"]["FCS1"] < 1200
    assert [row["abbr"] for row in elo.table(ncaaf, r)] == ["UGA"]


# ---- refresh: planning, budget, isolation -------------------------------------------------

NOW = datetime(2026, 11, 24, 21, 0, tzinfo=refresh.ET).timestamp()


def _day(t):
    return datetime.fromtimestamp(t, refresh.ET).strftime("%Y%m%d")


def test_plan_skips_locked_and_far_future_and_puts_recent_first():
    days = [_day(NOW + i * DAY) for i in range(-10, 6)]
    meta = {"season": 2027, "start": NOW - 30 * DAY, "end": NOW + 100 * DAY, "calendar": days}
    store = {"seasons": {"current": meta}, "units": {f"d:{days[0]}": {"locked": True, "games": [], "fetched_at": 0}}}
    p = refresh.plan(NBA, store, NOW)
    assert f"d:{days[0]}" not in p                                  # finished: never again
    assert f"d:{days[-1]}" not in p and f"d:{days[-2]}" not in p     # 4-5 days out: not yet
    assert p[0] == f"d:{days[13]}"                                   # recent/upcoming before backfill


def test_offseason_skips_last_season_backfill():
    meta = lambda season, start, end: {"season": season, "start": start, "end": end,
                                       "calendar": [_day(start + i * DAY) for i in range(0, 200, 10)]}
    store = {"seasons": {"current": meta(2026, NOW - 250 * DAY, NOW - 10 * DAY),
                         "previous": meta(2025, NOW - 600 * DAY, NOW - 380 * DAY)}, "units": {}}
    keys = refresh.plan(LEAGUES["mlb"], store, NOW)
    assert keys and all(k >= "d:2026" for k in keys)                 # none from last season


def test_sparse_mlb_calendar_is_walked_daily():
    meta = {"season": 2026, "start": NOW - 100 * DAY, "end": NOW - 10 * DAY, "calendar": ["20260713"]}
    assert len(refresh.season_units(LEAGUES["mlb"], meta)) == 91


def test_merge_keeps_the_pregame_line():
    pre = game("1", NOW, "A", "B", None, None, state="pre", book={"home": 0.6, "away": 0.4, "draw": None})
    live = game("1", NOW, "A", "B", 10, 8, state="in")
    assert refresh.merge_games([pre], [live])[0]["book"]["home"] == 0.6


def _payload(meta_days, events):
    """A minimal ESPN-shaped scoreboard."""
    def ev(gid, t, h, a, hs, as_, state):
        side = lambda ha, tid, s: {"homeAway": ha, "team": {"id": tid, "abbreviation": tid, "displayName": tid},
                                   "score": str(s)}
        return {"id": gid, "date": datetime.fromtimestamp(t).astimezone().isoformat(), "season": {"year": 2027, "type": 2},
                "competitions": [{"competitors": [side("home", h, hs), side("away", a, as_)],
                                  "status": {"type": {"state": state, "completed": state == "post", "name": "", "shortDetail": ""}}}]}
    return {"leagues": [{"season": {"year": 2027, "startDate": "2026-10-01T07:00Z", "endDate": "2027-06-01T07:00Z"},
                         "calendar": [f"{d[:4]}-{d[4:6]}-{d[6:]}T07:00Z" for d in meta_days]}],
            "events": [ev(*e) for e in events]}


@pytest.fixture
def tmp_sports(tmp_path, monkeypatch):
    monkeypatch.setattr(refresh, "DIR", tmp_path)
    monkeypatch.setattr(refresh, "INDEX", tmp_path / "index.json")
    return tmp_path


def test_refresh_backfills_within_budget_and_isolates_a_down_league(tmp_sports, monkeypatch):
    days = [_day(NOW - i * DAY) for i in range(40, 0, -1)]
    calls = []

    async def fetch(league, unit, client=None):
        calls.append((league.key, unit))
        if league.key == "nhl":
            raise RuntimeError("ESPN 503")
        if league.key != "nba":
            return {"leagues": [{}], "events": []}
        t = NOW - 2 * DAY if unit is None else datetime.strptime(unit[2:], "%Y%m%d").replace(tzinfo=refresh.ET).timestamp() + 20 * 3600
        return _payload(days, [(f"g{unit}", t, "A", "B", 100, 90, "post")])

    monkeypatch.setattr(espn, "fetch", fetch)
    monkeypatch.setattr(refresh, "FETCH_BUDGET", 10)
    out = asyncio.run(refresh.refresh(now=NOW))
    assert out["leagues"]["nhl"]["error"].startswith("RuntimeError")
    nba_units = [u for k, u in calls if k == "nba" and u and u.startswith("d:")]
    assert len(nba_units) <= 10 + 2                                  # budget + unbudgeted recent days
    idx = refresh.read_index()
    by = {lg["key"]: lg for lg in idx["leagues"]}
    assert "ESPN 503" in by["nhl"]["errors"][0] and by["nba"]["errors"] == []
    blob = refresh.read_league("nba")
    assert blob["ready"] and blob["ratings"][0]["abbr"] == "A" and blob["backfill"]["done"] < blob["backfill"]["total"]

    calls.clear()                                                    # a later run fetches no finished day twice
    asyncio.run(refresh.refresh(now=NOW + 3600))
    again = {u for k, u in calls if k == "nba" and u and u.startswith("d:")}
    first = set(nba_units)
    old_finished = {u for u in first if u < f"d:{_day(NOW - 3 * DAY)}"}
    assert not (again & old_finished)


# ---- API ----------------------------------------------------------------------------------

def test_sports_api_is_private_reads_files_and_404s_unknown(tmp_sports, monkeypatch):
    name = f"sp{int(time.time() * 1000) % 10**9}"
    auth.create_user(name, "sports-pw")
    client = TestClient(app)
    try:
        assert client.get("/api/sports").status_code == 401
        assert client.get("/api/sports/nba").status_code == 401
        client.post("/api/login", json={"username": name, "password": "sports-pw"})

        async def boom(*a, **k):
            raise AssertionError("the request path fetched upstream")
        monkeypatch.setattr(espn, "fetch", boom)
        assert client.get("/api/sports").json() == {"ready": False, "leagues": []}
        assert client.get("/api/sports/nba").json()["ready"] is False
        assert client.get("/api/sports/robinhood").status_code == 404
    finally:
        auth.disable_user(name)


def test_sports_refresh_is_scheduled_on_its_own_start_slot():
    names = [n for n, _, _ in scheduler.job_specs()]
    assert "sports_refresh" in names
    delays = [scheduler.start_delay(n) for n in names]
    assert len(set(delays)) == len(delays)
