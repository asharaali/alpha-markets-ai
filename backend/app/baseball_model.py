"""
The MLB 'AI model' for Alpha Markets AI — the baseball counterpart to soccer_model.

Baseball is NOT soccer, and this engine is built around the ways it differs:

  1. TEAM STRENGTH is an Elo rating (1500-centred), same idea as the soccer engine.
  2. STARTING PITCHERS decide baseball games more than any single factor in any major
     sport. A game between two teams with an ace vs. a spot starter is a different game
     than the season matchup. We carry a pitcher-quality tier per side that suppresses
     the OPPONENT's expected runs (an ace throttles the other team's offense).
  3. PARK FACTORS matter — Coors Field inflates runs ~18%, pitcher parks (Oracle,
     T-Mobile) deflate them ~8%. Applied to the home stadium.
  4. RUNS ARE OVERDISPERSED. Goals in soccer are ~Poisson; runs in baseball have
     variance well above the mean (crooked-number innings). So we model each team's runs
     with a NEGATIVE BINOMIAL, not a Poisson — this is the single biggest calibration
     fix for a run model and it matters most for totals and the run line.
  5. NO DRAWS. Regulation ties go to extra innings; we resolve that tie mass to the
     stronger team (by Elo) so win probabilities are honest.

From the two teams' run distributions we build the full run matrix (like the soccer
scoreline matrix) and read off every market: moneyline, run line (+/-1.5), total runs
over/under, each team's total, and first-5-innings (F5). Elo updates as results come in.

Honest limits: pitcher tiers default to league-average when no probable-starter feed is
wired; bullpen usage, weather, and lineup cards are not modelled. Ratings self-correct via
update_after_result and can be replaced by a trained_model_mlb.json fit to real seasons.
"""
from __future__ import annotations
import json
import math
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# League-average COMBINED runs per game (both teams). ~4.3/team in recent seasons.
BASE_TOTAL_RUNS = 8.6
# Elo points that equal roughly one run of per-game supremacy.
ELO_PER_RUN = 68.0
# Home-field edge in Elo points (~53-54% at a neutral matchup).
HFA_ELO = 22.0
# Negative-binomial dispersion (size r). var = lam + lam^2/r. r~4 reproduces MLB's
# runs-per-team variance (~9-10 at a ~4.3 mean) — clearly overdispersed vs Poisson.
NB_DISPERSION = 4.0
# Max runs per team to enumerate in the run matrix.
MAX_RUNS = 18
# Fraction of a full game's scoring that has happened through 5 innings (5 of 9).
F5_FRACTION = 5.0 / 9.0

# Starting-pitcher quality tiers -> multiplier applied to the OPPONENT's expected runs.
# <1 = suppresses the other team's offense (an ace); >1 = gets hit around (a weak arm).
PITCHER_FACTOR = {"ace": 0.82, "good": 0.91, "avg": 1.00, "weak": 1.12}


def pitcher_factor(quality) -> float:
    """Run-suppression multiplier for a starter. Accepts either a real numeric multiplier
    (from the live probable-pitcher feed, e.g. 0.83) or a tier string (ace/good/avg/weak)
    used as the fallback when no starter data is available."""
    if isinstance(quality, (int, float)):
        return float(quality)
    return PITCHER_FACTOR.get((quality or "avg").strip().lower(), 1.00)


# Seed Elo ratings (approx recent-season strength, 1500-centred). Keyed by the full team
# name The Odds API uses. Unknown clubs default to DEFAULT_ELO. These self-correct as
# real results come in via update_after_result.
TEAM_ELO: Dict[str, float] = {
    "Los Angeles Dodgers": 1571, "Atlanta Braves": 1548, "Philadelphia Phillies": 1546,
    "New York Yankees": 1544, "Houston Astros": 1528, "San Diego Padres": 1526,
    "New York Mets": 1524, "Baltimore Orioles": 1533, "Cleveland Guardians": 1521,
    "Milwaukee Brewers": 1518, "Arizona Diamondbacks": 1515, "Seattle Mariners": 1512,
    "Kansas City Royals": 1508, "Boston Red Sox": 1506, "Minnesota Twins": 1505,
    "Chicago Cubs": 1503, "Detroit Tigers": 1502, "Texas Rangers": 1498, "Tampa Bay Rays": 1500,
    "St. Louis Cardinals": 1495, "San Francisco Giants": 1494, "Toronto Blue Jays": 1490,
    "Cincinnati Reds": 1488, "Pittsburgh Pirates": 1476, "Los Angeles Angels": 1466,
    "Washington Nationals": 1462, "Athletics": 1458, "Oakland Athletics": 1458,
    "Miami Marlins": 1452, "Colorado Rockies": 1440, "Chicago White Sox": 1422,
}
DEFAULT_ELO = 1470.0

