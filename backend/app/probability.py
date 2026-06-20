"""
Pure betting math. No I/O, no state — just the numbers that actually give you an edge.

The core ideas:
- Odds imply a probability, but bookmakers/exchanges bake in a margin ("vig"/"overround")
  so the implied probabilities sum to MORE than 100%. We strip that out to get the
  market's true opinion.
- Expected value (EV) tells you if a bet is +EV (worth making) or -EV (a long-run loser).
- The Kelly criterion tells you HOW MUCH to stake to grow a bankroll without going broke.
"""
from __future__ import annotations
from typing import Dict, List


# ---------- odds format conversions ----------

def american_to_decimal(american: float) -> float:
    if american > 0:
        return 1 + american / 100.0
    return 1 + 100.0 / abs(american)


def decimal_to_american(decimal_odds: float) -> int:
    if decimal_odds <= 1:
        return 0
    if decimal_odds >= 2:
        return round((decimal_odds - 1) * 100)
    return round(-100 / (decimal_odds - 1))


def prob_to_decimal(prob: float) -> float:
    """Fair decimal odds for a given probability (no margin)."""
    prob = min(max(prob, 1e-9), 1 - 1e-9)
    return 1.0 / prob


# ---------- implied probability + vig removal ----------

def implied_prob(decimal_odds: float) -> float:
    if decimal_odds <= 0:
        return 0.0
    return 1.0 / decimal_odds


def remove_vig(decimal_odds: List[float]) -> List[float]:
    """
    Turn a set of decimal odds for mutually-exclusive outcomes into
    vig-free 'true' probabilities that sum to 1.0.
    """
    raw = [implied_prob(o) for o in decimal_odds]
    total = sum(raw)
    if total <= 0:
        return [0.0 for _ in raw]
    return [p / total for p in raw]


def overround(decimal_odds: List[float]) -> float:
    """How much margin the book is charging, as a fraction. 0.05 = 5% vig."""
    return sum(implied_prob(o) for o in decimal_odds) - 1.0


# ---------- the edge: model vs market ----------

def edge(model_prob: float, market_prob_vigfree: float) -> float:
    """Positive = your model thinks this is more likely than the fair market price."""
    return model_prob - market_prob_vigfree


def expected_value(model_prob: float, decimal_odds: float) -> float:
    """
    EV per $1 staked. +0.08 means you expect to make 8 cents per dollar, long run.
    Negative means the bet is a long-run loser no matter how it lands once.
    """
    return model_prob * (decimal_odds - 1) - (1 - model_prob)


def kelly_fraction(model_prob: float, decimal_odds: float) -> float:
    """
    Full-Kelly stake as a fraction of bankroll. Returns 0 if the bet is not +EV.
    f* = (b*p - q) / b   where b = decimal_odds - 1, q = 1 - p
    """
    b = decimal_odds - 1
    if b <= 0:
        return 0.0
    p = model_prob
    q = 1 - p
    f = (b * p - q) / b
    return max(0.0, f)


def recommended_stake(model_prob: float, decimal_odds: float,
                      bankroll: float, kelly_fraction_used: float) -> Dict[str, float]:
    """Half-Kelly (or whatever fraction) stake in dollars, capped at 25% of bankroll for safety."""
    full = kelly_fraction(model_prob, decimal_odds)
    sized = full * kelly_fraction_used
    sized = min(sized, 0.25)  # never recommend betting more than a quarter of bankroll on one leg
    return {
        "kelly_full_pct": round(full * 100, 2),
        "recommended_pct": round(sized * 100, 2),
        "recommended_dollars": round(sized * bankroll, 2),
    }
