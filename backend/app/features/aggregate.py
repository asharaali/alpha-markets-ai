"""Play-by-play -> per-team, per-game box of efficiency metrics.

This is the NORMALIZED layer: it turns ~50,000 raw plays a season into one row per team
per game, holding the things that actually predict future football (EPA per play, success
rate, pressure, explosiveness, red-zone conversion, turnover rate, pace) rather than the
box-score counting stats that mostly measure game script.

The reduction is expensive enough (a few seconds a season) and stable enough (a completed
game never changes) that results are cached to disk, keyed by the source file's mtime so a
weekly nflverse refresh invalidates automatically.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from app.config import settings
from app.core.logging import get_logger
from app.data import csvstream as cs
from app.data import nflverse, teams

log = get_logger(__name__)

CACHE_DIR = Path(settings.CACHE_DIR) / "aggregates"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Plays that tell us nothing about offensive quality. Kneels, spikes, and special teams
# would otherwise drag every good team's EPA down for winning comfortably.
_IGNORED_PLAY_TYPES = {"kickoff", "punt", "extra_point", "field_goal", "no_play",
                       "qb_kneel", "qb_spike", ""}


@dataclass
class TeamGame:
    """One team's offensive performance in one game, plus context."""

    season: int
    week: int
    game_id: str
    team: str
    opponent: str
    home: bool

    plays: int = 0
    epa_total: float = 0.0
    success_plays: int = 0

    dropbacks: int = 0
    pass_epa_total: float = 0.0
    pass_success: int = 0
    sacks_taken: int = 0
    qb_hits_taken: int = 0

    rushes: int = 0
    rush_epa_total: float = 0.0
    rush_success: int = 0

    explosive_plays: int = 0          # >= 20 air/ground yards
    turnovers: int = 0                # interceptions + lost fumbles
    drives: int = 0
    redzone_trips: int = 0
    redzone_tds: int = 0
    neutral_plays: int = 0            # win prob 20-80%, first three quarters
    neutral_passes: int = 0
    no_huddle: int = 0
    points: int = 0
    points_allowed: int = 0

    # ---- rates (safe against divide-by-zero on a team that never had the ball) ----
    def _rate(self, num: float, den: float) -> Optional[float]:
        return (num / den) if den else None

    @property
    def epa_per_play(self) -> Optional[float]:
        return self._rate(self.epa_total, self.plays)

    @property
    def success_rate(self) -> Optional[float]:
        return self._rate(self.success_plays, self.plays)

    @property
    def pass_epa_per_dropback(self) -> Optional[float]:
        return self._rate(self.pass_epa_total, self.dropbacks)

    @property
    def rush_epa_per_carry(self) -> Optional[float]:
        return self._rate(self.rush_epa_total, self.rushes)

    @property
    def sack_rate(self) -> Optional[float]:
        return self._rate(self.sacks_taken, self.dropbacks)

    @property
    def pressure_rate(self) -> Optional[float]:
        """Sacks + QB hits per dropback — the closest proxy to true pressure rate that
        public play-by-play supports (charted pressures are not in the free feed)."""
        return self._rate(self.sacks_taken + self.qb_hits_taken, self.dropbacks)

    @property
    def explosive_rate(self) -> Optional[float]:
        return self._rate(self.explosive_plays, self.plays)

    @property
    def turnover_rate(self) -> Optional[float]:
        return self._rate(self.turnovers, self.drives)

    @property
    def redzone_td_rate(self) -> Optional[float]:
        return self._rate(self.redzone_tds, self.redzone_trips)

    @property
    def neutral_pass_rate(self) -> Optional[float]:
        """Pass rate in a neutral game script — the honest read on how a team WANTS to play,
        stripped of the trailing-team pass spam that makes raw pass rate useless."""
        return self._rate(self.neutral_passes, self.neutral_plays)

    def rates(self) -> Dict[str, Optional[float]]:
        return {
            "epa_per_play": self.epa_per_play,
            "success_rate": self.success_rate,
            "pass_epa_per_dropback": self.pass_epa_per_dropback,
            "rush_epa_per_carry": self.rush_epa_per_carry,
            "sack_rate": self.sack_rate,
            "pressure_rate": self.pressure_rate,
            "explosive_rate": self.explosive_rate,
            "turnover_rate": self.turnover_rate,
            "redzone_td_rate": self.redzone_td_rate,
            "neutral_pass_rate": self.neutral_pass_rate,
            "plays_per_game": float(self.plays),
            "points": float(self.points),
            "points_allowed": float(self.points_allowed),
        }


# The metrics the rating/feature layers consume, and whether a HIGHER value is better for
# the offense producing it. Defence inverts this automatically.
METRICS: Dict[str, bool] = {
    "epa_per_play": True, "success_rate": True, "pass_epa_per_dropback": True,
    "rush_epa_per_carry": True, "sack_rate": False, "pressure_rate": False,
    "explosive_rate": True, "turnover_rate": False, "redzone_td_rate": True,
    "neutral_pass_rate": True, "plays_per_game": True, "points": True,
}


def _key(game_id: str, team: str) -> Tuple[str, str]:
    return game_id, team


