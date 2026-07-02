"""
Alpha Markets AI — MLB model training & backtesting harness.

The baseball counterpart to train.py. Same honest method, adapted to baseball:

  1. Walk the model through EVERY regular-season game in date order, updating each
     team's Elo after every game (the "over and over").
  2. Grid-search the model's params (Elo K, home-field edge, runs-per-Elo, and the
     negative-binomial dispersion) to MINIMISE real out-of-sample error (log loss +
     Brier) on the moneyline of games the params were not tuned to see.
  3. Report a calibration curve — when the model says 60%, does it happen ~60%?

Data: Retrosheet game logs (free, canonical). Downloaded + cached under
training/data/mlb/. Output: backend/app/trained_model_mlb.json (trained ratings for
every team + best params + measured metrics). baseball_model.py loads it automatically.

Run:  python training/train_mlb.py
"""
from __future__ import annotations
import csv
import io
import json
import math
import time
import zipfile
from collections import defaultdict
from datetime import date
from pathlib import Path

import httpx

DATA_DIR = Path(__file__).parent / "data" / "mlb"
OUT = Path(__file__).parent.parent / "app" / "trained_model_mlb.json"

SEASONS = list(range(2015, 2025))     # 2015–2024 regular seasons (~24k games)
EVAL_START = date(2022, 1, 1)         # out-of-sample scoring window
MAX_RUNS = 16

# Retrosheet 3-letter code -> the full team name our model/odds feed use.
CODE_MAP = {
    "ANA": "Los Angeles Angels", "LAA": "Los Angeles Angels",
    "ARI": "Arizona Diamondbacks", "ATL": "Atlanta Braves", "BAL": "Baltimore Orioles",
    "BOS": "Boston Red Sox", "CHA": "Chicago White Sox", "CHN": "Chicago Cubs",
    "CIN": "Cincinnati Reds", "CLE": "Cleveland Guardians", "COL": "Colorado Rockies",
    "DET": "Detroit Tigers", "HOU": "Houston Astros", "KCA": "Kansas City Royals",
    "LAN": "Los Angeles Dodgers", "MIA": "Miami Marlins", "FLO": "Miami Marlins",
    "MIL": "Milwaukee Brewers", "MIN": "Minnesota Twins", "NYA": "New York Yankees",
    "NYN": "New York Mets", "OAK": "Athletics", "PHI": "Philadelphia Phillies",
    "PIT": "Pittsburgh Pirates", "SDN": "San Diego Padres", "SEA": "Seattle Mariners",
    "SFN": "San Francisco Giants", "SLN": "St. Louis Cardinals", "TBA": "Tampa Bay Rays",
    "TEX": "Texas Rangers", "TOR": "Toronto Blue Jays", "WAS": "Washington Nationals",
    "MON": "Washington Nationals",
}


# ---------- data ----------

