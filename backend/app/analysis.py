"""
The brain that turns 'model probabilities + live odds' into actionable, honest calls:
  - per-outcome edge / EV / Kelly stake
  - Safe / Mid / Risky tiering (by hit-probability, only surfacing +EV bets)
  - combo (parlay) evaluation
  - cash-out / hedge decisions when the live picture changes

Sport-aware: every model call dispatches through app.sports, so the same logic serves
soccer (World Cup) and MLB. Soccer is the default when no sport is given, so existing
behaviour and URLs are unchanged.
"""
from __future__ import annotations
from typing import Dict, List, Optional
import random

from app import probability as P
from app import sports
from app.config import settings

def _stage_for(sport: str) -> Optional[Dict]:
    """Tournament stage only applies to soccer; MLB has no bracket."""
    if sports.normalize(sport) == "soccer":
        from app.tournament import current_stage
        return current_stage()
    return None


# AUTO-builder discipline: on markets no sportsbook cross-checks ("model-only"), a big model-vs-
# Kalshi gap is our blind spot, not confirmed value. We keep only this fraction of that gap and
# shrink the rest back toward the live Kalshi price, so an AUTO-built combo (optimizer or risk
# tiers) can't compound unconfirmed edges into a fantasy payout. Manual hand-picks skip this —
# you chose the leg, you own the read (they post fair_prob straight to /api/combo).
MODEL_ONLY_TRUST = 0.5


def _legs_from_singles(singles: List[Dict], games: Optional[List[Dict]] = None) -> List[Dict]:
    """Turn real-priced Kalshi singles (from data_sources.kalshi_single / kalshi_mlb) into
    parlay candidate legs for the AUTO builders. Uses the TRADEABLE Kalshi decimal odds — never
    the model's own 1/prob fair odds — so any combo prices its payout/EV off real money. The
    per-leg prob is the market-shrunk fair_prob, pulled further toward the Kalshi price on
    model-only markets (see MODEL_ONLY_TRUST). Optionally restrict to a set of requested games.
    Reference-only markets (goalscorer/props) are excluded."""
    want = {(g.get("home"), g.get("away")) for g in games} if games else None
    out: List[Dict] = []
    for b in singles:
        odds = b.get("market_odds_decimal")
        if odds is None:                               # untraded / no live book -> can't price honestly
            continue
        if b.get("confidence") == "reference":         # props are lineup-blind: never auto-parlay
            continue
        if want is not None and (b.get("home"), b.get("away")) not in want:
            continue
        market = b.get("bet_type") or b.get("category") or ""
        sel = b.get("selection", "")
        # fair_prob = model blended toward the sharp book price; the honest per-leg prob.
        prob = b.get("fair_prob", b.get("model_prob"))
        # Extra discipline where no book confirms the price: keep only part of the model's edge,
        # scaled by how much we trust that market type. Corners/correct-score are the crudest
        # sub-models (rate-based, no tactics/lineups), so their unconfirmed "edges" get shrunk
        # hardest — a big model-vs-Kalshi gap there is model blindness, not value.
        if (b.get("confidence") or "").startswith("model") and odds and prob is not None:
            implied = 1.0 / odds
            prob = implied + (prob - implied) * MODEL_ONLY_TRUST * _reliability(market)
        out.append({
            "home": b.get("home"), "away": b.get("away"),
            "market": market, "mtype": market,
            "selection": sel,
            "label": sel if " v " in sel else f"{b.get('home')} v {b.get('away')}: {sel}",
            "model_prob": prob,
            "market_odds_decimal": odds,
            # Carry the exact Kalshi ticker so the built combo is one-tap placeable across ALL
            # markets (not just moneyline, which is all _find_market can look up by name).
            "kalshi_ticker": b.get("ticker"),
            "side": b.get("side", "yes"),          # buy YES, or buy NO (e.g. "BTTS No")

            "value_bet": bool(b.get("value_bet")),
            "ev": b.get("ev_per_dollar", 0.0),
            "edge": b.get("edge"),
            # "high"/"medium" = a sharp sportsbook line cross-checks this; "model-only" = no book
            # covers it, so a big model-vs-Kalshi gap is our blind spot, not confirmed value.
            "confidence": b.get("confidence"),
        })
    return out


def _is_model_only(leg: Dict) -> bool:
    """No sportsbook cross-checks this market — the model is flying blind vs the Kalshi price."""
    return (leg.get("confidence") or "").startswith("model")


def _low_reliability(leg: Dict) -> bool:
    """Crude-model markets (corners, correct score, goalscorer): the auto-builders never stack
    more than one of these per combo — two crude reads multiplied is compounding blindness."""
    return _reliability(leg.get("market")) < 0.70


