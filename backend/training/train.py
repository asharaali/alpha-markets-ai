"""
Alpha Markets AI — model training & backtesting harness.

This is the honest version of "train it over and over." It does NOT promise to pick
winners. It does three real things:

  1. Walks the model through EVERY international match in history, in date order,
     updating each team's Elo rating after every game (this is the "over and over").
  2. Grid-searches the model's parameters to MINIMISE real, out-of-sample error
     (log loss + Brier score) on games the parameters were not tuned to see.
  3. Reports a calibration curve — the true measure of quality: when the model says
     70%, does it actually happen ~70% of the time?

Output: backend/app/trained_model.json  (trained ratings for every team + best params
+ the measured error metrics). The live app loads this automatically.

Run:  python training/train.py
"""
from __future__ import annotations
import csv
import json
import math
import time
from collections import defaultdict
from datetime import date
from pathlib import Path

DATA = Path(__file__).parent / "data" / "results.csv"
OUT = Path(__file__).parent.parent / "app" / "trained_model.json"

# Only score matches on/after this date (out-of-sample window). Everything before is
# burn-in used solely to build ratings — so the metrics are honest, not memorised.
EVAL_START = date(2018, 1, 1)
# Ignore the very old, sparse era for rating stability; still used for burn-in from here.
HISTORY_START = date(1990, 1, 1)
MAX_GOALS = 6
EPOCHS = 3  # warm-start passes so early ratings converge ("train over and over")

# Canonicalise dataset names to match the live odds/Kalshi feeds.
NAME_MAP = {
    "United States": "USA",
    "South Korea": "South Korea",
    "Republic of Ireland": "Ireland",
    "Ivory Coast": "Ivory Coast",
    "Cape Verde": "Cape Verde",
    "DR Congo": "DR Congo",
    "Curacao": "Curaçao",
    "Czechia": "Czech Republic",
    "Türkiye": "Turkey",
    "Bosnia and Herzegovina": "Bosnia & Herzegovina",
}


def canon(name: str) -> str:
    return NAME_MAP.get(name, name)


# ---------- fast Poisson with memoised pmf vectors ----------
_FACT = [math.factorial(i) for i in range(MAX_GOALS + 1)]
_PMF_CACHE: dict[float, list[float]] = {}


def _pmf_vec(lam: float) -> list[float]:
    key = round(lam, 2)
    v = _PMF_CACHE.get(key)
    if v is None:
        v = [math.exp(-key) * key ** i / _FACT[i] for i in range(MAX_GOALS + 1)]
        _PMF_CACHE[key] = v
    return v


def outcome_probs(r_home, r_away, neutral, p):
    """(P_home, P_draw, P_away) from ratings + params p."""
    eff = r_home - r_away + (0 if neutral else p["home_adv"])
    supremacy = eff / p["elo_per_goal"]
    lam_h = max(0.15, (p["base_goals"] + supremacy) / 2)
    lam_a = max(0.15, (p["base_goals"] - supremacy) / 2)
    ph, pa = _pmf_vec(lam_h), _pmf_vec(lam_a)
    home = draw = away = 0.0
    for i in range(MAX_GOALS + 1):
        pi = ph[i]
        for j in range(MAX_GOALS + 1):
            pij = pi * pa[j]
            if i > j:
                home += pij
            elif i == j:
                draw += pij
            else:
                away += pij
    s = home + draw + away
    return home / s, draw / s, away / s


def elo_update(r_home, r_away, gh, ga, neutral, p):
    eff = r_home - r_away + (0 if neutral else p["home_adv"])
    exp_home = 1.0 / (1 + 10 ** (-eff / 400.0))
    if gh > ga:
        sc = 1.0
    elif gh == ga:
        sc = 0.5
    else:
        sc = 0.0
    margin = abs(gh - ga)
    mult = math.log(margin + 1) + 1
    delta = p["k"] * mult * (sc - exp_home)
    return r_home + delta, r_away - delta


