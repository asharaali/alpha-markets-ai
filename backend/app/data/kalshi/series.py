"""Which Kalshi NFL series we cover, and how to read each one's contracts.

Kalshi lists over 300 NFL series. Most are season-long futures, awards and novelty markets
that a game-level model has no opinion on. This registry is the deliberate subset the
models can actually price, and the parser that turns each contract's subtitle into
structured (market type, team/player, line).

Every parser returns None rather than guessing. An unparsed contract is reported as
unmapped and shown nowhere — silently mis-reading "Seattle wins by over 13.5" as a
moneyline would be far worse than skipping it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from app.core.types import MarketType
from app.data import teams


@dataclass
class ParsedContract:
    market_type: MarketType
    label: str                    # human-readable, e.g. "Seahawks by 14+"
    selection: str                # the side being bought, for logs and cards
    team: Optional[str] = None
    player: Optional[str] = None
    line: Optional[float] = None


@dataclass
class SeriesSpec:
    ticker: str
    market_type: MarketType
    category: str                 # Game Line | Game Prop | Player Prop | Segment
    label: str
    parse: Callable[[str, str, str], Optional[ParsedContract]]
    # Player props are priced for REFERENCE only unless the projection layer can confirm
    # the player's role — see app.strategies.props for why.
    reference_only: bool = False


_NUM = r"(\d+(?:\.\d+)?)"


def _team_in(text: str, away: str, home: str, *, exact: bool = False) -> Optional[str]:
    """Resolve a team named in a subtitle, constrained to this game's two teams.

    `exact=True` requires the subtitle to be nothing BUT the team name. Use it wherever the
    contract's meaning is carried entirely by the team — a moneyline, a half winner —
    because a substring match there would read "Seattle wins by over 13.5 points" as a
    moneyline on Seattle and silently price a spread as a straight win.
    """
    cleaned = (text or "").strip()
    resolved = teams.resolve(cleaned)
    if resolved in (away, home):
        return resolved
    if exact:
        return None
    lowered = cleaned.lower()
    for abbr in (away, home):
        team = teams.get(abbr)
        if team and (team.name.lower() in lowered
                     or team.full_name.lower() in lowered
                     or cleaned.upper() == abbr):
            return abbr
    return None


# ----------------------------------------------------------------- parsers

def _parse_moneyline(sub: str, away: str, home: str) -> Optional[ParsedContract]:
    # Exact match only: a moneyline contract's subtitle is the team name and nothing else.
    team = _team_in(sub, away, home, exact=True)
    if not team:
        return None
    return ParsedContract(MarketType.MONEYLINE, f"{teams.display(team)} to win",
                          selection=teams.display(team), team=team)


_SPREAD_RE = re.compile(rf"^(.*?)\s+wins by over\s+{_NUM}\s+points?$", re.I)


def _parse_spread(sub: str, away: str, home: str) -> Optional[ParsedContract]:
    m = _SPREAD_RE.match(sub.strip())
    if not m:
        return None
    team = _team_in(m.group(1), away, home)
    if not team:
        return None
    line = float(m.group(2))
    return ParsedContract(MarketType.SPREAD,
                          f"{teams.display(team)} by more than {line:g}",
                          selection=f"{teams.display(team)} -{line:g}",
                          team=team, line=line)


_TOTAL_RE = re.compile(rf"^Over\s+{_NUM}\s+points scored$", re.I)
_1H_TOTAL_RE = re.compile(rf"^Over\s+{_NUM}\s+1H points scored$", re.I)


def _parse_total(sub: str, away: str, home: str) -> Optional[ParsedContract]:
    m = _TOTAL_RE.match(sub.strip())
    if not m:
        return None
    line = float(m.group(1))
    return ParsedContract(MarketType.TOTAL, f"Total over {line:g}",
                          selection=f"Over {line:g}", line=line)


def _parse_first_half_total(sub: str, away: str, home: str) -> Optional[ParsedContract]:
    m = _1H_TOTAL_RE.match(sub.strip())
    if not m:
        return None
    line = float(m.group(1))
    return ParsedContract(MarketType.FIRST_HALF_TOTAL, f"1st half over {line:g}",
                          selection=f"1H Over {line:g}", line=line)


_TEAM_TOTAL_RE = re.compile(rf"^(.*?)\s+over\s+{_NUM}\s+points scored$", re.I)


def _parse_team_total(sub: str, away: str, home: str) -> Optional[ParsedContract]:
    m = _TEAM_TOTAL_RE.match(sub.strip())
    if not m:
        return None
    team = _team_in(m.group(1), away, home)
    if not team:
        return None
    line = float(m.group(2))
    return ParsedContract(MarketType.TEAM_TOTAL,
                          f"{teams.display(team)} over {line:g} points",
                          selection=f"{teams.display(team)} Over {line:g}",
                          team=team, line=line)


_MARGIN_BAND_RE = re.compile(r"^(.*?)\s+wins by\s+(\d+)\s+to\s+(\d+)\s+points?$", re.I)
_MARGIN_PLUS_RE = re.compile(r"^(.*?)\s+wins by\s+(\d+)\s+or more points?$", re.I)


def _parse_win_margin(sub: str, away: str, home: str) -> Optional[ParsedContract]:
    text = sub.strip()
    if text.lower() == "tie":
        return ParsedContract(MarketType.WIN_MARGIN, "Game ends in a tie",
                              selection="Tie", line=0.0)
    m = _MARGIN_BAND_RE.match(text)
    if m:
        team = _team_in(m.group(1), away, home)
        if not team:
            return None
        lo, hi = int(m.group(2)), int(m.group(3))
        return ParsedContract(MarketType.WIN_MARGIN,
                              f"{teams.display(team)} by {lo}-{hi}",
                              selection=f"{teams.display(team)} {lo}-{hi}",
                              team=team, line=float(lo))
    m = _MARGIN_PLUS_RE.match(text)
    if m:
        team = _team_in(m.group(1), away, home)
        if not team:
            return None
        lo = int(m.group(2))
        return ParsedContract(MarketType.WIN_MARGIN,
                              f"{teams.display(team)} by {lo}+",
                              selection=f"{teams.display(team)} {lo}+",
                              team=team, line=float(lo))
    return None


# "Sam Darnold: 225+" — every player prop series shares this shape.
_PLAYER_RE = re.compile(rf"^(.+?):\s*{_NUM}\+$")


def _player_parser(market_type: MarketType, noun: str
                   ) -> Callable[[str, str, str], Optional[ParsedContract]]:
    def parse(sub: str, away: str, home: str) -> Optional[ParsedContract]:
        m = _PLAYER_RE.match(sub.strip())
        if not m:
            return None
        player = m.group(1).strip()
        line = float(m.group(2))
        return ParsedContract(market_type, f"{player} {line:g}+ {noun}",
                              selection=f"{player} {line:g}+ {noun}",
                              player=player, line=line)
    return parse


def _parse_first_half_winner(sub: str, away: str, home: str) -> Optional[ParsedContract]:
    team = _team_in(sub, away, home, exact=True)
    if not team:
        return None
    return ParsedContract(MarketType.FIRST_HALF_WINNER,
                          f"{teams.display(team)} to lead at half",
                          selection=f"{teams.display(team)} 1H", team=team)


# ----------------------------------------------------------------- registry

SERIES: Dict[str, SeriesSpec] = {
    "KXNFLGAME": SeriesSpec("KXNFLGAME", MarketType.MONEYLINE, "Game Line",
                            "Moneyline", _parse_moneyline),
    "KXNFLSPREAD": SeriesSpec("KXNFLSPREAD", MarketType.SPREAD, "Game Line",
                              "Spread", _parse_spread),
    "KXNFLTOTAL": SeriesSpec("KXNFLTOTAL", MarketType.TOTAL, "Game Line",
                             "Total Points", _parse_total),
    "KXNFLTEAMTOTAL": SeriesSpec("KXNFLTEAMTOTAL", MarketType.TEAM_TOTAL, "Game Line",
                                 "Team Total", _parse_team_total),
    "KXNFLWINMARGIN": SeriesSpec("KXNFLWINMARGIN", MarketType.WIN_MARGIN, "Game Prop",
                                 "Winning Margin", _parse_win_margin),
    "KXNFL1HTOTAL": SeriesSpec("KXNFL1HTOTAL", MarketType.FIRST_HALF_TOTAL, "Segment",
                               "1st Half Total", _parse_first_half_total),
    "KXNFL1HWINNER": SeriesSpec("KXNFL1HWINNER", MarketType.FIRST_HALF_WINNER, "Segment",
                                "1st Half Winner", _parse_first_half_winner),
    "KXNFLPASSYDS": SeriesSpec("KXNFLPASSYDS", MarketType.PASS_YARDS, "Player Prop",
                               "Passing Yards",
                               _player_parser(MarketType.PASS_YARDS, "pass yds"),
                               reference_only=True),
    "KXNFLPASSTDS": SeriesSpec("KXNFLPASSTDS", MarketType.PASS_TDS, "Player Prop",
                               "Passing TDs",
                               _player_parser(MarketType.PASS_TDS, "pass TD"),
                               reference_only=True),
    "KXNFLRSHYDS": SeriesSpec("KXNFLRSHYDS", MarketType.RUSH_YARDS, "Player Prop",
                              "Rushing Yards",
                              _player_parser(MarketType.RUSH_YARDS, "rush yds"),
                              reference_only=True),
    "KXNFLRECYDS": SeriesSpec("KXNFLRECYDS", MarketType.RECV_YARDS, "Player Prop",
                              "Receiving Yards",
                              _player_parser(MarketType.RECV_YARDS, "rec yds"),
                              reference_only=True),
    "KXNFLREC": SeriesSpec("KXNFLREC", MarketType.RECEPTIONS, "Player Prop",
                           "Receptions",
                           _player_parser(MarketType.RECEPTIONS, "rec"),
                           reference_only=True),
    "KXNFLANYTD": SeriesSpec("KXNFLANYTD", MarketType.ANYTIME_TD, "Player Prop",
                             "Anytime Touchdown",
                             _player_parser(MarketType.ANYTIME_TD, "TD"),
                             reference_only=True),
}

GAME_LINE_SERIES = [s.ticker for s in SERIES.values() if s.category == "Game Line"]
PLAYER_PROP_SERIES = [s.ticker for s in SERIES.values() if s.category == "Player Prop"]


def spec(series_ticker: str) -> Optional[SeriesSpec]:
    return SERIES.get(series_ticker.upper())


def series_for(market_type: MarketType) -> Optional[str]:
    for s in SERIES.values():
        if s.market_type is market_type:
            return s.ticker
    return None


def all_specs() -> List[SeriesSpec]:
    return list(SERIES.values())
