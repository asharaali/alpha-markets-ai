"""
Keeps the soccer Elo ratings CURRENT during the tournament by folding in real final scores.

Why: the trained ratings are a snapshot (trained_model.json, retrained occasionally), but a
World Cup moves fast — the team that just laboured past a minnow in the Round of 32 is not
the team the snapshot rated. soccer_model.update_after_result already knows how to move
ratings; nothing was calling it automatically. This closes that loop:

  - pull recently completed games from the (already cached) Odds API scores feed;
    disabled entirely in demo mode so we never learn from fake scores
  - apply each FINAL result exactly once (persisted set of applied event ids)
  - persist the patched ratings keyed to the trained snapshot they were built on, so a
    retrain supersedes the live patches and a server restart doesn't lose them
"""
from __future__ import annotations
import json
import time
from pathlib import Path
from typing import Dict

from app import soccer_model
from app.config import settings
from app.data_sources.kalshi import KALSHI_NAME_MAP

_STATE_PATH = Path(__file__).parent.parent / "data" / "elo_live.json"
_SYNC_TTL = 1800   # re-check for new finals at most every 30 min
_last = {"ts": 0.0}


def _canon(name: str) -> str:
    return KALSHI_NAME_MAP.get((name or "").strip(), (name or "").strip())


def _base_id() -> str:
    """Identity of the trained snapshot the live patches sit on."""
    return str(soccer_model.MODEL_INFO.get("trained_at") or "seed")


def _load_state() -> Dict:
    try:
        st = json.loads(_STATE_PATH.read_text())
        if st.get("base") == _base_id():     # a retrain supersedes old live patches
            return st
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return {"base": _base_id(), "applied": {}, "ratings": {}}


def apply_persisted() -> None:
    """On startup: re-apply the persisted live rating patches on top of the trained snapshot."""
    st = _load_state()
    if st["ratings"]:
        soccer_model.TEAM_ELO.update({k: float(v) for k, v in st["ratings"].items()})
        print(f"[elo_sync] restored live Elo patches for {len(st['ratings'])} teams "
              f"({len(st['applied'])} results applied on snapshot {st['base']})")


async def maybe_sync() -> None:
    """Fold any newly-completed games into the Elo ratings. TTL-gated and idempotent, so it's
    safe to call from any hot endpoint."""
    if settings.DEMO_MODE or time.time() - _last["ts"] < _SYNC_TTL:
        return
    _last["ts"] = time.time()
    from app.data_sources.odds_api import get_scores
    try:
        scores = await get_scores("soccer")
    except Exception as exc:
        print(f"[elo_sync] scores fetch failed: {exc}")
        return

    st = _load_state()
    changed = False
    for s in scores or []:
        if not s.get("completed"):
            continue
        eid = s.get("id")
        if not eid or eid in st["applied"]:
            continue
        h, a = _canon(s.get("home_team")), _canon(s.get("away_team"))
        # Only learn on teams the model already rates — an unknown name here is a feed-vs-model
        # naming gap (fix KALSHI_NAME_MAP), and learning under the wrong key would fork a team
        # into two divergent ratings.
        if h not in soccer_model.TEAM_ELO or a not in soccer_model.TEAM_ELO:
            print(f"[elo_sync] SKIP unmapped team name(s): {s.get('home_team')!r} vs "
                  f"{s.get('away_team')!r} — add an alias to KALSHI_NAME_MAP")
            continue
        smap = {x.get("name"): x.get("score") for x in (s.get("scores") or [])}
        try:
            gh, ga = int(smap[s.get("home_team")]), int(smap[s.get("away_team")])
        except (KeyError, TypeError, ValueError):
            continue
        upd = soccer_model.update_after_result(h, a, gh, ga)
        st["applied"][eid] = f"{h} {gh}-{ga} {a}"
        st["ratings"].update(upd)
        changed = True
        print(f"[elo_sync] applied final: {h} {gh}-{ga} {a} -> {upd}")

    if changed:
        _STATE_PATH.parent.mkdir(exist_ok=True)
        _STATE_PATH.write_text(json.dumps(st, indent=2))
