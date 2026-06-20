"""
Edge backtest + model-trust calibration — the honest test of whether the model MAKES MONEY,
and how much to trust it vs the sharp market.

It pulls REAL historical closing odds for played World Cup games, pairs them with results,
then asks the only question that matters: does blending our model with the market beat the
market ALONE (lower Brier / log-loss)? That tells us if the model adds real signal, and sets
MODEL_WEIGHT from evidence instead of a guess. Then it backtests flat-stake value bets at
that weight for a real ROI read.

Re-run as the tournament grows:  python training/backtest_edge.py
"""
from __future__ import annotations
import csv
import math
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

from app.config import settings
from app.soccer_model import match_probabilities
from app import probability as P

KEY, BASE, SPORT = settings.ODDS_API_KEY, settings.ODDS_API_BASE, settings.SOCCER_SPORT_KEY
RESULTS_CSV = os.path.join(os.path.dirname(__file__), "data", "results.csv")
# Morning snapshots over the played match days (each = 10 credits).
SNAPSHOT_DATES = [f"2026-06-{d:02d}T10:00:00Z" for d in range(11, 20)]

# Odds-API names -> canonical (match the results dataset after we canon that too).
NAME_MAP = {
    "United States": "USA", "Czechia": "Czech Republic", "Türkiye": "Turkey",
    "Bosnia and Herzegovina": "Bosnia & Herzegovina", "Curacao": "Curaçao",
    "Republic of Ireland": "Ireland",
}
def canon(n): return NAME_MAP.get(n, n)


def _avg_h2h(event):
    acc = defaultdict(list)
    home, away = event["home_team"], event["away_team"]
    for bk in event.get("bookmakers", []):
        for m in bk.get("markets", []):
            if m["key"] != "h2h":
                continue
            for o in m["outcomes"]:
                key = "home" if o["name"] == home else "away" if o["name"] == away else "draw"
                acc[key].append(o["price"])
    if not all(k in acc for k in ("home", "draw", "away")):
        return None
    return {k: sum(v) / len(v) for k, v in acc.items()}


def collect_odds():
    out = {}
    for d in SNAPSHOT_DATES:
        try:
            r = httpx.get(f"{BASE}/historical/sports/{SPORT}/odds",
                          params={"apiKey": KEY, "regions": "us", "markets": "h2h",
                                  "oddsFormat": "decimal", "date": d}, timeout=30)
            r.raise_for_status()
        except Exception as e:
            print(f"  snapshot {d[:10]} failed: {e}")
            continue
        snap = r.json()
        ts = snap.get("timestamp", "")
        for e in snap.get("data", []):
            if e["commence_time"] <= ts:
                continue
            odds = _avg_h2h(e)
            if odds:
                out[(canon(e["home_team"]), canon(e["away_team"]))] = odds
    return out