def _download(season: int):
    """Return the season's game-log zip bytes, or None if unavailable. Cached zips are
    used first; a missing season is attempted once, then skipped (so a flaky network just
    trains on fewer seasons instead of aborting — re-run later to backfill)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cached = DATA_DIR / f"gl{season}.zip"
    if cached.exists() and cached.stat().st_size > 1024:
        return cached.read_bytes()
    url = f"https://www.retrosheet.org/gamelogs/gl{season}.zip"
    for attempt in range(3):
        try:
            r = httpx.get(url, timeout=httpx.Timeout(90.0, connect=30.0),
                          follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            cached.write_bytes(r.content)
            return r.content
        except Exception as exc:
            print(f"  {season} download attempt {attempt+1} failed ({type(exc).__name__})")
            time.sleep(2 * (attempt + 1))
    print(f"  {season}: SKIPPED (could not download — re-run later to include it)")
    return None


def load_games():
    rows = []
    for season in SEASONS:
        data = _download(season)
        if data is None:
            continue
        z = zipfile.ZipFile(io.BytesIO(data))
        text = z.read(z.namelist()[0]).decode("latin-1")
        n0 = len(rows)
        for r in csv.reader(io.StringIO(text)):
            try:
                d = date(int(r[0][:4]), int(r[0][4:6]), int(r[0][6:8]))
                away, home = CODE_MAP.get(r[3]), CODE_MAP.get(r[6])
                as_, hs = int(r[9]), int(r[10])
            except (ValueError, IndexError, KeyError):
                continue
            if not away or not home or as_ == hs:   # skip unmapped + (vanishingly rare) ties
                continue
            rows.append((d, home, away, hs, as_))
        print(f"  {season}: +{len(rows)-n0} games")
    rows.sort(key=lambda x: x[0])
    return rows


# ---------- model (mirrors baseball_model.py) ----------

_PMF_CACHE: dict = {}


def _pmf_cdf(lam: float, r: float):
    """Negative-binomial pmf + cdf vectors for mean lam, dispersion r (cached)."""
    key = (round(lam, 2), r)
    hit = _PMF_CACHE.get(key)
    if hit is not None:
        return hit
    lam = max(lam, 1e-6)
    pmf = []
    for k in range(MAX_RUNS + 1):
        logp = (math.lgamma(k + r) - math.lgamma(r) - math.lgamma(k + 1)
                + r * math.log(r / (r + lam)) + k * math.log(lam / (r + lam)))
        pmf.append(math.exp(logp))
    s = sum(pmf)
    pmf = [x / s for x in pmf]
    cdf, acc = [], 0.0
    for x in pmf:
        acc += x
        cdf.append(acc)
    _PMF_CACHE[key] = (pmf, cdf)
    return pmf, cdf


def win_prob(rh, ra, p):
    """P(home win) — extra-innings ties resolved to the stronger side by Elo."""
    supremacy = ((rh + p["hfa_elo"]) - ra) / p["elo_per_run"]
    lam_h = max(1.6, (p["base_runs"] + supremacy) / 2.0)
    lam_a = max(1.6, (p["base_runs"] - supremacy) / 2.0)
    ph, _ = _pmf_cdf(lam_h, p["nb_dispersion"])
    pa, cdf_a = _pmf_cdf(lam_a, p["nb_dispersion"])
    # P(home reg win)=sum_i ph[i]*P(away<i); P(tie)=sum_i ph[i]*pa[i]
    reg_home = tie = 0.0
    for i in range(len(ph)):
        reg_home += ph[i] * (cdf_a[i - 1] if i > 0 else 0.0)
        tie += ph[i] * pa[i]
    p_extra = 1.0 / (1.0 + 10 ** (-((rh + p["hfa_elo"]) - ra) / 400.0))
    return reg_home + tie * p_extra


def elo_update(rh, ra, hs, as_, p):
    exp_home = 1.0 / (1 + 10 ** (-((rh + p["hfa_elo"]) - ra) / 400.0))
    sc = 1.0 if hs > as_ else 0.0
    mult = math.log(abs(hs - as_) + 1) + 1
    delta = p["k"] * mult * (sc - exp_home)
    return rh + delta, ra - delta


def run_walkforward(games, p, ratings=None, score=True):
    if ratings is None:
        ratings = defaultdict(lambda: 1500.0)
    ll = brier = 0.0
    correct = n = 0
    calib = [[0, 0.0, 0] for _ in range(10)]
    for d, home, away, hs, as_ in games:
        rh, ra = ratings[home], ratings[away]
        if score and d >= EVAL_START:
            ph = min(max(win_prob(rh, ra, p), 1e-9), 1 - 1e-9)
            home_won = hs > as_
            ll -= math.log(ph if home_won else 1 - ph)
            brier += (ph - (1.0 if home_won else 0.0)) ** 2
            pred_home = ph >= 0.5
            correct += (pred_home == home_won)
            n += 1
            conf = ph if pred_home else 1 - ph
            b = min(9, int(conf * 10))
            calib[b][0] += 1
            calib[b][1] += conf
            calib[b][2] += (pred_home == home_won)
        ratings[home], ratings[away] = elo_update(rh, ra, hs, as_, p)
    if not n:
        return None, dict(ratings)
    return {"log_loss": ll / n, "brier": brier / n, "accuracy": correct / n,
            "n_eval": n, "calibration": calib}, dict(ratings)


def grid_search(games):
    grid = {
        "k": [3, 4, 6],
        "hfa_elo": [16, 24, 32],
        "elo_per_run": [55, 68, 82],
        "nb_dispersion": [3.5, 4.5],
        "base_runs": [8.6],
    }
    combos = [
        {"k": k, "hfa_elo": h, "elo_per_run": e, "nb_dispersion": r, "base_runs": b,
         "default_elo": 1460.0}
        for k in grid["k"] for h in grid["hfa_elo"] for e in grid["elo_per_run"]
        for r in grid["nb_dispersion"] for b in grid["base_runs"]
    ]
    print(f"Grid-searching {len(combos)} parameter sets over {len(games)} games…")
    best = None
    for i, p in enumerate(combos, 1):
        m, _ = run_walkforward(games, p)
        if m and (best is None or m["log_loss"] < best[1]["log_loss"]):
            best = (p, m)
        if i % 9 == 0:
            print(f"  …{i}/{len(combos)}  best log_loss so far: {best[1]['log_loss']:.4f}")
    return best


def baseline_logloss(games):
    h = n = 0
    for d, home, away, hs, as_ in games:
        if d < EVAL_START:
            continue
        h += (hs > as_)
        n += 1
    rate = h / n
    ll = 0.0
    for d, home, away, hs, as_ in games:
        if d < EVAL_START:
            continue
        ll -= math.log(rate if hs > as_ else 1 - rate)
    return ll / n, rate


def main():
    t0 = time.time()
    print("Loading Retrosheet game logs…")
    games = load_games()
    print(f"Loaded {len(games)} games ({games[0][0]} → {games[-1][0]}).")

    base_ll, home_rate = baseline_logloss(games)
    print(f"Baseline (always predict home at its {home_rate*100:.1f}% base rate) "
          f"log loss: {base_ll:.4f}\n")

    best_params, best_metrics = grid_search(games)
    print(f"\nBest params: {best_params}")
    print(f"Out-of-sample (since {EVAL_START}):")
    print(f"  log loss : {best_metrics['log_loss']:.4f}  (baseline {base_ll:.4f} — lower is better)")
    print(f"  brier    : {best_metrics['brier']:.4f}")
    print(f"  accuracy : {best_metrics['accuracy']*100:.1f}%  on {best_metrics['n_eval']} games")

    # Final ratings: a couple of warm-start passes so early ratings settle.
    ratings = None
    for _ in range(3):
        _, ratings = run_walkforward(games, best_params,
                                     ratings=defaultdict(lambda: 1500.0, ratings or {}),
                                     score=False)
    ratings = {t: round(r, 1) for t, r in ratings.items()}

    print("\nCalibration (model confidence vs reality):")
    calib_out = []
    for i, (cnt, psum, hits) in enumerate(best_metrics["calibration"]):
        if cnt == 0:
            continue
        pred, act = psum / cnt, hits / cnt
        calib_out.append({"bin": f"{i*10}-{i*10+10}%", "games": cnt,
                          "predicted": round(pred, 3), "actual": round(act, 3)})
        print(f"  {i*10:>3}-{i*10+10:<3}%  {cnt:>6}   {pred*100:>7.1f}%   {act*100:>6.1f}%")

    print("\nTrained ratings (all 30 teams):")
    for t, r in sorted(ratings.items(), key=lambda x: x[1], reverse=True):
        print(f"  {t:<24} {r}")

    OUT.write_text(json.dumps({
        "trained_at": date.today().isoformat(),
        "sport": "mlb",
        "n_games_trained": len(games),
        "eval_start": EVAL_START.isoformat(),
        "params": best_params,
        "metrics": {k: best_metrics[k] for k in ("log_loss", "brier", "accuracy", "n_eval")},
        "baseline_log_loss": round(base_ll, 4),
        "home_win_rate": round(home_rate, 3),
        "calibration": calib_out,
        "ratings": ratings,
    }, indent=2))
    print(f"\n✓ Saved {OUT}  ({len(ratings)} teams)  in {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
