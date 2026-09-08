"""Turning a model probability and a live Kalshi price into an honest edge.

Three disciplines are enforced here, and they are the difference between a research tool
and a machine for generating confident nonsense:

1. BLEND TOWARD THE MARKET. The price is not our competitor, it is evidence — thousands of
   dollars of other people's opinion. The published "fair" probability is a weighted blend
   of model and market, with the weight set by how much evidence the model actually has and
   how efficient that market type is. A moneyline gets less model weight than a team total,
   because moneylines are the most picked-over price on the board.

2. PUNISH EXTREME DISAGREEMENT. If the model says 70% and a deep market says 45%, the
   overwhelmingly likely explanation is that the model is missing something the market
   knows — a coaching decision, a late scratch, a suspension. So disagreement beyond a
   threshold is shrunk HARDER, not celebrated as a bigger edge.

3. REFUSE TO PRICE A NON-PRICE. A market with a 20c spread and $30 resting has no price to
   have an edge against, and an edge computed from it is arithmetic, not information.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from app.config import settings
from app.core.types import (Confidence, MarketQuote, MarketType, Side, Signal)

# How much weight the model gets before any adjustment, by market type. Lower means we
# defer more to the price.
#
# These values are set by MEASUREMENT, not preference. Sweeping the blend weight across
# three seasons of walk-forward backtesting (app.backtest.engine) shows the Brier score
# rising monotonically with model weight against closing sportsbook lines — on the hardest
# benchmark available, more model makes the probability WORSE, and the measured optimum is
# to defer almost entirely to the price. Value-bet ROI over the same sweep is least bad in
# the 0.15-0.20 region.
#
# So the defaults sit near that measured region rather than where a model author would like
# them to be. The model still earns its place: it prices markets no closing line covers, it
# explains why a price is what it is, and Kalshi days before kickoff is a materially less
# efficient venue than a Sunday-morning close. But it is not allowed to override the price,
# because the evidence says it has not earned that.
BASE_MODEL_WEIGHT: Dict[MarketType, float] = {
    MarketType.MONEYLINE: 0.16,
    MarketType.SPREAD: 0.18,
    MarketType.TOTAL: 0.18,
    # Team totals and margin bands are the markets closing lines cover least well, so the
    # model gets slightly more say exactly where the benchmark itself is weakest.
    MarketType.TEAM_TOTAL: 0.25,
    MarketType.WIN_MARGIN: 0.25,
    MarketType.FIRST_HALF_WINNER: 0.15,
    MarketType.FIRST_HALF_TOTAL: 0.15,
    MarketType.FIRST_HALF_SPREAD: 0.15,
}
DEFAULT_MODEL_WEIGHT = 0.15

# Beyond this much model-vs-market disagreement, extra shrinkage kicks in.
DISAGREEMENT_TOLERANCE = 0.12
# Fraction of the excess disagreement that survives the extra shrink.
EXCESS_TRUST = 0.35


def vig_free(quotes: Sequence[MarketQuote]) -> Dict[str, float]:
    """Normalise a complete set of mutually-exclusive outcomes to sum to 1.

    Kalshi is an exchange rather than a bookmaker, so its mids usually sum to within a
    point of 100% already — but "usually" is not "always", and a market quoted 63/39 is
    telling you 61.8/38.2, not 63/39.
    """
    mids = {}
    for q in quotes:
        m = q.mid
        if m is not None and m > 0:
            mids[q.ticker] = m
    total = sum(mids.values())
    if total <= 0:
        return {}
    return {ticker: value / total for ticker, value in mids.items()}


def market_probability(quote: MarketQuote,
                       vigfree: Optional[Dict[str, float]] = None) -> Optional[float]:
    """The market's probability for this quote's side."""
    if vigfree and quote.ticker in vigfree:
        base = vigfree[quote.ticker]
    else:
        base = quote.mid
    if base is None:
        return None
    return base if quote.side is Side.YES else 1.0 - base


def model_weight(market_type: MarketType, *, sample_confidence: float) -> float:
    """How much of the published probability comes from the model rather than the price."""
    base = BASE_MODEL_WEIGHT.get(market_type, DEFAULT_MODEL_WEIGHT)
    # A model running on one decayed prior season has earned less of a say than one with a
    # full sample behind it.
    return base * (0.55 + 0.45 * min(max(sample_confidence, 0.0), 1.0))


def blend(model_prob: float, market_prob: float, weight: float) -> float:
    """Blend model and market, shrinking extreme disagreement harder."""
    fair = weight * model_prob + (1.0 - weight) * market_prob
    gap = fair - market_prob
    excess = abs(gap) - DISAGREEMENT_TOLERANCE
    if excess > 0:
        # Keep the tolerated portion in full and only a fraction of the rest.
        sign = 1.0 if gap > 0 else -1.0
        kept = DISAGREEMENT_TOLERANCE + excess * EXCESS_TRUST
        fair = market_prob + sign * kept
    return min(max(fair, 1e-4), 1.0 - 1e-4)


def expected_value(prob: float, cost: float) -> Optional[float]:
    """EV per dollar staked on a binary contract bought at `cost` and paying $1."""
    if cost is None or cost <= 0 or cost >= 1:
        return None
    return (prob / cost) - 1.0


def decimal_odds(cost: float) -> Optional[float]:
    if cost is None or cost <= 0:
        return None
    return 1.0 / cost


def american_odds(cost: float) -> Optional[int]:
    d = decimal_odds(cost)
    if d is None or d <= 1:
        return None
    if d >= 2:
        return round((d - 1) * 100)
    return round(-100 / (d - 1))


def kelly_fraction(prob: float, cost: float) -> float:
    """Full-Kelly stake as a fraction of bankroll. Zero when the bet is not +EV."""
    if cost is None or cost <= 0 or cost >= 1:
        return 0.0
    b = (1.0 / cost) - 1.0
    if b <= 0:
        return 0.0
    f = (b * prob - (1.0 - prob)) / b
    return max(0.0, f)


def is_value(*, edge: float, ev: Optional[float], market_prob: float,
             liquid: bool, min_ev: Optional[float] = None) -> bool:
    """The gate every recommendation passes through.

    Four conditions, and all four matter:
      * The price must be real (liquid) — an edge computed against a 20c-wide book with no
        depth behind it is arithmetic, not an opportunity.
      * Expected return per dollar must clear MIN_EV. This, not the probability gap, is the
        quantity being maximised.
      * The probability gap must clear a small absolute noise floor, so a rounding
        difference on a cent-quoted market cannot present as an edge.
      * The price must sit inside the sane band. Extreme longshots and near-locks are where
        model error is largest relative to the price and where a small probability mistake
        becomes a large money mistake.
    """
    if not liquid or ev is None:
        return False
    if ev < (min_ev if min_ev is not None else settings.MIN_EV):
        return False
    if edge < settings.MIN_EDGE:
        return False
    return settings.LONGSHOT_FLOOR <= market_prob <= settings.HEAVY_FAV_CAP


def price_signal(signal: Signal, quote: MarketQuote, *,
                 vigfree: Optional[Dict[str, float]] = None,
                 sample_confidence: float = 0.6) -> Signal:
    """Attach a live quote to a signal and compute the published probability, edge and EV.

    `signal.model_prob` is left as the RAW model output; `market_prob` and `edge` describe
    the blended, published view. Both are carried so the UI can show the model's own number
    next to what we are actually willing to stand behind.
    """
    mprob = market_probability(quote, vigfree)
    signal.quote = quote
    if mprob is None:
        signal.market_prob = None
        signal.edge = None
        signal.ev_per_dollar = None
        return signal

    weight = model_weight(signal.market_type, sample_confidence=sample_confidence)
    fair = blend(signal.model_prob, mprob, weight)
    cost = quote.cost
    liquid = _liquid(quote)

    signal.market_prob = mprob
    signal.edge = fair - mprob
    signal.ev_per_dollar = expected_value(fair, cost) if cost else None
    signal.features.update({
        "fair_prob": round(fair, 4),
        "model_weight": round(weight, 3),
        "raw_model_prob": round(signal.model_prob, 4),
        "cost": round(cost, 4) if cost else None,
        "decimal_odds": round(decimal_odds(cost), 3) if cost else None,
        "american_odds": american_odds(cost) if cost else None,
        "depth_usd": quote.depth_usd,
        "spread_width": round(quote.spread_width, 4) if quote.spread_width else None,
        "liquid": liquid,
        "value": is_value(edge=signal.edge, ev=signal.ev_per_dollar,
                          market_prob=mprob, liquid=liquid),
    })
    return signal


def _liquid(quote: MarketQuote) -> bool:
    width = quote.spread_width
    if width is None or width > settings.MAX_QUOTE_SPREAD:
        return False
    return quote.depth_usd >= 50.0


def published_prob(signal: Signal) -> float:
    """The probability we actually stand behind — blended where a price exists."""
    fair = signal.features.get("fair_prob")
    return float(fair) if fair is not None else signal.model_prob
