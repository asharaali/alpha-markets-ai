"""
Individual (single-bet) Kalshi World Cup markets — every bet type Kalshi lists for a game,
not just the moneyline, each overlaid with the model's fair value AND a cross-book check.

Kalshi groups World Cup markets into separate series, one per bet type:
  KXWCGAME    Regulation moneyline    -> game line
  KXWCSPREAD  Regulation spread       -> game line
  KXWCTOTAL   Regulation total goals  -> game line
  KXWCBTTS    Both teams to score     -> game prop
  KXWCCORNERS Total corners           -> game prop
  KXWCSCORE   Correct score           -> game prop
  KXWCGOAL    Player goalscorer       -> player prop
  KXWCADVANCE Which team advances     -> event (knockout)

For each Kalshi contract we compute the model's probability, the live Kalshi mid price,
and (where sportsbooks cover the same market) a vig-free CONSENSUS across many books. The
final fair value blends model + book consensus, so "how good is this bet" reflects the
whole market, not just one source.
"""
from __future__ import annotations
import asyncio
import math
import re
import time
from typing import Dict, List, Optional, Tuple

import httpx

from app import probability as P
from app.analysis import MODEL_WEIGHT, MIN_EDGE, LONGSHOT_FLOOR, HEAVY_FAV_CAP, _tier
from app.soccer_model import match_probabilities, extended_markets
from app.data_sources.kalshi_orderbook import orderbook_prices
from app.data_sources.kalshi import KALSHI_NAME_MAP

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
_UA = {"User-Agent": "AlphaMarketsAI/1.0"}

# series -> (category, bet-type label)
SERIES = {
    "KXWCGAME":    ("Game Line", "Moneyline"),
    "KXWCSPREAD":  ("Game Line", "Spread"),
    "KXWCTOTAL":   ("Game Line", "Total Goals"),
    "KXWCBTTS":    ("Game Prop", "Both Teams To Score"),
    "KXWCCORNERS": ("Game Prop", "Total Corners"),
    "KXWCSCORE":   ("Game Prop", "Correct Score"),
    "KXWCGOAL":    ("Player Prop", "Goalscorer"),
    "KXWCADVANCE": ("Event", "To Advance"),
}

_CACHE: Dict[str, object] = {"data": None, "ts": 0.0}
_CACHE_TTL = 90

# Player props are priced for REFERENCE only, never flagged as value: the goalscorer model
# is rate-based and blind to what the prop market actually prices — the starting XI, who's
# on penalties, injuries, tactical role. A big model-vs-market gap there is our blind spot,
# not an edge (same discipline as the MLB player props).
_REFERENCE_SERIES = {"KXWCGOAL"}

# Markets where the complementary "No" is a natural standalone pick: Kalshi lists a single
# binary market (YES = the event), so betting the other side means BUYING NO on the same ticker.
# For these we surface BOTH sides so you can actually pick e.g. "BTTS No".
_TWO_SIDED = {"KXWCBTTS"}


def _no_side_label(series: str, yes_prob: float, yes_label: str) -> Tuple[float, str]:
    """(model prob, label) for the NO side of a two-sided market. Prob = complement; the caller
    prices it as 1 - yes_price and places it by BUYING NO on the same ticker."""
    if series == "KXWCBTTS":
        return 1.0 - yes_prob, yes_label.replace("BTTS Yes", "BTTS No")
    return 1.0 - yes_prob, f"{yes_label} (No)"


def _canon(name: str) -> str:
    return KALSHI_NAME_MAP.get((name or "").strip(), (name or "").strip())


def _teams_from_title(title: str) -> Optional[Tuple[str, str]]:
    """'Colombia vs Ghana: Regulation Time Total Goals' -> ('Colombia','Ghana')."""
    head = title.split(":", 1)[0]
    if " vs " not in head:
        return None
    a, b = head.split(" vs ", 1)
    return _canon(a), _canon(b)


# ---------------- model lookups per matchup ----------------

