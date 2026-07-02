"""
Individual (single-bet) Kalshi MLB markets — the baseball counterpart to kalshi_single.

Kalshi groups MLB markets into series, one per bet type:
  KXMLBGAME    Game winner (moneyline)  -> yes_sub_title is a team ("Los Angeles D")
  KXMLBTOTAL   Total runs               -> yes_sub_title is "Over X.5 runs scored"

For each contract we compute the baseball model's fair value, the live Kalshi mid price,
and (for the moneyline) a vig-free CONSENSUS across the sportsbook board so "how good is
this bet" reflects the whole market, not just Kalshi.
"""
from __future__ import annotations
import asyncio
import re
import time
from typing import Dict, List, Optional, Tuple

import httpx

from app import probability as P
from app.analysis import MODEL_WEIGHT, MIN_EDGE, LONGSHOT_FLOOR, HEAVY_FAV_CAP, _tier
from app import baseball_model as B
from app.data_sources.kalshi_orderbook import orderbook_prices

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
_UA = {"User-Agent": "AlphaMarketsAI/1.0"}

SERIES = {
    "KXMLBGAME":   ("Game Line", "Moneyline"),
    "KXMLBSPREAD": ("Game Line", "Run Line"),
    "KXMLBTOTAL":  ("Game Line", "Total Runs"),
    "KXMLBF5":     ("Game Prop", "First 5 Innings"),
}

_CACHE: Dict[str, object] = {"data": None, "ts": 0.0}
_CACHE_TTL = 90


def _canon(name: str) -> str:
    """Map a Kalshi short team name ('Los Angeles D') to the model's full name."""
    n = (name or "").strip()
    if n in B.TEAM_ELO:
        return n
    parts = n.split()
    disamb = parts[-1] if parts and len(parts[-1]) == 1 else None
    city = " ".join(parts[:-1]) if disamb else n
    cands = [full for full in B.TEAM_ELO if full.startswith(city)]
    if len(cands) == 1:
        return cands[0]
    if disamb:
        for full in cands:
            nick = full[len(city):].strip()
            if nick and nick[0].upper() == disamb.upper():
                return full
    return cands[0] if cands else n


def _teams_from_title(title: str) -> Optional[Tuple[str, str]]:
    """'San Diego vs Los Angeles D[: Total Runs]' -> (home, away).
    Kalshi lists MLB as 'Away vs Home', so the second team is the home side."""
    head = title.split(":", 1)[0]
    if " vs " not in head:
        return None
    away, home = head.split(" vs ", 1)
    return _canon(home), _canon(away)


def _evaluate(model_prob: float, price: float, book_prob: Optional[float], book_count: int) -> Dict:
    if book_prob is not None:
        fair = MODEL_WEIGHT * model_prob + (1 - MODEL_WEIGHT) * book_prob
        sources = f"model + {book_count} books"
        agree = abs(model_prob - book_prob) <= 0.06
    else:
        fair = model_prob
        sources = "model only (Kalshi-exclusive market)"
        agree = None
    ev = (fair / price - 1) if price > 0 else 0.0
    edge = fair - price
    value = (edge >= MIN_EDGE and ev > 0 and LONGSHOT_FLOOR <= price <= HEAVY_FAV_CAP)
    if agree is False:
        value = False
    conf = "high" if (book_prob is not None and agree) else "medium" if book_prob is not None else "model-only"
    return {
        "model_prob": round(model_prob, 4),
        "book_prob": round(book_prob, 4) if book_prob is not None else None,
        "fair_prob": round(fair, 4),
        "kalshi_price_cents": round(price * 100, 1),
        "edge": round(edge, 4), "ev_per_dollar": round(ev, 4),
        "value_bet": value, "tier": _tier(fair), "confidence": conf,
        "books_agree": agree, "sources": sources,
    }


async def _fetch_series(client: httpx.AsyncClient, series: str) -> List[Dict]:
    for attempt in range(3):
        try:
            r = await client.get(f"{KALSHI_BASE}/events",
                                 params={"series_ticker": series, "with_nested_markets": "true",
                                         "status": "open", "limit": 60})
            if r.status_code == 429:
                await asyncio.sleep(0.6 * (attempt + 1))
                continue
            r.raise_for_status()
            return [{**e, "_series": series} for e in r.json().get("events", [])]
        except Exception as exc:
            if attempt == 2:
                print(f"[kalshi_mlb] {series} fetch failed: {exc}")
    return []


def _ml_consensus(board: List[Dict]) -> Dict[Tuple[str, str], Dict]:
    """Vig-free 2-way moneyline consensus from the sportsbook board, keyed by (home, away)."""
    idx = {}
    for ev in board:
        home, away = ev.get("home"), ev.get("away")
        h2h = (ev.get("markets") or {}).get("h2h") or {}
        if not (h2h.get("home") and h2h.get("away")):
            continue
        vf = P.remove_vig([h2h["home"], h2h["away"]])
        idx[(home, away)] = {"home": vf[0], "away": vf[1]}
    return idx


