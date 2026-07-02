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
import re

from app import probability as P
from app import sports
from app.config import settings

# Knockout soccer uses a tighter set of clean markets for parlay legs — but not SO tight
# that only match-result + corners survive (which made every parlay corner spam). Totals and
# double-chance hold up fine in knockouts; we still drop the noisy legs (exact margin/spread,
# BTTS) that a tight, low-event knockout game makes unreliable.
_KNOCKOUT_RE = re.compile(r"match result|double chance|total goals|corners", re.I)


def _stage_for(sport: str) -> Optional[Dict]:
    """Tournament stage only applies to soccer; MLB has no bracket."""
    if sports.normalize(sport) == "soccer":
        from app.tournament import current_stage
        return current_stage()
    return None


def _candidate_legs(home: str, away: str, sport: str = "soccer",
                    stage: Optional[Dict] = None) -> List[Dict]:
    M = sports.model(sport)
    cfg = sports.config(sport)
    knockout = bool(stage and stage.get("is_knockout"))
    gate = _KNOCKOUT_RE if knockout else re.compile(cfg["parlay_markets"], re.I)
    em = (M.extended_markets(home, away, stage=stage) if sports.normalize(sport) == "soccer"
          else M.extended_markets(home, away))
    legs = []
    for cat, sels in em["markets"].items():
        if not gate.search(cat):
            continue
        mtype = "goalscorer" if "goalscorer" in cat.lower() else cat
        for s in sels:
            # A 90-min Draw in a soccer knockout is a trap leg (someone still advances) — skip it.
            if knockout and "match result" in cat.lower() and s["label"] == "Draw":
                continue
            # F5 'Tie' and any explicit tie/draw is a confusing standalone parlay leg — skip.
            if s["label"].lower().startswith("tie"):
                continue
            legs.append({
                "home": home, "away": away, "market": cat, "mtype": mtype,
                "selection": s["label"], "label": f"{home} v {away}: {s['label']}",
                "model_prob": s["prob"], "market_odds_decimal": s["fair_odds"],
            })
    return legs


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
    "total corners": 0.62, "correct score": 0.55, "goalscorer": 0.50,
}


def _reliability(market: str) -> float:
    m = (market or "").lower()
    for k, v in _MARKET_RELIABILITY.items():
        if k in m:
            return v
    return 0.75


def build_auto_parlay(games: List[Dict], style: str = "moderate safe", max_legs: Optional[int] = None,
                      bankroll: Optional[float] = None, sport: str = "soccer") -> Dict:
    """Auto-build a parlay across the sport's parlay-eligible markets at the chosen risk tier.
    Legs are picked near the tier's target probability, but DIVERSIFIED — one leg per game and
    (where possible) one per market type, favouring the markets the model is most reliable on —
    so you don't get three near-identical corner-unders. Soccer is stage-aware; MLB is not."""
    stage = _stage_for(sport)
    style = style.lower().strip()
    target, n = _resolve_tier(style, stage)
    if max_legs:
        n = max_legs

    cands = []
    for g in games:
        cands += _candidate_legs(g["home"], g["away"], sport, stage)
    # Player props are reference-only (lineup-blind) — never auto-build them into a parlay.
    cands = [c for c in cands if "goalscorer" not in c["mtype"].lower()]
    if not cands:
        return {"error": "no games/markets found for that day"}

    # Rank by closeness to the tier target, scaled by how much we trust that market.
    cands.sort(key=lambda c: abs(c["model_prob"] - target) / _reliability(c["market"]))

    def _fill(require_distinct_type: bool):
        used_games = {(l["home"], l["away"]) for l in picked}
        used_types = {l["market"] for l in picked}
        for c in cands:
            if len(picked) >= n:
                break
            if (c["home"], c["away"]) in used_games:
                continue
            if require_distinct_type and c["market"] in used_types:
                continue
            picked.append(c)
            used_games.add((c["home"], c["away"]))
            used_types.add(c["market"])

    picked: List[Dict] = []
    _fill(require_distinct_type=True)      # first pass: force market-type variety
    _fill(require_distinct_type=False)     # top up if the slate is too small to stay varied

    if not picked:
        return {"error": f"no legs found for '{style}' that day"}
    res = evaluate_combo(picked, bankroll, sport=sport)
    res["style"] = style
    if stage:
        res["stage"] = stage["stage"]
        res["stage_label"] = stage["label"]
    return res


def _legset(combo) -> frozenset:
    """A parlay's identity = its set of (game, selection) legs, order-independent."""
    return frozenset((l.get("home"), l.get("away"), l.get("selection")) for l in combo)


def build_optimal_parlay(matches: List[Dict], max_legs: int = 3,
                         bankroll: Optional[float] = None,
                         exclude: Optional[set] = None, sport: str = "soccer") -> Dict:
    """The '⭐ Best Parlay' optimizer. Builds ONLY from legs where the model genuinely beats
    the book (real +EV value bets on the primary win market), then picks the combination with
    the best balance of edge and hit-probability."""
    import itertools
    exclude = exclude or set()
    win_market = "Moneyline" if sports.normalize(sport) == "mlb" else "Match Result"
    value_legs = []
    for m in matches:
        for s in m.get("suggestions", []):
            if s["market"] == win_market and s.get("value_bet"):
                value_legs.append({
                    "home": m["home"], "away": m["away"], "market": win_market,
                    "selection": s["selection"], "label": f"{m['home']} v {m['away']}: {s['selection']}",
                    # Use the market-SHRUNK fair prob (what we actually trust), not the raw
                    # model prob. Multiplying raw model probs across legs compounds small
                    # per-leg errors into fantasy EV (the old "+199% parlay" bug).
                    "model_prob": s.get("fair_prob", s["model_prob"]),
                    "market_odds_decimal": s["market_odds_decimal"],
                    "ev": s["ev_per_dollar"],
                })
    if not value_legs:
        return {"optimize": True, "error": "No +EV edge on this day's board — the honest move is no bet."}

    value_legs.sort(key=lambda l: l["ev"], reverse=True)
    value_legs = value_legs[:14]

    ranked = []
    for r in range(1, min(max_legs, len(value_legs)) + 1):
        for combo in itertools.combinations(value_legs, r):
            if len({(l["home"], l["away"]) for l in combo}) != r:
                continue
            res = evaluate_combo([{k: l[k] for k in ("label", "model_prob", "market_odds_decimal",
                                  "home", "away", "market", "selection")} for l in combo],
                                 bankroll, sport=sport)
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

    res = dict(fresh[0][2])
    res["optimize"] = True
    stage = _stage_for(sport)
    if stage:
        res["stage_label"] = stage["label"]
    res["alternatives"] = [r[2] for r in fresh[1:4]]
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

    from app.bet_log import reality_factor
    rf = reality_factor(user) if user else None
    adj_prob = combined_prob * combo_factor
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
