"""
The brain that turns 'model probabilities + live odds' into actionable, honest calls:
  - per-outcome edge / EV / Kelly stake
  - Safe / Mid / Risky tiering (by hit-probability, only surfacing +EV bets)
  - combo (parlay) evaluation
  - cash-out / hedge decisions when the live picture changes
"""
from __future__ import annotations
from typing import Dict, List, Optional

from app import probability as P
from app.config import settings
import re
from app.soccer_model import (match_probabilities, live_match_probabilities,
                              live_leg_probability, extended_markets)

# Markets Kalshi allows in a parlay.
_KALSHI_RE = re.compile(r"match result|winning margin|spread|total goals|both teams|goalscorer", re.I)


def _candidate_legs(home: str, away: str) -> List[Dict]:
    legs = []
    for cat, sels in extended_markets(home, away)["markets"].items():
        if not _KALSHI_RE.search(cat):
            continue
        mtype = "goalscorer" if "goalscorer" in cat.lower() else cat
        for s in sels:
            legs.append({
                "home": home, "away": away, "market": cat, "mtype": mtype,
                "selection": s["label"], "label": f"{home} v {away}: {s['label']}",
                "model_prob": s["prob"], "market_odds_decimal": s["fair_odds"],
            })
    return legs


# 5 risk tiers -> (TARGET per-leg probability, number of legs). Each leg is chosen near
# this probability, so the tiers grade smoothly from near-locks to longshots.
RISK_TIERS = {
    "safe":          (0.88, 3),
    "moderate safe": (0.78, 3),
    "slight risk":   (0.67, 3),
    "medium risk":   (0.55, 3),
    "risky":         (0.42, 4),
}


def build_auto_parlay(games: List[Dict], style: str = "moderate safe", max_legs: Optional[int] = None,
                      bankroll: Optional[float] = None) -> Dict:
    """
    Auto-build a parlay across ALL Kalshi market types (who wins, margin, totals, BTTS,
    player props) at the chosen risk tier. One leg per (game, market type) so it's varied.
    Each leg is picked near the tier's target probability => smooth grading across tiers.
    """
    style = style.lower().strip()
    target, n = RISK_TIERS.get(style, RISK_TIERS["moderate safe"])
    if max_legs:
        n = max_legs

    cands = []
    for g in games:
        cands += _candidate_legs(g["home"], g["away"])
    if not cands:
        return {"error": "no games/markets found for that day"}

    # Closest to the tier's target probability first => distinct legs per risk level.
    cands.sort(key=lambda c: abs(c["model_prob"] - target))
    picked, used = [], set()
    for c in cands:
        key = (c["home"], c["away"], c["mtype"])
        if key in used:
            continue
        picked.append(c)
        used.add(key)
        if len(picked) >= n:
            break

    if not picked:
        return {"error": f"no legs found for '{style}' that day"}
    res = evaluate_combo(picked, bankroll)
    res["style"] = style
    return res


def _legset(combo) -> frozenset:
    """A parlay's identity = its set of (game, selection) legs, order-independent."""
    return frozenset((l.get("home"), l.get("away"), l.get("selection")) for l in combo)