def _model_prob_for(series: str, sub: str, home: str, away: str,
                    sph="avg", spa="avg") -> Tuple[Optional[float], str]:
    sub = (sub or "").strip()
    if series == "KXMLBGAME":
        team = _canon(sub)
        p_home, p_away = B.moneyline_prob(home, away, sph, spa)
        if team == home:
            return p_home, f"{away} @ {home}: {home} win"
        if team == away:
            return p_away, f"{away} @ {home}: {away} win"
        return None, sub
    if series == "KXMLBTOTAL":
        m = re.search(r"(Over|Under)\s+([\d.]+)", sub)
        if not m:
            return None, sub
        line = float(m.group(2))
        over = B.run_total_prob(home, away, line, sph, spa)
        is_over = m.group(1).lower() == "over"
        return (over if is_over else 1 - over), f"{away} @ {home}: {m.group(1)} {line} runs"
    if series == "KXMLBSPREAD":                     # 'Los Angeles D wins by over 1.5 runs'
        m = re.search(r"(.+?)\s+wins by over\s+([\d.]+)", sub)
        if not m:
            return None, sub
        team = _canon(m.group(1).strip())
        margin = float(m.group(2))
        if team not in (home, away):
            return None, sub
        p = B.run_margin_prob(home, away, team == home, margin, sph, spa)
        return p, f"{away} @ {home}: {team} -{margin}"
    if series == "KXMLBF5":                          # 'X wins first 5 innings' | 'Tie'
        h5, tie5, a5 = B.f5_probs(home, away, sph, spa)
        if sub.lower().strip() in ("tie", "draw"):
            return tie5, f"{away} @ {home}: Tie (F5)"
        team = _canon(sub.replace("wins first 5 innings", "").strip())
        if team == home:
            return h5, f"{away} @ {home}: {home} (F5)"
        if team == away:
            return a5, f"{away} @ {home}: {away} (F5)"
        return None, sub
    return None, sub


def _book_prob_for(series: str, clean_label: str, home: str, away: str,
                   cons: Optional[Dict]) -> Optional[float]:
    if not cons or series != "KXMLBGAME":
        return None   # sportsbook run-line/totals not pulled yet -> model-only
    if f"{home} win" in clean_label:
        return cons["home"]
    if f"{away} win" in clean_label:
        return cons["away"]
    return None


async def get_single_bets(board: Optional[List[Dict]] = None) -> List[Dict]:
    """All individual Kalshi MLB bets, each with model fair value + moneyline cross-book check."""
    age = time.time() - float(_CACHE["ts"])
    if _CACHE["data"] is not None and age < _CACHE_TTL:
        return _CACHE["data"]  # type: ignore[return-value]

    cons_idx = _ml_consensus(board or [])
    # Starter multipliers from the (already enriched) odds board, so the model here prices
    # the same real pitching matchup the Live Board does.
    sp_idx = {(g.get("home"), g.get("away")): (g.get("sp_home", "avg"), g.get("sp_away", "avg"))
              for g in (board or [])}

    async with httpx.AsyncClient(timeout=25, headers=_UA) as client:
        all_events = []
        for batch in await asyncio.gather(*[_fetch_series(client, s) for s in SERIES]):
            all_events += batch
        tickers = [m.get("ticker") for e in all_events for m in (e.get("markets") or []) if m.get("ticker")]
        prices: Dict[str, float] = {}
        if tickers:
            sem = asyncio.Semaphore(8)

            async def _book(t):
                async with sem:
                    try:
                        return t, await orderbook_prices(client, t)
                    except Exception:
                        return t, (None, None, 0)

            for t, (yb, ya, _d) in await asyncio.gather(*[_book(t) for t in tickers]):
                if yb is not None and ya is not None:
                    prices[t] = (yb + ya) / 2.0

    out: List[Dict] = []
    for e in all_events:
        series = e["_series"]
        teams = _teams_from_title(e.get("title", ""))
        if not teams:
            continue
        home, away = teams
        cons = cons_idx.get((home, away))
        sph, spa = sp_idx.get((home, away), ("avg", "avg"))
        cat, bet_type = SERIES[series]
        for m in (e.get("markets") or []):
            price = prices.get(m.get("ticker"))
            if price is None:
                continue
            mp, label = _model_prob_for(series, m.get("yes_sub_title", ""), home, away, sph, spa)
            if mp is None:
                continue
            book_prob = _book_prob_for(series, label, home, away, cons)
            ev = _evaluate(mp, price, book_prob, 1 if cons else 0)
            out.append({
                "ticker": m.get("ticker"), "category": cat, "bet_type": bet_type,
                "home": home, "away": away, "selection": label,
                "event_title": e.get("title"), **ev,
            })

    out.sort(key=lambda b: (b["value_bet"], b["ev_per_dollar"]), reverse=True)
    _CACHE["data"] = out
    _CACHE["ts"] = time.time()
    return out
