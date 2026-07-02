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
from app import sports
from app.data_sources.odds_api import (get_soccer_matches, get_live_scores, merge_scores,
                                       get_matches, get_scores, QUOTA, LIVE_STATUS)
from app.data_sources.kalshi import get_kalshi_wc_games
from app.data_sources.kalshi_markets import get_weather_markets, CITIES
from app.weather_analysis import analyze_weather
from app import weather_calibration
from app.analysis import (analyze_match, evaluate_combo, cashout_decision,
                          monitor_live_bets, build_auto_parlay, build_optimal_parlay,
                          next_best_tips, _legset)
from app.soccer_model import (match_probabilities, update_after_result, MODEL_INFO,
                              extended_markets, live_leg_probability)
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
    sport: Optional[str] = None


class LogBetRequest(BaseModel):
    combo: dict
    stake: float = 0.0
    book: str = ""


class SettleRequest(BaseModel):
    bet_id: str
    hit: bool


class PlaceComboRequest(BaseModel):
    combo: dict                 # the evaluated combo (with structured legs)
    amount: float               # total dollars to put on the combo
    mode: str = "paper"         # paper | live  (live is gated to your Kalshi account)
    book: str = "Kalshi"


class CashoutComboRequest(BaseModel):
    bet_id: str


class AutoParlayRequest(BaseModel):
    games: List[dict]            # [{"home": "...", "away": "..."}]
    style: str = "moderate safe" # safe | moderate safe | slight risk | medium risk | risky | optimize
    legs: Optional[int] = None   # None => tier decides leg count
    sport: Optional[str] = None


class ManualBetRequest(BaseModel):
    description: str
    stake: float = 0.0
    hit: bool
    odds: float = 0.0
    book: str = ""


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
        "build": "kalshi-cashout-alerts-v15",
        "demo_mode": settings.DEMO_MODE,
        "live_data": not settings.DEMO_MODE,
        "kelly_fraction": settings.KELLY_FRACTION,
        "bankroll": settings.DEFAULT_BANKROLL,
        "odds_cache_ttl": settings.ODDS_CACHE_TTL,
        "quota_remaining": QUOTA["remaining"],
        "quota_used": QUOTA["used"],
    }


@app.get("/api/sports")
def sports_list():
    """The sports this engine covers (for the frontend sport switch)."""
    return {"sports": sports.all_sports(), "default": sports.DEFAULT_SPORT}


@app.get("/api/matches")
async def matches(bankroll: Optional[float] = None, sport: Optional[str] = None):
    sport = sports.normalize(sport)
    raw = await get_matches(sport)
    # Fold in live/final scores so in-play games use the live model.
    scores = await get_scores(sport)
    any_live = merge_scores(raw, scores, sport) if scores else False

    analyzed = [analyze_match(m, bankroll, sport) for m in raw]
    # Live games first, then soonest start first (so today's slate is at the top).
    analyzed.sort(key=lambda m: (m["status"] != "live", m["commence_time"]))

    poll_seconds = settings.LIVE_POLL_SECONDS if any_live else settings.IDLE_POLL_SECONDS
    return {
        "count": len(analyzed),
        "sport": sport,
        "demo_mode": settings.DEMO_MODE,
        "data_live": LIVE_STATUS["live"],
        "data_reason": LIVE_STATUS["reason"],
        "any_live": any_live,
        "poll_seconds": poll_seconds,
        "quota_remaining": QUOTA["remaining"],
        "matches": analyzed,
    }


@app.get("/api/model")
def model(team_a: str, team_b: str, sport: Optional[str] = None):
    M = sports.model(sport)
    return M.match_probabilities(team_a, team_b)


@app.get("/api/markets")
def markets_endpoint(team_a: str, team_b: str, sport: Optional[str] = None):
    """All bet types the model can price for a matchup (sport-appropriate)."""
    return sports.model(sport).extended_markets(team_a, team_b)


@app.get("/api/model-info")
def model_info(sport: Optional[str] = None):
    """Training provenance + measured out-of-sample accuracy/calibration."""
    return sports.model(sport).MODEL_INFO


