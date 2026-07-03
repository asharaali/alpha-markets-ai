"""
Prices a Kalshi 15-minute crypto binary — "will <coin> be at/above the target price at the
15-minute mark?" — as a driftless cash-or-nothing digital option.

Over a 15-minute horizon expected drift is negligible next to volatility, so we model the price
as a driftless geometric Brownian motion and read the risk-neutral probability straight off it:

    P(S_T >= K) = N( ln(S / K) / (sigma * sqrt(T)) )

  S     = current spot (Coinbase)
  K     = the market's target/strike
  T     = time to close, in years
  sigma = annualized volatility, estimated live from recent 1-min candles (crypto_spot)
  N     = standard-normal CDF

The model's probability vs the live Kalshi price is the edge. This is a genuinely grounded
quant model (unlike the sport ratings) — its only real assumption is the vol estimate.
"""
from __future__ import annotations
import math

_SEC_PER_YEAR = 365 * 24 * 3600


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def prob_at_or_above(spot: float, strike: float, seconds_to_close: float,
                     sigma_annual: float) -> float:
    """Risk-neutral P(final price >= strike). Degenerates sensibly as time or vol -> 0."""
    if spot <= 0 or strike <= 0:
        return 0.5
    if seconds_to_close <= 0 or sigma_annual <= 0:
        return 1.0 if spot >= strike else 0.0
    T = seconds_to_close / _SEC_PER_YEAR
    d = math.log(spot / strike) / (sigma_annual * math.sqrt(T))
    return _norm_cdf(d)
