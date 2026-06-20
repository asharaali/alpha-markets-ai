"""
Player props — anytime goalscorer model.

Built from 47k historical international goals. For each team we learn each player's
share of the team's goals (recent era, so it's mostly current internationals). In a
given match we split the team's expected goals across those players by their share,
then a Poisson gives P(player scores at least once).

Honest limits: this is historical scoring rate only. It does NOT know today's lineup,
injuries, suspensions, or who's actually starting. Treat it as "who tends to score for
this team," not a guaranteed starter list. Lineups are the next data upgrade.
"""
from __future__ import annotations
import csv
import math
from collections import defaultdict
from pathlib import Path
from typing import Dict, List

_DATA = Path(__file__).parent.parent / "training" / "data" / "goalscorers.csv"
_SINCE = "2021-01-01"  # recent era => mostly current squads

# Same canonicalisation as the trainer, so keys match the live feed's team names.
_NAME_MAP = {
    "United States": "USA", "Republic of Ireland": "Ireland", "Czechia": "Czech Republic",
    "Türkiye": "Turkey", "Bosnia and Herzegovina": "Bosnia & Herzegovina",
    "Curacao": "Curaçao", "South Korea": "South Korea", "DR Congo": "DR Congo",
}


def _canon(n: str) -> str:
    return _NAME_MAP.get(n, n)


# team -> {player: goals}, team -> total goals  (loaded once at import)
_TEAM_SCORERS: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
_TEAM_TOTAL: Dict[str, int] = defaultdict(int)
LOADED = False


def _load():
    global LOADED
    if LOADED or not _DATA.exists():
        return
    with open(_DATA, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r.get("date", "") < _SINCE:
                continue
            if r.get("own_goal", "").upper() == "TRUE":
                continue
            scorer = (r.get("scorer") or "").strip()
            if not scorer:
                continue
            team = _canon((r.get("team") or "").strip())
            _TEAM_SCORERS[team][scorer] += 1
            _TEAM_TOTAL[team] += 1
    LOADED = True


_load()


def anytime_scorers(team: str, team_expected_goals: float, top_n: int = 6) -> List[Dict]:
    """Top likely scorers for `team` in a match where it's expected to score ~team_expected_goals."""
    scorers = _TEAM_SCORERS.get(team)
    total = _TEAM_TOTAL.get(team, 0)
    if not scorers or total == 0:
        return []
    out = []
    for name, goals in scorers.items():
        share = goals / total
        lam = share * team_expected_goals          # this player's expected goals in the match
        prob = 1 - math.exp(-lam)                   # P(scores at least once)
        out.append({
            "player": name,
            "team": team,
            "prob": round(prob, 4),
            "fair_odds": round(1 / prob, 2) if prob > 0 else None,
            "goals_since_2021": int(goals),
        })
    out.sort(key=lambda x: x["prob"], reverse=True)
    return out[:top_n]