@app.get("/api/kalshi")
async def kalshi(sport: Optional[str] = None):
    if sports.normalize(sport) == "mlb":
        # MLB game-line board = the KXMLBGAME moneyline singles, grouped for a card view.
        from app.data_sources.kalshi_mlb import get_single_bets
        board = await get_matches("mlb")
        bets = [b for b in await get_single_bets(board) if b["bet_type"] == "Moneyline"]
        return {"count": len(bets), "tradeable": len(bets), "sport": "mlb", "singles": bets}
    games = await get_kalshi_wc_games()
    tradeable = sum(1 for g in games if g.get("tradeable"))
    return {"count": len(games), "tradeable": tradeable, "sport": "soccer", "games": games}


@app.get("/api/kalshi/singles")
async def kalshi_singles(category: Optional[str] = None, sport: Optional[str] = None):
    """
    Every INDIVIDUAL Kalshi bet, each with the model's fair value AND (where the books cover
    the market) a multi-bookmaker consensus cross-check. Soccer: moneyline, spread, totals,
    BTTS, corners, correct score, goalscorer, to-advance. MLB: moneyline + total runs.
    """
    if sports.normalize(sport) == "mlb":
        from app.data_sources.kalshi_mlb import get_single_bets, SERIES
        board = await get_matches("mlb")
    else:
        from app.data_sources.kalshi_single import get_single_bets, SERIES
        board = await get_soccer_matches()
    bets = await get_single_bets(board)
    if category:
        bets = [b for b in bets if b["category"].lower() == category.lower()]
    cats = sorted({s[0] for s in SERIES.values()})
    return {"count": len(bets), "categories": cats, "sport": sports.normalize(sport),
            "value_count": sum(b["value_bet"] for b in bets), "bets": bets}


@app.get("/api/research")
async def research_bet(home: str, away: str, player: Optional[str] = None):
    """
    Web research for a specific bet: recent Google News headlines for the matchup (and a
    player, for props) with automatic injury / suspension / lineup flags. Reads the public
    news feed — no key — so you can sanity-check a bet against what's actually being reported.
    """
    from app.news_research import research
    return await research(home, away, player)


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
    return evaluate_combo(legs, req.bankroll, user=request.state.user,
                          sport=sports.normalize(req.sport))


@app.post("/api/parlay/auto")
async def parlay_auto(req: AutoParlayRequest, request: Request):
    """Auto-build a parlay: 5 risk tiers, or 'optimize' for the best money+safety balance."""
    sport = sports.normalize(req.sport)
    # Parlays can run 2–8 legs (default 3). Clamp whatever the UI sends.
    legs = max(2, min(int(req.legs or 3), 8))
    if req.style.lower().strip() in ("optimize", "best"):
        raw = await get_matches(sport)
        scores = await get_scores(sport)
        if scores:
            merge_scores(raw, scores, sport)
        names = {(g["home"], g["away"]) for g in req.games}
        subset = [analyze_match(m, sport=sport) for m in raw if (m["home"], m["away"]) in names]
        # Skip parlays already on your slip so each request surfaces a fresh one.
        placed = {_legset(b.get("legs", [])) for b in bet_log.pending_bets(request.state.user)}
        return build_optimal_parlay(subset, legs, exclude=placed, sport=sport)
    return build_auto_parlay(req.games, req.style, legs, sport=sport)


