"""
Kalshi perpetual futures (perps / "margin") — funding-rate carry model.

Directional perp trading at 6x is a coin flip with fees; the durable edge on a brand-new,
retail-heavy venue is the FUNDING RATE. Every 8 hours the crowded side pays the other side to
pull the perp back to spot. When Kalshi's retail book leans long harder than the global market
does, Kalshi funding runs rich vs veteran venues (Hyperliquid, Deribit) — and a delta-neutral
position (short the Kalshi perp, long spot or a cheaper perp elsewhere) COLLECTS that spread
with no price exposure. This module prices that trade:

  funding APR        = per-period rate, annualized (rate * periods/day * 365)
  spread             = Kalshi funding APR - benchmark (median of external venues)
  basis              = Kalshi mark vs its own reference index (instantaneous crowding)
  persistence        = how consistently Kalshi funding has run positive over trailing history
                       (one hot 8-hour print is noise; three weeks of positive prints is a lean)
  carry economics    = what $1,000 of notional earns per day/year, gross of trading costs
  verdict            = COLLECT (spread rich + persistent) or PASS, with confidence

Everything here is pure math on numbers the fetcher (data_sources/kalshi_perps.py) provides.
Note: funding sign convention everywhere is the standard one — positive = longs pay shorts.
"""
from __future__ import annotations
import statistics
from typing import Dict, List, Optional

# Baseline funding on basically every perp venue is +0.01% per 8h (~11% APR) — that's the
# "interest rate" leg, not crowding. Only the spread ABOVE the benchmark venues is edge.
_PERIODS_KALSHI_PER_DAY = 3          # Kalshi funds every 8 hours

# Verdict gates, in annualized terms. 5% spread on a delta-neutral book is real money;
# below that, trading costs + hedge slippage eat it.
_SPREAD_INTERESTING = 0.05           # 5% APR spread: worth watching
_SPREAD_RICH = 0.10                  # 10% APR spread: actionable if persistent
_PERSISTENCE_MIN = 0.60              # >=60% of trailing periods leaning the same way


def funding_apr(rate_per_period: float, periods_per_day: float) -> float:
    """Annualize a per-period funding rate (simple, not compounded — funding is paid, not rolled)."""
    return rate_per_period * periods_per_day * 365.0


def kalshi_apr(rate_8h: float) -> float:
    return funding_apr(rate_8h, _PERIODS_KALSHI_PER_DAY)


def basis_bps(mark: Optional[float], reference: Optional[float]) -> Optional[float]:
    """Perp mark vs the underlying index, in basis points. Positive = perp trades over spot
    (longs crowded right now)."""
    if not mark or not reference or reference <= 0:
        return None
    return (mark / reference - 1.0) * 10_000


def history_stats(rates_8h: List[float]) -> Dict:
    """Trailing Kalshi funding prints (per-8h rates, any order) -> how persistent the lean is.
    `pos_share` is the fraction of periods longs paid; `avg_apr` the mean annualized rate."""
    n = len(rates_8h)
    if n == 0:
        return {"n": 0, "avg_apr": None, "pos_share": None, "max_apr": None}
    aprs = [kalshi_apr(r) for r in rates_8h]
    return {
        "n": n,
        "avg_apr": round(statistics.mean(aprs), 4),
        "pos_share": round(sum(1 for r in rates_8h if r > 0) / n, 3),
        "max_apr": round(max(aprs, key=abs), 4),
    }


def benchmark_apr(external_aprs: Dict[str, Optional[float]]) -> Optional[float]:
    """The 'what the world charges' number: median funding APR across external venues that
    reported. Median (not mean) so one venue having a weird print doesn't skew the benchmark."""
    vals = [v for v in external_aprs.values() if v is not None]
    return round(statistics.median(vals), 4) if vals else None


def carry(kalshi_funding_apr: float, bench_apr: Optional[float]) -> Dict:
    """The two delta-neutral constructions and what each pays, gross, per $1k of notional.

    vs_spot: short Kalshi perp + long spot (Coinbase). You collect the FULL Kalshi funding
             (when positive). Cleanest hedge; ties up full spot capital.
    vs_perp: short Kalshi perp + long a perp elsewhere. You collect Kalshi funding but PAY the
             other venue's — you net the spread. Less capital (both legs levered), two
             liquidation surfaces.
    """
    spread = (kalshi_funding_apr - bench_apr) if bench_apr is not None else None
    return {
        "vs_spot_apr": round(kalshi_funding_apr, 4),
        "vs_spot_usd_per_1k_day": round(1000 * kalshi_funding_apr / 365, 2),
        "vs_perp_apr": round(spread, 4) if spread is not None else None,
        "vs_perp_usd_per_1k_day": round(1000 * spread / 365, 2) if spread is not None else None,
    }


def liquidation_move(leverage: Optional[float]) -> Optional[float]:
    """Rough adverse price move (as a fraction) that liquidates a position at this leverage.
    Upper bound — maintenance margin triggers a bit sooner. At 6x, ~a 17% move ends you;
    BTC does that in a bad week. This is why the model never recommends naked direction."""
    if not leverage or leverage <= 1:
        return None
    return round(1.0 / leverage, 4)


def assess(spread_apr: Optional[float], est_apr: float, hist: Dict,
           basis: Optional[float]) -> Dict:
    """The verdict: is Kalshi funding rich enough — and persistent enough — to collect?

    COLLECT needs all three legs of the story to agree: the current estimate is rich vs the
    benchmark, history says the lean is a habit (not one print), and we can size confidence by
    how far past the gate it is. A hot estimate with no history = WATCH, not a trade. Negative
    spread (Kalshi cheaper than the world) is surfaced as CHEAP — the mirror trade (long Kalshi,
    short elsewhere) exists but is rarely worth it on a retail-long venue, so it never fires."""
    if spread_apr is None:
        return {"action": "PASS", "confidence": None,
                "reason": "no external benchmark available — can't price the spread"}
    persistent = (hist.get("pos_share") or 0) >= _PERSISTENCE_MIN and hist.get("n", 0) >= 9
    crowded_now = basis is not None and basis > 0
    if spread_apr >= _SPREAD_RICH and persistent:
        conf = "high" if (spread_apr >= 2 * _SPREAD_RICH and crowded_now) else "medium"
        return {"action": "COLLECT", "confidence": conf,
                "reason": (f"Kalshi funding {est_apr*100:.1f}% APR runs {spread_apr*100:.1f}% "
                           f"over the benchmark and longs paid in {hist['pos_share']*100:.0f}% "
                           f"of trailing periods — short the Kalshi perp, hedge long, collect")}
    if spread_apr >= _SPREAD_INTERESTING:
        why = "lean isn't persistent yet" if not persistent else "spread below the rich gate"
        return {"action": "WATCH", "confidence": "low",
                "reason": (f"spread {spread_apr*100:.1f}% APR is interesting but {why} "
                           f"({hist.get('n', 0)} periods, "
                           f"{(hist.get('pos_share') or 0)*100:.0f}% positive)")}
    if spread_apr <= -_SPREAD_INTERESTING:
        return {"action": "CHEAP", "confidence": "low",
                "reason": (f"Kalshi funding {abs(spread_apr)*100:.1f}% APR UNDER the benchmark — "
                           "mirror carry exists (long Kalshi / short elsewhere) but rarely pays "
                           "after costs; noted, not recommended")}
    return {"action": "PASS", "confidence": None,
            "reason": (f"spread {spread_apr*100:+.1f}% APR vs benchmark — Kalshi funding is "
                       "in line with the world; no carry worth the legs")}
