"""Canonical NFL team registry and name resolution.

Every venue spells teams differently — nflverse uses LA/JAX, Kalshi uses LAR/JAC and
display names like "Los Angeles R", the Odds API uses full names like "Los Angeles Rams".
One team getting mapped wrong means a market silently never matches a game, so all name
resolution funnels through `resolve()` here.

Coordinates and timezones come from nflverse/nfldata's airports.csv (team travel origin);
divisions, stadium names and default roof are stable reference data. The per-GAME roof
from the schedule always wins over the default here — teams play neutral-site and
international games where the home stadium's roof is irrelevant.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional

from app.core.types import Team

TEAMS: Dict[str, Team] = {
    "ARI": Team(abbr="ARI", name="Cardinals", full_name="Arizona Cardinals",
        city="Arizona", conference="NFC", division="NFC West",
        stadium="State Farm Stadium", roof="dome",
        surface="turf", lat=33.434, lon=-112.008,
        timezone="America/Phoenix"),
    "ATL": Team(abbr="ATL", name="Falcons", full_name="Atlanta Falcons",
        city="Atlanta", conference="NFC", division="NFC South",
        stadium="Mercedes-Benz Stadium", roof="dome",
        surface="turf", lat=33.64, lon=-84.427,
        timezone="America/New_York"),
    "BAL": Team(abbr="BAL", name="Ravens", full_name="Baltimore Ravens",
        city="Baltimore", conference="AFC", division="AFC North",
        stadium="M&T Bank Stadium", roof="outdoors",
        surface="turf", lat=39.175, lon=-76.668,
        timezone="America/New_York"),
    "BUF": Team(abbr="BUF", name="Bills", full_name="Buffalo Bills",
        city="Buffalo", conference="AFC", division="AFC East",
        stadium="Highmark Stadium", roof="outdoors",
        surface="grass", lat=42.94, lon=-78.732,
        timezone="America/New_York"),
    "CAR": Team(abbr="CAR", name="Panthers", full_name="Carolina Panthers",
        city="Carolina", conference="NFC", division="NFC South",
        stadium="Bank of America Stadium", roof="outdoors",
        surface="grass", lat=35.214, lon=-80.943,
        timezone="America/New_York"),
    "CHI": Team(abbr="CHI", name="Bears", full_name="Chicago Bears",
        city="Chicago", conference="NFC", division="NFC North",
        stadium="Soldier Field", roof="outdoors",
        surface="grass", lat=41.979, lon=-87.904,
        timezone="America/Chicago"),
    "CIN": Team(abbr="CIN", name="Bengals", full_name="Cincinnati Bengals",
        city="Cincinnati", conference="AFC", division="AFC North",
        stadium="Paycor Stadium", roof="outdoors",
        surface="turf", lat=39.046, lon=-84.662,
        timezone="America/New_York"),
    "CLE": Team(abbr="CLE", name="Browns", full_name="Cleveland Browns",
        city="Cleveland", conference="AFC", division="AFC North",
        stadium="Huntington Bank Field", roof="outdoors",
        surface="turf", lat=41.412, lon=-81.85,
        timezone="America/New_York"),
    "DAL": Team(abbr="DAL", name="Cowboys", full_name="Dallas Cowboys",
        city="Dallas", conference="NFC", division="NFC East",
        stadium="AT&T Stadium", roof="dome",
        surface="turf", lat=32.896, lon=-97.037,
        timezone="America/Chicago"),
    "DEN": Team(abbr="DEN", name="Broncos", full_name="Denver Broncos",
        city="Denver", conference="AFC", division="AFC West",
        stadium="Empower Field at Mile High", roof="outdoors",
        surface="grass", lat=39.858, lon=-104.667,
        timezone="America/Denver"),
    "DET": Team(abbr="DET", name="Lions", full_name="Detroit Lions",
        city="Detroit", conference="NFC", division="NFC North",
        stadium="Ford Field", roof="dome",
        surface="turf", lat=42.212, lon=-83.353,
        timezone="America/New_York"),
    "GB": Team(abbr="GB", name="Packers", full_name="Green Bay Packers",
        city="Green Bay", conference="NFC", division="NFC North",
        stadium="Lambeau Field", roof="outdoors",
        surface="grass", lat=44.485, lon=-88.129,
        timezone="America/Chicago"),
    "HOU": Team(abbr="HOU", name="Texans", full_name="Houston Texans",
        city="Houston", conference="AFC", division="AFC South",
        stadium="NRG Stadium", roof="dome",
        surface="turf", lat=29.98, lon=-95.34,
        timezone="America/Chicago"),
    "IND": Team(abbr="IND", name="Colts", full_name="Indianapolis Colts",
        city="Indianapolis", conference="AFC", division="AFC South",
        stadium="Lucas Oil Stadium", roof="dome",
        surface="turf", lat=39.717, lon=-86.294,
        timezone="America/New_York"),
    "JAX": Team(abbr="JAX", name="Jaguars", full_name="Jacksonville Jaguars",
        city="Jacksonville", conference="AFC", division="AFC South",
        stadium="EverBank Stadium", roof="outdoors",
        surface="grass", lat=30.494, lon=-81.688,
        timezone="America/New_York"),
    "KC": Team(abbr="KC", name="Chiefs", full_name="Kansas City Chiefs",
        city="Kansas City", conference="AFC", division="AFC West",
        stadium="GEHA Field at Arrowhead Stadium", roof="outdoors",
        surface="grass", lat=39.297, lon=-94.714,
        timezone="America/Chicago"),
    "LA": Team(abbr="LA", name="Rams", full_name="Los Angeles Rams",
        city="LA Rams", conference="NFC", division="NFC West",
        stadium="SoFi Stadium", roof="dome",
        surface="turf", lat=33.942, lon=-118.408,
        timezone="America/Los_Angeles"),
    "LAC": Team(abbr="LAC", name="Chargers", full_name="Los Angeles Chargers",
        city="LA Chargers", conference="AFC", division="AFC West",
        stadium="SoFi Stadium", roof="dome",
        surface="turf", lat=33.942, lon=-118.408,
        timezone="America/Los_Angeles"),
    "LV": Team(abbr="LV", name="Raiders", full_name="Las Vegas Raiders",
        city="Las Vegas", conference="AFC", division="AFC West",
        stadium="Allegiant Stadium", roof="dome",
        surface="turf", lat=36.08, lon=-115.152,
        timezone="America/Los_Angeles"),
    "MIA": Team(abbr="MIA", name="Dolphins", full_name="Miami Dolphins",
        city="Miami", conference="AFC", division="AFC East",
        stadium="Hard Rock Stadium", roof="outdoors",
        surface="grass", lat=25.793, lon=-80.291,
        timezone="America/New_York"),
    "MIN": Team(abbr="MIN", name="Vikings", full_name="Minnesota Vikings",
        city="Minnesota", conference="NFC", division="NFC North",
        stadium="U.S. Bank Stadium", roof="dome",
        surface="turf", lat=44.88, lon=-93.217,
        timezone="America/Chicago"),
    "NE": Team(abbr="NE", name="Patriots", full_name="New England Patriots",
        city="New England", conference="AFC", division="AFC East",
        stadium="Gillette Stadium", roof="outdoors",
        surface="turf", lat=41.724, lon=-71.428,
        timezone="America/New_York"),
    "NO": Team(abbr="NO", name="Saints", full_name="New Orleans Saints",
        city="New Orleans", conference="NFC", division="NFC South",
        stadium="Caesars Superdome", roof="dome",
        surface="turf", lat=29.993, lon=-90.258,
        timezone="America/Chicago"),
    "NYG": Team(abbr="NYG", name="Giants", full_name="New York Giants",
        city="NY Giants", conference="NFC", division="NFC East",
        stadium="MetLife Stadium", roof="outdoors",
        surface="turf", lat=40.692, lon=-74.169,
        timezone="America/New_York"),
    "NYJ": Team(abbr="NYJ", name="Jets", full_name="New York Jets",
        city="NY Jets", conference="AFC", division="AFC East",
        stadium="MetLife Stadium", roof="outdoors",
        surface="turf", lat=40.692, lon=-74.169,
        timezone="America/New_York"),
    "PHI": Team(abbr="PHI", name="Eagles", full_name="Philadelphia Eagles",
        city="Philadelphia", conference="NFC", division="NFC East",
        stadium="Lincoln Financial Field", roof="outdoors",
        surface="grass", lat=39.872, lon=-75.241,
        timezone="America/New_York"),
    "PIT": Team(abbr="PIT", name="Steelers", full_name="Pittsburgh Steelers",
        city="Pittsburgh", conference="AFC", division="AFC North",
        stadium="Acrisure Stadium", roof="outdoors",
        surface="grass", lat=40.491, lon=-80.233,
        timezone="America/New_York"),
    "SEA": Team(abbr="SEA", name="Seahawks", full_name="Seattle Seahawks",
        city="Seattle", conference="NFC", division="NFC West",
        stadium="Lumen Field", roof="outdoors",
        surface="turf", lat=47.449, lon=-122.309,
        timezone="America/Los_Angeles"),
    "SF": Team(abbr="SF", name="49ers", full_name="San Francisco 49ers",
        city="San Francisco", conference="NFC", division="NFC West",
        stadium="Levi's Stadium", roof="outdoors",
        surface="grass", lat=37.619, lon=-122.375,
        timezone="America/Los_Angeles"),
    "TB": Team(abbr="TB", name="Buccaneers", full_name="Tampa Bay Buccaneers",
        city="Tampa Bay", conference="NFC", division="NFC South",
        stadium="Raymond James Stadium", roof="outdoors",
        surface="grass", lat=27.975, lon=-82.533,
        timezone="America/New_York"),
    "TEN": Team(abbr="TEN", name="Titans", full_name="Tennessee Titans",
        city="Tennessee", conference="AFC", division="AFC South",
        stadium="Nissan Stadium", roof="outdoors",
        surface="grass", lat=36.124, lon=-86.678,
        timezone="America/Chicago"),
    "WAS": Team(abbr="WAS", name="Commanders", full_name="Washington Commanders",
        city="Washington", conference="NFC", division="NFC East",
        stadium="Northwest Stadium", roof="outdoors",
        surface="grass", lat=38.852, lon=-77.037,
        timezone="America/New_York"),
}


ALL_ABBRS: List[str] = sorted(TEAMS)

# Non-canonical spellings we accept, mapped to the nflverse abbreviation we standardise on.
# Kalshi tickers use LAR/JAC; older feeds use SD/OAK/STL/WSH; display names vary by venue.
_ALIASES: Dict[str, str] = {
    "LAR": "LA", "RAMS": "LA", "LOS ANGELES R": "LA", "LOS ANGELES RAMS": "LA",
    "ST LOUIS": "LA", "STL": "LA",
    "LOS ANGELES C": "LAC", "LOS ANGELES CHARGERS": "LAC", "SD": "LAC",
    "SAN DIEGO": "LAC", "CHARGERS": "LAC",
    "JAC": "JAX", "JACKSONVILLE": "JAX", "JAGUARS": "JAX",
    "WSH": "WAS", "WFT": "WAS", "WASHINGTON": "WAS", "COMMANDERS": "WAS",
    "OAK": "LV", "OAKLAND": "LV", "RAIDERS": "LV", "LAS VEGAS": "LV",
    "NEW YORK G": "NYG", "NY GIANTS": "NYG", "N.Y. GIANTS": "NYG", "GIANTS": "NYG",
    "NEW YORK J": "NYJ", "NY JETS": "NYJ", "N.Y. JETS": "NYJ", "JETS": "NYJ",
    "GNB": "GB", "GREEN BAY": "GB", "PACKERS": "GB",
    "KAN": "KC", "KANSAS CITY": "KC", "CHIEFS": "KC",
    "NWE": "NE", "NEW ENGLAND": "NE", "PATRIOTS": "NE",
    "NOR": "NO", "NEW ORLEANS": "NO", "SAINTS": "NO",
    "SFO": "SF", "SAN FRANCISCO": "SF", "49ERS": "SF", "NINERS": "SF",
    "TAM": "TB", "TAMPA BAY": "TB", "BUCCANEERS": "TB", "BUCS": "TB",
    "ARZ": "ARI", "CRD": "ARI", "ARIZONA": "ARI", "CARDINALS": "ARI",
    "BLT": "BAL", "RAV": "BAL", "BALTIMORE": "BAL", "RAVENS": "BAL",
    "CLV": "CLE", "CLEVELAND": "CLE", "BROWNS": "CLE",
    "HST": "HOU", "HTX": "HOU", "HOUSTON": "HOU", "TEXANS": "HOU",
    "CLT": "IND", "INDIANAPOLIS": "IND", "COLTS": "IND",
    "OTI": "TEN", "TENNESSEE": "TEN", "TITANS": "TEN",
    "RAI": "LV", "SEAHAWKS": "SEA", "SEATTLE": "SEA",
}

# Built once: every unambiguous spelling of every team -> its abbreviation.
_LOOKUP: Dict[str, str] = {}
for _abbr, _t in TEAMS.items():
    for _key in (_abbr, _t.name, _t.full_name, _t.city, f"{_t.city} {_t.name}"):
        _LOOKUP[_key.upper()] = _abbr
_LOOKUP.update(_ALIASES)


def resolve(name: Optional[str]) -> Optional[str]:
    """Any known spelling of a team -> its canonical abbreviation, or None.

    Returns None rather than guessing: an unmatched market must be reported as unmatched,
    never quietly attached to the wrong game.
    """
    if not name:
        return None
    key = " ".join(str(name).strip().upper().replace(".", "").split())
    hit = _LOOKUP.get(key)
    if hit:
        return hit
    # Kalshi appends a one-letter disambiguator to shared-city teams ("Los Angeles R"), so
    # allow a prefix match with at most a couple of trailing characters. The bound matters:
    # an unbounded prefix match reads "Seattle wins by over 13.5 points" as the Seahawks,
    # which would silently price a spread contract as a moneyline.
    for candidate, abbr in sorted(_LOOKUP.items(), key=lambda kv: -len(kv[0])):
        if len(candidate) >= 6 and key.startswith(candidate) and len(key) - len(candidate) <= 2:
            return abbr
    return None


def get(abbr: Optional[str]) -> Optional[Team]:
    if not abbr:
        return None
    return TEAMS.get(resolve(abbr) or "")


def require(abbr: str) -> Team:
    team = get(abbr)
    if team is None:
        from app.core.errors import NotFound
        raise NotFound(f"unknown team: {abbr!r}")
    return team


def display(abbr: Optional[str]) -> str:
    team = get(abbr)
    return team.name if team else (abbr or "?")


def is_divisional(home: str, away: str) -> bool:
    h, a = get(home), get(away)
    return bool(h and a and h.division == a.division)


def is_conference(home: str, away: str) -> bool:
    h, a = get(home), get(away)
    return bool(h and a and h.conference == a.conference)


_EARTH_MILES = 3958.8


def travel_miles(from_team: str, to_team: str) -> float:
    """Great-circle distance between two teams' home cities, in miles."""
    a, b = get(from_team), get(to_team)
    if not a or not b:
        return 0.0
    lat1, lon1, lat2, lon2 = map(math.radians, (a.lat, a.lon, b.lat, b.lon))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return round(2 * _EARTH_MILES * math.asin(math.sqrt(h)), 1)


_TZ_OFFSET = {"America/New_York": 0, "America/Chicago": -1,
              "America/Denver": -2, "America/Phoenix": -2,
              "America/Los_Angeles": -3}


def timezone_shift(from_team: str, to_team: str) -> int:
    """Hours of body-clock shift a team absorbs travelling to an opponent (signed).

    Negative = travelling west (later body clock); positive = travelling east, which is the
    direction the research consistently finds harder, especially for late kickoffs.
    """
    a, b = get(from_team), get(to_team)
    if not a or not b:
        return 0
    return _TZ_OFFSET.get(b.timezone, 0) - _TZ_OFFSET.get(a.timezone, 0)


def to_dict(abbr: str) -> Dict[str, object]:
    t = require(abbr)
    return {
        "abbr": t.abbr, "name": t.name, "full_name": t.full_name, "city": t.city,
        "conference": t.conference, "division": t.division, "stadium": t.stadium,
        "roof": t.roof, "surface": t.surface, "timezone": t.timezone,
    }
