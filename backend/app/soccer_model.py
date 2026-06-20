"""
The actual 'AI model' for soccer — this is the engine behind Alpha Markets AI's
World Cup predictions.

Method (this is a real, well-established approach, not a black box):
  1. Each team has an Elo rating (strength). Stronger team = higher rating.
  2. The rating gap -> an expected goal supremacy (how many more goals the favourite scores).
  3. We spread that into expected goals for each side (lambda_home, lambda_away).
  4. Goals in soccer follow a Poisson distribution, so we build the full scoreline
     matrix (0-0, 1-0, 2-1, ...) and sum the cells to get P(home win / draw / away win),
     plus over/under 2.5 goals and both-teams-to-score.

Elo ratings update as results come in (update_after_result), so the model gets
sharper as the tournament progresses.
"""
from __future__ import annotations
import json
import math
from pathlib import Path
from typing import Dict, List, Optional

# International average total goals per match — used as the baseline scoring level.
BASE_TOTAL_GOALS = 2.6
# Elo points that equal roughly one goal of supremacy.
ELO_PER_GOAL = 220.0
# Max goals per team to enumerate in the Poisson matrix.
MAX_GOALS = 8


# Seed ratings for the 2026 World Cup field (approximate international Elo).
# Unknown teams default to 1700. These self-correct as results come in.
TEAM_ELO: Dict[str, float] = {
    "Argentina": 2105, "France": 2080, "Spain": 2055, "Brazil": 2050,
    "England": 2010, "Germany": 1985, "Portugal": 2000, "Netherlands": 1990,
    "Belgium": 1955, "Italy": 1965, "Croatia": 1905, "Uruguay": 1925,
    "Colombia": 1900, "Morocco": 1885, "Switzerland": 1865, "Denmark": 1855,
    "Japan": 1850, "Senegal": 1845, "USA": 1835, "Mexico": 1825,
    "Ecuador": 1815, "Serbia": 1810, "Canada": 1800, "Korea Republic": 1795,
    "Poland": 1790, "Australia": 1765, "Nigeria": 1815, "Ivory Coast": 1790,
    "Iran": 1770, "Egypt": 1780, "Cameroon": 1760, "Ghana": 1745,
    "Saudi Arabia": 1690, "Qatar": 1660, "New Zealand": 1630,
    "Algeria": 1730, "Iraq": 1640, "Jordan": 1620, "Haiti": 1560,
    "Curaçao": 1545, "Curacao": 1545,
}

# Unseeded minnows: a conservative default so the model doesn't overrate them
# (which was manufacturing fake "value" on heavy underdogs).
DEFAULT_ELO = 1580.0
# Small bump for the host nations (USA, Canada, Mexico) playing at home.
HOST_NATIONS = {"USA", "Canada", "Mexico"}
HOST_BONUS = 40.0

# ---------------------------------------------------------------------------
# Load data-trained ratings + params if training has been run. This replaces the
# hand-seeded numbers above with values fit to 32k+ historical matches.
# Re-train any time with:  python training/train.py
# ---------------------------------------------------------------------------
MODEL_INFO: Dict = {"trained": False}
_TRAINED_PATH = Path(__file__).parent / "trained_model.json"
if _TRAINED_PATH.exists():
    try:
        _t = json.loads(_TRAINED_PATH.read_text())
        TEAM_ELO = {k: float(v) for k, v in _t["ratings"].items()}
        ELO_PER_GOAL = float(_t["params"]["elo_per_goal"])
        BASE_TOTAL_GOALS = float(_t["params"]["base_goals"])
        # Use the learned home advantage as the host-nation bump (others ~neutral at the WC).
        HOST_BONUS = float(_t["params"]["home_adv"])
        # Truly-unknown teams sit below the field on the trained 1500-centred scale.
        DEFAULT_ELO = 1450.0
        MODEL_INFO = {
            "trained": True,
            "trained_at": _t.get("trained_at"),
            "n_matches_trained": _t.get("n_matches_trained"),
            "eval_start": _t.get("eval_start"),
            "metrics": _t.get("metrics"),
            "baseline_log_loss": _t.get("baseline_log_loss"),
            "calibration": _t.get("calibration"),
            "params": _t.get("params"),
            "n_teams": len(TEAM_ELO),
        }
    except (json.JSONDecodeError, KeyError) as exc:  # keep hand-seeded fallback
        print(f"[soccer_model] could not load trained_model.json ({exc}); using seed ratings")


def get_rating(team: str) -> float:
    return TEAM_ELO.get(team, DEFAULT_ELO)


