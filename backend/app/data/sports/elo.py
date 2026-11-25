"""Elo ratings, pregame win probabilities, and an honest out-of-sample record.

Every probability is taken BEFORE the game updates the ratings, so the season record is a real
forecast record. Where we saw the sportsbook line before kickoff, the same games are scored for
the book too, so the page can say plainly whether Elo beats the market (it usually does not).
"""
from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.data.sports.leagues import League

MEAN = 1500.0
DRAW_BASE, DRAW_SCALE = 0.27, 400.0
MIN_COLLEGE_GAMES = 3


def expected(diff: float) -> float:
    return 1.0 / (1.0 + 10 ** (-diff / 400.0))


def probs(league: League, home: float, away: float, neutral: bool) -> Dict[str, Optional[float]]:
    """Win probabilities for home/away (and draw in soccer) from the two ratings."""
    d = home - away + (0.0 if neutral else league.hfa)
    e = expected(d)
    if not league.draws:
        return {"home": e, "away": 1 - e, "draw": None}
    p_draw = DRAW_BASE * math.exp(-((d / DRAW_SCALE) ** 2))
    p_home = min(max(e - p_draw / 2, 0.01), 0.98)
    p_away = max(1.0 - p_home - p_draw, 0.01)
    s = p_home + p_away + p_draw
    return {"home": p_home / s, "away": p_away / s, "draw": p_draw / s}


def mov_mult(margin: int, winner_diff: float) -> float:
    """FiveThirtyEight's NFL margin multiplier; damps blowouts by heavy favourites."""
    if margin <= 0:
        return 1.0
    return math.log(margin + 1) * 2.2 / (winner_diff * 0.001 + 2.2)


def counts(g: Dict[str, Any], league: League) -> bool:
    """Final, scored, regular season or playoffs (soccer has no preseason type in ESPN's data)."""
    if not g.get("completed") or g["home"]["score"] is None or g["away"]["score"] is None:
        return False
    return league.draws or g.get("season_type") in (2, 3, 5)     # 5 = NBA play-in


def _outcome(g: Dict[str, Any]) -> Optional[str]:
    h, a = g["home"]["score"], g["away"]["score"]
    return "home" if h > a else "away" if a > h else "draw"


def _logloss(p: Dict[str, Optional[float]], outcome: str) -> float:
    return -math.log(max(p.get(outcome) or 0.0, 1e-6))


def _brier(p: Dict[str, Optional[float]], outcome: str) -> float:
    keys = [k for k in ("home", "away", "draw") if p.get(k) is not None]
    return sum(((p[k] or 0.0) - (1.0 if k == outcome else 0.0)) ** 2 for k in keys)


