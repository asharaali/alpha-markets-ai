"""
MLB player-prop model — batter & pitcher props, the baseball counterpart to the soccer
goalscorer model. Prices Kalshi's prop series:

  KXMLBHR   batter home runs      (1+, 2+)
  KXMLBHIT  batter hits           (1+ .. 4+)
  KXMLBTB   batter total bases    (2+ .. 5+)
  KXMLBHRR  batter H + R + RBI    (1+ .. 4+)
  KXMLBKS   pitcher strikeouts    (2+ .. )
  KXMLBRFI  a run in the 1st inning (game prop, Yes/No)

Method (honest, rate-based):
  - Each player's SEASON per-plate-appearance rate, regressed toward league average by
    sample size, drives an expected per-game count. Batter volume comes from the player's
    own PA/AB per game (captures lineup role). Home-park factor is applied.
  - Discrete tails: hits use a Binomial(at-bats, AVG); HR/TB/HRR/Ks use a Poisson on the
    expected count (a standard, well-behaved approximation for count props).
  - First-inning run is derived from the same run model that powers the moneyline, so it's
    fully matchup- and pitcher-aware.

Honest limit (v1): batter props use the hitter's own rate + park, NOT the specific opposing
starter's suppression — that's a v2 refinement. Pitcher strikeouts already reflect the arm.
No lineup/injury feed, so a benched star is still priced from his season rate.
"""
from __future__ import annotations
import math
import time
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Dict, Optional

import httpx

STATS_BASE = "https://statsapi.mlb.com/api/v1"

# League per-plate-appearance anchors for regressing small samples (approx MLB rates).
LG_HR_PA = 0.032
LG_TB_PA = 0.135          # total bases per PA
LG_HRR_PA = 0.55          # (hits + runs + RBI) per PA
LG_AVG = 0.245            # league batting average (hits per AB)
LG_K_PER_IP = 0.95        # ~8.5 K/9
PA_REGRESS = 60.0         # PA of regression toward league (batters)
IP_REGRESS = 40.0         # IP of regression toward league (pitchers)
FIRST_INN_FACTOR = 0.82   # calibrates 1st-inning run rate to reality (~0.5 league YRFI)

_HIT_CACHE: Dict[str, object] = {"data": None, "ts": 0.0}
_PIT_CACHE: Dict[str, object] = {"data": None, "ts": 0.0}
_TTL = 12 * 3600


def _season() -> int:
    return datetime.now(ZoneInfo("America/New_York")).year


def _norm(name: str) -> str:
    """Normalize a player name for matching (strip accents, punctuation, suffixes, case)."""
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    s = s.lower().replace(".", "").replace("'", "").replace("-", " ")
    for suf in (" jr", " sr", " ii", " iii", " iv"):
        if s.endswith(suf):
            s = s[: -len(suf)]
    return " ".join(s.split())


def _poisson_tail(lam: float, k: int) -> float:
    """P(X >= k) for X ~ Poisson(lam)."""
    lam = max(lam, 1e-9)
    cdf = sum(math.exp(-lam) * lam ** i / math.factorial(i) for i in range(k))
    return max(0.0, min(1.0, 1.0 - cdf))


def _binom_tail(n: int, p: float, k: int) -> float:
    """P(X >= k) for X ~ Binomial(n, p)."""
    p = min(max(p, 0.0), 1.0)
    if k <= 0:
        return 1.0
    if n <= 0:
        return 0.0
    cdf = 0.0
    for i in range(k):
        cdf += math.comb(n, i) * p ** i * (1 - p) ** (n - i)
    return max(0.0, min(1.0, 1.0 - cdf))


# ---------------- season stat indexes (bulk, cached) ----------------

async def _bulk_stats(group: str) -> list:
    season = _season()
    async with httpx.AsyncClient(timeout=45) as client:
        r = await client.get(f"{STATS_BASE}/stats",
                             params={"stats": "season", "group": group, "season": season,
                                     "sportId": 1, "gameType": "R", "limit": 2000,
                                     "playerPool": "All"})
        r.raise_for_status()
        return (r.json().get("stats") or [{}])[0].get("splits", [])