# Park run multipliers, applied to the HOME stadium. Anchored to well-known park factors.
PARK_FACTOR: Dict[str, float] = {
    "Colorado Rockies": 1.18, "Boston Red Sox": 1.06, "Cincinnati Reds": 1.06,
    "Arizona Diamondbacks": 1.03, "Kansas City Royals": 1.02, "Texas Rangers": 1.02,
    "Baltimore Orioles": 1.02, "Philadelphia Phillies": 1.02, "New York Yankees": 1.01,
    "Chicago Cubs": 1.01, "Toronto Blue Jays": 1.01,
    "San Francisco Giants": 0.93, "Seattle Mariners": 0.92, "San Diego Padres": 0.95,
    "Miami Marlins": 0.95, "Oakland Athletics": 0.96, "Athletics": 0.96,
    "Detroit Tigers": 0.97, "Tampa Bay Rays": 0.97, "Cleveland Guardians": 0.98,
    "New York Mets": 0.98, "Los Angeles Angels": 0.98, "Pittsburgh Pirates": 0.98,
    "Minnesota Twins": 0.99, "St. Louis Cardinals": 0.99,
}

# ---------------------------------------------------------------------------
# Optional trained ratings/params (drop-in, same pattern as the soccer model).
# ---------------------------------------------------------------------------
MODEL_INFO: Dict = {"trained": False, "sport": "mlb", "method": "Elo + starter tiers + park + negative-binomial runs"}
_TRAINED_PATH = Path(__file__).parent / "trained_model_mlb.json"
if _TRAINED_PATH.exists():
    try:
        _t = json.loads(_TRAINED_PATH.read_text())
        TEAM_ELO = {k: float(v) for k, v in _t["ratings"].items()}
        ELO_PER_RUN = float(_t["params"].get("elo_per_run", ELO_PER_RUN))
        BASE_TOTAL_RUNS = float(_t["params"].get("base_runs", BASE_TOTAL_RUNS))
        HFA_ELO = float(_t["params"].get("hfa_elo", HFA_ELO))
        NB_DISPERSION = float(_t["params"].get("nb_dispersion", NB_DISPERSION))
        DEFAULT_ELO = float(_t["params"].get("default_elo", DEFAULT_ELO))
        MODEL_INFO.update({"trained": True, "trained_at": _t.get("trained_at"),
                           "n_games_trained": _t.get("n_games_trained"),
                           "metrics": _t.get("metrics"), "params": _t.get("params"),
                           "n_teams": len(TEAM_ELO)})
    except (json.JSONDecodeError, KeyError) as exc:
        print(f"[baseball_model] could not load trained_model_mlb.json ({exc}); using seed ratings")

if not MODEL_INFO.get("n_teams"):
    MODEL_INFO["n_teams"] = len(TEAM_ELO)


def get_rating(team: str) -> float:
    """Elo lookup, tolerant of nickname-only names (e.g. 'Yankees' -> 'New York Yankees')."""
    if not team:
        return DEFAULT_ELO
    if team in TEAM_ELO:
        return TEAM_ELO[team]
    tl = team.strip().lower()
    for name, elo in TEAM_ELO.items():
        if name.lower() == tl or name.lower().endswith(tl) or tl.endswith(name.split()[-1].lower()):
            return elo
    return DEFAULT_ELO


def _park(home: str) -> float:
    if home in PARK_FACTOR:
        return PARK_FACTOR[home]
    hl = (home or "").strip().lower()
    for name, pf in PARK_FACTOR.items():
        if name.lower().endswith(hl) or hl.endswith(name.split()[-1].lower()):
            return pf
    return 1.00