def _expected_goals(team_a: str, team_b: str) -> tuple[float, float]:
    ra = get_rating(team_a) + (HOST_BONUS if team_a in HOST_NATIONS else 0)
    rb = get_rating(team_b) + (HOST_BONUS if team_b in HOST_NATIONS else 0)
    supremacy = (ra - rb) / ELO_PER_GOAL  # expected goal difference for A
    lam_a = max(0.18, (BASE_TOTAL_GOALS + supremacy) / 2.0)
    lam_b = max(0.18, (BASE_TOTAL_GOALS - supremacy) / 2.0)
    return lam_a, lam_b


def _poisson_pmf(k: int, lam: float) -> float:
    return math.exp(-lam) * lam ** k / math.factorial(k)


def match_probabilities(team_a: str, team_b: str) -> Dict:
    """Full model output for team_a (home) vs team_b (away)."""
    lam_a, lam_b = _expected_goals(team_a, team_b)
    pa = [_poisson_pmf(i, lam_a) for i in range(MAX_GOALS + 1)]
    pb = [_poisson_pmf(j, lam_b) for j in range(MAX_GOALS + 1)]

    p_home = p_draw = p_away = 0.0
    p_over25 = p_btts = 0.0
    top_scores: List[tuple[str, float]] = []

    for i in range(MAX_GOALS + 1):
        for j in range(MAX_GOALS + 1):
            p = pa[i] * pb[j]
            if i > j:
                p_home += p
            elif i == j:
                p_draw += p
            else:
                p_away += p
            if i + j >= 3:
                p_over25 += p
            if i >= 1 and j >= 1:
                p_btts += p
            top_scores.append((f"{i}-{j}", p))

    top_scores.sort(key=lambda x: x[1], reverse=True)

    return {
        "team_a": team_a,
        "team_b": team_b,
        "expected_goals": {"a": round(lam_a, 2), "b": round(lam_b, 2)},
        "probs": {
            "home": round(p_home, 4),
            "draw": round(p_draw, 4),
            "away": round(p_away, 4),
        },
        "totals": {
            "over_2_5": round(p_over25, 4),
            "under_2_5": round(1 - p_over25, 4),
            "btts_yes": round(p_btts, 4),
            "btts_no": round(1 - p_btts, 4),
        },
        "likely_scorelines": [
            {"score": s, "prob": round(p, 4)} for s, p in top_scores[:5]
        ],
    }


def live_match_probabilities(team_a: str, team_b: str,
                             score_a: int, score_b: int, minute: int) -> Dict:
    """
    In-play win/draw/loss probabilities. This is what makes cash-out actually live.

    The pre-match expected goals are scaled by the fraction of the match still to play
    (goals arrive at a roughly constant Poisson rate), then layered on top of the goals
    already scored. As the clock runs down, the remaining-goal rate shrinks toward zero,
    so the probabilities lock onto the current scoreline — exactly how a real live market
    behaves.
    """
    lam_a, lam_b = _expected_goals(team_a, team_b)
    minute = max(0, min(minute, 95))
    remaining_frac = max(0.0, (95 - minute) / 95.0)
    rem_a = lam_a * remaining_frac
    rem_b = lam_b * remaining_frac

    pa = [_poisson_pmf(i, rem_a) for i in range(MAX_GOALS + 1)]
    pb = [_poisson_pmf(j, rem_b) for j in range(MAX_GOALS + 1)]

    p_home = p_draw = p_away = 0.0
    p_over25 = p_btts = 0.0
    for i in range(MAX_GOALS + 1):
        for j in range(MAX_GOALS + 1):
            p = pa[i] * pb[j]
            fa, fb = score_a + i, score_b + j
            if fa > fb:
                p_home += p
            elif fa == fb:
                p_draw += p
            else:
                p_away += p
            if fa + fb >= 3:
                p_over25 += p
            if fa >= 1 and fb >= 1:
                p_btts += p

    return {
        "team_a": team_a,
        "team_b": team_b,
        "live": True,
        "minute": minute,
        "score": {"a": score_a, "b": score_b},
        "expected_final_goals": {"a": round(score_a + rem_a, 2), "b": round(score_b + rem_b, 2)},
        "probs": {
            "home": round(p_home, 4),
            "draw": round(p_draw, 4),
            "away": round(p_away, 4),
        },
        "totals": {
            "over_2_5": round(p_over25, 4),
            "under_2_5": round(1 - p_over25, 4),
            "btts_yes": round(p_btts, 4),
            "btts_no": round(1 - p_btts, 4),
        },
    }


def _sel(label: str, prob: float) -> Dict:
    prob = min(max(prob, 1e-4), 0.9999)
    return {"label": label, "prob": round(prob, 4), "fair_odds": round(1 / prob, 2)}


