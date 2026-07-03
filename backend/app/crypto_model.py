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
from typing import Dict

_SEC_PER_YEAR = 365 * 24 * 3600
_MIN_PER_YEAR = 525_600.0

# How much of recent per-minute momentum we assume persists over the remaining window. Short-
# horizon crypto momentum is weak and decays fast, so we keep this LOW — the drift is a gentle
# lean, never a takeover. The nudge is also hard-capped at a small fraction of one std dev, so
# the traded edge stays grounded in spot-vs-target + vol, not a noisy 5-minute blip.
_MOMENTUM_PERSISTENCE = 0.20
_DRIFT_STD_CAP = 0.30         # expected drift move can't exceed 0.30 std devs of the window


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def directional_signal(ret_5m: float, ret_15m: float, sigma_annual: float) -> Dict:
    """Read recent price action into a directional lean. Returns {direction, strength, score,
    note}. `score` is the 5-min move as a z-score of noise, confirmed by the 15-min trend.
    A big |score| = the recent move is large relative to normal wiggle AND the trend agrees."""
    sigma_min = max(sigma_annual, 1e-9) / math.sqrt(_MIN_PER_YEAR)
    noise_5m = sigma_min * math.sqrt(5) or 1e-9
    z = ret_5m / noise_5m
    # Trend agreement: full weight when 5-min and 15-min point the same way, half when they fight.
    agree = 1.0 if (ret_5m >= 0) == (ret_15m >= 0) else 0.5
    score = z * agree
    if score > 0.5:
        direction = "up"
    elif score < -0.5:
        direction = "down"
    else:
        direction = "flat"
    strength = min(1.0, abs(score) / 2.5)
    note = f"{ret_5m*100:+.2f}% last 5m, {ret_15m*100:+.2f}% 15m"
    return {"direction": direction, "strength": round(strength, 2),
            "score": round(score, 2), "note": note}


def prob_at_or_above(spot: float, strike: float, seconds_to_close: float,
                     sigma_annual: float, ret_5m: float = 0.0) -> float:
    """P(final price >= strike), nudged by recent momentum. Driftless base (over 15 min drift is
    tiny next to vol) plus a small, capped momentum drift from the last 5 minutes. Degenerates
    sensibly as time or vol -> 0."""
    if spot <= 0 or strike <= 0:
        return 0.5
    if seconds_to_close <= 0 or sigma_annual <= 0:
        return 1.0 if spot >= strike else 0.0
    T = seconds_to_close / _SEC_PER_YEAR
    vol_window = sigma_annual * math.sqrt(T)                 # 1 std dev of log-move this window
    mins_left = seconds_to_close / 60.0
    # Expected drift over the remaining window from recent momentum, then hard-capped.
    drift = (ret_5m / 5.0) * mins_left * _MOMENTUM_PERSISTENCE
    cap = _DRIFT_STD_CAP * vol_window
    drift = max(-cap, min(cap, drift))
    d = (math.log(spot / strike) + drift) / vol_window
    return _norm_cdf(d)
