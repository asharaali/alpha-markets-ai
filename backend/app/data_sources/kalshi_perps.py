"""
Kalshi perpetual futures (the "margin" API surface) overlaid with the funding-carry model.

Pulls every active Kalshi perp (mark, reference index, funding estimate, leverage, OI), the
trailing week of Kalshi funding prints (one bulk call), and live funding from two veteran
venues for the benchmark:

  - Hyperliquid (funds HOURLY — normalized to APR before comparing)
  - Deribit     (8h funding, BTC/ETH only)

Kalshi's market-data + funding endpoints are public — no key needed (verified live). Prices are
per-CONTRACT (e.g. the BTC perp is 0.0001 BTC, so a $6.25 contract = $62,500 BTC); reference and
mark share that scale, so basis math works directly and we divide by contract_size only to show
the implied spot. Cached ~60s: funding estimates drift slowly, no reason to hammer anyone.
"""
from __future__ import annotations
import asyncio
import re
import time
from typing import Dict, List, Optional

import httpx

from app import perps_model

MARGIN_BASE = "https://external-api.kalshi.com/trade-api/v2/margin"
_HL_INFO = "https://api.hyperliquid.xyz/info"
_DERIBIT = "https://www.deribit.com/api/v2/public/ticker"
_UA = {"User-Agent": "AlphaMarketsAI/1.0"}

_TICKER_RE = re.compile(r"^KX([A-Z0-9]+)PERP$")
_DERIBIT_COINS = {"BTC", "ETH"}          # only their deep, always-on perpetuals
# Kalshi coin symbol -> benchmark symbol where venues use k-prefixed 1000x units (funding
# rates are unit-independent, so comparing across the scaling is fine).
_COIN_ALIASES = {"KSHIB": "SHIB", "KPEPE": "PEPE", "KBONK": "BONK"}
_HIST_DAYS = 7                           # trailing window for the persistence read (21 prints)

_CACHE: Dict[str, object] = {"data": None, "ts": 0.0}
_CACHE_TTL = 60.0


def _f(v) -> Optional[float]:
    """Kalshi margin API returns fixed-point decimal STRINGS ('6.2479'); tolerate missing."""
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _coin(ticker: str) -> Optional[str]:
    m = _TICKER_RE.match(ticker or "")
    return m.group(1) if m else None


async def _kalshi_markets(c: httpx.AsyncClient) -> List[Dict]:
    r = await c.get(f"{MARGIN_BASE}/markets")
    r.raise_for_status()
    return [m for m in r.json().get("markets", []) if m.get("status") == "active"]


async def _kalshi_estimate(c: httpx.AsyncClient, ticker: str) -> Optional[Dict]:
    try:
        r = await c.get(f"{MARGIN_BASE}/funding_rates/estimate", params={"ticker": ticker})
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


async def _kalshi_history(c: httpx.AsyncClient) -> Dict[str, List[float]]:
    """Trailing funding prints for ALL perps in one call (empty ticker = every market)."""
    try:
        r = await c.get(f"{MARGIN_BASE}/funding_rates/historical",
                        params={"start_ts": int(time.time()) - _HIST_DAYS * 86400})
        out: Dict[str, List[float]] = {}
        for row in r.json().get("funding_rates", []):
            out.setdefault(row.get("market_ticker", ""), []).append(row.get("funding_rate", 0.0))
        return out
    except Exception as exc:
        print(f"[kalshi_perps] history fetch failed: {exc}")
        return {}


async def _hyperliquid_aprs(c: httpx.AsyncClient) -> Dict[str, float]:
    """Coin -> funding APR from Hyperliquid. `funding` in each asset ctx is the HOURLY rate."""
    try:
        r = await c.post(_HL_INFO, json={"type": "metaAndAssetCtxs"})
        meta, ctxs = r.json()
        names = [a.get("name", "") for a in meta.get("universe", [])]
        return {n.lstrip("k"): perps_model.funding_apr(float(ctx["funding"]), 24)
                for n, ctx in zip(names, ctxs) if ctx.get("funding") is not None}
    except Exception as exc:
        print(f"[kalshi_perps] hyperliquid fetch failed: {exc}")
        return {}


