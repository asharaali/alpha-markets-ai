"""
Self-scoring track record for the crypto directional model.

Every actionable pick the model surfaces is snapshotted once (keyed by market ticker). Because
Kalshi settles each 15-minute window automatically, we can grade every pick against the real
result with zero manual settling — so the model keeps its own honest score: hit rate, whether
it's calibrated (does a "60%" pick actually win ~60%?), and a Brier score. This is the only real
way to find out if the chart signal has any edge, or if it's just paying the vig on noise.

One global log (it's the model's record, not any single user's bets).
"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

from app.config import settings

_FILE = Path(settings.DATA_DIR) / "crypto_picks.json"
Path(settings.DATA_DIR).mkdir(parents=True, exist_ok=True)


def _load() -> List[Dict]:
    if not _FILE.exists():
        return []
    try:
        return json.loads(_FILE.read_text())
    except json.JSONDecodeError:
        return []


def _save(rows: List[Dict]) -> None:
    _FILE.write_text(json.dumps(rows, indent=2))


def _parse(iso: Optional[str]) -> Optional[datetime]:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None


def record_picks(markets: List[Dict]) -> int:
    """Snapshot each actionable pick once per window (keyed by ticker). Returns # newly logged."""
    rows = _load()
    seen = {r["ticker"] for r in rows}
    added = 0
    for m in markets:
        p = m.get("pick") or {}
        if not p.get("side") or m.get("ticker") in seen:
            continue
        sig = m.get("signal") or {}
        rows.append({
            "ticker": m["ticker"], "coin": m["coin"], "strike": m["strike"],
            "side": p["side"], "model_prob": p["model_prob"], "price_cents": p["price_cents"],
            "ev": p.get("ev_per_dollar"), "confidence": p.get("confidence"),
            "signal_direction": sig.get("direction"), "signal_strength": sig.get("strength"),
            "spot_at_pick": m.get("spot"), "close_time": m.get("close_time"),
            "created": datetime.now(timezone.utc).isoformat(),
            "settled": False, "result": None, "won": None,
        })
        seen.add(m["ticker"])
        added += 1
    if added:
        _save(rows)
    return added


async def settle_due(fetch_result: Callable) -> int:
    """Grade unsettled picks whose window has closed. fetch_result(ticker) -> 'yes'|'no'|None."""
    rows = _load()
    now = datetime.now(timezone.utc)
    changed = 0
    for r in rows:
        if r.get("settled"):
            continue
        close = _parse(r.get("close_time"))
        if close and now < close:
            continue                      # window still open
        res = await fetch_result(r["ticker"])
        if res in ("yes", "no"):
            r["settled"] = True
            r["result"] = res
            r["won"] = (res == r["side"])
            changed += 1
    if changed:
        _save(rows)
    return changed


def _bucket(rows: List[Dict]) -> Dict:
    n = len(rows)
    wins = sum(1 for r in rows if r.get("won"))
    return {"n": n, "wins": wins, "hit_rate": round(wins / n, 3) if n else None}


def stats() -> Dict:
    """Hit rate, calibration (avg predicted prob vs actual win rate) and Brier, plus splits by
    confidence and by chart signal. Brier < 0.25 beats a coin flip; hit_rate ~ avg_model_prob
    means the model is honestly calibrated."""
    rows = _load()
    settled = [r for r in rows if r.get("settled")]
    n = len(settled)
    wins = sum(1 for r in settled if r.get("won"))
    avg_model = round(sum(r["model_prob"] for r in settled) / n, 3) if n else None
    brier = round(sum((r["model_prob"] - (1.0 if r["won"] else 0.0)) ** 2
                      for r in settled) / n, 3) if n else None
    return {
        "graded": n, "pending": len(rows) - n, "wins": wins,
        "hit_rate": round(wins / n, 3) if n else None,
        "avg_model_prob": avg_model, "brier": brier,
        "edge_vs_coinflip": round(wins / n - 0.5, 3) if n else None,
        "by_confidence": {c: _bucket([r for r in settled if r.get("confidence") == c])
                          for c in ("high", "medium", "low")},
        "by_signal": {d: _bucket([r for r in settled if r.get("signal_direction") == d])
                      for d in ("up", "down", "flat")},
        "recent": list(reversed(rows[-12:])),
    }
