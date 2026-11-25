"""Sports refresh job: fetch ESPN a little at a time, keep finished days on disk, rebuild blobs.

Per run (every 5 min) each league gets ESPN's current scoreboard (live scores + calendar), then
the run spends a small shared budget on dates/weeks not yet stored: this season first, then last
season (Elo needs it so ratings are not all 1500 in November). The first full backfill takes a few
hours by design; Render has half a CPU. A finished, past unit is never fetched again.

The request path only reads the blobs written here.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from app.config import settings
from app.core.http import make_client
from app.data.sports import elo, espn
from app.data.sports.leagues import LEAGUES, League

DIR = Path(settings.CACHE_DIR) / "sports"
ET = ZoneInfo("America/New_York")
FETCH_BUDGET = 24              # backfill fetches per run, shared by all leagues
FUTURE_DAYS = 3                # upcoming window shown on the page
FUTURE_REFETCH_S = 1800       # upcoming days: lines move slowly
RECENT_REFETCH_S = 900         # today / yesterday / this week (live scores also come via step 1)
UPCOMING_LIMIT = 120


def _err(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:200]


def _write(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".part{os.getpid()}")
    tmp.write_text(json.dumps(obj, separators=(",", ":")))
    tmp.replace(path)


def _read(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def store_path(key: str) -> Path:
    return DIR / f"{key}.store.json"


def blob_path(key: str) -> Path:
    return DIR / f"{key}.json"


INDEX = DIR / "index.json"


# ---- units: which dates / weeks a season is made of --------------------------------------

def _day(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, ET).strftime("%Y%m%d")


def season_units(league: League, meta: Dict[str, Any]) -> List[Tuple[str, Optional[float]]]:
    """[(unit key, approximate start epoch)] for one season's calendar."""
    out: List[Tuple[str, Optional[float]]] = []
    cal = meta.get("calendar") or []
    if league.mode == "day" and meta.get("start") and meta.get("end"):
        span_days = int((meta["end"] - meta["start"]) // 86400)
        if len(cal) < 0.15 * span_days:
            # MLB's calendar lists only special dates (opening day, All-Star, playoffs): walk every day.
            first = datetime.fromtimestamp(meta["start"], ET).date()
            cal = [(first + timedelta(days=i)).strftime("%Y%m%d") for i in range(span_days + 1)]
    for c in cal:
        if league.mode == "day" and isinstance(c, str):
            out.append((f"d:{c}", datetime.strptime(c, "%Y%m%d").replace(tzinfo=ET).timestamp()))
        elif league.mode == "week" and isinstance(c, list) and len(c) == 3 and c[0] and c[1]:
            out.append((f"w:{meta['season']}:{c[0]}:{c[1]}", c[2]))
    return out


def recent_span(league: League) -> float:
    return (9 if league.mode == "week" else 2) * 86400


def plan(league: League, store: Dict[str, Any], now: float) -> List[str]:
    """Units to fetch now, in priority order: unlocked recent/upcoming first, then backfill newest first."""
    units = store.get("units", {})
    seasons = store.get("seasons", {})
    want: List[Tuple[int, float, str]] = []
    cur_end = (seasons.get("current") or {}).get("end")
    for skey, meta in seasons.items():
        if skey == "previous" and cur_end and now > cur_end:
            continue        # offseason: the finished season alone gives final ratings
        prio_base = 1 if skey == "current" else 2
        for key, at in season_units(league, meta):
            if at is None:
                continue
            u = units.get(key)
            if at > now + FUTURE_DAYS * 86400:
                continue
            if u and u.get("locked"):
                continue
            if at > now - recent_span(league):     # recent or upcoming: refresh, not backfill
                if u and now - u.get("fetched_at", 0) < (FUTURE_REFETCH_S if at > now else RECENT_REFETCH_S):
                    continue
                want.append((0, -at, key))
            elif not u or not u.get("locked"):
                want.append((prio_base, -at, key))
    want.sort()
    return [k for _, _, k in want]


def _locked(league: League, games: List[Dict[str, Any]], at: Optional[float], now: float) -> bool:
    """A unit is final when it ended over a day ago and nothing in it is still scheduled or live."""
    if at is None:
        return False
    span = 7 * 86400 if league.mode == "week" else 86400
    if at + span > now - 86400:
        return False
    # Two weeks on, a game still "scheduled" is a postponement ESPN never closed: stop refetching.
    return at < now - 14 * 86400 or all(g["state"] == "post" for g in games)


def merge_games(old: List[Dict[str, Any]], new: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep the last pregame book line we saw: ESPN drops odds once a game starts."""
    prev = {g["id"]: g for g in old}
    for g in new:
        if not g.get("book") and prev.get(g["id"], {}).get("book"):
            g["book"] = prev[g["id"]]["book"]
    return new


# ---- one league ------------------------------------------------------------------------------

async def update_league(league: League, client, budget: List[int], now: float) -> Dict[str, Any]:
    """Fetch for one league, update its store. Returns {changed, errors, fetched}."""
    path = store_path(league.key)
    store = _read(path, {"units": {}, "seasons": {}})
    errors: List[str] = []
    changed = False
    fetched = 0

    # 1. ESPN's current view: live scores + this season's calendar.
    payload = await espn.fetch(league, None, client)
    games, meta = espn.parse_scoreboard(payload)
    fetched += 1
    if meta.get("season"):
        if store["seasons"].get("current", {}).get("season") not in (None, meta["season"]):
            store["seasons"]["previous"] = store["seasons"]["current"]      # season rolled over
        store["seasons"]["current"] = meta
    changed |= _absorb(league, store, games, now)

    # 2. Last season's calendar, once.
    cur = store["seasons"].get("current") or {}
    probe_due = now - store.get("previous_probe_at", 0) > 86400
    if "previous" not in store["seasons"] and cur.get("start") and budget[0] > 0 and probe_due:
        store["previous_probe_at"] = now          # at most once a day if ESPN has no older season
        changed = True
        try:
            if league.mode == "day":
                probe = f"d:{_day(cur['start'] - 300 * 86400)}"
            else:
                probe = f"y:{(cur.get('season') or 0) - 1}"
            budget[0] -= 1
            fetched += 1
            pg, pmeta = espn.parse_scoreboard(await espn.fetch(league, probe, client))
            if pmeta.get("season") and pmeta["season"] != cur.get("season"):
                store["seasons"]["previous"] = pmeta
                changed = True
        except Exception as exc:  # noqa: BLE001 - one league's backfill must not stop the rest
            errors.append(f"previous season: {_err(exc)}")

    # 3. Units: recent refresh (not budgeted) + backfill (budgeted).
    starts = {k: at for m in store["seasons"].values() for k, at in season_units(league, m)}
    for key in plan(league, store, now):
        recent = (starts.get(key) or 0) > now - recent_span(league)
        if not recent:
            if budget[0] <= 0:
                break
            budget[0] -= 1
        try:
            g, _ = espn.parse_scoreboard(await espn.fetch(league, key, client))
            fetched += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{key}: {_err(exc)}")
            continue
        old = store["units"].get(key, {}).get("games", [])
        g = merge_games(old, g)
        store["units"][key] = {"fetched_at": now, "games": g, "locked": _locked(league, g, starts.get(key), now)}
        changed = True

    scratch = store["units"].get("live")
    if scratch:
        scratch["games"] = [g for g in scratch["games"] if g["start"] > now - 3 * 86400]
    if changed:
        _write(path, store)
    return {"changed": changed, "errors": errors, "fetched": fetched, "store": store}


def _absorb(league: League, store: Dict[str, Any], games: List[Dict[str, Any]], now: float) -> bool:
    """Fold the current scoreboard's games into whichever stored units hold them."""
    if not games:
        return False
    by_id = {g["id"]: g for g in games}
    hit = False
    for u in store["units"].values():
        if u.get("locked"):
            continue
        for i, g in enumerate(u["games"]):
            if g["id"] in by_id:
                new = merge_games([g], [by_id.pop(g["id"])])[0]
                if new != g:
                    u["games"][i] = new
                    hit = True
    if by_id:     # games in no stored unit yet (first run): keep them in a scratch unit
        key = "live"
        old = store["units"].get(key, {}).get("games", [])
        store["units"][key] = {"fetched_at": now, "games": merge_games(old, list(by_id.values())), "locked": False}
        hit = True
    return hit


# ---- the page blob --------------------------------------------------------------------------

def all_games(store: Dict[str, Any]) -> List[Dict[str, Any]]:
    seen: Dict[str, Dict[str, Any]] = {}
    for key, u in store.get("units", {}).items():
        for g in u.get("games", []):
            # A real unit beats the scratch 'live' copy; a fresher fetch beats an older one.
            if g["id"] not in seen or key != "live":
                seen[g["id"]] = g
    return list(seen.values())


def build_league(league: League, store: Dict[str, Any], now: float, errors: List[str]) -> Dict[str, Any]:
    cur = store.get("seasons", {}).get("current") or {}
    season = cur.get("season")
    games = all_games(store)
    result = elo.run(league, games, current_season=season, week_ago=now - 7 * 86400)
    ratings = result["ratings"]

    def rate(side):
        return ratings.get(side["id"], league.other_start if league.top_conferences and
                           side.get("conf") not in league.top_conferences else elo.MEAN)

    today0 = datetime.fromtimestamp(now, ET).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    window = [g for g in games if g["state"] == "in" or today0 <= g["start"] < today0 + (FUTURE_DAYS + 1) * 86400]
    window.sort(key=lambda g: (g["state"] != "in", g["start"]))
    slate = []
    for g in window[:UPCOMING_LIMIT]:
        rh, ra = rate(g["home"]), rate(g["away"])
        p = elo.probs(league, rh, ra, g["neutral"])
        book = g.get("book")
        slate.append({**{k: g[k] for k in ("id", "start", "state", "detail", "neutral", "completed")},
                      "home": {**g["home"], "elo": round(rh, 1)}, "away": {**g["away"], "elo": round(ra, 1)},
                      "elo": {k: (round(v, 4) if v is not None else None) for k, v in p.items()},
                      "book": book,
                      "gap": round(p["home"] - book["home"], 4) if book else None})

    gotd = _game_of_the_day(slate, today0)
    seasons = store.get("seasons", {})
    used = [m for n, m in seasons.items() if not (n == "previous" and cur.get("end") and now > cur["end"])]
    units = [k for m in used for k, at in season_units(league, m) if at and at < now]
    have = sum(1 for k in units if store.get("units", {}).get(k))
    in_season = bool(cur.get("start") and cur.get("end") and cur["start"] <= now <= cur["end"]) and \
        any(at and now - 14 * 86400 < at < now + 14 * 86400 for _, at in season_units(league, cur))
    return {"ready": True, "key": league.key, "label": league.label, "built_at": now, "source": espn.SOURCE,
            "season": season, "season_name": cur.get("season_name"),
            "status": "in_season" if in_season else "offseason",
            "backfill": {"done": have, "total": len(units)},
            "slate": slate, "game_of_the_day": gotd, "ratings": elo.table(league, result),
            "record": result["record"], "params": {"k": league.k, "hfa": league.hfa, "regress": league.regress},
            "errors": errors}


def _game_of_the_day(slate: List[Dict[str, Any]], today0: float) -> Optional[str]:
    """Today's best matchup: strong teams, close game. Score = mean Elo - 400 * |p - 0.5|."""
    best, best_id = None, None
    for g in slate:
        if g["state"] == "post" or not (today0 <= g["start"] < today0 + 86400 or g["state"] == "in"):
            continue
        s = (g["home"]["elo"] + g["away"]["elo"]) / 2 - 400 * abs(g["elo"]["home"] - 0.5)
        if best is None or s > best:
            best, best_id = s, g["id"]
    return best_id


def summary(blob: Dict[str, Any]) -> Dict[str, Any]:
    live = sum(1 for g in blob.get("slate", []) if g["state"] == "in")
    gid = blob.get("game_of_the_day")
    g = next((x for x in blob.get("slate", []) if x["id"] == gid), None)
    return {"key": blob["key"], "label": blob["label"], "status": blob["status"], "live": live,
            "games": len(blob.get("slate", [])), "built_at": blob["built_at"], "backfill": blob["backfill"],
            "errors": blob.get("errors", []),
            "game_of_the_day": g and {"id": g["id"], "home": g["home"]["abbr"], "away": g["away"]["abbr"],
                                      "start": g["start"], "state": g["state"], "detail": g["detail"],
                                      "p_home": g["elo"]["home"]}}


# ---- job + reads ----------------------------------------------------------------------------

async def refresh(now: Optional[float] = None) -> Dict[str, Any]:
    now = now or time.time()
    budget = [FETCH_BUDGET]
    out: Dict[str, Any] = {}
    index = _read(INDEX, {"leagues": {}})
    async with make_client(timeout=25, headers=espn.UA) as client:
        for league in LEAGUES.values():
            try:
                res = await update_league(league, client, budget, now)
            except Exception as exc:  # noqa: BLE001 - one league down leaves the others live
                out[league.key] = {"error": _err(exc)}
                prev = index["leagues"].get(league.key)
                index["leagues"][league.key] = {**(prev or {"key": league.key, "label": league.label,
                                                            "status": "unknown", "live": 0, "games": 0,
                                                            "built_at": None, "backfill": None,
                                                            "game_of_the_day": None}),
                                                "errors": [_err(exc)]}
                continue
            # Always rebuild (tens of ms): built_at doubles as "checked at", so a quiet league
            # never reads STALE just because nothing happened.
            blob = await asyncio.to_thread(build_league, league, res["store"], now, res["errors"])
            _write(blob_path(league.key), blob)
            index["leagues"][league.key] = summary(blob)
            out[league.key] = {"fetched": res["fetched"], "errors": len(res["errors"])}
    index.update(ready=True, built_at=now, order=list(LEAGUES))
    _write(INDEX, index)
    return {"leagues": out, "budget_left": budget[0]}


def read_index() -> Dict[str, Any]:
    idx = _read(INDEX, None)
    if not idx:
        return {"ready": False, "leagues": []}
    return {"ready": True, "built_at": idx.get("built_at"),
            "leagues": [idx["leagues"][k] for k in idx.get("order", []) if k in idx["leagues"]]}


def read_league(key: str) -> Optional[Dict[str, Any]]:
    if key not in LEAGUES:
        return None
    return _read(blob_path(key), {"ready": False, "key": key, "label": LEAGUES[key].label})