def extended_markets(team_a: str, team_b: str) -> Dict:
    """
    Every bet type the model can price for this match, from the Poisson scoreline matrix:
    match result, double chance, total-goals over/under (multiple lines), each team's
    goals over/under, both-teams-to-score, correct score, and player anytime-goalscorer.

    These carry the model's probability + fair odds. (Live book prices only exist for the
    match-result market today, so value flags live on the Live Board; here it's the model's
    honest read so you can build any market into a parlay.)
    """
    from app.scorer_model import anytime_scorers

    lam_a, lam_b = _expected_goals(team_a, team_b)
    pa = [_poisson_pmf(i, lam_a) for i in range(MAX_GOALS + 1)]
    pb = [_poisson_pmf(j, lam_b) for j in range(MAX_GOALS + 1)]

    home = draw = away = 0.0
    total_dist = [0.0] * (2 * MAX_GOALS + 1)
    score_probs = []
    for i in range(MAX_GOALS + 1):
        for j in range(MAX_GOALS + 1):
            p = pa[i] * pb[j]
            if i > j:
                home += p
            elif i == j:
                draw += p
            else:
                away += p
            total_dist[i + j] += p
            score_probs.append((i, j, p))

    def total_over(line: int) -> float:  # P(total goals > line.5) = P(total >= line+1)
        return sum(total_dist[line + 1:])

    def team_over(vec, line: int) -> float:  # P(team goals > line.5)
        return 1 - sum(vec[:line + 1])

    btts_yes = (1 - pa[0]) * (1 - pb[0])

    # Winning margin / spread (matches Kalshi's "Team wins by more than X.5 goals").
    home_by_2plus = sum(p for i, j, p in score_probs if i - j >= 2)
    home_by_3plus = sum(p for i, j, p in score_probs if i - j >= 3)
    away_by_2plus = sum(p for i, j, p in score_probs if j - i >= 2)
    away_by_3plus = sum(p for i, j, p in score_probs if j - i >= 3)

    score_probs.sort(key=lambda x: x[2], reverse=True)

    markets = {
        "Match Result": [
            _sel(team_a, home), _sel("Draw", draw), _sel(team_b, away),
        ],
        "Double Chance": [
            _sel(f"{team_a} or Draw", home + draw),
            _sel(f"{team_a} or {team_b}", home + away),
            _sel(f"Draw or {team_b}", draw + away),
        ],
        "Total Goals (Over/Under)": [
            s for line in (0, 1, 2, 3, 4, 5)
            for s in (_sel(f"Over {line}.5", total_over(line)),
                      _sel(f"Under {line}.5", 1 - total_over(line)))
        ],
        "Winning Margin (Spread)": [
            _sel(f"{team_a} wins by 2+", home_by_2plus),
            _sel(f"{team_a} wins by 3+", home_by_3plus),
            _sel(f"{team_b} wins by 2+", away_by_2plus),
            _sel(f"{team_b} wins by 3+", away_by_3plus),
        ],
        f"{team_a} Goals": [
            s for line in (0, 1, 2)
            for s in (_sel(f"{team_a} Over {line}.5", team_over(pa, line)),
                      _sel(f"{team_a} Under {line}.5", 1 - team_over(pa, line)))
        ],
        f"{team_b} Goals": [
            s for line in (0, 1, 2)
            for s in (_sel(f"{team_b} Over {line}.5", team_over(pb, line)),
                      _sel(f"{team_b} Under {line}.5", 1 - team_over(pb, line)))
        ],
        "Both Teams To Score": [
            _sel("BTTS: Yes", btts_yes), _sel("BTTS: No", 1 - btts_yes),
        ],
        "Correct Score": [
            _sel(f"{i}-{j}", p) for i, j, p in score_probs[:8]
        ],
        f"{team_a} Anytime Goalscorer": [
            {"label": f"{s['player']} to score", "prob": s["prob"], "fair_odds": s["fair_odds"]}
            for s in anytime_scorers(team_a, lam_a)
        ],
        f"{team_b} Anytime Goalscorer": [
            {"label": f"{s['player']} to score", "prob": s["prob"], "fair_odds": s["fair_odds"]}
            for s in anytime_scorers(team_b, lam_b)
        ],
    }
    return {
        "team_a": team_a, "team_b": team_b,
        "expected_goals": {"a": round(lam_a, 2), "b": round(lam_b, 2)},
        "markets": {k: v for k, v in markets.items() if v},
    }