def apply_news_risk(legs_pool: List[Dict], risk_map: Dict) -> None:
    """Fold live injury/availability news (Google News, via app.news_research) into the leg pool.
    The model knows nothing about lineups, so a flagged matchup means its inputs may be stale and
    the live market is more likely to be right: shrink the leg's prob further toward the traded
    price, kill the value flag if that erases the edge, and carry the headlines on the leg so any
    combo it lands in can show WHY it was downgraded. Mutates the legs in place."""
    NEWS_TRUST = 0.6   # keep 60% of the remaining model-vs-market gap on news-flagged games
    for leg in legs_pool:
        r = risk_map.get((leg.get("home"), leg.get("away")))
        if not r or not r.get("alerts"):
            continue
        odds = leg.get("market_odds_decimal")
        if odds and leg.get("model_prob") is not None:
            implied = 1.0 / odds
            leg["model_prob"] = implied + (leg["model_prob"] - implied) * NEWS_TRUST
            leg["edge"] = leg["model_prob"] - implied
            leg["ev"] = leg["model_prob"] * odds - 1.0
            leg["value_bet"] = bool(leg.get("value_bet")) and leg["edge"] >= MIN_EDGE and leg["ev"] > 0
        leg["news_alert"] = True
        leg["news_headlines"] = r["alerts"][:2]


def _news_note(legs: List[Dict]) -> str:
    """One honest sentence listing the flagged games in a built combo (with a headline each)."""
    seen, parts = set(), []
    for l in legs:
        g = (l.get("home"), l.get("away"))
        if not l.get("news_alert") or g in seen:
            continue
        seen.add(g)
        head = (l.get("news_headlines") or [""])[0]
        parts.append(f"{g[0]} v {g[1]} (\"{head}\")")
    if not parts:
        return ""
    return (" ⚠️ Injury/availability news on " + "; ".join(parts)
            + " — those legs were already shrunk toward the market; read the headlines before placing.")


# 5 risk tiers -> (TARGET per-leg probability, number of legs).
RISK_TIERS = {
    "safe":          (0.88, 3),
    "moderate safe": (0.78, 3),
    "slight risk":   (0.67, 3),
    "medium risk":   (0.55, 3),
    "risky":         (0.42, 4),
}


def _resolve_tier(style: str, stage: Optional[Dict]) -> tuple[float, int]:
    """Tier target + leg count, adjusted for a soccer knockout stage (safer, fewer legs)."""
    target, n = RISK_TIERS.get(style, RISK_TIERS["moderate safe"])
    if stage and stage.get("is_knockout"):
        depth = stage.get("depth", 1)
        target = min(0.93, target + 0.03 + 0.01 * depth)
        if n >= 4:
            n -= 1
    return target, n


# How much we trust each market type as a parlay leg. Match result is the model's
# strongest suit; corners are rate-based (no tactics/game-state) so we lean on them least.
# A leg's pick score = distance-from-target / reliability, so higher-trust markets win ties.
_MARKET_RELIABILITY = {
    "match result": 1.00, "moneyline": 1.00, "total goals": 0.90, "total runs": 0.90,
    "double chance": 0.90, "run line": 0.85, "winning margin": 0.82, "spread": 0.82,
    "team total": 0.80, "both teams to score": 0.78, "first 5": 0.80,
    "to advance": 0.85,
    "total corners": 0.62, "correct score": 0.55, "goalscorer": 0.50,
}


def _reliability(market: str) -> float:
    m = (market or "").lower()
    for k, v in _MARKET_RELIABILITY.items():
        if k in m:
            return v
    return 0.75