def _model_lookups(home: str, away: str) -> Dict:
    """Build fast {selection -> prob} maps for every market type from one model pass."""
    em = extended_markets(home, away)
    mk = em["markets"]

    def as_map(cat):
        return {s["label"]: s["prob"] for s in mk.get(cat, [])}

    moneyline = as_map("Match Result")           # {home, 'Draw', away}
    spread = as_map("Winning Margin (Spread)")   # {'X wins by 2+', 'X wins by 3+', ...}
    totals = as_map("Total Goals (Over/Under)")  # {'Over 1.5', 'Under 1.5', ...}
    btts = as_map("Both Teams To Score")         # {'BTTS: Yes', 'BTTS: No'}
    corners = as_map("Total Corners")            # {'8+ corners', 'Under 8 corners', ...}
    score = as_map("Correct Score")              # {'i-j': prob}

    # Player goalscorer: collapse both teams' scorers, keep 1+ prob and recover lambda for 2+.
    scorers = {}
    for cat in (f"{home} Anytime Goalscorer", f"{away} Anytime Goalscorer"):
        for s in mk.get(cat, []):
            name = s["label"].replace(" to score", "").strip()
            p1 = s["prob"]
            lam = -math.log(max(1e-9, 1 - p1))   # invert P(>=1)=1-e^-lam
            scorers[name] = {"1": p1, "2": 1 - math.exp(-lam) * (1 + lam)}

    # Advance (knockout): win in regulation + half of the draws (penalty coin-flip).
    probs = match_probabilities(home, away)["probs"]
    advance = {home: probs["home"] + probs["draw"] * 0.5,
               away: probs["away"] + probs["draw"] * 0.5}

    return {"moneyline": moneyline, "spread": spread, "totals": totals, "btts": btts,
            "corners": corners, "score": score, "scorers": scorers, "advance": advance,
            "home": home, "away": away}


def _model_prob_for(series: str, sub: str, lk: Dict) -> Tuple[Optional[float], str]:
    """Map a Kalshi contract's yes_sub_title to the model probability. Returns (prob, clean_label)."""
    sub = (sub or "").strip()
    home, away = lk["home"], lk["away"]
    s = sub

    if series == "KXWCGAME":                       # 'Reg Time: Colombia' | 'Reg Time: Tie'
        pick = s.split(":", 1)[-1].strip()
        if pick.lower() in ("tie", "draw"):
            return lk["moneyline"].get("Draw"), f"{home} v {away}: Draw"
        team = _canon(pick)
        return lk["moneyline"].get(team), f"{home} v {away}: {team} win"

    if series == "KXWCSPREAD":                     # 'Goal Diff Reg Time: Colombia wins by more than 1.5 goals'
        m = re.search(r"(.+?)\s+wins by more than\s+([\d.]+)", s)
        if not m:
            return None, sub
        team = _canon(m.group(1).split(":")[-1].strip())
        line = float(m.group(2))
        key = f"{team} wins by {int(line) + 1}+"    # >1.5 => by 2+, >2.5 => by 3+
        return lk["spread"].get(key), f"{team} wins by {int(line)+1}+"

    if series == "KXWCTOTAL":                       # 'Reg Time: Over 2.5 goals scored'
        m = re.search(r"(Over|Under)\s+([\d.]+)", s)
        if not m:
            return None, sub
        key = f"{m.group(1)} {m.group(2)}"
        return lk["totals"].get(key), f"{home} v {away}: {key} goals"

    if series == "KXWCBTTS":
        # Kalshi lists ONE BTTS market; its YES contract = both teams score (yes_sub_title is
        # "Reg Time: Both Teams To Score", which has no "yes"/"no" token). So the YES side is
        # always "BTTS Yes"; the "No" side is offered separately as a buy-NO in the assembler.
        return lk["btts"].get("BTTS: Yes"), f"{home} v {away}: BTTS Yes"

    if series == "KXWCCORNERS":                     # 'N+ corners'
        m = re.search(r"(\d+)\+", s)
        if not m:
            return None, sub
        return lk["corners"].get(f"{m.group(1)}+ corners"), f"{home} v {away}: {m.group(1)}+ corners"

    if series == "KXWCSCORE":                        # 'i-j'
        m = re.search(r"(\d+)\s*[-–]\s*(\d+)", s)
        if not m:
            return None, sub
        key = f"{m.group(1)}-{m.group(2)}"
        return lk["score"].get(key), f"{home} v {away}: {key}"

    if series == "KXWCGOAL":                          # 'James Rodriguez: 2+'
        m = re.search(r"(.+?):\s*(\d+)\+", s)
        if not m:
            return None, sub
        name, n = m.group(1).strip(), m.group(2)
        rec = lk["scorers"].get(name)
        # tolerate small name spelling diffs (accents) by loose match
        if not rec:
            for k, v in lk["scorers"].items():
                if k.split()[-1].lower() == name.split()[-1].lower():
                    rec = v
                    break
        if not rec:
            return None, sub
        return rec.get(n), f"{name} {n}+ goals"

    if series == "KXWCADVANCE":                       # 'Colombia advances'
        team = _canon(s.replace("advances", "").strip())
        return lk["advance"].get(team), f"{team} to advance"

    return None, sub


