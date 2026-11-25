"""The leagues the Sports page covers and their Elo settings.

Elo constants follow the published FiveThirtyEight-style setups, scaled for the log
margin-of-victory multiplier used here (ln(margin+1) is ~1 in low-scoring sports and ~2.5 in
basketball, so basketball K is smaller). They are fixed by hand, not tuned on the games they
are later scored on, and the page says the model is unproven.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import FrozenSet, Optional


@dataclass(frozen=True)
class League:
    key: str
    label: str
    path: str                      # ESPN sport/league path
    mode: str                      # "day": calendar of dates; "week": calendar of weeks
    k: float
    hfa: float                     # home-field advantage in Elo points (0 on neutral sites)
    regress: float                 # share pulled back to 1500 between seasons
    draws: bool = False
    groups: Optional[str] = None   # ESPN division filter for college
    # College football: teams outside these conferences (FCS) start lower than 1500.
    top_conferences: FrozenSet[str] = field(default_factory=frozenset)
    other_start: float = 1500.0


FBS = frozenset({"1", "4", "5", "8", "9", "12", "15", "17", "18", "37", "151"})

LEAGUES = {
    "nfl": League("nfl", "NFL", "football/nfl", "week", k=20, hfa=40, regress=1 / 3),
    "nba": League("nba", "NBA", "basketball/nba", "day", k=4, hfa=70, regress=0.25),
    "nhl": League("nhl", "NHL", "hockey/nhl", "day", k=8, hfa=35, regress=0.3),
    "ncaaf": League("ncaaf", "NCAAF", "football/college-football", "week", k=25, hfa=55, regress=0.4,
                    groups="80", top_conferences=FBS, other_start=1200.0),
    "ncaab": League("ncaab", "NCAAB", "basketball/mens-college-basketball", "day", k=10, hfa=80,
                    regress=0.5, groups="50"),
    "epl": League("epl", "Premier League", "soccer/eng.1", "day", k=20, hfa=60, regress=0.2, draws=True),
    "mlb": League("mlb", "MLB", "baseball/mlb", "day", k=3, hfa=24, regress=1 / 3),
}
