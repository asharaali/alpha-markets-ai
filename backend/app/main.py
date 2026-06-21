"""
Alpha Markets AI — FastAPI backend.

Run:  uvicorn app.main:app --reload --port 8000   (from the backend/ folder)
Then open http://localhost:8000
"""
from __future__ import annotations
import asyncio
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from app.config import settings
from app import auth
from app.data_sources.odds_api import (get_soccer_matches, get_live_scores, merge_scores,
                                       QUOTA, LIVE_STATUS)
from app.data_sources.kalshi import get_kalshi_wc_games
from app.data_sources.kalshi_markets import get_weather_markets, CITIES
from app.weather_analysis import analyze_weather
from app import weather_calibration
from app.analysis import (analyze_match, evaluate_combo, cashout_decision,
                          monitor_live_bets, build_auto_parlay, build_optimal_parlay,
                          next_best_tips, _legset)
from app.soccer_model import match_probabilities, update_after_result, MODEL_INFO, extended_markets
from app import bet_log
from app import autobet
from app.notifications import send_push

app = FastAPI(title="Alpha Markets AI", version="0.1.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# API paths reachable without being logged in.
_PUBLIC = {"/api/health", "/api/login", "/api/signup", "/api/logout", "/api/me"}


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Identify the user from the session cookie; gate the data API behind login."""
    user = None
    tok = request.cookies.get("amai_session")
    if tok:
        user = auth.read_token(tok)
    request.state.user = user
    path = request.url.path
    if path.startswith("/api/") and path not in _PUBLIC and not user:
        return JSONResponse({"error": "login required"}, status_code=401)
    return await call_next(request)


def _set_session(resp: Response, username: str, request: Request):
    # Mark secure only over HTTPS (so it works on the cloud AND on local http).
    https = (request.headers.get("x-forwarded-proto", "").startswith("https")
             or request.url.scheme == "https")
    resp.set_cookie("amai_session", auth.make_token(username), max_age=60 * 60 * 24 * 60,
                    httponly=True, samesite="lax", secure=https)


class AuthRequest(BaseModel):
    username: str
    password: str


@app.post("/api/signup")
def signup(req: AuthRequest, request: Request):
    username, err = auth.create_user(req.username, req.password)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    # First account inherits any pre-accounts bets (your $41 parlay, etc.).
    if len(auth.all_usernames()) == 1:
        bet_log.migrate_legacy(username)
    u = auth.get_user(username)
    resp = JSONResponse({"user": username, "ntfy_topic": u["ntfy_topic"]})
    _set_session(resp, username, request)
    return resp


@app.post("/api/login")
def login(req: AuthRequest, request: Request):
    if not auth.verify_user(req.username, req.password):
        return JSONResponse({"error": "wrong username or password"}, status_code=401)
    username = req.username.strip().lower()
    u = auth.get_user(username)
    resp = JSONResponse({"user": username, "ntfy_topic": u["ntfy_topic"]})
    _set_session(resp, username, request)
    return resp


@app.post("/api/logout")
def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("amai_session")
    return resp


@app.get("/api/me")
def me(request: Request):
    user = request.state.user
    if not user:
        return {"user": None}
    u = auth.get_user(user) or {}
    return {"user": user, "ntfy_topic": u.get("ntfy_topic")}


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
        "build": "early-warning-tracking-v8",
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


@app.get("/api/weather")
async def weather(bankroll: Optional[float] = None):
    """Markets tab: Kalshi daily-high temperature contracts priced against the NWS forecast."""
    rows = await get_weather_markets()
    weather_calibration.log_forecasts(rows)   # snapshot for later calibration
    res = analyze_weather(rows, bankroll)
    res["cities"] = [c[0] for c in CITIES.values()]
    return res


@app.get("/api/weather/calibration")
async def weather_calib():
    """How well the weather model's forecasts have matched reality so far (by lead time)."""
    return await weather_calibration.calibration_report()


@app.post("/api/combo")
def combo(req: ComboRequest, request: Request):
    legs = [leg.model_dump() for leg in req.legs]
    return evaluate_combo(legs, req.bankroll, user=request.state.user)


@app.post("/api/parlay/auto")
async def parlay_auto(req: AutoParlayRequest, request: Request):
    """Auto-build a parlay: 5 risk tiers, or 'optimize' for the best money+safety balance."""
    # Parlays can run 2–8 legs (default 3). Clamp whatever the UI sends.
    legs = max(2, min(int(req.legs or 3), 8))
    if req.style.lower().strip() in ("optimize", "best"):
        raw = await get_soccer_matches()
        scores = await get_live_scores()
        if scores:
            merge_scores(raw, scores)
        names = {(g["home"], g["away"]) for g in req.games}
        subset = [analyze_match(m) for m in raw if (m["home"], m["away"]) in names]
        # Skip parlays already on your slip so each request surfaces a fresh one.
        placed = {_legset(b.get("legs", [])) for b in bet_log.pending_bets(request.state.user)}
        return build_optimal_parlay(subset, legs, exclude=placed)
    return build_auto_parlay(req.games, req.style, legs)


@app.post("/api/combo/log")
def combo_log(req: LogBetRequest, request: Request):
    """Save a combo as a pending bet so you can settle it later."""
    entry = bet_log.log_bet(request.state.user, req.combo, req.stake, req.book)
    # Confirm on the phone that it's armed — and warn loudly if it CAN'T be tracked live
    # (manual legs with no game data => the monitor can't watch it => no cash-out alerts).
    legs = entry.get("legs", [])
    trackable = bool(legs) and all(isinstance(l, dict) and l.get("home") and l.get("away") for l in legs)
    topic = (auth.get_user(request.state.user) or {}).get("ntfy_topic")
    if topic:
        if trackable:
            send_push(f"{_legtext({'legs': legs})} — logged. I'm watching it live: goal + cash-out alerts are ON.",
                      title="✅ Tracking your bet", tags=["eyes"], topic=topic)
        else:
            send_push("Logged — but this bet was entered manually so I CAN'T track it live or alert you to "
                      "cash out. Re-add it with the game→market→pick dropdowns to get live alerts.",
                      title="⚠️ Not tracked live", priority="high", tags=["warning"], topic=topic)
    entry["trackable"] = trackable
    return entry


@app.post("/api/combo/settle")
def combo_settle(req: SettleRequest, request: Request):
    """Tell the model whether a logged combo hit or missed — this is how it learns."""
    b = bet_log.settle_bet(request.state.user, req.bet_id, req.hit)
    return b or {"error": "bet not found"}


@app.delete("/api/combo/{bet_id}")
def combo_delete(bet_id: str, request: Request):
    return {"deleted": bet_log.delete_bet(request.state.user, bet_id)}


@app.get("/api/bets")
def bets(request: Request):
    """Your record + the learning stats (hit rate, ROI, reality factor)."""
    return bet_log.stats(request.state.user)


@app.get("/api/tips/next")
async def tips_next(request: Request):
    """Bounce-back tips: the strongest fresh +EV plays to bet next (used after a miss)."""
    raw = await get_soccer_matches()
    scores = await get_live_scores()
    if scores:
        merge_scores(raw, scores)
    analyzed = [analyze_match(m) for m in raw]
    placed = {(b["home"], b["away"], b["selection"]) for b in autobet.today_bets(request.state.user)}
    return {"tips": next_best_tips(analyzed, exclude_selections=placed, n=3)}


@app.get("/api/bets/live")
async def bets_live(request: Request):
    """Live cash-out monitor: which pending parlays are going wrong right now."""
    scores = await get_live_scores()
    statuses = monitor_live_bets(scores or [], request.state.user)
    return {"any_live": any(s["any_live"] for s in statuses), "bets": statuses}


class AutoBetConfig(BaseModel):
    enabled: Optional[bool] = None
    mode: Optional[str] = None          # paper | live
    max_stake: Optional[float] = None
    daily_cap: Optional[float] = None
    min_edge: Optional[float] = None
    max_bets_day: Optional[int] = None


@app.get("/api/autobet")
def autobet_status(request: Request):
    return autobet.status(request.state.user)


@app.post("/api/autobet")
def autobet_set(req: AutoBetConfig, request: Request):
    return autobet.set_config(request.state.user, req.model_dump())


@app.post("/api/autobet/verify")
async def autobet_verify(request: Request):
    """Read-only check that the Kalshi key is wired and can see your account (no order placed)."""
    if not autobet.live_available(request.state.user):
        return {"ok": False, "error": "Live trading isn't enabled for this account."}
    from app.kalshi_trade import get_balance
    ok, info = await get_balance()
    return {"ok": ok, "result": info}


@app.post("/api/autobet/test-live-order")
async def autobet_test_live_order(request: Request):
    """Place ONE real minimum-size order on the most liquid market to prove the live path.
    Real money — but a single contract (~$1), gated to the live account only."""
    if not autobet.live_available(request.state.user):
        return {"ok": False, "error": "Live trading isn't enabled for this account (need Kalshi key + live flag + correct login)."}
    rows = await get_weather_markets()
    priced = [r for r in rows if r.get("yes_ask") and (r.get("depth") or 0) >= 20]
    if not priced:
        return {"ok": False, "error": "No liquid market available to test on right now."}
    best = max(priced, key=lambda r: r["depth"])          # deepest book = safest to fill
    from app.kalshi_trade import place_order
    cost = best["yes_ask"]
    ok, info = await place_order(best["ticker"], "yes", cost * 1.1)   # sized to ~1 contract
    return {"ok": ok, "result": info, "market": f"{best['city']} {best['label']}",
            "ticker": best["ticker"], "price_cents": round(cost * 100)}


@app.post("/api/notify/test")
def notify_test(request: Request):
    """Send a test push to YOUR topic so you can confirm your phone is hooked up."""
    u = auth.get_user(request.state.user) or {}
    topic = u.get("ntfy_topic")
    ok = send_push("🔔 You're connected — alerts for cash-out and live parlay swings are ON.",
                   title="Alpha Markets AI", tags=["bell"], topic=topic)
    return {"sent": ok, "topic": topic}


# ---------- background push-notification monitor ----------
_notify_state: dict = {}   # bet_id -> last bucket sent
_score_state: dict = {}    # (home, away) -> last (home_goals, away_goals) seen


def _legtext(status) -> str:
    labels = [l.get("label", "leg").split(": ")[-1] for l in status.get("legs", [])]
    return " + ".join(labels)[:90] or "your parlay"


async def _bounce_back(username: str, topic: str):
    """After a parlay dies, immediately push the strongest fresh play to bet next."""
    try:
        analyzed = [analyze_match(m) for m in await get_soccer_matches()]
        placed = {(b["home"], b["away"], b["selection"]) for b in autobet.today_bets(username)}
        tips = next_best_tips(analyzed, exclude_selections=placed, n=3)
        if tips:
            t = tips[0]
            send_push(f"Bounce back: {t['label']} @ {t['market_odds_decimal']} "
                      f"· edge +{(t['edge'] or 0)*100:.0f}%. Strongest fresh value on the board.",
                      title="💡 Place this next", tags=["bulb"], topic=topic)
    except Exception as exc:
        print(f"[bounce_back] {exc}")


async def _notify_loop():
    await asyncio.sleep(5)
    while True:
        delay = 300
        try:
            # Only fetch scores once, then check every user's bets against them.
            users_with_bets = [u for u in auth.all_usernames() if bet_log.pending_bets(u)]
            if users_with_bets:
                scores = await get_live_scores()
                any_live = False
                for username in users_with_bets:
                    topic = (auth.get_user(username) or {}).get("ntfy_topic")
                    for s in monitor_live_bets(scores or [], username):
                        if s["any_live"]:
                            any_live = True

                        # --- GOAL alerts: ping the moment a tracked game's score changes ---
                        for gv in s.get("live_games", []):
                            gkey = (gv["home"], gv["away"])
                            sig = (gv["sa"], gv["sb"])
                            prev = _score_state.get(gkey)
                            if prev is not None and sig != prev:
                                scorer = (gv["home"] if sig[0] > prev[0] else gv["away"])
                                send_push(f"GOAL — {scorer} scored! {gv['home']} {sig[0]}-{sig[1]} {gv['away']} "
                                          f"({gv['minute']}'). Watching your parlay.",
                                          title="⚽ GOAL", tags=["soccer"], topic=topic)
                            _score_state[gkey] = sig

                        # "sliding" = lost a real chunk of its value but not yet collapsed —
                        # this is the EARLY warning so he can still cash out for something.
                        sliding = s["any_live"] and not s["cash_out"] and s.get("health", 1) < 0.78
                        bucket = ("cashout" if s["cash_out"]
                                  else "sliding" if sliding
                                  else "great" if (s["any_live"] and s["live_prob"] >= 0.85)
                                  else "live" if s["any_live"] else "idle")
                        if bucket != _notify_state.get(s["id"]):
                            if bucket == "cashout":
                                send_push(f"{_legtext(s)} is slipping — live {s['live_prob']*100:.0f}% "
                                          f"(was {s['entry_prob']*100:.0f}%). {s['action']}.",
                                          title="🔴 CASH OUT", priority="high", tags=["rotating_light"], topic=topic)
                            elif bucket == "sliding":
                                send_push(f"{_legtext(s)} is turning — down to {s['live_prob']*100:.0f}% "
                                          f"(from {s['entry_prob']*100:.0f}%). Consider cashing out NOW while it still has value.",
                                          title="🟠 Heads up — cash out?", priority="high", tags=["warning"], topic=topic)
                                # Dead parlay? Hand him the next best play so there's no dead end.
                                if "DEAD" in s["action"]:
                                    await _bounce_back(username, topic)
                            elif bucket == "great":
                                send_push(f"{_legtext(s)} looking great — live {s['live_prob']*100:.0f}%! "
                                          f"On track to hit.", title="🟢 Parlay cruising",
                                          tags=["white_check_mark"], topic=topic)
                            elif bucket == "live" and _notify_state.get(s["id"]) in (None, "idle"):
                                send_push(f"Kickoff — now tracking {_legtext(s)} live. I'll ping you if it turns.",
                                          title="⚽ Game on", priority="low", tags=["soccer"], topic=topic)
                            _notify_state[s["id"]] = bucket
                delay = 45 if any_live else 120

            # Auto-bet pass for users who've armed it (paper or live).
            autobet_users = [u for u in auth.all_usernames() if autobet.get_config(u)["enabled"]]
            if autobet_users:
                analyzed = [analyze_match(m) for m in await get_soccer_matches()]
                # Price the weather board once for everyone too.
                weather_values = analyze_weather(await get_weather_markets())["value_bets"]
                for username in autobet_users:
                    topic = (auth.get_user(username) or {}).get("ntfy_topic")
                    soccer = await autobet.scan_and_place(username, analyzed)
                    weather = await autobet.scan_and_place_weather(username, weather_values)
                    for p in soccer + weather:
                        tag = "PAPER" if p["mode"] == "paper" else "LIVE 💸"
                        send_push(f"[{tag}] {p['selection']} ({p['home']} v {p['away']}) "
                                  f"${p['stake']} @ {p['odds']} · edge +{p['edge']*100:.0f}% · {p['status']}",
                                  title="🤖 Auto-bet placed", topic=topic, tags=["robot"])
                delay = min(delay, 120)
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
