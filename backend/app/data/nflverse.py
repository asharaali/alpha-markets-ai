"""nflverse ingest — the raw NFL data layer.

nflverse publishes the whole league as flat files on GitHub Releases: free, versioned,
updated within hours of each game, and requiring no API key or account. That makes it the
right spine for this system; the paid odds feed is an optional overlay on top, not a
dependency.

Everything here returns RAW-but-typed rows. Opponent adjustment, rate computation, and any
notion of "form" belong in app.features, not here — this module's only job is to make the
upstream files available and parsed.

A season's files simply do not exist until that season starts, so every accessor treats a
404 as an empty result. Week 1 with zero games played is a first-class, expected state.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence

from app.config import settings
from app.core.logging import get_logger
from app.core.types import Game
from app.data import csvstream as cs
from app.data import teams
from app.data.feed import fetch_file

log = get_logger(__name__)

BASE = settings.NFLVERSE_BASE


def _url(release: str, filename: str) -> str:
    return f"{BASE}/{release}/{filename}"


# --------------------------------------------------------------------------- schedule

_SCHEDULE_COLS = [
    "game_id", "season", "game_type", "week", "gameday", "gametime", "weekday",
    "away_team", "home_team", "away_score", "home_score", "location", "result", "total",
    "away_rest", "home_rest", "spread_line", "total_line", "away_moneyline",
    "home_moneyline", "div_game", "roof", "surface", "temp", "wind",
    "away_qb_name", "home_qb_name", "stadium",
]


def _kickoff_iso(gameday: Optional[str], gametime: Optional[str]) -> str:
    """nflverse gives local Eastern date + time; normalise to a UTC ISO timestamp.

    Eastern is UTC-4 in the DST window that covers essentially the whole NFL regular
    season, and UTC-5 for January/February playoff dates.
    """
    from datetime import datetime, timedelta, timezone

    if not gameday:
        return ""
    t = (gametime or "13:00")[:5]
    try:
        naive = datetime.strptime(f"{gameday} {t}", "%Y-%m-%d %H:%M")
    except ValueError:
        try:
            naive = datetime.strptime(gameday, "%Y-%m-%d")
        except ValueError:
            return ""
    offset = 4 if 3 <= naive.month <= 10 else 5
    return (naive + timedelta(hours=offset)).replace(tzinfo=timezone.utc).isoformat()


def _to_game(row: Dict[str, str]) -> Optional[Game]:
    home = teams.resolve(row.get("home_team"))
    away = teams.resolve(row.get("away_team"))
    if not home or not away:
        return None
    hs = cs.integer(row.get("home_score"))
    as_ = cs.integer(row.get("away_score"))
    return Game(
        game_id=row.get("game_id", ""),
        season=cs.integer(row.get("season"), 0) or 0,
        week=cs.integer(row.get("week"), 0) or 0,
        game_type=cs.text(row.get("game_type")) or "REG",
        kickoff=_kickoff_iso(cs.text(row.get("gameday")), cs.text(row.get("gametime"))),
        home=home, away=away,
        home_score=hs, away_score=as_,
        completed=hs is not None and as_ is not None,
        roof=cs.text(row.get("roof")),
        surface=cs.text(row.get("surface")),
        div_game=cs.flag(row.get("div_game")),
        home_rest=cs.integer(row.get("home_rest")),
        away_rest=cs.integer(row.get("away_rest")),
        stadium=cs.text(row.get("stadium")),
        temp=cs.num(row.get("temp")),
        wind=cs.num(row.get("wind")),
        home_qb=cs.text(row.get("home_qb_name")),
        away_qb=cs.text(row.get("away_qb_name")),
        spread_line=cs.num(row.get("spread_line")),
        total_line=cs.num(row.get("total_line")),
    )


async def schedule(seasons: Optional[Sequence[int]] = None) -> List[Game]:
    """Every scheduled game for the requested seasons (default: all seasons on file).

    `spread_line` here is the HISTORICAL closing line from the sportsbook consensus and is
    only used by the backtester for closing-line value. It is never shown as a live price.
    """
    path = await fetch_file(_url("schedules", "games.csv"),
                            ttl=settings.SCHEDULE_CACHE_TTL, source="nflverse")
    if path is None:
        return []
    want = set(str(s) for s in seasons) if seasons else None
    rows = cs.stream(path, _SCHEDULE_COLS,
                     where=(lambda r: r.get("season") in want) if want else None,
                     strict=True)
    games = [g for g in (_to_game(r) for r in rows) if g is not None]
    games.sort(key=lambda g: (g.season, g.week, g.kickoff))
    return games


# --------------------------------------------------------------------------- play-by-play

# The play-level columns the feature layer actually consumes. Pulling 30 of 372 columns
# instead of all of them is roughly a 10x saving on a 50,000-row season.
PBP_COLS = [
    "game_id", "season", "week", "season_type", "posteam", "defteam",
    "home_team", "away_team", "play_type", "epa", "success", "yards_gained",
    "down", "ydstogo", "yardline_100", "pass", "rush", "qb_dropback", "sack", "qb_hit",
    "interception", "fumble_lost", "touchdown", "pass_touchdown", "rush_touchdown",
    "penalty", "special_teams_play", "aborted_play", "wp", "no_huddle",
    "half_seconds_remaining", "fixed_drive_result", "drive", "complete_pass",
    "air_yards", "yards_after_catch", "xpass", "qtr", "score_differential",
    "passer_player_id", "passer_player_name", "rusher_player_id", "rusher_player_name",
    "receiver_player_id", "receiver_player_name", "td_team",
]


async def play_by_play_path(season: int) -> Optional[Path]:
    return await fetch_file(_url("pbp", f"play_by_play_{season}.csv.gz"),
                            ttl=settings.NFLVERSE_CACHE_TTL, source="nflverse",
                            required=False)


async def plays(season: int, *, max_week: Optional[int] = None,
                columns: Optional[Sequence[str]] = None) -> Iterator[Dict[str, str]]:
    """Stream a season's plays, optionally truncated at `max_week`.

    `max_week` is the backtester's look-ahead guard: asking for week 8 must never see a
    single snap from week 8 or later. Callers building features for an upcoming game pass
    max_week = that game's week - 1.
    """
    path = await play_by_play_path(season)
    if path is None:
        log.info("nflverse: no play-by-play published for %s yet", season)
        return iter(())

    def _filter(row: Dict[str, str]) -> bool:
        if max_week is not None:
            wk = cs.integer(row.get("week"))
            if wk is None or wk > max_week:
                return False
        # Regular season only: preseason snaps are not predictive of regular-season play,
        # and mixing postseason into rate stats double-counts the best teams.
        return row.get("season_type") == "REG"

    return cs.stream(path, list(columns or PBP_COLS), where=_filter)


# --------------------------------------------------------------------------- injuries

@dataclass
class InjuryReport:
    season: int
    week: int
    team: str
    player: str
    player_id: Optional[str]
    position: Optional[str]
    report_status: Optional[str]      # Out | Doubtful | Questionable | "" (no game status)
    practice_status: Optional[str]
    injury: Optional[str]

    @property
    def severity(self) -> float:
        """0 = healthy/expected to play, 1 = will not play. Practice status breaks ties.

        The published game-status ladder (Out/Doubtful/Questionable) is the primary signal;
        when a player carries no game status yet, missed practice is the early tell.
        """
        status = (self.report_status or "").strip().lower()
        if status == "out":
            return 1.0
        if status == "doubtful":
            return 0.75
        if status == "questionable":
            return 0.30
        practice = (self.practice_status or "").strip().lower()
        if "did not participate" in practice:
            return 0.45
        if "limited" in practice:
            return 0.15
        return 0.0

    def to_dict(self) -> Dict[str, object]:
        return {"team": self.team, "week": self.week, "player": self.player,
                "player_id": self.player_id, "position": self.position,
                "report_status": self.report_status or None,
                "practice_status": self.practice_status,
                "injury": self.injury, "severity": round(self.severity, 2)}


_INJURY_COLS = ["season", "season_type", "team", "week", "gsis_id", "position",
                "full_name", "report_status", "practice_primary_injury", "practice_status"]


async def injuries(season: int, *, week: Optional[int] = None) -> List[InjuryReport]:
    path = await fetch_file(_url("injuries", f"injuries_{season}.csv"),
                            ttl=settings.INJURY_CACHE_TTL, source="nflverse",
                            required=False)
    if path is None:
        return []
    out: List[InjuryReport] = []
    for row in cs.stream(path, _INJURY_COLS):
        wk = cs.integer(row.get("week"))
        if week is not None and wk != week:
            continue
        team = teams.resolve(row.get("team"))
        if not team:
            continue
        out.append(InjuryReport(
            season=cs.integer(row.get("season"), season) or season,
            week=wk or 0, team=team,
            player=cs.text(row.get("full_name")) or "?",
            player_id=cs.text(row.get("gsis_id")),
            position=cs.text(row.get("position")),
            report_status=cs.text(row.get("report_status")),
            practice_status=cs.text(row.get("practice_status")),
            injury=cs.text(row.get("practice_primary_injury")),
        ))
    return out


# --------------------------------------------------------------------------- depth charts

_DEPTH_COLS = ["dt", "team", "player_name", "gsis_id", "pos_grp", "pos_name",
               "pos_abb", "pos_rank"]


async def depth_charts(season: int) -> List[Dict[str, object]]:
    """Latest published depth chart rows. Used to know WHO is expected to play a role."""
    path = await fetch_file(_url("depth_charts", f"depth_charts_{season}.csv"),
                            ttl=settings.INJURY_CACHE_TTL, source="nflverse",
                            required=False)
    if path is None:
        return []
    rows: List[Dict[str, object]] = []
    for r in cs.stream(path, _DEPTH_COLS):
        team = teams.resolve(r.get("team"))
        if not team:
            continue
        rows.append({
            "asof": cs.text(r.get("dt")), "team": team,
            "player": cs.text(r.get("player_name")),
            "player_id": cs.text(r.get("gsis_id")),
            "group": cs.text(r.get("pos_grp")),
            "position": cs.text(r.get("pos_abb")),
            "position_name": cs.text(r.get("pos_name")),
            "rank": cs.integer(r.get("pos_rank")),
        })
    # Keep only the most recent snapshot: the file is append-only across the season, and
    # ranking a player off a stale chart is how a benched backup becomes "the starter".
    latest = max((str(r["asof"] or "") for r in rows), default="")
    if latest:
        rows = [r for r in rows if r["asof"] == latest]
    return rows


# --------------------------------------------------------------------------- rosters

_ROSTER_COLS = ["season", "team", "position", "depth_chart_position", "status",
                "full_name", "gsis_id", "week", "game_type", "years_exp"]


async def rosters(season: int, *, week: Optional[int] = None) -> List[Dict[str, object]]:
    path = await fetch_file(_url("weekly_rosters", f"roster_weekly_{season}.csv"),
                            ttl=settings.INJURY_CACHE_TTL, source="nflverse",
                            required=False)
    if path is None:
        return []
    out: List[Dict[str, object]] = []
    for r in cs.stream(path, _ROSTER_COLS):
        wk = cs.integer(r.get("week"))
        if week is not None and wk != week:
            continue
        team = teams.resolve(r.get("team"))
        if not team:
            continue
        out.append({
            "team": team, "week": wk, "player": cs.text(r.get("full_name")),
            "player_id": cs.text(r.get("gsis_id")),
            "position": cs.text(r.get("position")),
            "depth_position": cs.text(r.get("depth_chart_position")),
            "status": cs.text(r.get("status")),
            "years_exp": cs.integer(r.get("years_exp")),
        })
    return out


# --------------------------------------------------------------------------- weekly stats

_PLAYER_WEEK_COLS = [
    "player_id", "player_display_name", "position", "season", "week", "season_type",
    "team", "opponent_team", "completions", "attempts", "passing_yards", "passing_tds",
    "passing_interceptions", "carries", "rushing_yards", "rushing_tds", "receptions",
    "targets", "receiving_yards", "receiving_tds", "target_share", "air_yards_share",
]


async def player_weeks(season: int, *, max_week: Optional[int] = None) -> List[Dict[str, object]]:
    """Per-player, per-game stat lines — the base for every player-prop projection."""
    path = await fetch_file(_url("stats_player", f"stats_player_week_{season}.csv"),
                            ttl=settings.NFLVERSE_CACHE_TTL, source="nflverse",
                            required=False)
    if path is None:
        return []
    out: List[Dict[str, object]] = []
    for r in cs.stream(path, _PLAYER_WEEK_COLS):
        if r.get("season_type") != "REG":
            continue
        wk = cs.integer(r.get("week"))
        if wk is None or (max_week is not None and wk > max_week):
            continue
        team = teams.resolve(r.get("team"))
        if not team:
            continue
        out.append({
            "player_id": cs.text(r.get("player_id")),
            "player": cs.text(r.get("player_display_name")),
            "position": cs.text(r.get("position")),
            "team": team, "week": wk,
            "opponent": teams.resolve(r.get("opponent_team")),
            "completions": cs.num(r.get("completions"), 0.0),
            "attempts": cs.num(r.get("attempts"), 0.0),
            "passing_yards": cs.num(r.get("passing_yards"), 0.0),
            "passing_tds": cs.num(r.get("passing_tds"), 0.0),
            "interceptions": cs.num(r.get("passing_interceptions"), 0.0),
            "carries": cs.num(r.get("carries"), 0.0),
            "rushing_yards": cs.num(r.get("rushing_yards"), 0.0),
            "rushing_tds": cs.num(r.get("rushing_tds"), 0.0),
            "receptions": cs.num(r.get("receptions"), 0.0),
            "targets": cs.num(r.get("targets"), 0.0),
            "receiving_yards": cs.num(r.get("receiving_yards"), 0.0),
            "receiving_tds": cs.num(r.get("receiving_tds"), 0.0),
            "target_share": cs.num(r.get("target_share")),
        })
    return out


async def available_seasons(current: int, back: int) -> List[int]:
    """The seasons we can actually build features from, newest first.

    Probes the play-by-play release rather than assuming: at the start of a season the
    current year's file does not exist yet, and pretending otherwise produces a model
    trained on nothing.
    """
    found: List[int] = []
    for season in range(current, current - back - 1, -1):
        if await play_by_play_path(season) is not None:
            found.append(season)
    return found