async def team_games(season: int, *, max_week: Optional[int] = None) -> List[TeamGame]:
    """Every team-game aggregate for a season, optionally truncated for backtesting."""
    path = await nflverse.play_by_play_path(season)
    if path is None:
        return []
    cache = CACHE_DIR / f"team_games_{season}.json"
    stamp = f"{path.stat().st_mtime_ns}:{path.stat().st_size}"
    if cache.exists():
        try:
            blob = json.loads(cache.read_text())
            if blob.get("stamp") == stamp:
                rows = [TeamGame(**r) for r in blob["rows"]]
                return _truncate(rows, max_week)
        except (json.JSONDecodeError, TypeError, KeyError):
            log.warning("aggregate cache for %s was unreadable; rebuilding", season)

    started = time.time()
    rows = _reduce(path, season)
    cache.write_text(json.dumps({"stamp": stamp, "rows": [asdict(r) for r in rows]}))
    log.info("aggregated %s: %d team-games from play-by-play in %.1fs",
             season, len(rows), time.time() - started)
    return _truncate(rows, max_week)


def _truncate(rows: List[TeamGame], max_week: Optional[int]) -> List[TeamGame]:
    if max_week is None:
        return rows
    return [r for r in rows if r.week <= max_week]


def _reduce(path: Path, season: int) -> List[TeamGame]:
    """The single pass over play-by-play. Kept deliberately flat and index-driven."""
    acc: Dict[Tuple[str, str], TeamGame] = {}
    # Red-zone trips are per-drive, so we remember which drives already counted.
    rz_seen: set = set()
    drive_seen: set = set()

    def slot(game_id: str, team: str, opp: str, home: bool, week: int) -> TeamGame:
        k = _key(game_id, team)
        tg = acc.get(k)
        if tg is None:
            tg = TeamGame(season=season, week=week, game_id=game_id, team=team,
                          opponent=opp, home=home)
            acc[k] = tg
        return tg

    for row in cs.stream(path, nflverse.PBP_COLS,
                         where=lambda r: r.get("season_type") == "REG"):
        game_id = row.get("game_id") or ""
        home = teams.resolve(row.get("home_team"))
        away = teams.resolve(row.get("away_team"))
        pos = teams.resolve(row.get("posteam"))
        if not (game_id and home and away):
            continue

        if pos is None:
            continue
        defteam = teams.resolve(row.get("defteam")) or (away if pos == home else home)
        week = cs.integer(row.get("week"), 0) or 0
        tg = slot(game_id, pos, defteam, pos == home, week)

        play_type = (row.get("play_type") or "").strip()
        if cs.flag(row.get("aborted_play")):
            continue

        drive = cs.text(row.get("drive"))
        if drive is not None:
            dk = (game_id, pos, drive)
            if dk not in drive_seen:
                drive_seen.add(dk)
                tg.drives += 1
            # A red-zone TRIP is the drive first reaching the opponent 20.
            y100 = cs.num(row.get("yardline_100"))
            if y100 is not None and y100 <= 20 and dk not in rz_seen:
                rz_seen.add(dk)
                tg.redzone_trips += 1
                if (row.get("fixed_drive_result") or "").strip() == "Touchdown":
                    tg.redzone_tds += 1

        if play_type in _IGNORED_PLAY_TYPES:
            continue

        is_pass = cs.flag(row.get("pass"))
        is_rush = cs.flag(row.get("rush"))
        if not (is_pass or is_rush):
            continue

        epa = cs.num(row.get("epa"))
        if epa is None:
            continue
        success = cs.num(row.get("success"), 0.0) or 0.0
        yards = cs.num(row.get("yards_gained"), 0.0) or 0.0

        tg.plays += 1
        tg.epa_total += epa
        if success >= 1:
            tg.success_plays += 1
        if yards >= 20:
            tg.explosive_plays += 1
        if cs.flag(row.get("interception")) or cs.flag(row.get("fumble_lost")):
            tg.turnovers += 1
        if cs.flag(row.get("no_huddle")):
            tg.no_huddle += 1

        # Neutral script: competitive win probability, before the fourth quarter.
        wp = cs.num(row.get("wp"))
        qtr = cs.integer(row.get("qtr"), 0) or 0
        if wp is not None and 0.20 <= wp <= 0.80 and qtr <= 3:
            tg.neutral_plays += 1
            if is_pass:
                tg.neutral_passes += 1

        if is_pass or cs.flag(row.get("sack")):
            tg.dropbacks += 1
            tg.pass_epa_total += epa
            if success >= 1:
                tg.pass_success += 1
            if cs.flag(row.get("sack")):
                tg.sacks_taken += 1
            if cs.flag(row.get("qb_hit")):
                tg.qb_hits_taken += 1
        elif is_rush:
            tg.rushes += 1
            tg.rush_epa_total += epa
            if success >= 1:
                tg.rush_success += 1

    rows = sorted(acc.values(), key=lambda t: (t.week, t.game_id, t.team))
    return rows


async def with_scores(season: int, rows: List[TeamGame]) -> List[TeamGame]:
    """Fill points scored/allowed on each team-game from the authoritative schedule."""
    games = {g.game_id: g for g in await nflverse.schedule(seasons=[season])}
    for tg in rows:
        g = games.get(tg.game_id)
        if not g or g.home_score is None or g.away_score is None:
            continue
        if tg.team == g.home:
            tg.points, tg.points_allowed = g.home_score, g.away_score
        else:
            tg.points, tg.points_allowed = g.away_score, g.home_score
    return rows