def _nb_pmf(k: int, lam: float, r: float = NB_DISPERSION) -> float:
    """Negative-binomial P(X=k) with mean lam and dispersion r (overdispersed runs)."""
    lam = max(lam, 1e-6)
    logp = (math.lgamma(k + r) - math.lgamma(r) - math.lgamma(k + 1)
            + r * math.log(r / (r + lam)) + k * math.log(lam / (r + lam)))
    return math.exp(logp)


def _run_vector(lam: float) -> List[float]:
    v = [_nb_pmf(k, lam) for k in range(MAX_RUNS + 1)]
    s = sum(v)
    return [x / s for x in v] if s > 0 else v   # renormalise the truncated tail


def expected_runs(home: str, away: str, sp_home: str = "avg", sp_away: str = "avg",
                  park: Optional[float] = None) -> Tuple[float, float]:
    """Expected runs for (home, away): Elo supremacy split around a park-adjusted base,
    then each side's total suppressed by the OPPOSING starting pitcher."""
    rh = get_rating(home) + HFA_ELO
    ra = get_rating(away)
    supremacy = (rh - ra) / ELO_PER_RUN                 # expected home run-differential
    base = BASE_TOTAL_RUNS * (park if park is not None else _park(home))
    lam_home = (base + supremacy) / 2.0
    lam_away = (base - supremacy) / 2.0
    lam_home *= pitcher_factor(sp_away)                 # away's starter suppresses home runs
    lam_away *= pitcher_factor(sp_home)
    return max(1.6, lam_home), max(1.6, lam_away)


def _elo_win_prob(home: str, away: str) -> float:
    """Straight Elo win prob for the home side — used to resolve extra-innings tie mass."""
    return 1.0 / (1.0 + 10 ** (-((get_rating(home) + HFA_ELO) - get_rating(away)) / 400.0))


def _matrix(lam_home: float, lam_away: float):
    return _run_vector(lam_home), _run_vector(lam_away)


def _moneyline_from_matrix(ph: List[float], pa: List[float], p_extra_home: float) -> Tuple[float, float]:
    """Win probs from the run matrix; regulation ties resolved to the stronger side."""
    p_home = p_away = p_tie = 0.0
    for i in range(len(ph)):
        for j in range(len(pa)):
            p = ph[i] * pa[j]
            if i > j:
                p_home += p
            elif i < j:
                p_away += p
            else:
                p_tie += p
    p_home += p_tie * p_extra_home
    p_away += p_tie * (1 - p_extra_home)
    return p_home, p_away


def _total_over(ph: List[float], pa: List[float], line: float) -> float:
    """P(total runs > line). line is a half number (e.g. 8.5)."""
    need = math.floor(line) + 1        # runs strictly above the half-line
    tot = 0.0
    for i in range(len(ph)):
        for j in range(len(pa)):
            if i + j >= need:
                tot += ph[i] * pa[j]
    return tot


def _team_over(vec: List[float], line: float) -> float:
    need = math.floor(line) + 1
    return sum(vec[need:]) if need < len(vec) else 0.0


def _cover_by(ph: List[float], pa: List[float], team_home: bool, margin: float) -> float:
    """P(home wins by > margin) if team_home else P(away wins by > margin). margin=1.5 => by 2+."""
    need = math.floor(margin) + 1
    tot = 0.0
    for i in range(len(ph)):
        for j in range(len(pa)):
            diff = (i - j) if team_home else (j - i)
            if diff >= need:
                tot += ph[i] * pa[j]
    return tot