# ---------------- evaluation ----------------

def _evaluate(model_prob: float, price: float, book_prob: Optional[float],
              book_count: int) -> Dict:
    """
    Fair value = model blended with the multi-book consensus when we have it; otherwise
    model only. Edge/EV are vs the Kalshi YES price (buy YES at `price`, pays $1).
    """
    if book_prob is not None:
        # Trust the sharp book consensus as the market prior (same weighting as the board),
        # but pulled toward the model. This is the "use other books" cross-check.
        fair = MODEL_WEIGHT * model_prob + (1 - MODEL_WEIGHT) * book_prob
        sources = f"model + {book_count} books"
        agree = abs(model_prob - book_prob) <= 0.06
    else:
        fair = model_prob
        sources = "model only (Kalshi-exclusive market)"
        agree = None

    ev = (fair / price - 1) if price > 0 else 0.0
    edge = fair - price
    # Value gate: meaningful edge, +EV, and not a longshot/heavy-fav trap.
    value = (edge >= MIN_EDGE and ev > 0 and LONGSHOT_FLOOR <= price <= HEAVY_FAV_CAP)
    # Cross-book discipline: if the books and the model disagree sharply, we DON'T trust the
    # inflated edge — that's a red flag (stale price, bad mapping, or info we don't have),
    # not a green light. Only flag value when the books corroborate, or when no book covers it.
    if agree is False:
        value = False
    # Confidence: highest when books corroborate the model AND there's real liquidity.
    if book_prob is not None and agree:
        conf = "high"
    elif book_prob is not None:
        conf = "medium"        # books exist but disagree with model -> be careful
    else:
        conf = "model-only"
    return {
        "model_prob": round(model_prob, 4),
        "book_prob": round(book_prob, 4) if book_prob is not None else None,
        "fair_prob": round(fair, 4),
        "kalshi_price_cents": round(price * 100, 1),
        # Real decimal odds for parlay math: a Kalshi YES contract costs `price` and pays $1,
        # so decimal odds = 1/price. This is the TRADEABLE price — the combo builder uses this
        # (not the model's 1/prob fair odds) so combined EV/payout are real, not fictional.
        "market_odds_decimal": round(1.0 / price, 4) if price > 0 else None,
        "edge": round(edge, 4),
        "ev_per_dollar": round(ev, 4),
        "value_bet": value,
        "tier": _tier(fair),
        "confidence": conf,
        "books_agree": agree,
        "sources": sources,
    }


# ---------------- fetch + assemble ----------------

async def _fetch_series(client: httpx.AsyncClient, series: str) -> List[Dict]:
    for attempt in range(3):
        try:
            r = await client.get(f"{KALSHI_BASE}/events",
                                 params={"series_ticker": series, "with_nested_markets": "true",
                                         "status": "open", "limit": 60})
            if r.status_code == 429:                 # rate limited -> back off and retry
                await asyncio.sleep(0.6 * (attempt + 1))
                continue
            r.raise_for_status()
            return [{**e, "_series": series} for e in r.json().get("events", [])]
        except Exception as exc:
            if attempt == 2:
                print(f"[kalshi_single] {series} fetch failed: {exc}")
    return []


