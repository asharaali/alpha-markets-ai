"""
Live odds adapter.

Real mode: pulls match odds from The Odds API (the-odds-api.com, free tier).
Demo mode: returns realistic mock World Cup fixtures so the whole app runs with
zero keys and zero cost. Demo odds 'drift' slightly on every poll so you can see
the live-update + cash-out behaviour working end to end.

Returned shape (normalized, provider-agnostic) per match:
{
  "id": str,
  "sport": "soccer",
  "league": "FIFA World Cup",
  "commence_time": iso8601,
  "home": str, "away": str,
  "status": "upcoming" | "live",
  "live_minute": int | None,
  "live_score": {"home": int, "away": int} | None,
  "markets": {
      "h2h": {"home": decimal, "draw": decimal, "away": decimal},
      "totals_2_5": {"over": decimal, "under": decimal},
      "btts": {"yes": decimal, "no": decimal},
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


# ---------------- DEMO MODE ----------------

_DEMO_FIXTURES = [
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

# Per-process seed so demo odds wander a little each poll (simulates a live market).
_DEMO_DRIFT_SEED = {}


def _fair_to_market_odds(probs: Dict[str, float], margin: float = 0.06) -> Dict[str, float]:
    """Turn fair probabilities into bookmaker decimal odds WITH a vig baked in."""
    out = {}
    for k, p in probs.items():
        p = min(max(p, 1e-6), 1 - 1e-6)
        # add margin proportionally so implied probs sum to >1
        p_with_vig = p * (1 + margin)
        out[k] = round(1.0 / p_with_vig, 2)
    return out


def _demo_matches() -> List[Dict]:
    from app.soccer_model import match_probabilities

    now = datetime.now(timezone.utc)
    matches: List[Dict] = []
    drift_bucket = int(time.time() // 12)  # odds nudge every ~12s

    for idx, (home, away, status, minute, sh, sa) in enumerate(_DEMO_FIXTURES):
        model = match_probabilities(home, away)
        seed_key = f"{home}-{away}-{drift_bucket}"
        rng = random.Random(hash(seed_key) & 0xFFFFFFFF)
        # The market is NOT the model. We push the market's view away from the model by a
        # per-outcome bias so genuine value (and value-less) spots appear — exactly what a
        # real edge engine has to find. Range is wide enough to sometimes beat the vig.
        jitter = lambda: 1 + rng.uniform(-0.18, 0.14)

        h2h_fair = {
            "home": model["probs"]["home"] * jitter(),
            "draw": model["probs"]["draw"] * jitter(),
            "away": model["probs"]["away"] * jitter(),
        }
        totals_fair = {
            "over": model["totals"]["over_2_5"] * jitter(),
            "under": model["totals"]["under_2_5"] * jitter(),
        }
        btts_fair = {
            "yes": model["totals"]["btts_yes"] * jitter(),
            "no": model["totals"]["btts_no"] * jitter(),
        }

        commence = now + (timedelta(hours=idx + 1) if status == "upcoming" else timedelta(minutes=-(minute or 0)))
        matches.append({
            "id": f"demo-{idx}",
            "sport": "soccer",
            "league": "FIFA World Cup",
            "commence_time": commence.isoformat(),
            "home": home,
            "away": away,
            "status": status,
            "live_minute": minute,
            "live_score": ({"home": sh, "away": sa} if status == "live" else None),
            "markets": {
                "h2h": _fair_to_market_odds(h2h_fair),
                "totals_2_5": _fair_to_market_odds(totals_fair),
                "btts": _fair_to_market_odds(btts_fair),
            },
        })
    return matches


# ---------------- LIVE MODE ----------------

# Simple in-memory cache so frequent frontend polls don't each cost an API request.
_LIVE_CACHE: Dict[str, object] = {"data": None, "ts": 0.0}
# Quota visibility, populated from The Odds API response headers.
QUOTA = {"remaining": None, "used": None}
# Whether the data we're serving is genuinely live, or a fallback. Surfaced to the UI.
LIVE_STATUS: Dict[str, object] = {"live": False, "reason": "starting up"}


async def _live_matches() -> List[Dict]:
    params = {
        "apiKey": settings.ODDS_API_KEY,
        "regions": settings.ODDS_REGIONS,
        "markets": "h2h",
        "oddsFormat": "decimal",
    }
    url = f"{settings.ODDS_API_BASE}/sports/{settings.SOCCER_SPORT_KEY}/odds"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        QUOTA["remaining"] = resp.headers.get("x-requests-remaining")
        QUOTA["used"] = resp.headers.get("x-requests-used")
        print(f"[odds_api] quota remaining={QUOTA['remaining']} used={QUOTA['used']}")
        raw = resp.json()

    matches: List[Dict] = []
    for ev in raw:
        home, away = ev.get("home_team"), ev.get("away_team")
        h2h = {"home": None, "draw": None, "away": None}
        books = ev.get("bookmakers", [])
        if books:
            for market in books[0].get("markets", []):
                if market.get("key") == "h2h":
                    for o in market.get("outcomes", []):
                        if o["name"] == home:
                            h2h["home"] = o["price"]
                        elif o["name"] == away:
                            h2h["away"] = o["price"]
                        else:
                            h2h["draw"] = o["price"]
        matches.append({
            "id": ev.get("id"),
            "sport": "soccer",
            "league": ev.get("sport_title", "FIFA World Cup"),
            "commence_time": ev.get("commence_time"),
            "home": home,
            "away": away,
            "status": "upcoming",
            "live_minute": None,
            "live_score": None,
            "markets": {"h2h": h2h, "totals_2_5": None, "btts": None},
        })
    return matches


def _estimate_minute(commence_iso: str) -> int:
    """Wall-clock since kickoff -> approx match minute (accounts for ~15' halftime)."""
    try:
        start = datetime.fromisoformat(commence_iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return 0
    elapsed = (datetime.now(timezone.utc) - start).total_seconds() / 60.0
    if elapsed <= 45:
        return max(0, int(elapsed))
    if elapsed <= 60:
        return 45  # halftime
    return min(95, int(elapsed - 15))


_SCORES_CACHE: Dict[str, object] = {"data": None, "ts": 0.0}


async def get_live_scores() -> List[Dict]:
    """Scores feed (live + recently completed). Cached briefly so live polling is cheap-ish."""
    if settings.DEMO_MODE:
        return []
    age = time.time() - float(_SCORES_CACHE["ts"])
    if _SCORES_CACHE["data"] is not None and age < settings.SCORES_CACHE_TTL:
        return _SCORES_CACHE["data"]  # type: ignore[return-value]
    url = f"{settings.ODDS_API_BASE}/sports/{settings.SOCCER_SPORT_KEY}/scores"
    params = {"apiKey": settings.ODDS_API_KEY, "daysFrom": 1}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            QUOTA["remaining"] = resp.headers.get("x-requests-remaining")
            QUOTA["used"] = resp.headers.get("x-requests-used")
            data = resp.json()
        _SCORES_CACHE["data"] = data
        _SCORES_CACHE["ts"] = time.time()
        return data
    except Exception as exc:
        print(f"[odds_api] scores fetch failed: {exc}")
        return _SCORES_CACHE["data"] or []  # type: ignore[return-value]


def merge_scores(matches: List[Dict], scores: List[Dict]) -> bool:
    """
    Fold live/final scores into the odds matches (matched by event id).
    Returns True if any match is currently in play.
    """
    by_id = {s.get("id"): s for s in scores}
    any_live = False
    now = datetime.now(timezone.utc)
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
            m["live_minute"] = _estimate_minute(m["commence_time"])
            m["live_score"] = {
                "home": int(sa) if sa is not None else 0,
                "away": int(sb) if sb is not None else 0,
            }

    # A game in play can fall off the odds board (pre-match betting closes). Make sure
    # live games still appear — with the live model — even without odds attached.
    have_ids = {m["id"] for m in matches}
    now2 = datetime.now(timezone.utc)
    for s in scores:
        if s.get("id") in have_ids or s.get("completed"):
            continue
        try:
            commence = datetime.fromisoformat(s["commence_time"].replace("Z", "+00:00"))
        except (ValueError, AttributeError, KeyError):
            continue
        if commence > now2:
            continue  # not started yet -> already on the upcoming board via odds
        score_map = {x["name"]: x.get("score") for x in (s.get("scores") or [])}
        home, away = s.get("home_team"), s.get("away_team")
        any_live = True
        matches.append({
            "id": s["id"], "sport": "soccer", "league": "FIFA World Cup",
            "commence_time": s["commence_time"], "home": home, "away": away,
            "status": "live", "live_minute": _estimate_minute(s["commence_time"]),
            "live_score": {"home": int(score_map.get(home) or 0),
                           "away": int(score_map.get(away) or 0)},
            "markets": {"h2h": None, "totals_2_5": None, "btts": None},
        })
    return any_live


async def get_soccer_matches() -> List[Dict]:
    if settings.DEMO_MODE:
        LIVE_STATUS.update(live=False, reason="demo mode (no key)")
        return _demo_matches()

    # Serve cached live odds if still fresh — protects the monthly quota.
    age = time.time() - float(_LIVE_CACHE["ts"])
    if _LIVE_CACHE["data"] is not None and age < settings.ODDS_CACHE_TTL:
        return _LIVE_CACHE["data"]  # type: ignore[return-value]

    try:
        data = await _live_matches()
        if data:
            _LIVE_CACHE["data"] = data
            _LIVE_CACHE["ts"] = time.time()
            LIVE_STATUS.update(live=True, reason="")
        return data
    except httpx.HTTPStatusError as exc:
        quota_out = exc.response.status_code == 401
        reason = ("Odds API monthly quota used up — upgrade the plan or wait for reset"
                  if quota_out else f"Odds API error {exc.response.status_code}")
        print(f"[odds_api] live fetch failed: {reason}")
        LIVE_STATUS.update(live=False, reason=reason)
        if _LIVE_CACHE["data"] is not None:
            return _LIVE_CACHE["data"]  # type: ignore[return-value]
        return _demo_matches()
    except Exception as exc:
        print(f"[odds_api] live fetch failed ({exc})")
        LIVE_STATUS.update(live=False, reason=f"connection error: {exc}")
        if _LIVE_CACHE["data"] is not None:
            return _LIVE_CACHE["data"]  # type: ignore[return-value]
        return _demo_matches()