@app.post("/api/combo/log")
async def combo_log(req: LogBetRequest, request: Request):
    """Save a combo as a pending bet so you can settle it later."""
    entry = bet_log.log_bet(request.state.user, req.combo, req.stake, req.book)
    # Confirm on the phone that it's armed — and warn loudly if it CAN'T be tracked live
    # (manual legs with no game data => the monitor can't watch it => no cash-out alerts).
    legs = entry.get("legs", [])
    trackable = bool(legs) and all(isinstance(l, dict) and l.get("home") and l.get("away") for l in legs)
    # Phase 2: for a SINGLE-LEG match-result bet, capture the live Kalshi market price now as
    # the cost basis + remember the ticker, so live alerts can show real cash-out P&L.
    if len(legs) == 1 and legs[0].get("market") == "Match Result":
        try:
            from app.kalshi_trade import live_cashout_price
            lg = legs[0]
            info = await live_cashout_price(lg["home"], lg["away"], lg["selection"])
            if info and info.get("market_yes"):
                bet_log.update_bet(request.state.user, entry["id"],
                                   {"kalshi_ticker": info["ticker"], "kalshi_entry": info["market_yes"]})
        except Exception as exc:
            print(f"[combo_log] kalshi entry capture failed: {exc}")
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


@app.post("/api/combo/place")
async def combo_place(req: PlaceComboRequest, request: Request):
    """
    Place a chosen combo + amount ON KALSHI, then track it as ONE combo for live cash-out.

    Kalshi has no native single-ticket parlay over the API yet, so the combo goes down as
    its legs (the amount split across them) but is recorded and managed as a single combo —
    one cash-out closes the whole thing. PAPER mode (default) simulates fills so you can use
    it with zero risk; LIVE is gated to your own Kalshi account.
    """
    user = request.state.user
    combo = req.combo or {}
    legs = combo.get("legs", [])
    trackable = bool(legs) and all(isinstance(l, dict) and l.get("home") and l.get("away")
                                   and l.get("selection") for l in legs)
    if not trackable:
        return {"ok": False, "error": "This combo has legs I can't place/track (need game + selection)."}

    go_live = req.mode == "live" and autobet.live_available(user)
    per_leg = round(req.amount / max(1, len(legs)), 2)
    placed_legs, all_ok = [], True
    for lg in legs:
        leg = dict(lg)
        preset_ticker = lg.get("kalshi_ticker")    # single bets arrive with their exact ticker
        if go_live:
            from app.kalshi_trade import place_leg_detailed, place_ticker_detailed
            r = (await place_ticker_detailed(preset_ticker, per_leg) if preset_ticker
                 else await place_leg_detailed(lg["home"], lg["away"], lg["selection"], per_leg))
            leg["kalshi_ticker"] = r.get("ticker") or preset_ticker
            leg["kalshi_entry"] = r.get("entry_price")
            leg["place_status"] = "LIVE ✓" if r.get("ok") else f"live failed: {r.get('info')}"
            all_ok = all_ok and bool(r.get("ok"))
        else:
            # Paper fill at the leg's fair price implied by its odds.
            leg["kalshi_entry"] = round(1.0 / float(lg["market_odds_decimal"]), 4)
            leg["place_status"] = "paper ✓"
        placed_legs.append(leg)

    combo = dict(combo); combo["legs"] = placed_legs
    entry = bet_log.log_bet(user, combo, stake=req.amount, book=req.book)
    bet_log.update_bet(user, entry["id"], {"mode": "live" if go_live else "paper",
                                           "placed_on_kalshi": go_live, "legs": placed_legs})
    topic = (auth.get_user(user) or {}).get("ntfy_topic")
    if topic:
        tag = "LIVE 💸" if go_live else "PAPER"
        send_push(f"[{tag}] Combo placed: {_legtext({'legs': placed_legs})} · ${req.amount}. "
                  f"I'm watching it live — I'll ping you to cash out if it turns.",
                  title="🤖 Combo on Kalshi", tags=["robot"], topic=topic)
    return {"ok": all_ok, "bet_id": entry["id"], "mode": "live" if go_live else "paper",
            "per_leg": per_leg, "legs": placed_legs,
            "note": ("Placed live on Kalshi." if go_live else
                     "Paper mode — simulated fills, no real money. Enable live trading to place for real.")}