def match_probabilities(home: str, away: str, sp_home: str = "avg", sp_away: str = "avg",
                        stage: Optional[Dict] = None) -> Dict:
    """Full model output for home vs away. Shape mirrors soccer_model.match_probabilities
    (probs.home / probs.draw / probs.away) so the rest of the app can consume it — but in
    baseball probs.draw is always 0 (no ties)."""
    lam_home, lam_away = expected_runs(home, away, sp_home, sp_away)
    ph, pa = _matrix(lam_home, lam_away)
    p_home, p_away = _moneyline_from_matrix(ph, pa, _elo_win_prob(home, away))
    total_run_line = round((lam_home + lam_away) * 2) / 2   # nearest half for the "book" total
    over = _total_over(ph, pa, total_run_line)

    # Most likely final scores (browsable, like soccer's likely_scorelines).
    scores = sorted(((f"{i}-{j}", ph[i] * pa[j]) for i in range(min(11, len(ph)))
                     for j in range(min(11, len(pa)))), key=lambda x: x[1], reverse=True)
    return {
        "team_a": home, "team_b": away,
        "probs": {"home": round(p_home, 4), "draw": 0.0, "away": round(p_away, 4)},
        "expected_runs": {"a": round(lam_home, 2), "b": round(lam_away, 2)},
        "totals": {
            "line": total_run_line,
            "over": round(over, 4), "under": round(1 - over, 4),
            # kept for shape-compatibility with soccer consumers (unused for MLB):
            "over_2_5": round(over, 4), "under_2_5": round(1 - over, 4),
            "btts_yes": 0.0, "btts_no": 1.0,
        },
        "likely_scorelines": [{"score": s, "prob": round(p, 4)} for s, p in scores[:5]],
    }


def run_total_prob(home: str, away: str, line: float, sp_home: str = "avg",
                   sp_away: str = "avg") -> float:
    """P(total runs > line) for any half-line — used to price Kalshi 'Over X.5 runs' markets."""
    lam_home, lam_away = expected_runs(home, away, sp_home, sp_away)
    ph, pa = _matrix(lam_home, lam_away)
    return _total_over(ph, pa, line)


def moneyline_prob(home: str, away: str, sp_home: str = "avg", sp_away: str = "avg") -> Tuple[float, float]:
    """(P(home win), P(away win)) — extra-innings ties resolved to the stronger side."""
    lam_home, lam_away = expected_runs(home, away, sp_home, sp_away)
    ph, pa = _matrix(lam_home, lam_away)
    return _moneyline_from_matrix(ph, pa, _elo_win_prob(home, away))


def run_margin_prob(home: str, away: str, team_is_home: bool, margin: float,
                    sp_home: str = "avg", sp_away: str = "avg") -> float:
    """P(the chosen team wins by MORE than `margin` runs) — prices Kalshi's KXMLBSPREAD
    'X wins by over N.5 runs' run-line contracts for any line."""
    lam_home, lam_away = expected_runs(home, away, sp_home, sp_away)
    ph, pa = _matrix(lam_home, lam_away)
    return _cover_by(ph, pa, team_is_home, margin)


def f5_probs(home: str, away: str, sp_home: str = "avg", sp_away: str = "avg") -> Tuple[float, float, float]:
    """(home, tie, away) win probabilities through the first 5 innings — a real 3-way
    market (no extras to break a tie through 5). Prices Kalshi's KXMLBF5."""
    lam_home, lam_away = expected_runs(home, away, sp_home, sp_away)
    ph5, pa5 = _matrix(lam_home * F5_FRACTION, lam_away * F5_FRACTION)
    h5 = a5 = tie5 = 0.0
    for i in range(len(ph5)):
        for j in range(len(pa5)):
            p = ph5[i] * pa5[j]
            if i > j:
                h5 += p
            elif i < j:
                a5 += p
            else:
                tie5 += p
    return h5, tie5, a5


def _sel(label: str, prob: float) -> Dict:
    prob = min(max(prob, 1e-4), 0.9999)
    return {"label": label, "prob": round(prob, 4), "fair_odds": round(1 / prob, 2)}