def build_optimal_parlay(matches: List[Dict], max_legs: int = 3,
                         bankroll: Optional[float] = None,
                         exclude: Optional[set] = None) -> Dict:
    """
    The '⭐ Best Parlay' optimizer. Maximises money AND safety by building ONLY from legs
    where the model genuinely beats the book (real +EV value bets, match-result market),
    then picking the combination with the best balance of edge and hit-probability.

    `exclude` is a set of leg-sets (see _legset) already placed/shown — those are skipped so
    repeat requests surface FRESH parlays instead of the same one. Up to 3 next-best
    alternatives ride along under "alternatives".
    Honest fallback: if there's no +EV edge, it says so and offers the single best value bet.
    """
    import itertools
    exclude = exclude or set()
    value_legs = []
    for m in matches:
        for s in m.get("suggestions", []):
            if s["market"] == "Match Result" and s.get("value_bet"):
                value_legs.append({
                    "home": m["home"], "away": m["away"], "market": "Match Result",
                    "selection": s["selection"], "label": f"{m['home']} v {m['away']}: {s['selection']}",
                    "model_prob": s["model_prob"], "market_odds_decimal": s["market_odds_decimal"],
                    "ev": s["ev_per_dollar"],
                })
    if not value_legs:
        return {"optimize": True, "error": "No +EV edge on this day's board — the honest move is no bet."}

    # Rank every valid +EV combo by money*safety. One leg per game (independent events).
    ranked = []
    for r in range(1, min(max_legs, len(value_legs)) + 1):
        for combo in itertools.combinations(value_legs, r):
            if len({(l["home"], l["away"]) for l in combo}) != r:
                continue  # different games only
            res = evaluate_combo([{k: l[k] for k in ("label", "model_prob", "market_odds_decimal",
                                  "home", "away", "market", "selection")} for l in combo], bankroll)
            if res["ev_per_dollar"] <= 0:
                continue
            score = res["ev_per_dollar"] * (res["combined_model_prob"] ** 0.5)
            ranked.append((score, _legset(combo), res))
    if not ranked:
        return {"optimize": True, "error": "No +EV combination — best is a single value bet."}

    ranked.sort(key=lambda x: x[0], reverse=True)
    fresh = [r for r in ranked if r[1] not in exclude]
    if not fresh:
        # Everything good is already on your slip — say so, still show the strongest.
        res = ranked[0][2]
        res["optimize"] = True
        res["all_placed"] = True
        res["note"] = "You've already got every +EV parlay on this board. This is the strongest one again."
        return res

    res = dict(fresh[0][2])
    res["optimize"] = True
    res["alternatives"] = [r[2] for r in fresh[1:4]]  # next-best fresh parlays for variety
    return res


def next_best_tips(matches: List[Dict], exclude_selections: Optional[set] = None,
                   n: int = 3) -> List[Dict]:
    """
    'Bounce back' tips: the strongest fresh +EV plays to put down next — used after a
    parlay misses so there's always a smart next move, never a dead end.
    `exclude_selections` is a set of (home, away, selection) already placed today.
    """
    exclude_selections = exclude_selections or set()
    tips = []
    for m in matches:
        if m.get("status") != "upcoming":     # next plays are pre-match only
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

# A sharp market is information. We don't take the raw model at face value — we shrink it
# toward the vig-free market price. MODEL_WEIGHT is how much we trust our model vs the market.
# 0.40 = "the market is a stronger prior than our model" (correct against sharp books).
MODEL_WEIGHT = 0.40

# Guardrails against the classic longshot trap (tiny model errors at long odds -> fake huge EV).
LONGSHOT_FLOOR = 0.12   # don't flag value on sides the market itself rates below this.
HEAVY_FAV_CAP = 0.82    # don't claim to beat a sharp market on a heavy favorite.


def _tier(prob: float) -> str:
    """Risk tier is about how likely the bet is to LAND, not how juicy the payout is."""
    if prob >= 0.60:
        return "safe"
    if prob >= 0.38:
        return "mid"
    return "risky"


def _build_suggestion(label: str, selection: str, model_prob: float,
                      market_odds: float, market_prob_vigfree: float,
                      bankroll: float) -> Dict:
    # Shrink the model toward the market — this is what stops the model running wild on
    # blowouts and inventing edge the sharps would never leave on the table.
    fair = MODEL_WEIGHT * model_prob + (1 - MODEL_WEIGHT) * market_prob_vigfree
    ev = P.expected_value(fair, market_odds)
    ed = P.edge(fair, market_prob_vigfree)
    stake = P.recommended_stake(fair, market_odds, bankroll, settings.KELLY_FRACTION)

    value_bet = (
        ed >= MIN_EDGE
        and ev > 0
        and market_prob_vigfree >= LONGSHOT_FLOOR   # not a longshot trap
        and market_prob_vigfree <= HEAVY_FAV_CAP    # not fighting a sharp price on a megafav
    )
    return {
        "market": label,
        "selection": selection,
        "model_prob": round(model_prob, 4),     # raw model, shown for transparency
        "fair_prob": round(fair, 4),            # market-shrunk estimate we actually bet on
        "market_prob": round(market_prob_vigfree, 4),
        "market_odds_decimal": market_odds,
        "market_odds_american": P.decimal_to_american(market_odds),
        "edge": round(ed, 4),
        "ev_per_dollar": round(ev, 4),
        "value_bet": value_bet,
        "tier": _tier(fair),
        "stake": stake,
    }


