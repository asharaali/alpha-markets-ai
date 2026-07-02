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
import asyncio
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


_RATING_CACHE: Dict[int, Tuple[float, Dict, float]] = {}   # pid -> (starter_talent_mult, info, ts)
_RATING_TTL = 6 * 3600
_SCHED_CACHE: Dict[str, object] = {"data": None, "ts": 0.0}
_SCHED_TTL = 1800
_BULLPEN_CACHE: Dict[str, object] = {"data": None, "ts": 0.0}
_BULLPEN_TTL = 12 * 3600


def _ra9(stat: Dict, ip_anchor: float) -> Tuple[Optional[float], Dict]:
    """A pitcher's/staff's expected runs-allowed rate: FIP (defense-independent) blended
    60/40 with ERA, regressed toward league average by innings pitched. Returns (ra9, info)."""
    try:
        ip = float(stat.get("inningsPitched") or 0)
        era = float(stat.get("era")) if stat.get("era") not in (None, "-.--", "") else LEAGUE_AVG_ERA
        hr = float(stat.get("homeRuns") or 0)
        bb = float(stat.get("baseOnBalls") or 0)
        k = float(stat.get("strikeOuts") or 0)
    except (TypeError, ValueError):
        return None, {"era": None, "fip": None, "ip": 0}
    if ip < 1:
        return None, {"era": era, "fip": None, "ip": 0}
    fip = (13 * hr + 3 * bb - 2 * k) / ip + FIP_CONSTANT
    talent = 0.6 * fip + 0.4 * era
    reliability = ip / (ip + ip_anchor)
    ra9 = reliability * talent + (1 - reliability) * LEAGUE_AVG_ERA
    return ra9, {"era": round(era, 2), "fip": round(fip, 2), "ip": ip}


# ---------------- per-team bullpen ratings ----------------

async def bullpen_index() -> Dict[str, float]:
    """{team_name: bullpen_multiplier} from each team's RELIEVER-only season stats, centered
    on the actual league-average bullpen (so an average pen = 1.0). Tighter clamp than
    starters — bullpens vary less game-to-game. Falls back to all-1.0 on any failure."""
    cache = _BULLPEN_CACHE
    if cache["data"] is not None and time.time() - float(cache["ts"]) < _BULLPEN_TTL:
        return cache["data"]  # type: ignore[return-value]
    season = datetime.now(_ET).year
    out: Dict[str, float] = {}
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            tr = await client.get(f"{STATS_BASE}/teams", params={"sportId": 1, "season": season})
            tr.raise_for_status()
            teams = [(t["id"], t["name"]) for t in tr.json().get("teams", [])]

            async def _team_ra9(tid):
                try:
                    r = await client.get(f"{STATS_BASE}/teams/{tid}/stats",
                                         params={"stats": "statSplits", "group": "pitching",
                                                 "sitCodes": "rp", "season": season})
                    r.raise_for_status()
                    splits = (r.json().get("stats") or [{}])[0].get("splits") or []
                    if splits:
                        ra9, _ = _ra9(splits[0]["stat"], ip_anchor=40.0)
                        return ra9
                except Exception:
                    pass
                return None

            ra9s = await asyncio.gather(*[_team_ra9(tid) for tid, _ in teams])
        valid = [x for x in ra9s if x]
        league_bp = sum(valid) / len(valid) if valid else LEAGUE_AVG_ERA
        for (tid, name), ra9 in zip(teams, ra9s):
            mult = (ra9 / league_bp) if ra9 else 1.0
            out[name] = round(min(max(mult, 0.85), 1.15), 3)
    except Exception as exc:
        print(f"[mlb_pitchers] bullpen fetch failed: {exc}")
    cache["data"], cache["ts"] = out, time.time()
    return out


# ---------------- starters + game multiplier ----------------

async def _starter_talent(client: httpx.AsyncClient, pid: int, season: int) -> Tuple[float, Dict]:
    """Starter's individual run-suppression talent (before the bullpen blend)."""
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
        ra9, info = _ra9(splits[0]["stat"], ip_anchor=IP_ANCHOR) if splits else (None, {"era": None, "fip": None, "ip": 0})
        talent = min(max(ra9 / LEAGUE_AVG_ERA, 0.55), 1.60) if ra9 else 1.0
        info["name"] = name
        _RATING_CACHE[pid] = (talent, info, time.time())
        return talent, info
    except Exception as exc:
        print(f"[mlb_pitchers] rating fetch failed for {pid}: {exc}")
        return 1.0, {"name": None, "era": None, "fip": None, "ip": 0}


def _game_mult(starter_talent: float, bullpen_mult: float) -> float:
    """A team's full pitching multiplier = its announced starter (~62% of the game) blended
    with its real bullpen (the rest). Applied to the OPPONENT's expected runs."""
    return round(min(max(SP_SHARE * starter_talent + (1 - SP_SHARE) * bullpen_mult, 0.75), 1.25), 3)


async def starters_index() -> Dict[Tuple[str, str], Dict]:
    """{(home_team, away_team): {home:{...}, away:{...}}} for games around today (ET). Each
    side = {name, mult, era, fip, label, bullpen} — mult is the full starter+bullpen game
    multiplier — or None when no starter is announced yet."""
    cache = _SCHED_CACHE
    if cache["data"] is not None and time.time() - float(cache["ts"]) < _SCHED_TTL:
        return cache["data"]  # type: ignore[return-value]

    today_et = datetime.now(_ET).date()
    start = (today_et - timedelta(days=1)).isoformat()
    end = (today_et + timedelta(days=2)).isoformat()
    season = today_et.year
    bullpens = await bullpen_index()
    index: Dict[Tuple[str, str], Dict] = {}
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            r = await client.get(f"{STATS_BASE}/schedule",
                                 params={"sportId": 1, "startDate": start, "endDate": end,
                                         "hydrate": "probablePitcher,team"})
            r.raise_for_status()
            games = [g for d in r.json().get("dates", []) for g in d.get("games", [])]

            async def _side(team_side):
                team = team_side["team"]["name"]
                bp = bullpens.get(team, 1.0)
                pp = team_side.get("probablePitcher") or {}
                pid = pp.get("id")
                if not pid:
                    return None
                talent, info = await _starter_talent(client, pid, season)
                mult = _game_mult(talent, bp)
                return {"name": info.get("name") or pp.get("fullName"), "mult": mult,
                        "era": info.get("era"), "fip": info.get("fip"),
                        "bullpen": bp, "label": quality_label(mult)}

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
