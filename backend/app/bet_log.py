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
from typing import Dict, List, Optional

from app.config import settings

# Each user's bets live in their own file => fully separate data.
_DATA = Path(settings.DATA_DIR)
_DATA.mkdir(parents=True, exist_ok=True)

# Need at least this many settled combos before we trust the reality factor.
MIN_SAMPLES_FOR_LEARNING = 10


def _store(user: str) -> Path:
    safe = "".join(c for c in (user or "default") if c.isalnum()) or "default"
    return _DATA / f"bets_{safe}.json"


def _load(user: str) -> List[Dict]:
    p = _store(user)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return []


def _save(user: str, bets: List[Dict]) -> None:
    _store(user).write_text(json.dumps(bets, indent=2))


def migrate_legacy(user: str) -> int:
    """One-time: move pre-accounts bets (single bet_log.json) into the first user's file."""
    legacy = _DATA / "bet_log.json"
    target = _store(user)
    if not legacy.exists() or target.exists():
        return 0
    try:
        data = json.loads(legacy.read_text())
    except json.JSONDecodeError:
        return 0
    if data:
        target.write_text(json.dumps(data, indent=2))
    legacy.rename(_DATA / "bet_log.migrated.json")
    return len(data)


def log_bet(user: str, combo: Dict, stake: float = 0.0, book: str = "") -> Dict:
    """Save an evaluated combo as a pending bet."""
    bets = _load(user)
    entry = {
        "id": uuid.uuid4().hex[:10],
        "created_at": time.time(),
        "book": book,
        # Which sport this bet belongs to, so the live monitor picks the right model.
        "sport": combo.get("sport", "soccer"),
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
    _save(user, bets)
    return entry


def log_manual_bet(user: str, description: str, stake: float, hit: bool,
                   odds: float = 0.0, book: str = "") -> Dict:
    """Log an ALREADY-SETTLED past bet straight into the record (for games that already
    finished — the combo builder only handles upcoming games). Counts toward record/ROI."""
    bets = _load(user)
    entry = {
        "id": uuid.uuid4().hex[:10],
        "created_at": time.time(),
        "book": book,
        "legs": [{"label": description}],   # text-only leg (not live-trackable; it's done)
        "leg_count": None,
        "model_prob": None,                 # no model prob -> excluded from reality factor
        "odds": odds or None,
        "payout_multiple": odds or None,
        "ev_per_dollar": None,
        "stake": stake,
        "manual": True,
        "status": "hit" if hit else "miss",
        "settled_at": time.time(),
    }
    bets.append(entry)
    _save(user, bets)
    return entry


def update_bet(user: str, bet_id: str, fields: Dict) -> Optional[Dict]:
    """Patch fields onto a stored bet (e.g. its Kalshi ticker + entry price for live P&L)."""
    bets = _load(user)
    for b in bets:
        if b["id"] == bet_id:
            b.update(fields)
            _save(user, bets)
            return b
    return None


def pending_bets(user: str) -> List[Dict]:
    return [b for b in _load(user) if b["status"] == "pending"]


def settle_bet(user: str, bet_id: str, hit: bool) -> Optional[Dict]:
    bets = _load(user)
    for b in bets:
        if b["id"] == bet_id:
            b["status"] = "hit" if hit else "miss"
            b["settled_at"] = time.time()
            _save(user, bets)
            return b
    return None


def cashout_bet(user: str, bet_id: str, cashout_value: float, source: str = "") -> Optional[Dict]:
    """
    Mark a pending bet as cashed out (sold before the games finished) and record the
    realized return. Status 'cashed_out' is kept SEPARATE from hit/miss: it counts toward
    your money (ROI) but not toward the model's calibration (reality factor), since the
    outcome was taken off the table early rather than allowed to resolve.
    `cashout_value` is the dollars you got back for the position.
    """
    bets = _load(user)
    for b in bets:
        if b["id"] == bet_id and b["status"] == "pending":
            b["status"] = "cashed_out"
            b["settled_at"] = time.time()
            b["cashout_value"] = round(float(cashout_value), 2)
            b["cashout_source"] = source
            _save(user, bets)
            return b
    return None


def delete_bet(user: str, bet_id: str) -> bool:
    bets = _load(user)
    new = [b for b in bets if b["id"] != bet_id]
    if len(new) != len(bets):
        _save(user, new)
        return True
    return False


def reality_factor(user: str) -> Optional[float]:
    """
    observed hit rate / predicted hit rate over settled combos.
    <1 means the model is overconfident and we should shrink future estimates.
    None until we have enough samples to mean anything.
    """
    settled = [b for b in _load(user) if b["status"] in ("hit", "miss") and b.get("model_prob")]
    if len(settled) < MIN_SAMPLES_FOR_LEARNING:
        return None
    predicted = sum(b["model_prob"] for b in settled)
    observed = sum(1 for b in settled if b["status"] == "hit")
    if predicted <= 0:
        return None
    return round(observed / predicted, 3)


def _avg_model_prob(settled: List[Dict]):
    """Average model prob over settled bets that HAVE one (manual past bets don't)."""
    ps = [b["model_prob"] for b in settled if b.get("model_prob") is not None]
    return round(sum(ps) / len(ps), 3) if ps else None


def stats(user: str) -> Dict:
    bets = _load(user)
    settled = [b for b in bets if b["status"] in ("hit", "miss")]
    cashed = [b for b in bets if b["status"] == "cashed_out"]
    pending = [b for b in bets if b["status"] == "pending"]
    hits = [b for b in settled if b["status"] == "hit"]

    # Money: settled win/loss + cashed-out realized value all count toward ROI.
    staked = (sum(b.get("stake", 0) or 0 for b in settled)
              + sum(b.get("stake", 0) or 0 for b in cashed))
    returned = (sum((b.get("stake", 0) or 0) * (b.get("odds") or 0) for b in hits)
                + sum(b.get("cashout_value", 0) or 0 for b in cashed))
    roi = ((returned - staked) / staked) if staked > 0 else None

    rf = reality_factor(user)
    return {
        "total": len(bets),
        "pending": len(pending),
        "settled": len(settled),
        "cashed_out": len(cashed),
        "hits": len(hits),
        "misses": len(settled) - len(hits),
        "hit_rate": round(len(hits) / len(settled), 3) if settled else None,
        "avg_model_prob": _avg_model_prob(settled),
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
        "recent_settled": sorted(settled + cashed,
                                 key=lambda b: b.get("settled_at") or 0, reverse=True)[:10],
    }