def build_auto_parlay(legs_pool: List[Dict], style: str = "moderate safe", max_legs: Optional[int] = None,
                      bankroll: Optional[float] = None, sport: str = "soccer") -> Dict:
    """Auto-build a parlay from REAL, tradeable Kalshi-priced legs (see _legs_from_singles) at
    the chosen risk tier. Legs are picked near the tier's target hit-probability, but DIVERSIFIED
    — one leg per game and (where possible) one per market type, favouring the markets the model
    is most reliable on — so you don't get three near-identical corner-unders. Because legs carry
    live Kalshi odds, the payout + EV are real, not the model's own fair odds. Soccer is
    stage-aware; MLB is not."""
    stage = _stage_for(sport)
    style = style.lower().strip()
    target, n = _resolve_tier(style, stage)
    if max_legs:
        n = max_legs

    # Only legs we can actually price + trade (real Kalshi odds). Reference props already dropped.
    # Drop degenerate near-locks (decimal odds < 1.13 ≈ >88% implied, e.g. "Over 0.5 goals"):
    # they pay almost nothing, add no real edge, and just make every parlay look the same.
    cands = [c for c in legs_pool
             if c.get("market_odds_decimal") and c["model_prob"] is not None
             and c["market_odds_decimal"] >= 1.13]
    if not cands:
        return {"error": "no live Kalshi-priced legs for those games right now"}

    # A tier is a PROMISE about per-leg risk, so only legs genuinely NEAR the target qualify.
    # Without this hard band, a thin knockout board quietly fills a "safe" parlay with the
    # closest thing it has — coin-flips — which is exactly how nonsense combos happen.
    TIER_BAND = 0.10
    in_band = [c for c in cands if abs(c["model_prob"] - target) <= TIER_BAND]
    if len(in_band) < 2:
        in_band = [c for c in cands if abs(c["model_prob"] - target) <= TIER_BAND + 0.05]
    if len(in_band) < 2:
        closest = min(cands, key=lambda c: abs(c["model_prob"] - target))
        return {"error": (f"No legs near the '{style}' risk level on today's board "
                          f"(target ~{round(target * 100)}% per leg; closest live leg is "
                          f"{closest.get('label')} at {round(closest['model_prob'] * 100)}%). "
                          f"An honest engine won't dress that up as '{style}' — pick another tier "
                          f"or wait for the board to fill in.")}
    # The safest tiers also step around games with fresh injury/availability news — a leg can't
    # be "safe" while the starting XI is in doubt (unless avoiding them empties the pool).
    if target >= 0.70:
        clean = [c for c in in_band if not c.get("news_alert")]
        if len(clean) >= 2:
            in_band = clean

    # Rank by closeness to the tier target, scaled by how much we trust that market (better EV
    # breaks ties), then take a SHORTLIST of the best-fitting legs and shuffle it — so re-clicking
    # a tier surfaces a fresh parlay drawn from legs that fit that risk level, not the same three.
    def _fit(c):
        return abs(c["model_prob"] - target) / _reliability(c["market"])
    in_band.sort(key=lambda c: (round(_fit(c), 2), -(c.get("ev") or 0)))
    shortlist = in_band[: max(n * 4, 12)]
    random.shuffle(shortlist)

    def _fill(pool: List[Dict], distinct_type: bool, distinct_game: bool):
        used_games = {(l["home"], l["away"]) for l in picked}
        used_types = {l["market"] for l in picked}
        # (game, market) already used: two legs of the SAME market in the SAME game are mutually
        # exclusive or nested (win vs draw, 8+ vs 9+ corners, over vs under) — never stack those.
        used_game_market = {(l["home"], l["away"], l["market"]) for l in picked}
        for c in pool:
            if len(picked) >= n:
                break
            gm = (c["home"], c["away"], c["market"])
            if gm in used_game_market:                           # blocks contradictory/nested same-game legs
                continue
            if distinct_game and (c["home"], c["away"]) in used_games:
                continue
            if distinct_type and c["market"] in used_types:
                continue
            # At most ONE crude-model leg (corners/correct score) per auto combo.
            if _low_reliability(c) and any(_low_reliability(l) for l in picked):
                continue
            picked.append(c)
            used_games.add((c["home"], c["away"]))
            used_types.add(c["market"])
            used_game_market.add(gm)

    # Prefer independent legs (one game each, varied markets); only stack same-game legs as a last
    # resort to reach the requested count on a thin slate — evaluate_combo haircuts the correlation.
    picked: List[Dict] = []
    _fill(shortlist, distinct_type=True, distinct_game=True)   # best: varied markets, one game each
    _fill(shortlist, distinct_type=False, distinct_game=True)  # then: one game each, any market
    _fill(shortlist, distinct_type=False, distinct_game=False) # last: same-game legs to hit the count
    n_tier_legs = len(picked)

    # The user asked for n legs. If the tier band can't supply them all, DELIVER the count anyway
    # by topping up with the nearest-to-target legs outside the band (news-clean ones first) and
    # disclose the drift — silently returning fewer legs than asked reads as broken.
    if len(picked) < n:
        in_ids = {id(c) for c in in_band}
        extras = sorted((c for c in cands if id(c) not in in_ids),
                        key=lambda c: (bool(c.get("news_alert")), round(_fit(c), 2),
                                       -(c.get("ev") or 0)))
        _fill(extras, distinct_type=False, distinct_game=True)
        _fill(extras, distinct_type=False, distinct_game=False)

    if not picked:
        return {"error": f"no live Kalshi legs found for '{style}' that day"}
    res = evaluate_combo(picked, bankroll, sport=sport)
    res["style"] = style
    if n_tier_legs < len(picked):
        off = len(picked) - n_tier_legs
        res["note"] = (res.get("note", "")
                       + f" ⚠️ Only {n_tier_legs} live leg(s) genuinely sit at the '{style}' risk "
                         f"level — the other {off} are the closest available, so treat this parlay "
                         f"as running away from its label (per-leg probabilities above are the "
                         f"honest read).")
    if len(picked) < n:
        res["note"] = (res.get("note", "")
                       + f" The live board can only support {len(picked)} of the {n} legs you asked "
                         f"for without stacking contradictory or duplicate-crude-model legs — a "
                         f"shorter parlay beats a padded one.")
    res["note"] = res.get("note", "") + _news_note(picked)
    if stage:
        res["stage"] = stage["stage"]
        res["stage_label"] = stage["label"]
    return res