def run(league: League, games: Iterable[Dict[str, Any]], *, current_season: Optional[int],
        week_ago: float) -> Dict[str, Any]:
    """Replay finals in time order. Returns ratings, per-team season records, and the forecast record."""
    ratings: Dict[str, float] = {}
    info: Dict[str, Dict[str, Any]] = {}
    rec: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"w": 0, "l": 0, "d": 0, "pts": 0, "form": []})
    before_week: Dict[str, float] = {}
    season_of_rating: Optional[int] = None
    scored = {"n": 0, "hits": 0, "logloss": 0.0, "brier": 0.0}
    vs_book = {"n": 0, "elo_logloss": 0.0, "book_logloss": 0.0, "elo_brier": 0.0, "book_brier": 0.0}

    def start(side: Dict[str, Any]) -> float:
        if league.top_conferences and side.get("conf") not in league.top_conferences:
            return league.other_start
        return MEAN

    finals = sorted((g for g in games if counts(g, league)), key=lambda g: g["start"])
    for g in finals:
        if g["season"] != season_of_rating:
            if season_of_rating is not None:     # new season: pull everyone part-way back to the mean
                ratings = {t: MEAN + (1 - league.regress) * (r - MEAN) for t, r in ratings.items()}
            season_of_rating = g["season"]
        h, a = g["home"], g["away"]
        for s in (h, a):
            if s["id"] not in ratings:
                ratings[s["id"]] = start(s)
            info[s["id"]] = {"abbr": s["abbr"], "name": s["name"], "short": s.get("short") or s["name"],
                             "rank": s.get("rank"), "conf": s.get("conf")}
        if g["start"] >= week_ago:
            for s in (h, a):
                before_week.setdefault(s["id"], ratings[s["id"]])
        rh, ra = ratings[h["id"]], ratings[a["id"]]
        p = probs(league, rh, ra, g["neutral"])
        out = _outcome(g)
        if out == "draw" and not league.draws:
            continue    # NFL tie: rare, unscored, ratings unchanged
        if g["season"] == current_season:
            scored["n"] += 1
            pick = max(("home", "away", "draw"), key=lambda k: p.get(k) or -1)
            scored["hits"] += pick == out
            scored["logloss"] += _logloss(p, out)
            scored["brier"] += _brier(p, out)
            book = g.get("book")
            if book and book.get("home") is not None and (not league.draws or book.get("draw") is not None):
                vs_book["n"] += 1
                vs_book["elo_logloss"] += _logloss(p, out)
                vs_book["book_logloss"] += _logloss(book, out)
                vs_book["elo_brier"] += _brier(p, out)
                vs_book["book_brier"] += _brier(book, out)
            for side, opp, me in ((h, a, "home"), (a, h, "away")):
                r = rec[side["id"]]
                res = "D" if out == "draw" else "W" if out == me else "L"
                r[{"W": "w", "L": "l", "D": "d"}[res]] += 1
                r["pts"] += {"W": 3, "D": 1, "L": 0}[res]
                r["form"] = (r["form"] + [res])[-5:]
        actual_home = 1.0 if out == "home" else 0.0 if out == "away" else 0.5
        d = rh - ra + (0.0 if g["neutral"] else league.hfa)
        margin = abs(h["score"] - a["score"])
        mult = mov_mult(margin, d if out == "home" else -d) if out != "draw" else 1.0
        shift = league.k * mult * (actual_home - expected(d))
        ratings[h["id"]] = rh + shift
        ratings[a["id"]] = ra - shift

    def avg(total: float, n: int) -> Optional[float]:
        return round(total / n, 4) if n else None

    record = {"n": scored["n"], "hit_rate": avg(scored["hits"], scored["n"]),
              "logloss": avg(scored["logloss"], scored["n"]), "brier": avg(scored["brier"], scored["n"]),
              "vs_book": {"n": vs_book["n"], "elo_logloss": avg(vs_book["elo_logloss"], vs_book["n"]),
                          "book_logloss": avg(vs_book["book_logloss"], vs_book["n"]),
                          "elo_brier": avg(vs_book["elo_brier"], vs_book["n"]),
                          "book_brier": avg(vs_book["book_brier"], vs_book["n"])}}
    return {"ratings": ratings, "info": info, "records": dict(rec), "before_week": before_week,
            "record": record, "rating_season": season_of_rating}


def table(league: League, result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Power ratings for teams that played this season, best first."""
    rows: List[Tuple[float, Dict[str, Any]]] = []
    for tid, r in result["records"].items():
        elo = result["ratings"][tid]
        i = result["info"][tid]
        if league.top_conferences and i.get("conf") not in league.top_conferences:
            continue        # FCS opponents of FBS teams: rated, but not ranked in the table
        if league.groups and not league.top_conferences and r["w"] + r["l"] < MIN_COLLEGE_GAMES:
            continue        # non-D1 opponents appear once or twice a season
        row = {"id": tid, "abbr": i["abbr"], "name": i["name"], "short": i["short"], "rank": i.get("rank"),
               "elo": round(elo, 1), "w": r["w"], "l": r["l"], "form": "".join(r["form"]),
               "change_7d": round(elo - result["before_week"].get(tid, elo), 1)}
        if league.draws:
            row.update(d=r["d"], pts=r["pts"])
        rows.append((elo, row))
    rows.sort(key=lambda x: -x[0])
    return [dict(row, pos=i + 1) for i, (_, row) in enumerate(rows)]