async def _combo_cashout_value(user: str, bet: dict) -> float:
    """Model fair value of an open combo position right now = potential payout × current
    combined probability. Used as the realized cash-out amount."""
    stake = bet.get("stake", 0) or 0
    payout_mult = bet.get("payout_multiple") or bet.get("odds") or 1.0
    cur_prob = bet.get("model_prob") or 0.0
    try:
        scores = await get_live_scores()
        for s in monitor_live_bets(scores or [], user):
            if s["id"] == bet["id"]:
                cur_prob = s["live_prob"]
                break
    except Exception:
        pass
    return round(stake * payout_mult * cur_prob, 2)


@app.post("/api/combo/cashout")
async def combo_cashout(req: CashoutComboRequest, request: Request):
    """
    One-tap cash out: sell the combo's Kalshi legs now (live mode) and record the realized
    value so the AI stops tracking it. In paper mode it books the model's fair cash-out value.
    """
    user = request.state.user
    bet = next((b for b in bet_log.pending_bets(user) if b["id"] == req.bet_id), None)
    if not bet:
        return {"ok": False, "error": "No pending bet with that id."}

    value = await _combo_cashout_value(user, bet)
    sold, live = [], bool(bet.get("placed_on_kalshi")) and autobet.live_available(user)
    if live:
        from app.kalshi_trade import close_ticker, close_position
        per = bet.get("stake", 0) / max(1, len(bet.get("legs", [])))
        for lg in bet.get("legs", []):
            tk = lg.get("kalshi_ticker")
            if tk:                                  # sell by ticker (works for every market)
                ok, info = await close_ticker(tk, per)
            else:
                ok, info = await close_position(lg["home"], lg["away"], lg["selection"], per)
            sold.append({"leg": lg.get("label"), "ok": ok, "info": info})

    b = bet_log.cashout_bet(user, req.bet_id, value,
                            source="live Kalshi sell" if live else "paper cash-out")
    topic = (auth.get_user(user) or {}).get("ntfy_topic")
    if topic:
        send_push(f"Cashed out {_legtext(bet)} for ${value}. Position closed — done tracking it.",
                  title="💵 Cashed out", tags=["money_with_wings"], topic=topic)
    return {"ok": True, "bet_id": req.bet_id, "cashout_value": value,
            "mode": "live" if live else "paper", "legs_sold": sold, "bet": b}


@app.post("/api/bets/manual")
def bets_manual(req: ManualBetRequest, request: Request):
    """Log an already-finished bet straight into the record (past games the builder can't reach)."""
    return bet_log.log_manual_bet(request.state.user, req.description, req.stake,
                                  req.hit, req.odds, req.book)


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
async def tips_next(request: Request, sport: Optional[str] = None):
    """Bounce-back tips: the strongest fresh +EV plays to bet next (used after a miss)."""
    sport = sports.normalize(sport)
    raw = await get_matches(sport)
    scores = await get_scores(sport)
    if scores:
        merge_scores(raw, scores, sport)
    analyzed = [analyze_match(m, sport=sport) for m in raw]
    placed = {(b["home"], b["away"], b["selection"]) for b in autobet.today_bets(request.state.user)}
    return {"tips": next_best_tips(analyzed, exclude_selections=placed, n=3)}


@app.get("/api/bets/live")
async def bets_live(request: Request):
    """Live cash-out monitor across ALL sports: which pending parlays are going wrong now."""
    statuses = []
    for sp in (s["key"] for s in sports.all_sports()):
        scores = await get_scores(sp)
        statuses += monitor_live_bets(scores or [], request.state.user, sport=sp)
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


@app.post("/api/notify/preview")
def notify_preview(request: Request):
    """Fire one of EACH real game-time alert to your phone, using the exact same send path
    the live monitor uses — so you can see what kickoff/goal/sliding/cash-out look like now."""
    topic = (auth.get_user(request.state.user) or {}).get("ntfy_topic")
    sent = 0
    sent += send_push("Kickoff — now tracking your parlay live. I'll ping you if it turns.",
                      title="⚽ Game on", priority="low", tags=["soccer"], topic=topic)
    sent += send_push("GOAL — Germany scored! Germany 1-0 Ivory Coast (23'). Watching your parlay.",
                      title="⚽ GOAL", tags=["soccer"], topic=topic)
    sent += send_push("Your parlay is turning — down to 38% (from 64%). Consider cashing out NOW "
                      "while it still has value.", title="🟠 Heads up — cash out?",
                      priority="high", tags=["warning"], topic=topic)
    sent += send_push("Your parlay is slipping — live 12% (was 64%). CASH OUT.",
                      title="🔴 CASH OUT", priority="high", tags=["rotating_light"], topic=topic)
    return {"sent": sent, "of": 4, "topic": topic}


