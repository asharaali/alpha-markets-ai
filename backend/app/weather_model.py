"""
Weather edge model — the brain behind the Markets tab.

Turns the official NWS daily-high forecast into a probability distribution over the
ACTUAL high, so we can price Kalshi temperature contracts and find where the market is
mispriced. This is the genuine edge: Kalshi temperature markets are often lazily priced,
while the NWS publishes a sharp, free forecast we can build on.

Educated, not psychic. The actual daily high is modeled as Normal(NWS forecast, sigma),
where sigma is the empirical NWS max-temperature forecast error and GROWS with lead time
(today's forecast is tighter than a 5-day-out one). The sigma defaults below are calibrated
to published NWS/NDFD verification: max-temp mean-absolute-error ~2.5°F day-of, and the
error std runs ~1.25x the MAE and climbs roughly 0.7°F per day of lead. Highs are reported
as whole degrees, so we apply a half-degree continuity correction.
"""
from __future__ import annotations
import math

# NWS max-temperature forecast error (std dev, °F) by lead time in days.
_SIGMA_BY_LEAD = {0: 2.7, 1: 3.1, 2: 3.8, 3: 4.6, 4: 5.4, 5: 6.2, 6: 7.0, 7: 7.8}


def sigma_for_lead(lead_days: int) -> float:
    lead_days = max(0, int(lead_days))
    if lead_days in _SIGMA_BY_LEAD:
        return _SIGMA_BY_LEAD[lead_days]
    top = max(_SIGMA_BY_LEAD)
    return _SIGMA_BY_LEAD[top] + 0.8 * (lead_days - top)


def _phi(z: float) -> float:
    """Standard-normal CDF."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def prob_high_ge(forecast_f: float, lead_days: int, threshold_f: float) -> float:
    """P(daily high >= threshold), with a half-degree continuity correction."""
    sigma = sigma_for_lead(lead_days)
    return 1.0 - _phi((threshold_f - 0.5 - forecast_f) / sigma)


def prob_high_le(forecast_f: float, lead_days: int, threshold_f: float) -> float:
    sigma = sigma_for_lead(lead_days)
    return _phi((threshold_f + 0.5 - forecast_f) / sigma)


def prob_high_between(forecast_f: float, lead_days: int, low_f: float, high_f: float) -> float:
    sigma = sigma_for_lead(lead_days)
    zlo = (low_f - 0.5 - forecast_f) / sigma
    zhi = (high_f + 0.5 - forecast_f) / sigma
    return max(0.0, _phi(zhi) - _phi(zlo))


def model_prob(forecast_f: float, lead_days: int, strike_type: str,
               floor=None, cap=None):
    """
    Probability a Kalshi temperature contract resolves YES, given the NWS forecast.

    Kalshi strike conventions (verified against live markets):
      greater, floor=F  -> "F+1° or above"  => YES if high >= F+1
      less,    cap=C    -> "C-1° or below"  => YES if high <= C-1
      between, floor=F cap=C -> "F° to C°"  => YES if F <= high <= C
    """
    st = (strike_type or "").lower()
    if st == "greater" and floor is not None:
        return prob_high_ge(forecast_f, lead_days, floor + 1)
    if st == "less" and cap is not None:
        return prob_high_le(forecast_f, lead_days, cap - 1)
    if st == "between" and floor is not None and cap is not None:
        return prob_high_between(forecast_f, lead_days, floor, cap)
    return None
