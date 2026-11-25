"""ESPN's public scoreboard JSON (unofficial, no key) reduced to the few fields we use.

A day of college basketball is ~1.4 MB of JSON; we keep ~300 bytes per game. Range queries
(`dates=A-B`) are refused by ESPN, so the refresh job fetches one date or one week at a time.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.core.http import make_client, request
from app.data.sports.leagues import League

BASE = "https://site.api.espn.com/apis/site/v2/sports"
SOURCE = "ESPN (unofficial)"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}


def _epoch(iso: Optional[str]) -> Optional[float]:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def american_to_prob(odds: Any) -> Optional[float]:
    """'-297' -> 0.748, '+241' -> 0.293. 'EVEN' -> 0.5. Anything else -> None."""
    if odds is None:
        return None
    s = str(odds).strip().upper()
    if s in ("EVEN", "EV", "PK"):
        return 0.5
    try:
        v = float(s.replace("+", ""))
    except ValueError:
        return None
    if not math.isfinite(v) or abs(v) < 100:
        return None
    return 100 / (v + 100) if v > 0 else -v / (-v + 100)


def book_probs(odds_list: Any) -> Optional[Dict[str, Any]]:
    """No-vig win probabilities from the sportsbook moneyline ESPN shows (closing line, else open)."""
    if not isinstance(odds_list, list) or not odds_list:
        return None
    o = odds_list[0] or {}
    ml = o.get("moneyline") or {}
    raw = {}
    for side in ("home", "away", "draw"):
        leg = ml.get(side) or {}
        raw[side] = american_to_prob((leg.get("close") or {}).get("odds") or (leg.get("open") or {}).get("odds"))
    if raw["home"] is None or raw["away"] is None:
        return None
    total = raw["home"] + raw["away"] + (raw["draw"] or 0.0)
    if total <= 0:
        return None
    return {"home": raw["home"] / total, "away": raw["away"] / total,
            "draw": (raw["draw"] / total) if raw["draw"] is not None else None,
            "detail": o.get("details"), "provider": (o.get("provider") or {}).get("name")}


def _int(v: Any) -> Optional[int]:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _side(c: Dict[str, Any]) -> Dict[str, Any]:
    t = c.get("team") or {}
    rec = next((r.get("summary") for r in c.get("records") or [] if r.get("type") == "total"), None)
    rank = _int((c.get("curatedRank") or {}).get("current"))
    return {"id": str(t.get("id") or ""), "abbr": t.get("abbreviation") or "", "name": t.get("displayName") or "",
            "short": t.get("shortDisplayName") or t.get("displayName") or "",
            "score": _int(c.get("score")), "record": rec, "rank": rank if rank and rank < 99 else None,
            "conf": t.get("conferenceId"), "winner": c.get("winner")}


def parse_event(e: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    comp = (e.get("competitions") or [None])[0]
    if not comp:
        return None
    sides = {c.get("homeAway"): c for c in comp.get("competitors") or []}
    if "home" not in sides or "away" not in sides:
        return None
    st = (comp.get("status") or e.get("status") or {}).get("type") or {}
    season = e.get("season") or {}
    g = {"id": str(e.get("id")), "start": _epoch(e.get("date")), "state": st.get("state") or "pre",
         "status": st.get("name") or "", "completed": bool(st.get("completed")),
         "detail": st.get("shortDetail") or st.get("detail") or "",
         "season": _int(season.get("year")), "season_type": _int(season.get("type")),
         "neutral": bool(comp.get("neutralSite")),
         "home": _side(sides["home"]), "away": _side(sides["away"]), "book": None}
    if g["state"] == "pre":
        g["book"] = book_probs(comp.get("odds"))
    return g


def parse_scoreboard(payload: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    games = [g for g in (parse_event(e) for e in payload.get("events") or []) if g and g["start"]]
    lg = (payload.get("leagues") or [{}])[0]
    season = lg.get("season") or {}
    meta = {"season": _int(season.get("year")), "season_name": season.get("displayName"),
            "start": _epoch(season.get("startDate")), "end": _epoch(season.get("endDate")),
            "calendar": _calendar(lg.get("calendar") or [])}
    return games, meta


def _calendar(cal: List[Any]) -> List[Any]:
    """Day leagues: ['20261003', ...]. Week leagues: [[seasontype, week, start_epoch], ...], preseason dropped."""
    out: List[Any] = []
    for item in cal:
        if isinstance(item, str):
            out.append(item[:10].replace("-", ""))
        elif isinstance(item, dict) and item.get("entries"):
            stype = _int(item.get("value"))
            if stype == 1:
                continue
            for wk in item["entries"]:
                out.append([stype, _int(wk.get("value")), _epoch(wk.get("startDate"))])
    return out


def params_for(league: League, unit: Optional[str]) -> Dict[str, Any]:
    """unit: None (ESPN's current day/week), 'd:YYYYMMDD', 'w:YEAR:TYPE:WEEK' or 'y:YEAR' (season meta)."""
    p: Dict[str, Any] = {"limit": 500}
    if league.groups:
        p["groups"] = league.groups
    if unit is None:
        return p
    kind, _, rest = unit.partition(":")
    if kind == "d":
        p["dates"] = rest
    elif kind == "w":
        year, stype, week = rest.split(":")
        p.update(dates=year, seasontype=stype, week=week)
    elif kind == "y":
        p["dates"] = rest
    return p


async def fetch(league: League, unit: Optional[str], client: Optional[httpx.AsyncClient] = None) -> Dict[str, Any]:
    url = f"{BASE}/{league.path}/scoreboard"
    own = client is None
    c = client or make_client(timeout=25, headers=UA)
    try:
        r = await request(c, "GET", url, params=params_for(league, unit), attempts=3, source="espn")
        r.raise_for_status()
        return r.json()
    finally:
        if own:
            await c.aclose()
