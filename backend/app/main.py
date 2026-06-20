"""
Alpha Markets AI — FastAPI backend.

Run:  uvicorn app.main:app --reload --port 8000   (from the backend/ folder)
Then open http://localhost:8000
"""
from __future__ import annotations
import asyncio
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from app.config import settings
from app.data_sources.odds_api import (get_soccer_matches, get_live_scores, merge_scores,
                                       QUOTA, LIVE_STATUS)
from app.data_sources.kalshi import get_kalshi_wc_games
from app.analysis import (analyze_match, evaluate_combo, cashout_decision,
                          monitor_live_bets, build_auto_parlay, build_optimal_parlay)
from app.soccer_model import match_probabilities, update_after_result, MODEL_INFO, extended_markets
from app import bet_log
from app.notifications import send_push

app = FastAPI(title="Alpha Markets AI", version="0.1.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


# ---------- API models ----------

class ComboLeg(BaseModel):
    label: str
    model_prob: float
    market_odds_decimal: float
    # Optional structured info so the leg can be tracked live for cash-out.
    home: Optional[str] = None
    away: Optional[str] = None
    market: Optional[str] = None
    selection: Optional[str] = None


class ComboRequest(BaseModel):
    legs: List[ComboLeg]
    bankroll: Optional[float] = None


class LogBetRequest(BaseModel):
    combo: dict
    stake: float = 0.0
    book: str = ""


class SettleRequest(BaseModel):
    bet_id: str
    hit: bool


class AutoParlayRequest(BaseModel):
    games: List[dict]            # [{"home": "...", "away": "..."}]
    style: str = "moderate safe" # safe | moderate safe | slight risk | medium risk | risky | optimize
    legs: Optional[int] = None   # None => tier decides leg count


class CashoutRequest(BaseModel):
    entry_price: float
    current_market_price: float
    model_prob: float
    stake: float = 100.0


class ResultRequest(BaseModel):
    team_a: str
    team_b: str
    goals_a: int
    goals_b: int


# ---------- API routes ----------

@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "demo_mode": settings.DEMO_MODE,
        "live_data": not settings.DEMO_MODE,
        "kelly_fraction": settings.KELLY_FRACTION,
        "bankroll": settings.DEFAULT_BANKROLL,
        "odds_cache_ttl": settings.ODDS_CACHE_TTL,
        "quota_remaining": QUOTA["remaining"],
        "quota_used": QUOTA["used"],
    }


@app.get("/api/matches")
async def matches(bankroll: Optional[float] = None):
    raw = await get_soccer_matches()
    # Fold in live/final scores so in-play games use the live model.
    scores = await get_live_scores()
    any_live = merge_scores(raw, scores) if scores else False

    analyzed = [analyze_match(m, bankroll) for m in raw]
    # Live games first, then soonest kickoff first (so today's slate is at the top).
    analyzed.sort(key=lambda m: (m["status"] != "live", m["commence_time"]))

    poll_seconds = settings.LIVE_POLL_SECONDS if any_live else settings.IDLE_POLL_SECONDS
    return {
        "count": len(analyzed),
        "demo_mode": settings.DEMO_MODE,
        "data_live": LIVE_STATUS["live"],
        "data_reason": LIVE_STATUS["reason"],
        "any_live": any_live,
        "poll_seconds": poll_seconds,
        "quota_remaining": QUOTA["remaining"],
        "matches": analyzed,
    }


@app.get("/api/model")
def model(team_a: str, team_b: str):
    return match_probabilities(team_a, team_b)


@app.get("/api/markets")
def markets_endpoint(team_a: str, team_b: str):
    """All bet types the model can price for a matchup (goals, scores, player props…)."""
    return extended_markets(team_a, team_b)


@app.get("/api/model-info")
def model_info():
    """Training provenance + measured out-of-sample accuracy/calibration."""
    return MODEL_INFO


@app.get("/api/kalshi")
async def kalshi():
    games = await get_kalshi_wc_games()
    tradeable = sum(1 for g in games if g.get("tradeable"))
    return {"count": len(games), "tradeable": tradeable, "games": games}


@app.post("/api/combo")
def combo(req: ComboRequest):
    legs = [leg.model_dump() for leg in req.legs]
    return evaluate_combo(legs, req.bankroll)


