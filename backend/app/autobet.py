"""
Auto-bet engine — autonomous order placement, built safe.

Defaults to PAPER mode (simulated orders, no real money). Going LIVE is double-gated:
the server must allow it (AUTOBET_LIVE_ALLOWED) AND a Kalshi key must be present. On top
of the user's own limits, two HARD ceilings can never be exceeded (max stake per bet, max
spend per day) — a runaway bug or a fat-fingered setting still can't drain the account.

It only fires on the model's strongest value picks (edge >= min_edge) on pre-match games,
never the same game twice in a day.
"""
from __future__ import annotations
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

from app.config import settings

_DATA = Path(settings.DATA_DIR)
DEFAULTS = {"enabled": False, "mode": "paper", "max_stake": 2.0,
            "daily_cap": 10.0, "min_edge": 0.06, "max_bets_day": 3}


def _safe(user: str) -> str:
    return "".join(c for c in (user or "x") if c.isalnum()) or "x"


def _cfg_path(user): return _DATA / f"autobet_{_safe(user)}.json"
def _log_path(user): return _DATA / f"autobet_log_{_safe(user)}.json"
def _today(): return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def get_config(user: str) -> Dict:
    cfg = dict(DEFAULTS)
    p = _cfg_path(user)
    if p.exists():
        try:
            cfg.update(json.loads(p.read_text()))
        except json.JSONDecodeError:
            pass
    return cfg


def set_config(user: str, updates: Dict) -> Dict:
    cfg = get_config(user)
    for k in ("enabled", "mode", "max_stake", "daily_cap", "min_edge", "max_bets_day"):
        if updates.get(k) is not None:
            cfg[k] = updates[k]
    # Hard safety clamps — the user cannot exceed these no matter what they enter.
    cfg["max_stake"] = max(0.0, min(float(cfg["max_stake"]), settings.AUTOBET_HARD_MAX_STAKE))
    cfg["daily_cap"] = max(0.0, min(float(cfg["daily_cap"]), settings.AUTOBET_HARD_DAILY_CAP))
    cfg["min_edge"] = max(0.03, float(cfg["min_edge"]))          # never bet razor-thin edges
    cfg["max_bets_day"] = max(0, min(int(cfg["max_bets_day"]), 20))
    cfg["enabled"] = bool(cfg["enabled"])
    # Cannot go live unless YOU are the Kalshi account owner + server allows it + key present.
    if cfg["mode"] == "live" and not live_available(user):
        cfg["mode"] = "paper"
    _cfg_path(user).write_text(json.dumps(cfg, indent=2))
    return cfg


def live_available(user: str = "") -> bool:
    """Live only for the one account the Kalshi key belongs to. Everyone else: paper-only."""
    return bool(settings.AUTOBET_LIVE_ALLOWED and settings.KALSHI_KEY_ID and settings.KALSHI_PRIVATE_KEY
                and (user or "").strip().lower() == settings.AUTOBET_LIVE_USER)


def _load_log(user) -> List[Dict]:
    p = _log_path(user)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            return []
    return []


def _save_log(user, log): _log_path(user).write_text(json.dumps(log, indent=2))
def today_bets(user): return [b for b in _load_log(user) if b.get("day") == _today()]
def today_spend(user): return round(sum(b.get("stake", 0) for b in today_bets(user)), 2)


def status(user: str) -> Dict:
    cfg = get_config(user)
    return {
        "config": cfg,
        "today_count": len(today_bets(user)),
        "today_spend": today_spend(user),
        "remaining_today": round(max(0.0, cfg["daily_cap"] - today_spend(user)), 2),
        "hard_max_stake": settings.AUTOBET_HARD_MAX_STAKE,
        "hard_daily_cap": settings.AUTOBET_HARD_DAILY_CAP,
        "live_available": live_available(user),
        "recent": sorted(_load_log(user), key=lambda b: b.get("ts", 0), reverse=True)[:25],
    }


async def scan_and_place(user: str, matches: List[Dict]) -> List[Dict]:
    """Run one auto-bet pass for a user over the analyzed board."""
    cfg = get_config(user)
    if not cfg["enabled"]:
        return []
    log = _load_log(user)
    done = {(b["home"], b["away"], b["selection"]) for b in log if b.get("day") == _today()}
    spend, count, placed = today_spend(user), len(today_bets(user)), []

    for m in matches:
        if m.get("status") != "upcoming":     # never chase in-play; pre-match only
            continue
        for s in m.get("suggestions", []):
            if s.get("market") != "Match Result" or not s.get("value_bet"):
                continue
            if s.get("edge", 0) < cfg["min_edge"]:
                continue
            key = (m["home"], m["away"], s["selection"])
            if key in done:
                continue
            if count >= cfg["max_bets_day"] or spend >= cfg["daily_cap"]:
                break
            stake = round(min(cfg["max_stake"], cfg["daily_cap"] - spend), 2)
            if stake <= 0:
                break
            entry = {"ts": time.time(), "day": _today(), "home": m["home"], "away": m["away"],
                     "selection": s["selection"], "odds": s["market_odds_decimal"],
                     "edge": round(s["edge"], 4), "ev": round(s["ev_per_dollar"], 4),
                     "stake": stake, "mode": cfg["mode"]}
            if cfg["mode"] == "live" and live_available(user):
                from app.kalshi_trade import place_yes
                ok, info = await place_yes(m["home"], m["away"], s["selection"], stake)
                entry["status"] = "LIVE ✓" if ok else "live failed"
                entry["info"] = info
            else:
                entry["status"] = "paper ✓"
            log.append(entry)
            placed.append(entry)
            spend += stake
            count += 1
            done.add(key)
    if placed:
        _save_log(user, log)
    return placed