def _legset(combo) -> frozenset:
    """A parlay's identity = its set of (game, selection) legs, order-independent."""
    return frozenset((l.get("home"), l.get("away"), l.get("selection")) for l in combo)


def build_optimal_parlay(legs_pool: List[Dict], max_legs: int = 3,
                         bankroll: Optional[float] = None,
                         exclude: Optional[set] = None, sport: str = "soccer") -> Dict:
    """The '⭐ Best Parlay' optimizer. Builds ONLY from legs where the model genuinely beats the
    live Kalshi price (real +EV value singles across ANY market — moneyline, spread, totals,
    corners, BTTS...), then picks the combination with the best balance of edge and hit-
    probability. One leg per game so combos stay (roughly) independent — no stacking correlated
    legs from the same match. Legs carry real Kalshi odds, so the EV is real.

    DISCIPLINE (this button actively RECOMMENDS, so it can't confidently stack unverifiable
    edges into a fantasy parlay): a "model-only" leg sits on a market no sportsbook cross-checks,
    so a huge model-vs-Kalshi gap there is our blind spot, not free money. We (1) drop model-only
    legs whose edge is too-good-to-be-true, and (2) cap how many model-only legs a combo can
    stack. Note most soccer edge IS model-only (corners/totals/BTTS aren't priced by books, and
    the moneyline the books cover is efficiently priced) — so we allow a couple, just not a 4-leg
    longshot pile. The manual builder + risk tiers stay fully open — there you pick and own it."""
    import itertools
    exclude = exclude or set()
    # A model-only "value" bet this big has no book to confirm it — treat it as the model being
    # wrong, not an edge, and keep it out of an auto-recommended combo. Then cap how many of the
    # (survivor) model-only legs we'll stack, so the payout can't balloon on unconfirmed edges.
    MODEL_ONLY_MAX_EV = 0.35
    MAX_MODEL_ONLY_LEGS = 2

    # Real +EV singles only. _legs_from_singles already dropped reference props + untraded legs.
    # The too-good-to-be-true cap tightens with market crudeness: an unconfirmed corners "edge"
    # gets far less benefit of the doubt than an unconfirmed moneyline one.
    value_legs = [l for l in legs_pool if l.get("value_bet") and l.get("market_odds_decimal")
                  and not (_is_model_only(l)
                           and (l.get("ev") or 0) > MODEL_ONLY_MAX_EV * _reliability(l.get("market")))]
    if not value_legs:
        return {"optimize": True, "error": "No +EV edge on this day's board — the honest move is no bet."}

    value_legs.sort(key=lambda l: l.get("ev", 0.0), reverse=True)
    value_legs = value_legs[:14]

    ranked = []
    for r in range(1, min(max_legs, len(value_legs)) + 1):
        for combo in itertools.combinations(value_legs, r):
            # One leg per game -> distinct matchups only (avoids stacking correlated same-game legs).
            if len({(l["home"], l["away"]) for l in combo}) != r:
                continue
            # Don't pile up unverifiable (model-only) legs into a longshot fantasy,
            # and never stack two crude-model legs (corners/correct score) together.
            if sum(_is_model_only(l) for l in combo) > MAX_MODEL_ONLY_LEGS:
                continue
            if sum(_low_reliability(l) for l in combo) > 1:
                continue
            res = evaluate_combo(list(combo), bankroll, sport=sport)
            if res["ev_per_dollar"] <= 0:
                continue
            score = res["ev_per_dollar"] * (res["combined_model_prob"] ** 0.5)
            ranked.append((score, _legset(combo), res))
    if not ranked:
        return {"optimize": True, "error": "No +EV combination — best is a single value bet."}

    ranked.sort(key=lambda x: x[0], reverse=True)
    fresh = [r for r in ranked if r[1] not in exclude]
    if not fresh:
        res = ranked[0][2]
        res["optimize"] = True
        res["all_placed"] = True
        res["note"] = "You've already got every +EV parlay on this board. This is the strongest one again."
        return res

    # Honor the requested size when a +EV combo of that size exists; otherwise fall back to the
    # best smaller one and say so — never pad a parlay with -EV legs just to hit a leg count.
    full_size = [r for r in fresh if r[2]["leg_count"] == max_legs]
    pick_from = full_size or fresh
    res = dict(pick_from[0][2])
    res["optimize"] = True
    stage = _stage_for(sport)
    if stage:
        res["stage_label"] = stage["label"]
    res["alternatives"] = [r[2] for r in pick_from[1:4]]
    n_model_only = sum(_is_model_only(l) for l in res.get("legs", []))
    res["note"] = (res.get("note", "")
                   + " ⭐ Optimizer discipline: skips too-good-to-be-true model-only edges and caps"
                     " how many legs no sportsbook cross-checks."
                   + ("" if full_size or max_legs <= 1 else
                      f" No +EV {max_legs}-leg combo exists on this board — padding to {max_legs} "
                      f"would mean adding losing legs, so this is the strongest "
                      f"{res['leg_count']}-leg play instead.")
                   + (" All legs here are book-corroborated." if n_model_only == 0 else
                      f" {n_model_only} leg(s) are model-only (no book confirmation) — size accordingly.")
                   + _news_note(res.get("legs", [])))
    return res