async def hitting_index() -> Dict[str, Dict]:
    """{normalized_name: hitting stat dict} for every player this season (cached)."""
    if _HIT_CACHE["data"] is not None and time.time() - float(_HIT_CACHE["ts"]) < _TTL:
        return _HIT_CACHE["data"]  # type: ignore[return-value]
    out: Dict[str, Dict] = {}
    try:
        for sp in await _bulk_stats("hitting"):
            name = sp.get("player", {}).get("fullName")
            if name:
                out[_norm(name)] = sp["stat"]
    except Exception as exc:
        print(f"[baseball_props] hitting index failed: {exc}")
    _HIT_CACHE["data"], _HIT_CACHE["ts"] = out, time.time()
    return out


async def pitching_index() -> Dict[str, Dict]:
    """{normalized_name: pitching stat dict} for every pitcher this season (cached)."""
    if _PIT_CACHE["data"] is not None and time.time() - float(_PIT_CACHE["ts"]) < _TTL:
        return _PIT_CACHE["data"]  # type: ignore[return-value]
    out: Dict[str, Dict] = {}
    try:
        for sp in await _bulk_stats("pitching"):
            name = sp.get("player", {}).get("fullName")
            if name:
                out[_norm(name)] = sp["stat"]
    except Exception as exc:
        print(f"[baseball_props] pitching index failed: {exc}")
    _PIT_CACHE["data"], _PIT_CACHE["ts"] = out, time.time()
    return out


# ---------------- prop probabilities ----------------

def _f(stat: Dict, key: str, default: float = 0.0) -> float:
    try:
        v = stat.get(key)
        return float(v) if v not in (None, "", "-.--", ".---") else default
    except (TypeError, ValueError):
        return default


def batter_prop(stat: Dict, kind: str, line: int, park: float = 1.0) -> Optional[float]:
    """P(>= line) for a batter prop. kind in {hr, hits, tb, hrr}."""
    g = _f(stat, "gamesPlayed")
    pa = _f(stat, "plateAppearances")
    ab = _f(stat, "atBats")
    if g < 1 or pa < 1:
        return None
    pa_g = min(max(pa / g, 2.0), 5.0)          # per-game plate appearances (role)
    rel = pa / (pa + PA_REGRESS)               # regression toward league by sample

    if kind == "hr":
        rate = rel * (_f(stat, "homeRuns") / pa) + (1 - rel) * LG_HR_PA
        lam = rate * pa_g * park
        return _poisson_tail(lam, line)
    if kind == "hits":
        ab_g = min(max(ab / g, 1.5), 4.6)
        ba = rel * (_f(stat, "hits") / ab if ab else LG_AVG) + (1 - rel) * LG_AVG
        return _binom_tail(round(ab_g), ba * park, line)
    if kind == "tb":
        rate = rel * (_f(stat, "totalBases") / pa) + (1 - rel) * LG_TB_PA
        lam = rate * pa_g * park
        return _poisson_tail(lam, line)
    if kind == "hrr":
        hrr = _f(stat, "hits") + _f(stat, "runs") + _f(stat, "rbi")
        rate = rel * (hrr / pa) + (1 - rel) * LG_HRR_PA
        lam = rate * pa_g * park
        return _poisson_tail(lam, line)
    return None


def pitcher_strikeouts(stat: Dict, line: int, opp_k_factor: float = 1.0) -> Optional[float]:
    """P(>= line strikeouts) for a starting pitcher this game."""
    ip = _f(stat, "inningsPitched")
    if ip < 1:
        return None
    gs = _f(stat, "gamesStarted") or _f(stat, "gamesPlayed") or 1
    rel = ip / (ip + IP_REGRESS)
    k_per_ip = rel * (_f(stat, "strikeOuts") / ip) + (1 - rel) * LG_K_PER_IP
    exp_ip = min(max(ip / gs, 4.0), 6.5) if gs else 5.3   # expected length of this start
    lam = k_per_ip * exp_ip * opp_k_factor
    return _poisson_tail(lam, line)


def first_inning_run_prob(lam_home: float, lam_away: float) -> float:
    """P(a run scores in the 1st inning by either team), from the game's expected runs."""
    lh = lam_home / 9.0 * FIRST_INN_FACTOR
    la = lam_away / 9.0 * FIRST_INN_FACTOR
    p_no_run = math.exp(-lh) * math.exp(-la)
    return max(0.0, min(1.0, 1.0 - p_no_run))