def _book_consensus_index(board: List[Dict]) -> Dict[Tuple[str, str], Dict]:
    """
    Vig-free moneyline consensus across ALL bookmakers on the sportsbook board (not just
    one). Keyed by (home, away). This is the 'other books' signal for the moneyline market.
    """
    idx = {}
    for ev in board:
        home, away = ev.get("home"), ev.get("away")
        h2h = (ev.get("markets") or {}).get("h2h") or {}
        if not all(h2h.get(k) for k in ("home", "draw", "away")):
            continue
        vf = P.remove_vig([h2h["home"], h2h["draw"], h2h["away"]])
        idx[(home, away)] = {"home": vf[0], "draw": vf[1], "away": vf[2], "n": 1}
    return idx


def _book_prob_for(series: str, clean_label: str, lk: Dict,
                   cons: Optional[Dict]) -> Optional[float]:
    """Pull the matching consensus probability for markets the books cover (moneyline today)."""
    if not cons:
        return None
    if series == "KXWCGAME":
        pick = clean_label.split(":")[-1].strip()   # 'Ghana win' | 'Draw' (not the 'A v B' prefix)
        if "Draw" in pick:
            return cons["draw"]
        return cons["home"] if lk["home"] in pick else cons["away"]
    if series == "KXWCADVANCE":     # advance ≈ win + half the draws, from the book consensus
        if lk["home"] in clean_label:
            return cons["home"] + cons["draw"] * 0.5
        return cons["away"] + cons["draw"] * 0.5
    return None  # spreads/totals/props: sportsbook lines not pulled yet -> model-only


async def get_single_bets(board: Optional[List[Dict]] = None) -> List[Dict]:
    """
    All individual Kalshi WC bets, categorized, each with model fair value + cross-book check.
    `board` = the sportsbook odds board (optional) used for the multi-book consensus.
    """
    age = time.time() - float(_CACHE["ts"])
    if _CACHE["data"] is not None and age < _CACHE_TTL:
        return _CACHE["data"]  # type: ignore[return-value]

    cons_idx = _book_consensus_index(board or [])

    async with httpx.AsyncClient(timeout=25, headers=_UA) as client:
        all_events = []
        for batch in await asyncio.gather(*[_fetch_series(client, s) for s in SERIES]):
            all_events += batch

        # Price every market's order book — but THROTTLED. Firing hundreds of concurrent
        # requests trips Kalshi's rate limit (429); a small semaphore keeps us under it.
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

    # Group by matchup so the model runs once per game.
    lookups: Dict[Tuple[str, str], Dict] = {}
    out: List[Dict] = []
    for e in all_events:
        series = e["_series"]
        teams = _teams_from_title(e.get("title", ""))
        if not teams:
            continue
        home, away = teams
        lk = lookups.get((home, away)) or lookups.setdefault((home, away), _model_lookups(home, away))
        cons = cons_idx.get((home, away))
        cat, bet_type = SERIES[series]

        for m in (e.get("markets") or []):
            yes_price = prices.get(m.get("ticker"))
            if yes_price is None:
                continue   # untraded -> no live price to evaluate honestly
            mp, label = _model_prob_for(series, m.get("yes_sub_title", ""), lk)
            if mp is None:
                continue
            # Buy-YES side, plus the buy-NO side for two-sided markets (e.g. BTTS No).
            sides = [("yes", mp, yes_price, label)]
            if series in _TWO_SIDED:
                no_prob, no_label = _no_side_label(series, mp, label)
                sides.append(("no", no_prob, 1.0 - yes_price, no_label))
            for side, p_model, p_price, lbl in sides:
                if not (0.0 < p_price < 1.0):
                    continue                          # no tradeable price on this side
                book_prob = _book_prob_for(series, lbl, lk, cons)
                ev = _evaluate(p_model, p_price, book_prob, cons["n"] if cons else 0)
                if series in _REFERENCE_SERIES:       # player props: reference-only, never a value flag
                    ev["value_bet"] = False
                    ev["confidence"] = "reference"
                    ev["sources"] = "model rate — not lineup/penalty-taker/injury adjusted (reference only)"
                out.append({
                    "ticker": m.get("ticker"), "side": side, "category": cat, "bet_type": bet_type,
                    "home": home, "away": away, "selection": lbl,
                    "event_title": e.get("title"), **ev,
                })

    # Best value first, but keep everything browsable.
    out.sort(key=lambda b: (b["value_bet"], b["ev_per_dollar"]), reverse=True)
    _CACHE["data"] = out
    _CACHE["ts"] = time.time()
    return out