async def _deribit_apr(c: httpx.AsyncClient, coin: str) -> Optional[float]:
    try:
        r = await c.get(_DERIBIT, params={"instrument_name": f"{coin}-PERPETUAL"})
        f8 = (r.json().get("result") or {}).get("funding_8h")
        return perps_model.funding_apr(float(f8), 3) if f8 is not None else None
    except Exception:
        return None


async def get_perp_markets(coin_filter: Optional[str] = None) -> List[Dict]:
    """Every active Kalshi perp priced by the funding-carry model: current funding vs the
    external benchmark, basis, trailing persistence, carry economics, and the verdict
    (COLLECT / WATCH / CHEAP / PASS). Actionable first, then by spread size."""
    now = time.time()
    if _CACHE["data"] is not None and now - float(_CACHE["ts"]) < _CACHE_TTL:
        data = _CACHE["data"]  # type: ignore[assignment]
        return [m for m in data if not coin_filter or m["coin"].lower() == coin_filter.lower()]

    async with httpx.AsyncClient(timeout=20, headers=_UA, follow_redirects=True) as c:
        markets, history, hl_aprs = await asyncio.gather(
            _kalshi_markets(c), _kalshi_history(c), _hyperliquid_aprs(c))
        estimates = await asyncio.gather(*[_kalshi_estimate(c, m["ticker"]) for m in markets])
        deribit_coins = sorted(_DERIBIT_COINS & {_coin(m["ticker"]) for m in markets if _coin(m["ticker"])})
        deribit_aprs = dict(zip(deribit_coins, await asyncio.gather(
            *[_deribit_apr(c, coin) for coin in deribit_coins])))

    out: List[Dict] = []
    for m, est in zip(markets, estimates):
        ticker = m["ticker"]
        coin = _coin(ticker)
        if not coin:
            continue
        size = _f(m.get("contract_size")) or 1.0
        mark = _f((m.get("settlement_mark_price") or {}).get("price"))
        ref = _f((m.get("reference_price") or {}).get("price"))
        rate_8h = (est or {}).get("funding_rate")
        if rate_8h is None:
            continue
        est_apr = perps_model.kalshi_apr(rate_8h)
        bench_coin = _COIN_ALIASES.get(coin, coin)
        external = {"hyperliquid": hl_aprs.get(bench_coin), "deribit": deribit_aprs.get(bench_coin)}
        bench = perps_model.benchmark_apr(external)
        spread = round(est_apr - bench, 4) if bench is not None else None
        hist = perps_model.history_stats(history.get(ticker, []))
        basis = perps_model.basis_bps(mark, ref)
        verdict = perps_model.assess(spread, est_apr, hist, basis)
        lev = m.get("leverage_estimate")
        out.append({
            "ticker": ticker, "coin": coin, "category": "Perps",
            "implied_spot": round(ref / size, 4) if ref else None,
            "mark": mark, "reference": ref,
            "basis_bps": round(basis, 2) if basis is not None else None,
            "funding": {
                "kalshi_rate_8h": rate_8h,
                "kalshi_apr": round(est_apr, 4),
                "benchmark_apr": bench,
                "external_apr": {k: (round(v, 4) if v is not None else None)
                                 for k, v in external.items()},
                "spread_apr": spread,
                "next_funding_time": (est or {}).get("next_funding_time"),
            },
            "history_7d": hist,
            "carry": perps_model.carry(est_apr, bench),
            "leverage_estimate": lev,
            "liquidation_move": perps_model.liquidation_move(lev),
            "open_interest_usd": _f(m.get("open_interest_notional_value_dollars")),
            "volume_24h_usd": _f(m.get("volume_24h_notional_value_dollars")),
            "verdict": verdict,
            "has_pick": verdict["action"] == "COLLECT",
        })

    out.sort(key=lambda x: (x["has_pick"], abs(x["funding"]["spread_apr"] or 0.0)), reverse=True)
    _CACHE["data"] = out
    _CACHE["ts"] = now
    return [m for m in out if not coin_filter or m["coin"].lower() == coin_filter.lower()]
