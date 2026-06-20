"""
Bet log + learning loop.

You log a combo, then later tell it whether it HIT or MISSED. It remembers every bet
in a JSON file and measures the thing that actually matters: does reality match the
model's confidence? If the model keeps saying "35% combos" that only land 20% of the
time, that gap is real information — we surface it as a 'reality factor' and use it to
de-risk future combo estimates.

This is honest learning: it doesn't pretend to get psychic, it keeps score and corrects
its own overconfidence from your real results.
"""
from __future__ import annotations
import json
import time
import uuid
from pathlib import Path
import os
from typing import Dict, List, Optional

# In the cloud, point this at a persistent disk (e.g. /var/data) so bets survive restarts.
_STORE = Path(os.getenv("BET_LOG_PATH", str(Path(__file__).parent.parent / "data" / "bet_log.json")))
_STORE.parent.mkdir(parents=True, exist_ok=True)

# Need at least this many settled combos before we trust the reality factor.
MIN_SAMPLES_FOR_LEARNING = 10


def _load() -> List[Dict]:
    if not _STORE.exists():
        return []
    try:
        return json.loads(_STORE.read_text())
    except json.JSONDecodeError:
        return []


def _save(bets: List[Dict]) -> None:
    _STORE.write_text(json.dumps(bets, indent=2))


def log_bet(combo: Dict, stake: float = 0.0, book: str = "") -> Dict:
    """Save an evaluated combo as a pending bet."""
    bets = _load()
    entry = {
        "id": uuid.uuid4().hex[:10],
        "created_at": time.time(),
        "book": book,
        # Store the full structured legs so we can recompute them live for cash-out.
        "legs": combo.get("legs", []),
        "leg_count": combo.get("leg_count"),
        "model_prob": combo.get("combined_model_prob"),
        "odds": combo.get("combined_odds_decimal"),
        "payout_multiple": combo.get("payout_multiple"),
        "ev_per_dollar": combo.get("ev_per_dollar"),
        "stake": stake,
        "status": "pending",
        "settled_at": None,
    }
    bets.append(entry)
    _save(bets)
    return entry


def pending_bets() -> List[Dict]:
    return [b for b in _load() if b["status"] == "pending"]


def settle_bet(bet_id: str, hit: bool) -> Optional[Dict]:
    bets = _load()
    for b in bets:
        if b["id"] == bet_id:
            b["status"] = "hit" if hit else "miss"
            b["settled_at"] = time.time()
            _save(bets)
            return b
    return None


def delete_bet(bet_id: str) -> bool:
    bets = _load()
    new = [b for b in bets if b["id"] != bet_id]
    if len(new) != len(bets):
        _save(new)
        return True
    return False


def reality_factor() -> Optional[float]:
    """
    observed hit rate / predicted hit rate over settled combos.
    <1 means the model is overconfident and we should shrink future estimates.
    None until we have enough samples to mean anything.
    """
    settled = [b for b in _load() if b["status"] in ("hit", "miss") and b.get("model_prob")]
    if len(settled) < MIN_SAMPLES_FOR_LEARNING:
        return None
    predicted = sum(b["model_prob"] for b in settled)
    observed = sum(1 for b in settled if b["status"] == "hit")
    if predicted <= 0:
        return None
    return round(observed / predicted, 3)


def stats() -> Dict:
    bets = _load()
    settled = [b for b in bets if b["status"] in ("hit", "miss")]
    pending = [b for b in bets if b["status"] == "pending"]
    hits = [b for b in settled if b["status"] == "hit"]

    staked = sum(b.get("stake", 0) or 0 for b in settled)
    returned = sum((b.get("stake", 0) or 0) * (b.get("odds") or 0)
                   for b in hits)
    roi = ((returned - staked) / staked) if staked > 0 else None

    rf = reality_factor()
    return {
        "total": len(bets),
        "pending": len(pending),
        "settled": len(settled),
        "hits": len(hits),
        "misses": len(settled) - len(hits),
        "hit_rate": round(len(hits) / len(settled), 3) if settled else None,
        "avg_model_prob": round(sum(b["model_prob"] for b in settled) / len(settled), 3) if settled else None,
        "staked": round(staked, 2),
        "returned": round(returned, 2),
        "net": round(returned - staked, 2),
        "roi": round(roi, 3) if roi is not None else None,
        "reality_factor": rf,
        "learning": (
            f"Model is well-calibrated (reality factor {rf})." if rf and 0.9 <= rf <= 1.1
            else f"Model runs {'hot' if rf and rf < 1 else 'cold'} — shrinking combo estimates by reality factor {rf}." if rf
            else f"Need {MIN_SAMPLES_FOR_LEARNING - len(settled)} more settled bets before the model starts auto-correcting."
        ),
        "pending_bets": sorted(pending, key=lambda b: b["created_at"], reverse=True),
        "recent_settled": sorted(settled, key=lambda b: b.get("settled_at") or 0, reverse=True)[:10],
    }
