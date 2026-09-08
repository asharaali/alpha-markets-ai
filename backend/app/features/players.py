"""Player usage and production form — the input to every player-prop projection.

Props are priced off a player's own recent per-game rates, recency-weighted, with the
game-to-game variance measured rather than assumed. That variance is the whole ballgame for
props: two receivers averaging 60 yards are completely different bets if one ranges 45-75
and the other ranges 5-140.

Where a player has too little history to measure their own spread, a positional prior fills
in — and the resulting projection is marked as such, so nothing downstream mistakes a
guess for a measurement.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from app.core.logging import get_logger
from app.data import nflverse, teams

log = get_logger(__name__)

# Weight halves every this many games back. Player usage changes faster than team quality.
GAME_HALF_LIFE = 5.0

STAT_FIELDS = ("attempts", "passing_yards", "passing_tds", "carries", "rushing_yards",
               "rushing_tds", "receptions", "targets", "receiving_yards", "receiving_tds")

# Coefficient of variation priors by stat, used when a player has too few games to measure
# their own. Drawn from league-wide game-log dispersion.
CV_PRIOR: Dict[str, float] = {
    "passing_yards": 0.30, "attempts": 0.22, "passing_tds": 0.70,
    "rushing_yards": 0.55, "carries": 0.35, "rushing_tds": 1.10,
    "receiving_yards": 0.65, "receptions": 0.45, "targets": 0.38,
    "receiving_tds": 1.20,
}
MIN_GAMES_FOR_OWN_VARIANCE = 4


@dataclass
class PlayerForm:
    player_id: str
    player: str
    position: Optional[str]
    team: Optional[str]
    games: int
    weighted_games: float
    means: Dict[str, float] = field(default_factory=dict)
    stdevs: Dict[str, float] = field(default_factory=dict)
    measured_variance: bool = False
    seasons_used: List[int] = field(default_factory=list)
    last_week: Optional[int] = None

    def mean(self, stat: str) -> float:
        return self.means.get(stat, 0.0)

    def stdev(self, stat: str) -> float:
        measured = self.stdevs.get(stat)
        if measured and measured > 0 and self.measured_variance:
            return measured
        cv = CV_PRIOR.get(stat, 0.5)
        return max(self.mean(stat) * cv, _floor(stat))

    def to_dict(self) -> Dict[str, object]:
        return {
            "player_id": self.player_id, "player": self.player,
            "position": self.position, "team": self.team,
            "games": self.games, "weighted_games": round(self.weighted_games, 2),
            "means": {k: round(v, 2) for k, v in self.means.items()},
            "stdevs": {k: round(self.stdev(k), 2) for k in self.means},
            "measured_variance": self.measured_variance,
            "seasons_used": self.seasons_used,
            "last_week": self.last_week,
        }


def _floor(stat: str) -> float:
    """A minimum spread, so a player with one quiet game is not priced as a certainty."""
    if "yards" in stat:
        return 12.0
    if stat in {"attempts", "targets", "carries"}:
        return 2.0
    return 0.4


async def build(season: int, *, max_week: Optional[int] = None,
                history_seasons: int = 1) -> Dict[str, PlayerForm]:
    """Recency-weighted per-game form for every player with recent snaps.

    Falls back to prior seasons when the current one has no games yet, which is the normal
    state in week 1 — and the reason a week-1 prop projection is inherently weaker than a
    week-10 one.
    """
    rows: List[Dict[str, object]] = []
    seasons_used: List[int] = []
    for offset in range(0, history_seasons + 1):
        s = season - offset
        cap = max_week if offset == 0 else None
        batch = await nflverse.player_weeks(s, max_week=cap)
        if not batch:
            continue
        seasons_used.append(s)
        for row in batch:
            row["_season"] = s
            row["_offset"] = offset
            rows.append(row)

    if not rows:
        log.info("no player game logs available for %s", season)
        return {}

    # Distance in games from "now", so recency weighting spans season boundaries.
    latest_week = max((int(r["week"]) for r in rows if r["_offset"] == 0), default=0)
    grouped: Dict[str, List[Dict[str, object]]] = {}
    for row in rows:
        pid = row.get("player_id")
        if not pid:
            continue
        grouped.setdefault(str(pid), []).append(row)

    out: Dict[str, PlayerForm] = {}
    for pid, logs in grouped.items():
        logs.sort(key=lambda r: (r["_season"], r["week"]))
        weights: List[float] = []
        for row in logs:
            if row["_offset"] == 0:
                games_ago = max(latest_week - int(row["week"]), 0)
            else:
                games_ago = latest_week + (row["_offset"] - 1) * 18 + (18 - int(row["week"]))
            weights.append(0.5 ** (games_ago / GAME_HALF_LIFE))
        total_weight = sum(weights)
        if total_weight <= 0:
            continue

        means: Dict[str, float] = {}
        stdevs: Dict[str, float] = {}
        for stat in STAT_FIELDS:
            values = [float(r.get(stat) or 0.0) for r in logs]
            mean = sum(v * w for v, w in zip(values, weights)) / total_weight
            means[stat] = mean
            if len(values) >= MIN_GAMES_FOR_OWN_VARIANCE:
                var = sum(w * (v - mean) ** 2 for v, w in zip(values, weights)) / total_weight
                stdevs[stat] = math.sqrt(max(var, 0.0))

        newest = logs[-1]
        out[pid] = PlayerForm(
            player_id=pid,
            player=str(newest.get("player") or "?"),
            position=newest.get("position"),  # type: ignore[arg-type]
            team=newest.get("team"),          # type: ignore[arg-type]
            games=len(logs),
            weighted_games=total_weight,
            means=means, stdevs=stdevs,
            measured_variance=len(logs) >= MIN_GAMES_FOR_OWN_VARIANCE,
            seasons_used=sorted(set(seasons_used), reverse=True),
            last_week=int(newest.get("week") or 0),
        )
    return out


def index_by_name(forms: Dict[str, PlayerForm]) -> Dict[str, PlayerForm]:
    """Name -> form, for matching Kalshi contracts (which carry names, not player ids).

    Where two players share a name we keep the one with more recent playing time, since a
    prop market is virtually always about the active one.
    """
    out: Dict[str, PlayerForm] = {}
    for form in forms.values():
        key = _normalise(form.player)
        existing = out.get(key)
        if existing is None or form.weighted_games > existing.weighted_games:
            out[key] = form
    return out


def _normalise(name: str) -> str:
    return " ".join((name or "").replace(".", "").replace("'", "").lower().split())


def lookup(name_index: Dict[str, PlayerForm], name: str) -> Optional[PlayerForm]:
    key = _normalise(name)
    hit = name_index.get(key)
    if hit:
        return hit
    # Kalshi occasionally writes "C.J. Stroud" where the feed has "CJ Stroud"; the
    # normaliser already strips punctuation, so only a suffix difference should remain.
    for suffix in (" jr", " sr", " ii", " iii", " iv"):
        if key.endswith(suffix):
            trimmed = key[: -len(suffix)]
            if trimmed in name_index:
                return name_index[trimmed]
        elif (key + suffix) in name_index:
            return name_index[key + suffix]
    return None