def analyze_match(match: Dict, bankroll: Optional[float] = None) -> Dict:
    """Attach model output + ranked value suggestions to a normalized match dict."""
    bankroll = bankroll or settings.DEFAULT_BANKROLL
    home, away = match["home"], match["away"]

    # If the game is in play, use the in-play model (current score + time left) — this is
    # what drives a live win-probability and an accurate cash-out call.
    is_live = match.get("status") == "live" and match.get("live_score") is not None
    if is_live:
        sc = match["live_score"]
        model = live_match_probabilities(home, away, sc["home"], sc["away"],
                                         match.get("live_minute", 0))
        pre = match_probabilities(home, away)
        model["pregame_probs"] = pre["probs"]
    else:
        model = match_probabilities(home, away)
    suggestions: List[Dict] = []

    markets = match.get("markets", {})

    # --- Match result (1X2) ---
    h2h = markets.get("h2h") or {}
    if all(h2h.get(k) for k in ("home", "draw", "away")):
        vigfree = P.remove_vig([h2h["home"], h2h["draw"], h2h["away"]])
        labels = [("home", home), ("draw", "Draw"), ("away", away)]
        for (key, name), mp in zip(labels, vigfree):
            suggestions.append(_build_suggestion(
                "Match Result", name, model["probs"][key], h2h[key], mp, bankroll))

    # --- Over/Under 2.5 goals ---
    tot = markets.get("totals_2_5") or {}
    if tot.get("over") and tot.get("under"):
        vigfree = P.remove_vig([tot["over"], tot["under"]])
        for (key, name, mkey), mp in zip(
                [("over", "Over 2.5", "over_2_5"), ("under", "Under 2.5", "under_2_5")], vigfree):
            suggestions.append(_build_suggestion(
                "Total Goals", name, model["totals"][mkey], tot[key], mp, bankroll))

    # --- Both teams to score ---
    btts = markets.get("btts") or {}
    if btts.get("yes") and btts.get("no"):
        vigfree = P.remove_vig([btts["yes"], btts["no"]])
        for (key, name, mkey), mp in zip(
                [("yes", "BTTS: Yes", "btts_yes"), ("no", "BTTS: No", "btts_no")], vigfree):
            suggestions.append(_build_suggestion(
                "Both Teams To Score", name, model["totals"][mkey], btts[key], mp, bankroll))

    # Best value first.
    suggestions.sort(key=lambda s: (s["value_bet"], s["ev_per_dollar"]), reverse=True)
    value_bets = [s for s in suggestions if s["value_bet"]]

    return {
        **match,
        "model": model,
        "suggestions": suggestions,
        "best_value": value_bets[0] if value_bets else None,
        "value_count": len(value_bets),
    }


def evaluate_combo(legs: List[Dict], bankroll: Optional[float] = None, user: Optional[str] = None) -> Dict:
    """
    Evaluate a parlay/combo (e.g. a Kalshi multi-leg).
    Each leg: {"model_prob": float, "market_odds_decimal": float, "label": str}
    Assumes legs are independent (true for unrelated matches; correlated legs are riskier).
    """
    bankroll = bankroll or settings.DEFAULT_BANKROLL
    if not legs:
        return {"error": "no legs provided"}

    combined_prob = 1.0
    combined_odds = 1.0
    for leg in legs:
        combined_prob *= float(leg["model_prob"])
        combined_odds *= float(leg["market_odds_decimal"])

    # Learning loop: if your logged results say the model is overconfident on combos,
    # shrink the probability we bet on by the measured 'reality factor'.
    from app.bet_log import reality_factor
    rf = reality_factor(user) if user else None
    adj_prob = combined_prob * rf if rf else combined_prob
    adj_prob = min(max(adj_prob, 1e-6), 0.999)

    ev = P.expected_value(adj_prob, combined_odds)
    stake = P.recommended_stake(adj_prob, combined_odds, bankroll, settings.KELLY_FRACTION)
    return {
        "legs": legs,
        "leg_count": len(legs),
        "combined_model_prob": round(combined_prob, 4),
        "experience_adjusted_prob": round(adj_prob, 4),
        "reality_factor": rf,
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
            + (f" Estimate shrunk by your logged results (reality factor {rf})." if rf else "")
        ),
    }


