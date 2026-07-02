"""
Probable starting pitchers + pitcher quality — the biggest single factor in an MLB game.

Source: MLB StatsAPI (statsapi.mlb.com, free, no key).
  - schedule?hydrate=probablePitcher  -> who's starting each game
  - people/{id} season pitching stats  -> ERA / IP / K / BB / HR

We turn a starter into a RUN-SUPPRESSION MULTIPLIER applied to the opponent's expected
runs, the honest sabermetric way:

  1. Rate true talent with FIP (Fielding-Independent Pitching: (13*HR+3*BB-2*K)/IP + C).
     FIP strips out team defense + batted-ball luck, so it's the right "how good is this
     arm" number for a forward-looking game model. We blend 60% FIP / 40% ERA.
  2. REGRESS to league average by innings pitched (reliability = IP/(IP+60)) so a 20-inning
     sample early in the year doesn't scream "ace" or "batting-practice."
  3. A starter only throws ~62% of a game; the bullpen (league-average by default) covers
     the rest. So the game-level multiplier = 0.62*starter + 0.38*bullpen. This is why even
     an ace only pulls the opponent's runs down to ~0.76x, not 0.55x.

Centered on LEAGUE AVERAGE: a league-average starter -> multiplier 1.00 -> no change, which
is exactly what the trained team-Elo model assumes, so the ratings stay unbiased. Only
above/below-average starters move the line.
"""
from __future__ import annotations
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple
from zoneinfo import ZoneInfo

import httpx

STATS_BASE = "https://statsapi.mlb.com/api/v1"
LEAGUE_AVG_ERA = 4.10      # reference run environment (FIP is scaled to match)
FIP_CONSTANT = 3.15        # makes league-average FIP land near league ERA
SP_SHARE = 0.62            # fraction of a 9-inning game a starter typically throws
IP_ANCHOR = 60.0           # innings of regression toward the mean
_ET = ZoneInfo("America/New_York")

# multiplier -> human label, for display only
def quality_label(mult: float) -> str:
    if mult <= 0.88:
        return "ace"
    if mult <= 0.97:
        return "good"
    if mult <= 1.05:
        return "avg"
    return "weak"


_RATING_CACHE: Dict[int, Tuple[float, Dict, float]] = {}   # pid -> (mult, info, ts)
_RATING_TTL = 6 * 3600
_SCHED_CACHE: Dict[str, object] = {"data": None, "ts": 0.0}
_SCHED_TTL = 1800


def _starter_multiplier(stat: Dict) -> Tuple[float, Dict]:
    """Season pitching stat line -> (game-level run-suppression multiplier, display info)."""
    try:
        ip = float(stat.get("inningsPitched") or 0)
        era = float(stat.get("era")) if stat.get("era") not in (None, "-.--", "") else LEAGUE_AVG_ERA
        hr = float(stat.get("homeRuns") or 0)
        bb = float(stat.get("baseOnBalls") or 0)
        k = float(stat.get("strikeOuts") or 0)
    except (TypeError, ValueError):
        return 1.0, {"era": None, "fip": None, "ip": 0}
    if ip < 1:
        return 1.0, {"era": era, "fip": None, "ip": 0}

    fip = (13 * hr + 3 * bb - 2 * k) / ip + FIP_CONSTANT
    talent = 0.6 * fip + 0.4 * era                     # forward-looking skill estimate
    reliability = ip / (ip + IP_ANCHOR)                # small samples -> pull to league avg
    ra9 = reliability * talent + (1 - reliability) * LEAGUE_AVG_ERA
    starter_mult = min(max(ra9 / LEAGUE_AVG_ERA, 0.55), 1.60)
    # Blend with a league-average bullpen for the innings the starter won't throw.
    game_mult = SP_SHARE * starter_mult + (1 - SP_SHARE) * 1.0
    game_mult = round(min(max(game_mult, 0.78), 1.22), 3)
    return game_mult, {"era": round(era, 2), "fip": round(fip, 2), "ip": ip}


async def _pitcher_rating(client: httpx.AsyncClient, pid: int, season: int) -> Tuple[float, Dict]:
    hit = _RATING_CACHE.get(pid)
    if hit and time.time() - hit[2] < _RATING_TTL:
        return hit[0], hit[1]
    try:
        r = await client.get(f"{STATS_BASE}/people/{pid}",
                             params={"hydrate": f"stats(group=[pitching],type=[season],season={season})"})
        r.raise_for_status()
        person = r.json()["people"][0]
        name = person.get("fullName")
        splits = (person.get("stats") or [{}])[0].get("splits") or []
        if splits:
            mult, info = _starter_multiplier(splits[0]["stat"])
        else:
            mult, info = 1.0, {"era": None, "fip": None, "ip": 0}   # no season stats yet
        info["name"] = name
        _RATING_CACHE[pid] = (mult, info, time.time())
        return mult, info
    except Exception as exc:
        print(f"[mlb_pitchers] rating fetch failed for {pid}: {exc}")
        return 1.0, {"name": None, "era": None, "fip": None, "ip": 0}


async def starters_index() -> Dict[Tuple[str, str], Dict]:
    """{(home_team, away_team): {home:{...}, away:{...}}} for games around today (ET),
    each side = {name, mult, era, fip, label} or None when no starter is announced yet."""
    cache = _SCHED_CACHE
    if cache["data"] is not None and time.time() - float(cache["ts"]) < _SCHED_TTL:
        return cache["data"]  # type: ignore[return-value]

    today_et = datetime.now(_ET).date()
    start = (today_et - timedelta(days=1)).isoformat()
    end = (today_et + timedelta(days=2)).isoformat()
    season = today_et.year
    index: Dict[Tuple[str, str], Dict] = {}
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            r = await client.get(f"{STATS_BASE}/schedule",
                                 params={"sportId": 1, "startDate": start, "endDate": end,
                                         "hydrate": "probablePitcher,team"})
            r.raise_for_status()
            games = [g for d in r.json().get("dates", []) for g in d.get("games", [])]

            # Collect probable-pitcher ids, rate each once (cached across games).
            async def _side(team_side):
                pp = team_side.get("probablePitcher") or {}
                pid = pp.get("id")
                if not pid:
                    return None
                mult, info = await _pitcher_rating(client, pid, season)
                return {"name": info.get("name") or pp.get("fullName"), "mult": mult,
                        "era": info.get("era"), "fip": info.get("fip"),
                        "label": quality_label(mult)}

            for g in games:
                home = g["teams"]["home"]["team"]["name"]
                away = g["teams"]["away"]["team"]["name"]
                h = await _side(g["teams"]["home"])
                a = await _side(g["teams"]["away"])
                index[(home, away)] = {"home": h, "away": a}
    except Exception as exc:
        print(f"[mlb_pitchers] schedule fetch failed: {exc}")

    cache["data"], cache["ts"] = index, time.time()
    return index