@app.get("/api/notify/diagnose")
async def notify_diagnose(request: Request):
    """X-ray of why you are (or aren't) getting alerts: your topic, your pending bets, whether
    each is trackable, which of their games are live right now, and if the monitor is running."""
    import time as _t
    user = request.state.user
    topic = (auth.get_user(user) or {}).get("ntfy_topic")
    pend = bet_log.pending_bets(user)
    scores = await get_live_scores()
    live_pairs = {(s.get("home_team"), s.get("away_team")) for s in (scores or []) if not s.get("completed")}
    statuses = monitor_live_bets(scores or [], user)
    bets = []
    for b in pend:
        legs = b.get("legs", [])
        trackable = bool(legs) and all(isinstance(l, dict) and l.get("home") and l.get("away") for l in legs)
        st = next((s for s in statuses if s["id"] == b["id"]), None)
        bets.append({
            "id": b["id"], "stake": b.get("stake"), "trackable": trackable,
            "legs": [l.get("label") if isinstance(l, dict) else str(l) for l in legs],
            "games": [f"{l.get('home')} v {l.get('away')}" for l in legs if isinstance(l, dict) and l.get("home")],
            "any_live": st["any_live"] if st else False,
            "action": st["action"] if st else "—",
        })
    beat_age = round(_t.time() - _loop_beat["ts"], 1) if _loop_beat["ts"] else None
    return {
        "topic": topic,
        "notify_enabled": settings.NOTIFY_ENABLED,
        "monitor_running": beat_age is not None and beat_age < 300,
        "monitor_last_ran_secs_ago": beat_age,
        "monitor_iterations": _loop_beat["iterations"],
        "pending_bet_count": len(pend),
        "live_games_now": sorted(f"{h} v {a}" for h, a in live_pairs),
        "your_bets": bets,
        "verdict": (
            "No pending bets logged — there's nothing to track. Log a bet via the Combo Builder dropdowns." if not pend
            else "You have bets but none are trackable — re-log via the game→market→pick dropdowns (not manual entry)." if not any(b["trackable"] for b in bets)
            else "Bets are tracked. You'll get alerts when their games go live and the score/odds move."
        ),
    }


# ---------- background push-notification monitor ----------
_notify_state: dict = {}   # bet_id -> last bucket sent
_score_state: dict = {}    # (home, away) -> last (home_goals, away_goals) seen
_loop_beat: dict = {"ts": 0.0, "iterations": 0}   # proves the loop is alive on the cloud


def _legtext(status) -> str:
    labels = [l.get("label", "leg").split(": ")[-1] for l in status.get("legs", [])]
    return " + ".join(labels)[:90] or "your parlay"


async def _real_kalshi_pnl(bet: dict):
    """For a single-leg match-result bet with a captured Kalshi entry, pull the live sell
    price + real P&L. Returns {price, pnl_pct, entry} or None. Phase 2 of cash-out sync."""
    legs = bet.get("legs", [])
    if len(legs) != 1 or not bet.get("kalshi_entry"):
        return None
    lg = legs[0]
    try:
        from app.kalshi_trade import live_cashout_price
        info = await live_cashout_price(lg.get("home"), lg.get("away"), lg.get("selection"))
    except Exception:
        return None
    if not info or not info.get("cashout_price"):
        return None
    entry = bet["kalshi_entry"]
    price = info["cashout_price"]
    pnl = round((price - entry) / entry * 100, 1) if entry else 0
    return {"price": price, "pnl_pct": pnl, "entry": entry}