@app.post("/api/parlay/auto")
async def parlay_auto(req: AutoParlayRequest):
    """Auto-build a parlay: 5 risk tiers, or 'optimize' for the best money+safety balance."""
    if req.style.lower().strip() in ("optimize", "best"):
        raw = await get_soccer_matches()
        scores = await get_live_scores()
        if scores:
            merge_scores(raw, scores)
        names = {(g["home"], g["away"]) for g in req.games}
        subset = [analyze_match(m) for m in raw if (m["home"], m["away"]) in names]
        return build_optimal_parlay(subset, req.legs or 3)
    return build_auto_parlay(req.games, req.style, req.legs)


@app.post("/api/combo/log")
def combo_log(req: LogBetRequest):
    """Save a combo as a pending bet so you can settle it later."""
    return bet_log.log_bet(req.combo, req.stake, req.book)


@app.post("/api/combo/settle")
def combo_settle(req: SettleRequest):
    """Tell the model whether a logged combo hit or missed — this is how it learns."""
    b = bet_log.settle_bet(req.bet_id, req.hit)
    return b or {"error": "bet not found"}


@app.delete("/api/combo/{bet_id}")
def combo_delete(bet_id: str):
    return {"deleted": bet_log.delete_bet(bet_id)}


@app.get("/api/bets")
def bets():
    """Your record + the learning stats (hit rate, ROI, reality factor)."""
    return bet_log.stats()


@app.get("/api/bets/live")
async def bets_live():
    """Live cash-out monitor: which pending parlays are going wrong right now."""
    scores = await get_live_scores()
    statuses = monitor_live_bets(scores or [])
    return {"any_live": any(s["any_live"] for s in statuses), "bets": statuses}


@app.post("/api/notify/test")
def notify_test():
    """Send a test push so you can confirm your phone is hooked up."""
    ok = send_push("🔔 You're connected — alerts for cash-out and live parlay swings are ON.",
                   title="Alpha Markets AI", tags=["bell"])
    return {"sent": ok, "topic": settings.NTFY_TOPIC}


# ---------- background push-notification monitor ----------
_notify_state: dict = {}


def _legtext(status) -> str:
    labels = [l.get("label", "leg").split(": ")[-1] for l in status.get("legs", [])]
    return " + ".join(labels)[:90] or "your parlay"


async def _notify_loop():
    await asyncio.sleep(5)
    while True:
        delay = 300
        try:
            if bet_log.pending_bets():
                scores = await get_live_scores()
                statuses = monitor_live_bets(scores or [])
                any_live = False
                for s in statuses:
                    if s["any_live"]:
                        any_live = True
                    bucket = ("cashout" if s["cash_out"]
                              else "great" if (s["any_live"] and s["live_prob"] >= 0.85)
                              else "live" if s["any_live"] else "idle")
                    if bucket != _notify_state.get(s["id"]):
                        if bucket == "cashout":
                            send_push(f"{_legtext(s)} is slipping — live {s['live_prob']*100:.0f}% "
                                      f"(was {s['entry_prob']*100:.0f}%). {s['action']}.",
                                      title="🔴 CASH OUT", priority="high", tags=["rotating_light"])
                        elif bucket == "great":
                            send_push(f"{_legtext(s)} looking great — live {s['live_prob']*100:.0f}%! "
                                      f"On track to hit.", title="🟢 Parlay cruising", tags=["white_check_mark"])
                        elif bucket == "live" and _notify_state.get(s["id"]) in (None, "idle"):
                            send_push(f"Kickoff — now tracking {_legtext(s)} live. I'll ping you if it turns.",
                                      title="⚽ Game on", priority="low", tags=["soccer"])
                        _notify_state[s["id"]] = bucket
                delay = 45 if any_live else 120
        except Exception as exc:
            print(f"[notify_loop] {exc}")
            delay = 120
        await asyncio.sleep(delay)


@app.on_event("startup")
async def _start_monitor():
    asyncio.create_task(_notify_loop())


@app.post("/api/cashout")
def cashout(req: CashoutRequest):
    return cashout_decision(
        req.entry_price, req.current_market_price, req.model_prob, req.stake)


@app.post("/api/result")
def result(req: ResultRequest):
    """Feed a final score back in so the model's Elo ratings sharpen."""
    return update_after_result(req.team_a, req.team_b, req.goals_a, req.goals_b)


# ---------- frontend ----------

@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