def next_best_tips(matches: List[Dict], exclude_selections: Optional[set] = None,
                   n: int = 3) -> List[Dict]:
    """'Bounce back' tips: the strongest fresh +EV plays to put down next."""
    exclude_selections = exclude_selections or set()
    tips = []
    for m in matches:
        if m.get("status") != "upcoming":
            continue
        for s in m.get("suggestions", []):
            if not s.get("value_bet"):
                continue
            key = (m["home"], m["away"], s.get("selection"))
            if key in exclude_selections:
                continue
            tips.append({
                "home": m["home"], "away": m["away"], "market": s.get("market"),
                "selection": s.get("selection"),
                "label": f"{s.get('selection')} ({m['home']} v {m['away']})",
                "model_prob": s.get("model_prob"), "market_odds_decimal": s.get("market_odds_decimal"),
                "edge": s.get("edge"), "ev": s.get("ev_per_dollar"),
            })
    tips.sort(key=lambda t: (t.get("ev") or 0), reverse=True)
    return tips[:n]


# Minimum edge (fair prob - market prob) before we call something a "value bet".
MIN_EDGE = 0.03
# We shrink the model toward the vig-free market price. MODEL_WEIGHT = how much we trust
# our model vs the (sharp) market. 0.40 = the market is a stronger prior than our model.
MODEL_WEIGHT = 0.40
# Guardrails against the classic longshot trap.
LONGSHOT_FLOOR = 0.12
HEAVY_FAV_CAP = 0.82


def _tier(prob: float) -> str:
    if prob >= 0.60:
        return "safe"
    if prob >= 0.38:
        return "mid"
    return "risky"


def _build_suggestion(label: str, selection: str, model_prob: float,
                      market_odds: float, market_prob_vigfree: float,
                      bankroll: float) -> Dict:
    fair = MODEL_WEIGHT * model_prob + (1 - MODEL_WEIGHT) * market_prob_vigfree
    ev = P.expected_value(fair, market_odds)
    ed = P.edge(fair, market_prob_vigfree)
    stake = P.recommended_stake(fair, market_odds, bankroll, settings.KELLY_FRACTION)
    value_bet = (
        ed >= MIN_EDGE and ev > 0
        and market_prob_vigfree >= LONGSHOT_FLOOR
        and market_prob_vigfree <= HEAVY_FAV_CAP
    )
    return {
        "market": label, "selection": selection,
        "model_prob": round(model_prob, 4),
        "fair_prob": round(fair, 4),
        "market_prob": round(market_prob_vigfree, 4),
        "market_odds_decimal": market_odds,
        "market_odds_american": P.decimal_to_american(market_odds),
        "edge": round(ed, 4),
        "ev_per_dollar": round(ev, 4),
        "value_bet": value_bet,
        "tier": _tier(fair),
        "stake": stake,
    }


def _analyze_soccer(match: Dict, model: Dict, suggestions: List[Dict], bankroll: float) -> None:
    home, away = match["home"], match["away"]
    markets = match.get("markets", {})
    h2h = markets.get("h2h") or {}
    if all(h2h.get(k) for k in ("home", "draw", "away")):
        vigfree = P.remove_vig([h2h["home"], h2h["draw"], h2h["away"]])
        for (key, name), mp in zip([("home", home), ("draw", "Draw"), ("away", away)], vigfree):
            suggestions.append(_build_suggestion("Match Result", name, model["probs"][key],
                                                 h2h[key], mp, bankroll))
    tot = markets.get("totals_2_5") or {}
    if tot.get("over") and tot.get("under"):
        vigfree = P.remove_vig([tot["over"], tot["under"]])
        for (key, name, mkey), mp in zip(
                [("over", "Over 2.5", "over_2_5"), ("under", "Under 2.5", "under_2_5")], vigfree):
            suggestions.append(_build_suggestion("Total Goals", name, model["totals"][mkey],
                                                 tot[key], mp, bankroll))
    btts = markets.get("btts") or {}
    if btts.get("yes") and btts.get("no"):
        vigfree = P.remove_vig([btts["yes"], btts["no"]])
        for (key, name, mkey), mp in zip(
                [("yes", "BTTS: Yes", "btts_yes"), ("no", "BTTS: No", "btts_no")], vigfree):
            suggestions.append(_build_suggestion("Both Teams To Score", name, model["totals"][mkey],
                                                 btts[key], mp, bankroll))