def monitor_live_bets(scores: List[Dict], user: str) -> List[Dict]:
    """
    Watch every pending logged parlay against the LIVE games it involves and decide
    whether to flash CASH OUT. The light turns on when the parlay's live combined
    probability has collapsed (a leg going wrong mid-game) — i.e. it's now unlikely to hit.
    """
    from app.data_sources.odds_api import _estimate_minute
    from app import bet_log

    # Index live/finished games by (home, away).
    idx = {}
    for s in scores:
        h, a = s.get("home_team"), s.get("away_team")
        score_map = {x["name"]: x.get("score") for x in (s.get("scores") or [])}
        idx[(h, a)] = {
            "completed": bool(s.get("completed")),
            "sa": int(score_map.get(h) or 0),
            "sb": int(score_map.get(a) or 0),
            "minute": _estimate_minute(s.get("commence_time", "")),
            "has_score": s.get("scores") is not None,
        }

    out = []
    for b in bet_log.pending_bets(user):
        legs = b.get("legs", [])
        # Skip legacy bets logged before structured legs existed (can't track live).
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
            if g and (g["completed"] or (g["has_score"] or g["minute"] > 0)):
                p, trk = live_leg_probability(h, a, g["sa"], g["sb"], g["minute"],
                                              leg.get("market"), leg.get("selection"))
                if trk and p is not None:
                    live_p = p
                    state = "final" if g["completed"] else f"live {g['minute']}'"
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
        elif any_live and (combined_live < entry_comb * 0.55 or combined_live < 0.10):
            action, cash_out = "CASH OUT", True
        elif any_live and combined_live >= entry_comb:
            action, cash_out = "ON TRACK", False
        elif any_live:
            action, cash_out = "HOLD — watch it", False
        else:
            action, cash_out = "not started", False

        # The actual live games this parlay touches (so the notifier can ping on goals).
        live_games, seen_g = [], set()
        for leg in legs:
            h, a = leg.get("home"), leg.get("away")
            g = idx.get((h, a)) if h and a else None
            if g and (h, a) not in seen_g and (g["has_score"] or g["minute"] > 0 or g["completed"]):
                seen_g.add((h, a))
                live_games.append({"home": h, "away": a, "sa": g["sa"], "sb": g["sb"],
                                   "minute": g["minute"], "completed": g["completed"]})

        out.append({
            "id": b["id"], "legs": leg_views,
            "entry_prob": round(entry_comb, 3), "live_prob": round(combined_live, 3),
            "health": round(health, 2), "any_live": any_live,
            "action": action, "cash_out": cash_out, "live_games": live_games,
        })
    return out


def cashout_decision(entry_price: float, current_market_price: float,
                     model_prob: float, stake: float = 100.0) -> Dict:
    """
    Should you cash out / hedge a position?

    Prices are 0-1 (Kalshi/Polymarket cents/100, or implied prob of a back bet).
      entry_price          = price you bought YES at (your cost per contract)
      current_market_price = price you could sell YES at right now
      model_prob           = the model's fair value for YES right now

    The 'tables turned' alert fires when the model's fair value has collapsed
    relative to where you got in.
    """
    entry_price = min(max(entry_price, 1e-4), 1 - 1e-4)
    current_market_price = min(max(current_market_price, 1e-4), 1 - 1e-4)
    model_prob = min(max(model_prob, 1e-4), 1 - 1e-4)

    pnl_pct = (current_market_price - entry_price) / entry_price
    in_profit = current_market_price > entry_price

    # Fair value vs the price you can sell at right now.
    market_vs_model = current_market_price - model_prob  # >0 => market overpays you to sell

    tables_turned = model_prob < entry_price * 0.8  # your side has lost >=20% of its fair value

    # Decision logic.
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
        "action": action,
        "reason": reason,
    }