async def _sync_manual_cashouts(username: str, topic: str):
    """
    Manual cash-out sync: if you sold a tracked combo's legs yourself on Kalshi, the
    contracts won't be in your positions anymore. Detect that, mark the combo cashed-out,
    and stop tracking it — so the app always matches what you actually hold.
    """
    if not autobet.live_available(username):
        return
    live_bets = [b for b in bet_log.pending_bets(username)
                 if b.get("placed_on_kalshi") and any(l.get("kalshi_ticker") for l in b.get("legs", []))]
    if not live_bets:
        return
    from app.kalshi_trade import get_positions
    ok, held = await get_positions()
    if not ok or not isinstance(held, dict):
        return
    for b in live_bets:
        tickers = [l["kalshi_ticker"] for l in b.get("legs", []) if l.get("kalshi_ticker")]
        if tickers and not any(t in held for t in tickers):   # nothing left => you sold it
            value = await _combo_cashout_value(username, b)
            bet_log.cashout_bet(username, b["id"], value, source="manual sell (auto-detected)")
            _notify_state.pop(b["id"], None)
            send_push(f"Synced: you cashed out {_legtext(b)} on Kalshi (~${value}). "
                      f"I've closed it here and stopped tracking it.",
                      title="🔄 Cash-out synced", tags=["arrows_counterclockwise"], topic=topic)


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
    import time as _t
    await asyncio.sleep(5)
    while True:
        delay = 300
        _loop_beat["ts"] = _t.time()
        _loop_beat["iterations"] += 1
        try:
            # Fetch each sport's scores once, then check every user's bets against them.
            users_with_bets = [u for u in auth.all_usernames() if bet_log.pending_bets(u)]
            if users_with_bets:
                all_sports = [s["key"] for s in sports.all_sports()]
                scores_by_sport = {sp: (await get_scores(sp)) for sp in all_sports}
                any_live = False
                for username in users_with_bets:
                    topic = (auth.get_user(username) or {}).get("ntfy_topic")
                    # Reconcile any combos you cashed out yourself on Kalshi before tracking.
                    await _sync_manual_cashouts(username, topic)
                    bets_by_id = {b["id"]: b for b in bet_log.pending_bets(username)}
                    statuses = []
                    for sp in all_sports:
                        statuses += monitor_live_bets(scores_by_sport.get(sp) or [], username, sport=sp)
                    for s in statuses:
                        if s["any_live"]:
                            any_live = True
                        is_mlb = s.get("sport") == "mlb"

                        # --- SCORE alerts: ping the moment a tracked game's score changes ---
                        for gv in s.get("live_games", []):
                            gkey = (gv["home"], gv["away"])
                            sig = (gv["sa"], gv["sb"])
                            prev = _score_state.get(gkey)
                            if prev is not None and sig != prev:
                                scorer = (gv["home"] if sig[0] > prev[0] else gv["away"])
                                clock = f"{gv['minute']}{'th' if is_mlb else chr(39)}"
                                word = "RUN" if is_mlb else "GOAL"
                                send_push(f"{word} — {scorer} scored! {gv['home']} {sig[0]}-{sig[1]} {gv['away']} "
                                          f"({clock}). Watching your parlay.",
                                          title=f"{'⚾' if is_mlb else '⚽'} {word}",
                                          tags=["baseball" if is_mlb else "soccer"], topic=topic)
                            _score_state[gkey] = sig

                        # "sliding" = lost a real chunk of its value but not yet collapsed —
                        # this is the EARLY warning so he can still cash out for something.
                        sliding = s["any_live"] and not s["cash_out"] and s.get("health", 1) < 0.78
                        bucket = ("cashout" if s["cash_out"]
                                  else "sliding" if sliding
                                  else "great" if (s["any_live"] and s["live_prob"] >= 0.85)
                                  else "live" if s["any_live"] else "idle")
                        if bucket != _notify_state.get(s["id"]):
                            # Phase 2: real Kalshi cash-out price + P&L for single-leg positions.
                            real = await _real_kalshi_pnl(bets_by_id.get(s["id"], {})) if bucket in ("cashout", "sliding") else None
                            real_txt = (f" 💵 Sell on Kalshi at {round(real['price']*100)}¢ now "
                                        f"({'+' if real['pnl_pct'] >= 0 else ''}{real['pnl_pct']}% vs your {round(real['entry']*100)}¢ entry)."
                                        if real else "")
                            if bucket == "cashout":
                                send_push(f"{_legtext(s)} is slipping — live {s['live_prob']*100:.0f}% "
                                          f"(was {s['entry_prob']*100:.0f}%). {s['action']}.{real_txt}",
                                          title="🔴 CASH OUT", priority="high", tags=["rotating_light"], topic=topic)
                                # Dead parlay? Hand him the next best play so there's no dead end.
                                if "DEAD" in s["action"]:
                                    await _bounce_back(username, topic)
                            elif bucket == "sliding":
                                send_push(f"{_legtext(s)} is turning — down to {s['live_prob']*100:.0f}% "
                                          f"(from {s['entry_prob']*100:.0f}%). Consider cashing out NOW while it still has value.{real_txt}",
                                          title="🟠 Heads up — cash out?", priority="high", tags=["warning"], topic=topic)
                            elif bucket == "great":
                                send_push(f"{_legtext(s)} looking great — live {s['live_prob']*100:.0f}%! "
                                          f"On track to hit.", title="🟢 Parlay cruising",
                                          tags=["white_check_mark"], topic=topic)
                            elif bucket == "live" and _notify_state.get(s["id"]) in (None, "idle"):
                                send_push(f"Kickoff — now tracking {_legtext(s)} live. I'll ping you if it turns.",
                                          title="⚽ Game on", priority="low", tags=["soccer"], topic=topic)
                            _notify_state[s["id"]] = bucket
                delay = 22 if any_live else 120   # poll fast during live games so alerts aren't stale

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