def _analyze_mlb(match: Dict, model: Dict, suggestions: List[Dict], bankroll: float) -> None:
    home, away = match["home"], match["away"]
    markets = match.get("markets", {})
    # Moneyline (2-way, no draw).
    h2h = markets.get("h2h") or {}
    if h2h.get("home") and h2h.get("away"):
        vigfree = P.remove_vig([h2h["home"], h2h["away"]])
        for (key, name), mp in zip([("home", home), ("away", away)], vigfree):
            suggestions.append(_build_suggestion("Moneyline", name, model["probs"][key],
                                                 h2h[key], mp, bankroll))
    # Total runs over/under (book odds present in demo; model-priced otherwise).
    tot = markets.get("totals") or {}
    if tot.get("over") and tot.get("under"):
        line = tot.get("line", model["totals"].get("line"))
        vigfree = P.remove_vig([tot["over"], tot["under"]])
        for (key, mkey), mp in zip([("over", "over"), ("under", "under")], vigfree):
            suggestions.append(_build_suggestion(f"Total Runs {line}", f"{key.title()} {line}",
                                                 model["totals"][mkey], tot[key], mp, bankroll))
    # Run line -1.5 / +1.5 (book odds present in demo).
    rl = markets.get("runline") or {}
    if rl.get("home") and rl.get("away"):
        em = sports.model("mlb").extended_markets(home, away,
                                                  match.get("sp_home", "avg"), match.get("sp_away", "avg"))
        rlm = {s["label"]: s["prob"] for s in em["markets"]["Run Line"]}
        vigfree = P.remove_vig([rl["home"], rl["away"]])
        for (key, label), mp in zip([("home", f"{home} -1.5"), ("away", f"{away} +1.5")], vigfree):
            suggestions.append(_build_suggestion("Run Line", label, rlm.get(label, 0.5),
                                                 rl[key], mp, bankroll))


def analyze_match(match: Dict, bankroll: Optional[float] = None, sport: Optional[str] = None) -> Dict:
    """Attach model output + ranked value suggestions to a normalized match dict."""
    bankroll = bankroll or settings.DEFAULT_BANKROLL
    sport = sports.normalize(sport or match.get("sport"))
    M = sports.model(sport)
    home, away = match["home"], match["away"]

    is_live = match.get("status") == "live" and match.get("live_score") is not None
    if is_live:
        sc = match["live_score"]
        if sport == "mlb":
            model = M.live_match_probabilities(home, away, sc["home"], sc["away"],
                                               match.get("live_inning") or 1,
                                               match.get("live_half") or "top",
                                               match.get("sp_home", "avg"), match.get("sp_away", "avg"))
            pre = M.match_probabilities(home, away, match.get("sp_home", "avg"), match.get("sp_away", "avg"))
        else:
            model = M.live_match_probabilities(home, away, sc["home"], sc["away"],
                                               match.get("live_minute", 0))
            pre = M.match_probabilities(home, away)
        model["pregame_probs"] = pre["probs"]
    else:
        model = (M.match_probabilities(home, away, match.get("sp_home", "avg"),
                                       match.get("sp_away", "avg")) if sport == "mlb"
                 else M.match_probabilities(home, away))

    suggestions: List[Dict] = []
    if sport == "mlb":
        _analyze_mlb(match, model, suggestions, bankroll)
    else:
        _analyze_soccer(match, model, suggestions, bankroll)

    suggestions.sort(key=lambda s: (s["value_bet"], s["ev_per_dollar"]), reverse=True)
    value_bets = [s for s in suggestions if s["value_bet"]]
    return {
        **match, "sport": sport, "model": model, "suggestions": suggestions,
        "best_value": value_bets[0] if value_bets else None,
        "value_count": len(value_bets),
    }


