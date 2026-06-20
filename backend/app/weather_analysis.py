"""
Weather market analysis — compares the model's fair probability to the live Kalshi price
and surfaces genuine +EV bets, with Kelly-sized stakes. Same honest discipline as the
soccer engine: it only flags a bet where the model beats the market by a real margin, and
it shrinks confidence as forecast lead time grows.
"""
from __future__ import annotations
from typing import Dict, List, Optional

from app.config import settings

# Minimum edge (model prob - market price) before we call it a value bet. Weather forecasts
# carry real uncertainty, so we demand a meaningful gap, not a rounding error.
MIN_EDGE = 0.06
# A sharp market is information. We don't take the raw forecast model at face value — we
# shrink it toward the market mid so a wild disagreement (usually a station/forecast mismatch
# or info we don't have) can't masquerade as a +2000% EV bet. 0.55 = trust the model a bit
# more than the market, since Kalshi weather is genuinely soft, but never blindly.
MODEL_WEIGHT = 0.55
# Skip illiquid price tails — pennies are lottery tickets, not edge.
PRICE_FLOOR, PRICE_CEIL = 0.07, 0.93
# A real two-sided quote needs at least this much resting depth to be worth acting on.
MIN_DEPTH = 20.0


def _kelly(p: float, price: float) -> float:
    """Fractional-Kelly stake (fraction of bankroll) for a YES contract bought at `price`."""
    if price <= 0 or price >= 1:
        return 0.0
    f = (p - price) / (1 - price)            # full Kelly for a binary payout
    return max(0.0, f * settings.KELLY_FRACTION)


def analyze_weather_row(row: Dict, bankroll: float) -> Optional[Dict]:
    raw_p = row.get("model_prob")
    if raw_p is None:
        return None
    yes_ask, yes_bid = row.get("yes_ask"), row.get("yes_bid")

    # Discipline gates: today's high is already partly realized (the market sees it, we
    # don't), and we only act on real two-sided books with depth.
    if row.get("lead_days", 0) < 1:
        return None
    if yes_ask is None or yes_bid is None or yes_ask <= yes_bid:
        return None
    if (row.get("depth") or 0) < MIN_DEPTH:
        return None

    # Shrink the model toward the market mid before judging value.
    market_mid = (yes_bid + yes_ask) / 2
    p = MODEL_WEIGHT * raw_p + (1 - MODEL_WEIGHT) * market_mid

    yes_edge = p - yes_ask
    no_edge = yes_bid - p
    if yes_edge >= no_edge and yes_edge >= MIN_EDGE:
        side, price, win_p, edge = "YES", yes_ask, p, yes_edge
    elif no_edge > yes_edge and no_edge >= MIN_EDGE:
        side, price, win_p, edge = "NO", 1 - yes_bid, 1 - p, no_edge
    else:
        return None
    if price < PRICE_FLOOR or price > PRICE_CEIL:
        return None

    kelly = _kelly(win_p, price)
    stake = round(min(kelly * bankroll, bankroll * 0.05), 2)   # hard 5%-of-bankroll cap
    ev_per_dollar = round((win_p - price) / price, 4) if price > 0 else 0
    return {
        **row,
        "model_prob_shrunk": round(p, 4),
        "value_bet": True,
        "side": side,
        "price": round(price, 2),
        "edge": round(edge, 4),
        "ev_per_dollar": ev_per_dollar,
        "win_prob": round(win_p, 4),
        "kelly_stake": stake,
        "confidence": ("high" if row["lead_days"] <= 1 else
                       "medium" if row["lead_days"] <= 3 else "low"),
    }


def analyze_weather(rows: List[Dict], bankroll: Optional[float] = None) -> Dict:
    bankroll = bankroll or settings.DEFAULT_BANKROLL
    priced = [r for r in rows if r.get("model_prob") is not None]
    values = []
    for r in priced:
        v = analyze_weather_row(r, bankroll)
        if v:
            values.append(v)
    # Strongest edges first.
    values.sort(key=lambda v: v["edge"], reverse=True)
    return {
        "count": len(priced),
        "value_count": len(values),
        "bankroll": bankroll,
        "min_edge": MIN_EDGE,
        "value_bets": values,
        "all_markets": sorted(priced, key=lambda r: (r["city"], r["date"], r["label"] or "")),
    }
