"""P&L snapshots pushed from Ashar's Mac (module 3).

Broker credentials (Kalshi key, Public secret, TopstepX key) never live on the server. A job on
the Mac reads each account read-only and POSTs one normalised snapshot here; this module
validates it against a strict whitelist, stores the latest one, and keeps a small equity history.

Nothing here identifies an account: no account ids, no usernames. Unknown keys are dropped,
strings are capped, numbers must be finite, so a bad push cannot smuggle data or break the page.
"""
from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import settings

DIR = Path(settings.DATA_DIR) / "pnl"
LATEST = DIR / "latest.json"
HISTORY = DIR / "history.jsonl"

VENUES = ("kalshi", "public", "topstep")
SCHEMA = 1
MAX_POSITIONS = 200
MAX_BODY_BYTES = 256_000
HISTORY_MIN_GAP_S = 600          # one equity point per 10 min is plenty for a curve
HISTORY_KEEP = 6000              # ~40 days at 10-min spacing
# How old a venue's own as-of may be before the page calls it stale. Topstep during the runner's
# window is carried from the runner's morning journal on purpose, so it gets a longer leash.
STALE_AFTER_S = {"kalshi": 1800, "public": 1800, "topstep": 8 * 3600}


class InvalidSnapshot(ValueError):
    pass


def _num(v: Any, field: str, *, required: bool = False) -> Optional[float]:
    if v is None:
        if required:
            raise InvalidSnapshot(f"{field} is required")
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise InvalidSnapshot(f"{field} must be a number")
    try:
        f = float(v)
    except ValueError as exc:
        raise InvalidSnapshot(f"{field} must be a number") from exc
    if not math.isfinite(f) or abs(f) > 1e10:
        raise InvalidSnapshot(f"{field} is out of range")
    return round(f, 4)


def _str(v: Any, cap: int = 120) -> Optional[str]:
    if v is None:
        return None
    return str(v)[:cap]


def _position(p: Any, i: int) -> Dict[str, Any]:
    if not isinstance(p, dict):
        raise InvalidSnapshot(f"position {i} must be an object")
    sym = _str(p.get("symbol"), 64)
    if not sym:
        raise InvalidSnapshot(f"position {i} needs a symbol")
    return {
        "symbol": sym,
        "name": _str(p.get("name")),
        "kind": _str(p.get("kind"), 24),
        "qty": _num(p.get("qty"), f"position {i} qty"),
        "value": _num(p.get("value"), f"position {i} value"),
        "cost": _num(p.get("cost"), f"position {i} cost"),
        "pnl": _num(p.get("pnl"), f"position {i} pnl"),
        "day_pnl": _num(p.get("day_pnl"), f"position {i} day_pnl"),
        "note": _str(p.get("note")),
    }


def _venue(v: Any) -> Dict[str, Any]:
    if not isinstance(v, dict):
        raise InvalidSnapshot("each venue must be an object")
    name = v.get("venue")
    if name not in VENUES:
        raise InvalidSnapshot(f"unknown venue {name!r}")
    positions = v.get("positions") or []
    if not isinstance(positions, list) or len(positions) > MAX_POSITIONS:
        raise InvalidSnapshot(f"{name}: positions must be a list of at most {MAX_POSITIONS}")
    ok = bool(v.get("ok"))
    return {
        "venue": name,
        "ok": ok,
        "error": _str(v.get("error"), 300),
        "as_of": _num(v.get("as_of"), f"{name} as_of", required=ok),
        "equity": _num(v.get("equity"), f"{name} equity"),
        "cash": _num(v.get("cash"), f"{name} cash"),
        "day_pnl": _num(v.get("day_pnl"), f"{name} day_pnl"),
        "unrealized_pnl": _num(v.get("unrealized_pnl"), f"{name} unrealized_pnl"),
        "start_balance": _num(v.get("start_balance"), f"{name} start_balance"),
        "note": _str(v.get("note"), 300),
        "positions": [_position(p, i) for i, p in enumerate(positions)],
    }


def validate(body: Any) -> Dict[str, Any]:
    if not isinstance(body, dict) or body.get("schema") != SCHEMA:
        raise InvalidSnapshot(f"expected an object with schema {SCHEMA}")
    venues = body.get("venues")
    if not isinstance(venues, list) or not venues:
        raise InvalidSnapshot("venues must be a non-empty list")
    clean = [_venue(v) for v in venues]
    names = [v["venue"] for v in clean]
    if len(set(names)) != len(names):
        raise InvalidSnapshot("each venue may appear once")
    return {"schema": SCHEMA, "venues": clean}


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".part{os.getpid()}")
    tmp.write_text(text)
    tmp.replace(path)


def _read_history() -> List[Dict[str, Any]]:
    try:
        lines = HISTORY.read_text().splitlines()
    except FileNotFoundError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def save(snapshot: Dict[str, Any], now: Optional[float] = None) -> Dict[str, Any]:
    """Store a validated snapshot as the latest; add an equity point if the last is old enough.

    A venue that failed this push keeps no equity in the point (null), rather than a zero that
    would draw a fake crash on the curve.
    """
    now = time.time() if now is None else now
    blob = {**snapshot, "received_at": now}
    _write_atomic(LATEST, json.dumps(blob))

    hist = _read_history()
    added = False
    if not hist or now - float(hist[-1].get("ts", 0)) >= HISTORY_MIN_GAP_S:
        by = {v["venue"]: (v["equity"] if v["ok"] else None) for v in snapshot["venues"]}
        hist.append({"ts": now, "by_venue": by})
        hist = hist[-HISTORY_KEEP:]
        _write_atomic(HISTORY, "".join(json.dumps(h) + "\n" for h in hist))
        added = True
    return {"stored": True, "history_point": added}


def read(now: Optional[float] = None) -> Dict[str, Any]:
    """What /api/pnl serves: latest snapshot with per-venue staleness, totals, and history."""
    now = time.time() if now is None else now
    try:
        blob = json.loads(LATEST.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"ready": False, "venues": [], "history": []}

    venues = blob.get("venues", [])
    for v in venues:
        age = now - v["as_of"] if v.get("as_of") else None
        v["age_s"] = age
        v["stale"] = (not v["ok"]) or age is None or age > STALE_AFTER_S.get(v["venue"], 1800)

    good = [v for v in venues if v["ok"] and v.get("equity") is not None]
    day = [v["day_pnl"] for v in good if v.get("day_pnl") is not None]
    totals = {
        "equity": round(sum(v["equity"] for v in good), 2) if good else None,
        "day_pnl": round(sum(day), 2) if day else None,
        "venues_counted": [v["venue"] for v in good],
        "venues_missing": [v["venue"] for v in venues if v not in good],
    }
    return {"ready": True, "received_at": blob.get("received_at"), "venues": venues,
            "totals": totals, "history": _read_history()[-2000:]}