def evaluate_combo(legs: List[Dict], bankroll: Optional[float] = None, user: Optional[str] = None,
                   sport: str = "soccer") -> Dict:
    """Evaluate a parlay/combo. Each leg: {model_prob, market_odds_decimal, label}. Assumes
    legs are independent (true for unrelated games; correlated legs are riskier)."""
    bankroll = bankroll or settings.DEFAULT_BANKROLL
    if not legs:
        return {"error": "no legs provided"}

    combined_prob = combined_odds = 1.0
    for leg in legs:
        combined_prob *= float(leg["model_prob"])
        combined_odds *= float(leg["market_odds_decimal"])

    # Soccer knockouts are coin-flippier, so shrink the independent-Poisson combined prob by
    # the stage's combo factor. MLB has no stage => factor 1.0.
    stage = _stage_for(sport)
    combo_factor = stage.get("combo_factor", 1.0) if stage else 1.0

    # Same-game legs are NOT independent (e.g. "Over 2.5" + "BTTS Yes" tend to hit together), so
    # multiplying their probabilities overstates a same-game parlay's real chance. Haircut the
    # combined prob for each leg beyond the first in any one game — keeps same-game EV honest.
    from collections import Counter
    _game_counts = Counter((l.get("home"), l.get("away")) for l in legs)
    _extra_same_game = sum(c - 1 for c in _game_counts.values() if c > 1)
    SAME_GAME_CORR = 0.85
    corr_factor = SAME_GAME_CORR ** _extra_same_game

    from app.bet_log import reality_factor
    rf = reality_factor(user) if user else None
    adj_prob = combined_prob * combo_factor * corr_factor
    if rf:
        adj_prob *= rf
    adj_prob = min(max(adj_prob, 1e-6), 0.999)

    ev = P.expected_value(adj_prob, combined_odds)
    stake = P.recommended_stake(adj_prob, combined_odds, bankroll, settings.KELLY_FRACTION)
    return {
        "legs": legs, "leg_count": len(legs), "sport": sports.normalize(sport),
        "combined_model_prob": round(combined_prob, 4),
        "experience_adjusted_prob": round(adj_prob, 4),
        "reality_factor": rf,
        "stage": stage["stage"] if stage else None,
        "stage_combo_factor": combo_factor,
        "same_game_corr_factor": round(corr_factor, 3),
        "combined_odds_decimal": round(combined_odds, 2),
        "combined_odds_american": P.decimal_to_american(combined_odds),
        "payout_multiple": round(combined_odds, 2),
        "ev_per_dollar": round(ev, 4),
        "value_bet": ev > 0,
        "tier": _tier(adj_prob),
        "stake": stake,
        "note": (
            "Every leg you add multiplies the payout but also multiplies the ways to lose. "
            "Combos are almost always -EV unless each leg is independently +value."
            + (f" Knockout-stage shrink applied ({stage['stage']}, factor {combo_factor})."
               if stage and combo_factor < 1.0 else "")
            + (f" {_extra_same_game} same-game leg(s) detected — correlation haircut applied "
               f"(factor {round(corr_factor, 3)}); same-game legs move together, so the true "
               "combined odds are worse than independent math implies."
               if _extra_same_game else "")
            + (f" Estimate shrunk by your logged results (reality factor {rf})." if rf else "")
        ),
    }