class LiveCashoutRequest(BaseModel):
    home: str
    away: str
    selection: str
    entry_price: float          # what you paid for YES (0-1)
    stake: float = 100.0


@app.post("/api/cashout/live")
async def cashout_live(req: LiveCashoutRequest):
    """Pull the REAL live Kalshi cash-out (sell) price for a position and run the decision
    against the model's current fair value — actual money, not a typed-in guess."""
    from app.kalshi_trade import live_cashout_price
    info = await live_cashout_price(req.home, req.away, req.selection)
    if not info:
        return {"ok": False, "error": "No matching open Kalshi market for that game/selection."}
    if not info.get("cashout_price"):
        return {"ok": False, "error": "That Kalshi market has no live sell price right now (thin book).",
                "ticker": info["ticker"]}
    # Model's current fair value for this selection (live if the game's in play, else pre-match).
    scores = await get_live_scores()
    g = next((s for s in (scores or []) if s.get("home_team") == req.home and s.get("away_team") == req.away), None)
    model_prob = info["market_yes"]   # fallback: market mid
    try:
        if g and not g.get("completed") and g.get("scores"):
            sm = {x["name"]: int(x.get("score") or 0) for x in g["scores"]}
            from app.data_sources.odds_api import _estimate_minute
            p, trk = live_leg_probability(req.home, req.away, sm.get(req.home, 0), sm.get(req.away, 0),
                                          _estimate_minute(g.get("commence_time", "")), "Match Result", req.selection)
            if trk and p is not None:
                model_prob = p
        else:
            mp = match_probabilities(req.home, req.away)["probs"]
            model_prob = {"Draw": mp["draw"]}.get(req.selection, mp["home"] if req.selection == req.home else mp["away"])
    except Exception:
        pass
    decision = cashout_decision(req.entry_price, info["cashout_price"], model_prob, req.stake)
    decision.update(ok=True, ticker=info["ticker"], live_cashout_price=info["cashout_price"],
                    book_depth=info["depth"], source="live Kalshi order book")
    return decision


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