def _remaining_dists(home, away, sa, sb, minute, red_a=0, red_b=0):
    """Poisson distributions for the goals STILL TO COME, given score + time + red cards."""
    lam_a, lam_b = _expected_goals(home, away)
    # Red card adjustment: a man down ~ -28% your goals, +12% the opponent's, per card.
    # (Active only when a live-events feed reports cards; defaults to 0 = no effect.)
    for _ in range(int(red_a)):
        lam_a *= 0.72
        lam_b *= 1.12
    for _ in range(int(red_b)):
        lam_b *= 0.72
        lam_a *= 1.12
    minute = max(0, min(minute, 95))
    frac = max(0.0, (95 - minute) / 95.0)
    ra, rb = lam_a * frac, lam_b * frac
    pa = [_poisson_pmf(i, ra) for i in range(MAX_GOALS + 1)]
    pb = [_poisson_pmf(j, rb) for j in range(MAX_GOALS + 1)]
    return pa, pb


def live_leg_probability(home, away, sa, sb, minute, market, selection, red_a=0, red_b=0):
    """
    Recompute a single bet leg's probability from the LIVE game state. Returns
    (probability, trackable). trackable=False for legs we can't follow live yet
    (e.g. anytime-goalscorer — needs a live scorer feed).
    """
    m = (market or "").lower()
    s = selection or ""
    sl = s.lower()
    if "goalscorer" in m or sl.endswith("to score"):
        return None, False  # needs a live scorer/cards feed

    pa, pb = _remaining_dists(home, away, sa, sb, minute, red_a, red_b)
    ph = pd = paw = 0.0
    for i in range(len(pa)):
        for j in range(len(pb)):
            p = pa[i] * pb[j]
            fa, fb = sa + i, sb + j
            if fa > fb:
                ph += p
            elif fa == fb:
                pd += p
            else:
                paw += p

    hl, al = home.lower(), away.lower()
    if "match result" in m:
        if s == home:
            return ph, True
        if s == away:
            return paw, True
        if "draw" in sl:
            return pd, True
    if "double chance" in m:
        val = (ph if hl in sl else 0) + (pd if "draw" in sl else 0) + (paw if al in sl else 0)
        return val, True
    if "both teams" in m or "btts" in m:
        yes = (1.0 if sa >= 1 else 1 - pa[0]) * (1.0 if sb >= 1 else 1 - pb[0])
        return (yes if "yes" in sl else 1 - yes), True
    if "margin" in m or "spread" in m or "wins by" in sl:
        k = 3 if ("3+" in s or "2.5" in s) else 2
        team_home = hl in sl
        val = 0.0
        for i in range(len(pa)):
            for j in range(len(pb)):
                diff = (sa + i) - (sb + j)
                if (team_home and diff >= k) or (not team_home and -diff >= k):
                    val += pa[i] * pb[j]
        return val, True
    if "correct score" in m:
        try:
            th, ta = map(int, s.split("-"))
        except ValueError:
            return None, False
        dh, da = th - sa, ta - sb
        if dh < 0 or da < 0 or dh >= len(pa) or da >= len(pb):
            return 0.0, True
        return pa[dh] * pb[da], True
    if "over" in sl or "under" in sl:
        import re
        mm = re.search(r"(\d+)\.5", s)
        line = int(mm.group(1)) if mm else 2
        is_over = "over" in sl
        if hl in sl and "total goals" not in m:           # home team total
            idx = line - sa + 1
            over = 1 - (sum(pa[:idx]) if idx > 0 else 0.0)
            return (over if is_over else 1 - over), True
        if al in sl and "total goals" not in m:           # away team total
            idx = line - sb + 1
            over = 1 - (sum(pb[:idx]) if idx > 0 else 0.0)
            return (over if is_over else 1 - over), True
        need = line + 1 - (sa + sb)                        # overall total
        if need <= 0:
            over = 1.0
        else:
            over = sum(pa[i] * pb[j] for i in range(len(pa)) for j in range(len(pb)) if i + j >= need)
        return (over if is_over else 1 - over), True
    return None, False


def update_after_result(team_a: str, team_b: str, goals_a: int, goals_b: int, k: float = 30.0):
    """Standard Elo update so ratings sharpen as real results come in."""
    ra, rb = get_rating(team_a), get_rating(team_b)
    exp_a = 1.0 / (1 + 10 ** ((rb - ra) / 400.0))
    if goals_a > goals_b:
        score_a = 1.0
    elif goals_a == goals_b:
        score_a = 0.5
    else:
        score_a = 0.0
    # Goal-difference multiplier (bigger wins move ratings more).
    margin = abs(goals_a - goals_b)
    mult = math.log(margin + 1) + 1
    delta = k * mult * (score_a - exp_a)
    TEAM_ELO[team_a] = ra + delta
    TEAM_ELO[team_b] = rb - delta
    return {team_a: round(TEAM_ELO[team_a], 1), team_b: round(TEAM_ELO[team_b], 1)}