def monitor_live_bets(scores: List[Dict], user: str, sport: str = "soccer") -> List[Dict]:
    """Watch pending logged parlays for the given sport against the LIVE games they involve
    and decide whether to flash CASH OUT."""
    from app.data_sources.odds_api import _estimate_minute, _estimate_inning
    from app import bet_log

    sport = sports.normalize(sport)
    M = sports.model(sport)
    is_mlb = sport == "mlb"

    idx = {}
    for s in scores:
        h, a = s.get("home_team"), s.get("away_team")
        score_map = {x["name"]: x.get("score") for x in (s.get("scores") or [])}
        idx[(h, a)] = {
            "completed": bool(s.get("completed")),
            "sa": int(score_map.get(h) or 0), "sb": int(score_map.get(a) or 0),
            "progress": (_estimate_inning(s.get("commence_time", "")) if is_mlb
                         else _estimate_minute(s.get("commence_time", ""))),
            "has_score": s.get("scores") is not None,
        }

    out = []
    for b in bet_log.pending_bets(user):
        # Crypto 15-min bets settle automatically on Kalshi in minutes — they carry no live
        # game feed, so the sports monitor must never try to track them (normalize would
        # otherwise fold the unknown 'crypto' sport into 'soccer' and mis-handle them).
        if b.get("sport") == "crypto":
            continue
        if sports.normalize(b.get("sport", "soccer")) != sport:
            continue
        legs = b.get("legs", [])
        if not legs or any(not isinstance(l, dict) for l in legs):
            continue
        combined_live = 1.0
        any_live = dead = False
        leg_views = []
        for leg in legs:
            entry = leg.get("model_prob") or leg.get("prob") or 0.5
            h, a = leg.get("home"), leg.get("away")
            g = idx.get((h, a)) if h and a else None
            live_p, state = entry, "not started"
            if g and (g["completed"] or (g["has_score"] or g["progress"] > 0)):
                if is_mlb:
                    p, trk = M.live_leg_probability(h, a, g["sa"], g["sb"], g["progress"],
                                                    leg.get("market"), leg.get("selection"))
                    unit = "inn"
                else:
                    p, trk = M.live_leg_probability(h, a, g["sa"], g["sb"], g["progress"],
                                                    leg.get("market"), leg.get("selection"))
                    unit = "'"
                if trk and p is not None:
                    live_p = p
                    state = "final" if g["completed"] else f"live {g['progress']}{unit}"
                    if not g["completed"]:
                        any_live = True
                    if g["completed"] and p < 0.02:
                        dead = True
                else:
                    state = "can't track live"
            combined_live *= live_p
            leg_views.append({
                "label": leg.get("label"), "entry_prob": round(entry, 3),
                "live_prob": round(live_p, 3), "state": state,
                "fading": live_p < entry * 0.7,
            })

        entry_comb = b.get("model_prob") or 0.0001
        health = combined_live / entry_comb if entry_comb else 1.0
        if dead:
            action, cash_out = "DEAD — leg already lost", True
        elif any_live and (combined_live < entry_comb * 0.70 or combined_live < 0.12):
            action, cash_out = "CASH OUT", True
        elif any_live and combined_live >= entry_comb:
            action, cash_out = "ON TRACK", False
        elif any_live:
            action, cash_out = "HOLD — watch it", False
        else:
            action, cash_out = "not started", False

        live_games, seen_g = [], set()
        for leg in legs:
            h, a = leg.get("home"), leg.get("away")
            g = idx.get((h, a)) if h and a else None
            if g and (h, a) not in seen_g and (g["has_score"] or g["progress"] > 0 or g["completed"]):
                seen_g.add((h, a))
                live_games.append({"home": h, "away": a, "sa": g["sa"], "sb": g["sb"],
                                   "minute": g["progress"], "completed": g["completed"]})

        out.append({
            "id": b["id"], "sport": sport, "legs": leg_views,
            "entry_prob": round(entry_comb, 3), "live_prob": round(combined_live, 3),
            "health": round(health, 2), "any_live": any_live,
            "action": action, "cash_out": cash_out, "live_games": live_games,
        })
    return out


def cashout_decision(entry_price: float, current_market_price: float,
                     model_prob: float, stake: float = 100.0) -> Dict:
    """Should you cash out / hedge a position? Prices are 0-1 (Kalshi/Polymarket cents/100)."""
    entry_price = min(max(entry_price, 1e-4), 1 - 1e-4)
    current_market_price = min(max(current_market_price, 1e-4), 1 - 1e-4)
    model_prob = min(max(model_prob, 1e-4), 1 - 1e-4)

    pnl_pct = (current_market_price - entry_price) / entry_price
    in_profit = current_market_price > entry_price
    market_vs_model = current_market_price - model_prob
    tables_turned = model_prob < entry_price * 0.8

    if tables_turned and current_market_price > entry_price * 0.6:
        action = "CASH OUT NOW"
        reason = ("Your side is deteriorating — the model's fair value has dropped well below "
                  "your entry. Take what the market still offers before it reprices.")
    elif in_profit and market_vs_model > 0.05:
        action = "CASH OUT / TAKE PROFIT"
        reason = ("The market is paying you MORE than the outcome is actually worth. "
                  "Selling here is +EV — lock the profit.")
    elif in_profit and market_vs_model >= -0.03:
        action = "PARTIAL CASH OUT"
        reason = ("You're in profit and the price is roughly fair. Bank some, let the rest ride.")
    elif not in_profit and model_prob > current_market_price + 0.05:
        action = "HOLD"
        reason = ("You're down on paper, but the model still rates your side higher than the "
                  "current price — the market is underrating you. Holding is +EV.")
    elif model_prob > current_market_price + 0.03:
        action = "HOLD"
        reason = "Model fair value is still above the sell price — there's edge left in the position."
    else:
        action = "CONSIDER EXIT"
        reason = "No remaining edge: model value and market price are aligned. Free up the capital."

    return {
        "entry_price": round(entry_price, 4),
        "current_market_price": round(current_market_price, 4),
        "model_fair_value": round(model_prob, 4),
        "unrealized_pnl_pct": round(pnl_pct * 100, 2),
        "cashout_value": round(current_market_price * (stake / entry_price), 2),
        "tables_turned": tables_turned,
        "action": action, "reason": reason,
    }