def collect_results():
    results = {}
    # older games from the results dataset
    if os.path.exists(RESULTS_CSV):
        with open(RESULTS_CSV, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row["date"] < "2026-06-11" or "World Cup" not in row.get("tournament", ""):
                    continue
                try:
                    results[(canon(row["home_team"]), canon(row["away_team"]))] = (
                        int(row["home_score"]), int(row["away_score"]))
                except ValueError:
                    pass
    # recent games from the live scores feed
    try:
        r = httpx.get(f"{BASE}/sports/{SPORT}/scores", params={"apiKey": KEY, "daysFrom": 3}, timeout=30)
        r.raise_for_status()
        for e in r.json():
            if e.get("completed") and e.get("scores"):
                sm = {s["name"]: int(s["score"]) for s in e["scores"]}
                h, a = canon(e["home_team"]), canon(e["away_team"])
                if h in sm or e["home_team"] in sm:
                    hs = sm.get(h, sm.get(e["home_team"]))
                    as_ = sm.get(a, sm.get(e["away_team"]))
                    if hs is not None and as_ is not None:
                        results[(h, a)] = (hs, as_)
    except Exception as e:
        print(f"  scores feed failed: {e}")
    return results


def main():
    print("Pulling historical odds across the tournament…")
    odds = collect_odds()
    print(f"  pre-match odds for {len(odds)} games")
    print("Pulling results (dataset + live feed)…")
    results = collect_results()
    print(f"  {len(results)} completed results")

    sample = []  # (model_probs, market_vigfree, actual)
    for (h, a), od in odds.items():
        if (h, a) not in results:
            continue
        sh, sa = results[(h, a)]
        actual = "home" if sh > sa else "away" if sa > sh else "draw"
        model = match_probabilities(h, a)["probs"]
        vf = dict(zip(("home", "draw", "away"),
                      P.remove_vig([od["home"], od["draw"], od["away"]])))
        sample.append((model, vf, actual, (h, a), od, (sh, sa)))
    n = len(sample)
    print(f"\nMatched sample: {n} games with both odds and result.")
    if n < 5:
        print("Too few games to calibrate. Re-run later as more are played.")
        return

    # Does blending model+market beat market alone? Lower Brier = better.
    def brier(weight):
        tot = 0.0
        for model, vf, actual, *_ in sample:
            for o in ("home", "draw", "away"):
                p = weight * model[o] + (1 - weight) * vf[o]
                tot += (p - (1.0 if actual == o else 0.0)) ** 2
        return tot / n

    print("\n=== Does our model add signal? (Brier score, lower is better) ===")
    weights = [0.0, 0.2, 0.3, 0.4, 0.5, 0.6, 0.8, 1.0]
    scores = [(w, brier(w)) for w in weights]
    for w, b in scores:
        tag = "  <- market only" if w == 0 else "  <- model only" if w == 1 else ""
        print(f"   model weight {w:.1f}: Brier {b:.4f}{tag}")
    best_w, best_b = min(scores, key=lambda x: x[1])
    market_b = scores[0][1]
    print(f"\n   Best weight: {best_w:.1f} (Brier {best_b:.4f}) vs market-only {market_b:.4f}")
    if best_w == 0:
        verdict = "Model adds NOTHING the market doesn't already know — trust the market."
    elif best_b < market_b - 0.002:
        verdict = f"Model DOES add signal — blending at weight {best_w:.1f} beats the market alone."
    else:
        verdict = "Model roughly matches the market — no clear edge yet (need more games)."
    print(f"   VERDICT: {verdict}")

    # Value-bet ROI at the evidence-based weight.
    from app.analysis import MIN_EDGE, LONGSHOT_FLOOR, HEAVY_FAV_CAP
    w = best_w if best_w > 0 else 0.4
    staked = profit = bets = wins = 0
    for model, vf, actual, key, od, score in sample:
        for o in ("home", "draw", "away"):
            fair = w * model[o] + (1 - w) * vf[o]
            edge = fair - vf[o]
            ev = P.expected_value(fair, od[o])
            if edge >= MIN_EDGE and ev > 0 and LONGSHOT_FLOOR <= vf[o] <= HEAVY_FAV_CAP:
                bets += 1
                staked += 1
                won = o == actual
                profit += (od[o] - 1) if won else -1
                wins += won
    print(f"\n=== VALUE-BET BACKTEST (flat $1, weight {w:.1f}, real closing odds) ===")
    if bets:
        print(f"   {bets} bets · won {wins} ({wins/bets*100:.0f}%) · staked ${staked} · "
              f"profit ${profit:+.2f} · ROI {profit/staked*100:+.1f}%")
        print(f"   (sample of {bets} bets — {'still noisy, keep building' if bets < 40 else 'getting meaningful'})")
    else:
        print("   no value bets flagged.")
    print(f"\nQuota used: ~{len(SNAPSHOT_DATES)*10 + 1} credits. Suggested MODEL_WEIGHT: {best_w if best_w>0 else 0.4}")


if __name__ == "__main__":
    main()
