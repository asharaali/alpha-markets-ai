"""
Live odds adapter (multi-sport).

Real mode: pulls match odds from The Odds API (the-odds-api.com, free tier) for whichever
sport is requested (soccer World Cup or MLB).
Demo mode: returns realistic mock fixtures so the whole app runs with zero keys and zero
cost. Demo odds 'drift' slightly on every poll so you can see the live-update + cash-out
behaviour working end to end.

Normalized, provider-agnostic match shape:
{
  "id": str, "sport": "soccer"|"mlb", "league": str, "commence_time": iso8601,
  "home": str, "away": str, "status": "upcoming"|"live"|"completed",
  "live_minute": int|None,          # soccer clock
  "live_inning": int|None, "live_half": str|None,   # baseball
  "live_score": {"home": int, "away": int} | None,
  "sp_home": str, "sp_away": str,   # baseball starting-pitcher tiers (avg when unknown)
  "markets": {                      # sport-appropriate; missing markets are None
      "h2h": {...}, "totals": {...}, "btts": {...}, "runline": {...},
  }
}
"""
from __future__ import annotations
import random
import time
from datetime import datetime, timedelta, timezone
from typing import List, Dict

import httpx

from app.config import settings
from app import sports


# ---------------- DEMO MODE ----------------

_DEMO_FIXTURES_SOCCER = [
    # (home, away, status, minute, score_home, score_away)
    ("Argentina", "Mexico", "live", 63, 1, 0),
    ("France", "USA", "live", 28, 0, 0),
    ("Brazil", "Morocco", "upcoming", None, None, None),
    ("England", "Senegal", "upcoming", None, None, None),
    ("Spain", "Japan", "live", 77, 2, 1),
    ("Portugal", "Canada", "upcoming", None, None, None),
    ("Netherlands", "Ecuador", "upcoming", None, None, None),
    ("Germany", "Korea Republic", "live", 11, 0, 1),
]

_DEMO_FIXTURES_MLB = [
    # (home, away, status, inning, score_home, score_away, sp_home, sp_away)
    ("Los Angeles Dodgers", "Colorado Rockies", "live", 6, 4, 1, "good", "weak"),
    ("New York Yankees", "Boston Red Sox", "live", 3, 1, 2, "ace", "avg"),
    ("Atlanta Braves", "Miami Marlins", "upcoming", None, None, None, "good", "avg"),
    ("Houston Astros", "Seattle Mariners", "upcoming", None, None, None, "avg", "good"),
    ("Philadelphia Phillies", "New York Mets", "live", 8, 3, 3, "avg", "avg"),
    ("San Diego Padres", "San Francisco Giants", "upcoming", None, None, None, "good", "avg"),
    ("Baltimore Orioles", "Tampa Bay Rays", "upcoming", None, None, None, "avg", "avg"),
    ("Chicago Cubs", "Cincinnati Reds", "live", 1, 0, 0, "avg", "weak"),
]

# Per-process seed so demo odds wander a little each poll (simulates a live market).
_DEMO_DRIFT_SEED = {}


def _fair_to_market_odds(probs: Dict[str, float], margin: float = 0.06) -> Dict[str, float]:
    """Turn fair probabilities into bookmaker decimal odds WITH a vig baked in."""
    out = {}
    for k, p in probs.items():
        p = min(max(p, 1e-6), 1 - 1e-6)
        out[k] = round(1.0 / (p * (1 + margin)), 2)
    return out