def extended_markets(home: str, away: str, sp_home: str = "avg", sp_away: str = "avg",
                     stage: Optional[Dict] = None) -> Dict:
    """Every MLB bet type the model can price, from the run matrix: moneyline, run line
    (+/-1.5), total runs (multiple lines), each team's total, and first-5-innings."""
    lam_home, lam_away = expected_runs(home, away, sp_home, sp_away)
    ph, pa = _matrix(lam_home, lam_away)
    p_extra = _elo_win_prob(home, away)
    p_home, p_away = _moneyline_from_matrix(ph, pa, p_extra)

    # First 5 innings: scale the run rates to 5 innings and rebuild the matrix. F5 is a
    # genuine 3-way market (there's no extra innings to break a tie through 5), so unlike
    # the full-game moneyline we keep the tie mass as its own outcome.
    ph5, pa5 = _matrix(lam_home * F5_FRACTION, lam_away * F5_FRACTION)
    h5 = a5 = tie5 = 0.0
    for i in range(len(ph5)):
        for j in range(len(pa5)):
            p = ph5[i] * pa5[j]
            if i > j:
                h5 += p
            elif i < j:
                a5 += p
            else:
                tie5 += p

    def totals(lines):
        out = []
        for ln in lines:
            o = _total_over(ph, pa, ln)
            out += [_sel(f"Over {ln} runs", o), _sel(f"Under {ln} runs", 1 - o)]
        return out

    # Run line by margin, matching Kalshi's KXMLBSPREAD ladder (wins by over 1.5/2.5/3.5).
    run_line = [
        _sel(f"{home} -1.5", _cover_by(ph, pa, True, 1.5)),
        _sel(f"{away} +1.5", 1 - _cover_by(ph, pa, True, 1.5)),
        _sel(f"{away} -1.5", _cover_by(ph, pa, False, 1.5)),
        _sel(f"{home} +1.5", 1 - _cover_by(ph, pa, False, 1.5)),
    ]
    for mgn in (2.5, 3.5):
        run_line += [_sel(f"{home} -{mgn}", _cover_by(ph, pa, True, mgn)),
                     _sel(f"{away} -{mgn}", _cover_by(ph, pa, False, mgn))]

    markets = {
        "Moneyline": [_sel(home, p_home), _sel(away, p_away)],
        "Run Line": run_line,
        # Full total-runs ladder Kalshi lists on KXMLBTOTAL (Over 2.5 up).
        "Total Runs (Over/Under)": totals([2.5, 3.5, 4.5, 5.5, 6.5, 7.5, 8.5, 9.5, 10.5, 11.5]),
        f"{home} Team Total": [
            s for ln in (2.5, 3.5, 4.5)
            for s in (_sel(f"{home} Over {ln}", _team_over(ph, ln)),
                      _sel(f"{home} Under {ln}", 1 - _team_over(ph, ln)))
        ],
        f"{away} Team Total": [
            s for ln in (2.5, 3.5, 4.5)
            for s in (_sel(f"{away} Over {ln}", _team_over(pa, ln)),
                      _sel(f"{away} Under {ln}", 1 - _team_over(pa, ln)))
        ],
        "First 5 Innings (F5)": [
            _sel(f"{home} (F5)", h5), _sel("Tie (F5)", tie5), _sel(f"{away} (F5)", a5),
        ],
    }
    return {
        "team_a": home, "team_b": away,
        "expected_runs": {"a": round(lam_home, 2), "b": round(lam_away, 2)},
        "markets": {k: v for k, v in markets.items() if v},
    }


def _remaining_matrix(home, away, sa, sb, inning, half, sp_home="avg", sp_away="avg"):
    """Run distributions for the runs STILL TO COME given the inning/half."""
    lam_home, lam_away = expected_runs(home, away, sp_home, sp_away)
    inning = max(1, int(inning or 1))
    elapsed = (inning - 1) + (0.5 if str(half).lower().startswith("bot") else 0.0)
    frac = max(0.0, (9.0 - elapsed) / 9.0)
    return _matrix(lam_home * frac, lam_away * frac)


def live_match_probabilities(home: str, away: str, score_home: int, score_away: int,
                             inning: int, half: str = "top",
                             sp_home: str = "avg", sp_away: str = "avg",
                             stage: Optional[Dict] = None) -> Dict:
    """In-play win probabilities: current score + expected runs over the innings remaining.
    As the game runs down, remaining scoring shrinks and the probabilities lock onto the
    current score — exactly how a live moneyline behaves."""
    ph, pa = _remaining_matrix(home, away, score_home, score_away, inning, half, sp_home, sp_away)
    p_extra = _elo_win_prob(home, away)
    p_home = p_away = p_tie = 0.0
    exp_home = exp_away = 0.0
    for i in range(len(ph)):
        exp_home += i * ph[i]
        for j in range(len(pa)):
            p = ph[i] * pa[j]
            fa, fb = score_home + i, score_away + j
            if fa > fb:
                p_home += p
            elif fa < fb:
                p_away += p
            else:
                p_tie += p
    for j in range(len(pa)):
        exp_away += j * pa[j]
    p_home += p_tie * p_extra
    p_away += p_tie * (1 - p_extra)
    return {
        "team_a": home, "team_b": away, "live": True,
        "inning": inning, "half": half,
        "score": {"a": score_home, "b": score_away},
        "expected_final_runs": {"a": round(score_home + exp_home, 2),
                                "b": round(score_away + exp_away, 2)},
        # soccer-shaped keys so the frontend's live-shift renderer works unchanged:
        "expected_final_goals": {"a": round(score_home + exp_home, 2),
                                 "b": round(score_away + exp_away, 2)},
        "probs": {"home": round(p_home, 4), "draw": 0.0, "away": round(p_away, 4)},
        "totals": {"over_2_5": 0.0, "under_2_5": 1.0, "btts_yes": 0.0, "btts_no": 1.0},
    }