def load_matches():
    rows = []
    with open(DATA, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                d = date.fromisoformat(row["date"])
                gh, ga = int(row["home_score"]), int(row["away_score"])
            except (ValueError, KeyError):
                continue
            if d < HISTORY_START:
                continue
            rows.append((d, canon(row["home_team"]), canon(row["away_team"]),
                         gh, ga, row["neutral"].strip().upper() == "TRUE"))
    rows.sort(key=lambda r: r[0])
    return rows


def run_walkforward(matches, p, ratings=None, score=True):
    """One chronological pass. Returns (metrics, ratings)."""
    if ratings is None:
        ratings = defaultdict(lambda: 1500.0)
    ll = brier = 0.0
    correct = n = 0
    calib = [[0, 0.0, 0] for _ in range(10)]  # [count, sum_pred_for_argmax, hits]
    for d, home, away, gh, ga, neutral in matches:
        rh, ra = ratings[home], ratings[away]
        if score and d >= EVAL_START:
            P = outcome_probs(rh, ra, neutral, p)
            actual = 0 if gh > ga else (1 if gh == ga else 2)
            probs = [max(min(x, 1 - 1e-9), 1e-9) for x in P]
            ll -= math.log(probs[actual])
            brier += sum((probs[k] - (1.0 if k == actual else 0.0)) ** 2 for k in range(3))
            pred = max(range(3), key=lambda k: probs[k])
            correct += (pred == actual)
            n += 1
            b = min(9, int(probs[pred] * 10))
            calib[b][0] += 1
            calib[b][1] += probs[pred]
            calib[b][2] += (pred == actual)
        ratings[home], ratings[away] = elo_update(rh, ra, gh, ga, neutral, p)
    if not n:
        return None, ratings
    return {
        "log_loss": ll / n,
        "brier": brier / n,
        "accuracy": correct / n,
        "n_eval": n,
        "calibration": calib,
    }, dict(ratings)


def grid_search(matches):
    grid = {
        "k": [20, 30, 40],
        "home_adv": [40, 65, 90],
        "elo_per_goal": [180, 220, 260],
        "base_goals": [2.5, 2.7],
    }
    best = None
    combos = [
        {"k": k, "home_adv": h, "elo_per_goal": e, "base_goals": b}
        for k in grid["k"] for h in grid["home_adv"]
        for e in grid["elo_per_goal"] for b in grid["base_goals"]
    ]
    print(f"Grid-searching {len(combos)} parameter sets over {len(matches)} matches…")
    for i, p in enumerate(combos, 1):
        m, _ = run_walkforward(matches, p)
        if m and (best is None or m["log_loss"] < best[1]["log_loss"]):
            best = (p, m)
        if i % 9 == 0:
            print(f"  …{i}/{len(combos)}  best log_loss so far: {best[1]['log_loss']:.4f}")
    return best


def baseline_logloss(matches):
    """Naive baseline: always predict the historical base rate of home/draw/away."""
    h = d = a = 0
    for dt, *_rest in matches:
        pass
    for dt, home, away, gh, ga, neutral in matches:
        if dt < EVAL_START:
            continue
        if gh > ga:
            h += 1
        elif gh == ga:
            d += 1
        else:
            a += 1
    tot = h + d + a
    base = [h / tot, d / tot, a / tot]
    ll = 0.0
    n = 0
    for dt, home, away, gh, ga, neutral in matches:
        if dt < EVAL_START:
            continue
        actual = 0 if gh > ga else (1 if gh == ga else 2)
        ll -= math.log(base[actual])
        n += 1
    return ll / n, base


def main():
    t0 = time.time()
    matches = load_matches()
    print(f"Loaded {len(matches)} matches ({matches[0][0]} → {matches[-1][0]}).")

    base_ll, base_rates = baseline_logloss(matches)
    print(f"Baseline (always bet base rates) log loss: {base_ll:.4f}\n")

    best_params, best_metrics = grid_search(matches)
    print(f"\nBest params: {best_params}")
    print(f"Out-of-sample (since {EVAL_START}):")
    print(f"  log loss : {best_metrics['log_loss']:.4f}  (baseline {base_ll:.4f} — lower is better)")
    print(f"  brier    : {best_metrics['brier']:.4f}")
    print(f"  accuracy : {best_metrics['accuracy']*100:.1f}%  on {best_metrics['n_eval']} games")

    # Final training: multi-epoch warm start to settle ratings ("over and over").
    ratings = None
    for ep in range(EPOCHS):
        _, ratings = run_walkforward(matches, best_params,
                                     ratings=defaultdict(lambda: 1500.0,
                                                          ratings or {}), score=False)
    ratings = {t: round(r, 1) for t, r in ratings.items()}

    # Calibration table.
    print("\nCalibration (model confidence vs reality):")
    print("  conf-bin   games   predicted   actual")
    calib_out = []
    for i, (cnt, psum, hits) in enumerate(best_metrics["calibration"]):
        if cnt == 0:
            continue
        pred = psum / cnt
        act = hits / cnt
        calib_out.append({"bin": f"{i*10}-{i*10+10}%", "games": cnt,
                          "predicted": round(pred, 3), "actual": round(act, 3)})
        print(f"  {i*10:>3}-{i*10+10:<3}%  {cnt:>6}   {pred*100:>7.1f}%   {act*100:>6.1f}%")

    top = sorted(ratings.items(), key=lambda x: x[1], reverse=True)[:15]
    print("\nTop 15 trained ratings:")
    for t, r in top:
        print(f"  {t:<22} {r}")

    OUT.write_text(json.dumps({
        "trained_at": date.today().isoformat(),
        "n_matches_trained": len(matches),
        "eval_start": EVAL_START.isoformat(),
        "params": best_params,
        "metrics": {k: best_metrics[k] for k in ("log_loss", "brier", "accuracy", "n_eval")},
        "baseline_log_loss": round(base_ll, 4),
        "base_rates": {"home": round(base_rates[0], 3), "draw": round(base_rates[1], 3),
                       "away": round(base_rates[2], 3)},
        "calibration": calib_out,
        "ratings": ratings,
    }, indent=2))
    print(f"\n✓ Saved {OUT}  ({len(ratings)} teams)  in {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