def _demo_matches_soccer() -> List[Dict]:
    from app.soccer_model import match_probabilities
    now = datetime.now(timezone.utc)
    matches: List[Dict] = []
    drift_bucket = int(time.time() // 12)
    for idx, (home, away, status, minute, sh, sa) in enumerate(_DEMO_FIXTURES_SOCCER):
        model = match_probabilities(home, away)
        rng = random.Random(hash(f"{home}-{away}-{drift_bucket}") & 0xFFFFFFFF)
        jitter = lambda: 1 + rng.uniform(-0.18, 0.14)
        h2h_fair = {"home": model["probs"]["home"] * jitter(),
                    "draw": model["probs"]["draw"] * jitter(),
                    "away": model["probs"]["away"] * jitter()}
        totals_fair = {"over": model["totals"]["over_2_5"] * jitter(),
                       "under": model["totals"]["under_2_5"] * jitter()}
        btts_fair = {"yes": model["totals"]["btts_yes"] * jitter(),
                     "no": model["totals"]["btts_no"] * jitter()}
        commence = now + (timedelta(hours=idx + 1) if status == "upcoming"
                          else timedelta(minutes=-(minute or 0)))
        matches.append({
            "id": f"demo-soccer-{idx}", "sport": "soccer", "league": "FIFA World Cup",
            "commence_time": commence.isoformat(), "home": home, "away": away,
            "status": status, "live_minute": minute, "live_inning": None, "live_half": None,
            "live_score": ({"home": sh, "away": sa} if status == "live" else None),
            "sp_home": "avg", "sp_away": "avg",
            "markets": {"h2h": _fair_to_market_odds(h2h_fair),
                        "totals_2_5": _fair_to_market_odds(totals_fair),
                        "btts": _fair_to_market_odds(btts_fair)},
        })
    return matches


def _demo_matches_mlb() -> List[Dict]:
    from app import baseball_model as B
    now = datetime.now(timezone.utc)
    matches: List[Dict] = []
    drift_bucket = int(time.time() // 12)
    for idx, (home, away, status, inning, sh, sa, sph, spa) in enumerate(_DEMO_FIXTURES_MLB):
        model = B.match_probabilities(home, away, sph, spa)
        em = B.extended_markets(home, away, sph, spa)
        rl = {s["label"]: s["prob"] for s in em["markets"]["Run Line"]}
        rng = random.Random(hash(f"{home}-{away}-{drift_bucket}") & 0xFFFFFFFF)
        jitter = lambda: 1 + rng.uniform(-0.16, 0.13)
        h2h_fair = {"home": model["probs"]["home"] * jitter(),
                    "away": model["probs"]["away"] * jitter()}
        totals_fair = {"over": model["totals"]["over"] * jitter(),
                       "under": model["totals"]["under"] * jitter()}
        runline_fair = {"home": rl.get(f"{home} -1.5", 0.4) * jitter(),
                        "away": rl.get(f"{away} +1.5", 0.6) * jitter()}
        commence = now + (timedelta(hours=idx + 1) if status == "upcoming"
                          else timedelta(minutes=-(inning or 0) * 20))
        matches.append({
            "id": f"demo-mlb-{idx}", "sport": "mlb", "league": "MLB",
            "commence_time": commence.isoformat(), "home": home, "away": away,
            "status": status, "live_minute": None,
            "live_inning": inning, "live_half": ("bottom" if status == "live" else None),
            "live_score": ({"home": sh, "away": sa} if status == "live" else None),
            "sp_home": sph, "sp_away": spa,
            "markets": {"h2h": _fair_to_market_odds(h2h_fair),
                        "totals": {**_fair_to_market_odds(totals_fair), "line": model["totals"]["line"]},
                        "runline": _fair_to_market_odds(runline_fair)},
        })
    return matches


def _demo_matches(sport: str) -> List[Dict]:
    return _demo_matches_mlb() if sports.normalize(sport) == "mlb" else _demo_matches_soccer()


# ---------------- LIVE MODE ----------------

# Per-sport caches so fetching one sport can't clobber another's data.
_LIVE_CACHE: Dict[str, Dict] = {}
_SCORES_CACHE: Dict[str, Dict] = {}
# Quota visibility, populated from The Odds API response headers.
QUOTA = {"remaining": None, "used": None}
# Whether the data we're serving is genuinely live, or a fallback. Surfaced to the UI.
LIVE_STATUS: Dict[str, object] = {"live": False, "reason": "starting up"}


async def _live_matches(sport: str) -> List[Dict]:
    cfg = sports.config(sport)
    is_mlb = cfg["key"] == "mlb"
    params = {"apiKey": settings.ODDS_API_KEY, "regions": settings.ODDS_REGIONS,
              "markets": "h2h", "oddsFormat": "decimal"}
    url = f"{settings.ODDS_API_BASE}/sports/{cfg['sport_key']}/odds"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        QUOTA["remaining"] = resp.headers.get("x-requests-remaining")
        QUOTA["used"] = resp.headers.get("x-requests-used")
        print(f"[odds_api:{cfg['key']}] quota remaining={QUOTA['remaining']} used={QUOTA['used']}")
        raw = resp.json()

    matches: List[Dict] = []
    for ev in raw:
        home, away = ev.get("home_team"), ev.get("away_team")
        # Multi-book consensus: average each outcome's price across EVERY bookmaker instead
        # of trusting one. We reject any book quoting a degenerate price (decimal <= 1.01,
        # i.e. a placeholder like 1.0 on a game it hasn't really lined yet) so one stale book
        # can't poison the number. This is both more robust and sharper than a single source.
        # Floor on a favourite's price: no single MLB game has a >~91% favourite (dec 1.10),
        # so a book quoting 1.02/1.03 on a real matchup is a stale placeholder, not a line.
        # Soccer stays lenient (WC minnows can be genuine 1.03 dogs of a superpower).
        min_dec = 1.10 if is_mlb else 1.02
        acc: Dict[str, list] = {}
        for bk in ev.get("bookmakers", []):
            mk = next((m for m in bk.get("markets", []) if m.get("key") == "h2h"), None)
            if not mk:
                continue
            outs = mk.get("outcomes", [])
            if not outs or any((o.get("price") or 0) < min_dec for o in outs):
                continue   # skip books with placeholder / implausible lines
            for o in outs:
                acc.setdefault(o["name"], []).append(o["price"])

        def _avg(name):
            v = acc.get(name)
            return round(sum(v) / len(v), 3) if v else None

        h2h = {"home": _avg(home), "away": _avg(away)}
        if not is_mlb:
            draw_prices = [p for n, ps in acc.items() if n not in (home, away) for p in ps]
            h2h["draw"] = round(sum(draw_prices) / len(draw_prices), 3) if draw_prices else None
        m = {
            "id": ev.get("id"), "sport": cfg["key"], "league": ev.get("sport_title", cfg["label"]),
            "commence_time": ev.get("commence_time"), "home": home, "away": away,
            "status": "upcoming", "live_minute": None, "live_inning": None, "live_half": None,
            "live_score": None, "sp_home": "avg", "sp_away": "avg",
            "markets": {"h2h": h2h},
        }
        if not is_mlb:
            m["markets"].update({"totals_2_5": None, "btts": None})
        matches.append(m)
    return matches


def _estimate_minute(commence_iso: str) -> int:
    """Soccer: wall-clock since kickoff -> approx match minute (accounts for ~15' halftime)."""
    try:
        start = datetime.fromisoformat(commence_iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return 0
    elapsed = (datetime.now(timezone.utc) - start).total_seconds() / 60.0
    if elapsed <= 45:
        return max(0, int(elapsed))
    if elapsed <= 60:
        return 45
    return min(95, int(elapsed - 15))


def _estimate_inning(commence_iso: str) -> int:
    """Baseball: wall-clock since first pitch -> approx inning (~20 min/inning, cap 9)."""
    try:
        start = datetime.fromisoformat(commence_iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return 1
    elapsed = (datetime.now(timezone.utc) - start).total_seconds() / 60.0
    return max(1, min(9, int(elapsed // 20) + 1))


async def get_scores(sport: str = "soccer") -> List[Dict]:
    """Scores feed (live + recently completed). Cached briefly so live polling is cheap-ish."""
    key = sports.normalize(sport)
    if settings.DEMO_MODE:
        return []
    cache = _SCORES_CACHE.setdefault(key, {"data": None, "ts": 0.0})
    if cache["data"] is not None and time.time() - float(cache["ts"]) < settings.SCORES_CACHE_TTL:
        return cache["data"]  # type: ignore[return-value]
    cfg = sports.config(key)
    url = f"{settings.ODDS_API_BASE}/sports/{cfg['sport_key']}/scores"
    params = {"apiKey": settings.ODDS_API_KEY, "daysFrom": 1}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            QUOTA["remaining"] = resp.headers.get("x-requests-remaining")
            QUOTA["used"] = resp.headers.get("x-requests-used")
            data = resp.json()
        cache["data"], cache["ts"] = data, time.time()
        return data
    except Exception as exc:
        print(f"[odds_api:{key}] scores fetch failed: {exc}")
        return cache["data"] or []  # type: ignore[return-value]


def merge_scores(matches: List[Dict], scores: List[Dict], sport: str = "soccer") -> bool:
    """Fold live/final scores into the odds matches (matched by event id). Returns True if
    any match is currently in play."""
    cfg = sports.config(sport)
    is_mlb = cfg["key"] == "mlb"
    by_id = {s.get("id"): s for s in scores}
    any_live = False
    now = datetime.now(timezone.utc)

    def _progress(m):
        if is_mlb:
            m["live_inning"] = _estimate_inning(m["commence_time"])
            m["live_half"] = "top"
        else:
            m["live_minute"] = _estimate_minute(m["commence_time"])

    for m in matches:
        s = by_id.get(m["id"])
        if not s:
            continue
        score_map = {x["name"]: x.get("score") for x in (s.get("scores") or [])}
        sa, sb = score_map.get(m["home"]), score_map.get(m["away"])
        try:
            commence = datetime.fromisoformat(m["commence_time"].replace("Z", "+00:00"))
        except (ValueError, AttributeError, KeyError):
            commence = now
        started = commence <= now
        if s.get("completed"):
            m["status"] = "completed"
            if sa is not None and sb is not None:
                m["live_score"] = {"home": int(sa), "away": int(sb)}
        elif started:
            m["status"] = "live"
            any_live = True
            _progress(m)
            m["live_score"] = {"home": int(sa) if sa is not None else 0,
                               "away": int(sb) if sb is not None else 0}

    # A game in play can fall off the odds board (pre-match betting closes). Keep it visible.
    have_ids = {m["id"] for m in matches}
    for s in scores:
        if s.get("id") in have_ids or s.get("completed"):
            continue
        try:
            commence = datetime.fromisoformat(s["commence_time"].replace("Z", "+00:00"))
        except (ValueError, AttributeError, KeyError):
            continue
        if commence > now:
            continue
        score_map = {x["name"]: x.get("score") for x in (s.get("scores") or [])}
        home, away = s.get("home_team"), s.get("away_team")
        any_live = True
        m = {"id": s["id"], "sport": cfg["key"], "league": cfg["label"],
             "commence_time": s["commence_time"], "home": home, "away": away,
             "status": "live", "live_minute": None, "live_inning": None, "live_half": None,
             "live_score": {"home": int(score_map.get(home) or 0),
                            "away": int(score_map.get(away) or 0)},
             "sp_home": "avg", "sp_away": "avg", "markets": {"h2h": None}}
        _progress(m)
        matches.append(m)
    return any_live


async def _attach_mlb_starters(matches: List[Dict]) -> None:
    """Fold real probable starting pitchers + their quality multipliers into MLB matches, so
    the model prices the actual pitching matchup (the biggest factor in a baseball game)."""
    from app.data_sources.mlb_pitchers import starters_index
    idx = await starters_index()
    for m in matches:
        s = idx.get((m["home"], m["away"]))
        if not s:
            continue
        for side in ("home", "away"):
            info = s.get(side)
            if info and info.get("mult"):
                m[f"sp_{side}"] = info["mult"]                 # numeric multiplier -> model
                m[f"sp_{side}_name"] = info.get("name")
                m[f"sp_{side}_era"] = info.get("era")
                m[f"sp_{side}_label"] = info.get("label")
                m[f"sp_{side}_bullpen"] = info.get("bullpen")


async def _fetch_matches(key: str) -> List[Dict]:
    if settings.DEMO_MODE:
        LIVE_STATUS.update(live=False, reason="demo mode (no key)")
        return _demo_matches(key)
    cache = _LIVE_CACHE.setdefault(key, {"data": None, "ts": 0.0})
    if cache["data"] is not None and time.time() - float(cache["ts"]) < settings.ODDS_CACHE_TTL:
        return cache["data"]  # type: ignore[return-value]
    try:
        data = await _live_matches(key)
        if data:
            cache["data"], cache["ts"] = data, time.time()
            LIVE_STATUS.update(live=True, reason="")
        return data
    except httpx.HTTPStatusError as exc:
        quota_out = exc.response.status_code == 401
        reason = ("Odds API monthly quota used up — upgrade the plan or wait for reset"
                  if quota_out else f"Odds API error {exc.response.status_code}")
        print(f"[odds_api:{key}] live fetch failed: {reason}")
        LIVE_STATUS.update(live=False, reason=reason)
        return cache["data"] if cache["data"] is not None else _demo_matches(key)
    except Exception as exc:
        print(f"[odds_api:{key}] live fetch failed ({exc})")
        LIVE_STATUS.update(live=False, reason=f"connection error: {exc}")
        return cache["data"] if cache["data"] is not None else _demo_matches(key)


async def get_matches(sport: str = "soccer") -> List[Dict]:
    key = sports.normalize(sport)
    data = await _fetch_matches(key)
    # Live MLB: overlay real probable starters (demo already carries illustrative tiers).
    if key == "mlb" and not settings.DEMO_MODE and data:
        try:
            await _attach_mlb_starters(data)
        except Exception as exc:
            print(f"[odds_api:mlb] starter enrichment skipped: {exc}")
    return data


# ---------------- backward-compatible soccer aliases ----------------

async def get_soccer_matches() -> List[Dict]:
    return await get_matches("soccer")


async def get_live_scores() -> List[Dict]:
    return await get_scores("soccer")