def live_leg_probability(home, away, sa, sb, inning, market, selection,
                         half="top", sp_home="avg", sp_away="avg", **_ignore):
    """Recompute one MLB bet leg's probability from the live game state.
    Returns (probability, trackable)."""
    m = (market or "").lower()
    s = selection or ""
    sl = s.lower()
    ph, pa = _remaining_matrix(home, away, sa, sb, inning, half, sp_home, sp_away)
    p_extra = _elo_win_prob(home, away)
    hl, al = home.lower(), away.lower()

    import re
    if "moneyline" in m or (sl in (hl, al)):
        # Win prob folding in the runs already on the board; regulation ties -> extras by Elo.
        p_home = p_away = p_tie = 0.0
        for i in range(len(ph)):
            for j in range(len(pa)):
                p = ph[i] * pa[j]
                fa, fb = sa + i, sb + j
                if fa > fb:
                    p_home += p
                elif fa < fb:
                    p_away += p
                else:
                    p_tie += p
        p_home += p_tie * p_extra
        p_away += p_tie * (1 - p_extra)
        if hl in sl:
            return p_home, True
        if al in sl:
            return p_away, True

    mm = re.search(r"([+-])(\d+)\.5", s)
    if "run line" in m or mm:
        if mm:
            team_home = hl in sl
            plus = mm.group(1) == "+"
            need = int(mm.group(2)) + 1        # -1.5 -> 2, -2.5 -> 3, -3.5 -> 4
            tot = 0.0
            for i in range(len(ph)):
                for j in range(len(pa)):
                    diff = (sa + i) - (sb + j)
                    fav_diff = diff if team_home else -diff
                    if (not plus and fav_diff >= need) or (plus and fav_diff > -need):
                        tot += ph[i] * pa[j]
            return tot, True

    if "over" in sl or "under" in sl or "total" in m:
        mm = re.search(r"(\d+)\.5", s)
        line = float(mm.group(0)) if mm else 8.5
        if hl in sl and "team total" in m:
            over = _team_over(ph, line - sa) if line - sa >= 0 else 1.0
            return (over if "over" in sl else 1 - over), True
        if al in sl and "team total" in m:
            over = _team_over(pa, line - sb) if line - sb >= 0 else 1.0
            return (over if "over" in sl else 1 - over), True
        need = math.floor(line) + 1 - (sa + sb)
        if need <= 0:
            over = 1.0
        else:
            over = sum(ph[i] * pa[j] for i in range(len(ph)) for j in range(len(pa)) if i + j >= need)
        return (over if "over" in sl else 1 - over), True

    if "f5" in m or "first 5" in m or "(f5)" in sl:
        return None, False   # needs an inning-level live feed
    return None, False


def update_after_result(team_a: str, team_b: str, runs_a: int, runs_b: int, k: float = 4.0):
    """Elo update from a final. MLB K is small (~4) — one game is low information over a
    162-game season. A run-margin multiplier lets blowouts move ratings a touch more."""
    ra, rb = get_rating(team_a), get_rating(team_b)
    exp_a = 1.0 / (1 + 10 ** ((rb - ra) / 400.0))
    score_a = 1.0 if runs_a > runs_b else 0.0 if runs_a < runs_b else 0.5
    margin = abs(runs_a - runs_b)
    mult = math.log(margin + 1) + 1
    delta = k * mult * (score_a - exp_a)
    TEAM_ELO[team_a] = ra + delta
    TEAM_ELO[team_b] = rb - delta
    return {team_a: round(TEAM_ELO[team_a], 1), team_b: round(TEAM_ELO[team_b], 1)}
