"""Auth, health and system status."""
from __future__ import annotations

import time
from typing import Any, Dict

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import auth, engine
from app.api import schemas
from app.api.deps import clear_session, current_user, set_session
from app.config import live_trading_available, settings
from app.data.kalshi import client as kalshi_client
from app.jobs import scheduler
from app.models import calibration
from app.tracking import store

router = APIRouter()

STARTED_AT = time.time()


@router.post("/api/signup")
def signup(body: schemas.AuthRequest, request: Request):
    username, error = auth.create_user(body.username, body.password)
    if error:
        return JSONResponse({"error": "invalid_request", "message": error},
                            status_code=400)
    response = JSONResponse({"user": username})
    set_session(response, username, request)
    return response


@router.post("/api/login")
def login(body: schemas.AuthRequest, request: Request):
    if not auth.verify_user(body.username, body.password):
        return JSONResponse({"error": "unauthorized",
                             "message": "wrong username or password"}, status_code=401)
    username = body.username.strip().lower()
    response = JSONResponse({"user": username})
    set_session(response, username, request)
    return response


@router.post("/api/logout")
def logout():
    response = JSONResponse({"ok": True})
    clear_session(response)
    return response


@router.get("/api/me")
def me(request: Request):
    user = current_user(request)
    if not user:
        return {"user": None}
    allowed, reason = live_trading_available(user)
    return {
        "user": user,
        "bankroll": store.get_bankroll(user),
        "live_trading": {"allowed": allowed, "reason": reason},
    }


@router.get("/api/health")
async def health() -> Dict[str, Any]:
    """Everything a reader needs to tell a healthy instance from a degraded one."""
    artifact = calibration.load()
    counts = store.stats_snapshot()
    try:
        season, week = await engine.current_week()
        schedule_ok = True
    except Exception as exc:  # noqa: BLE001
        season, week, schedule_ok = settings.SEASON, None, False

    return {
        "status": "ok" if artifact else "calibrating",
        "app": settings.APP_NAME,
        "version": settings.VERSION,
        "uptime_seconds": round(time.time() - STARTED_AT, 1),
        "season": season,
        "week": week,
        "model": {
            "calibrated": artifact is not None,
            "fitted_at": artifact.fitted_at if artifact else None,
            "seasons_fitted": artifact.seasons_fitted if artifact else [],
            "sample_games": artifact.sample_games if artifact else 0,
            "note": (None if artifact else
                     "The game model is still being fitted in the background. Projections "
                     "become available when it finishes; this normally takes under a "
                     "minute on first start."),
        },
        "data_sources": {
            "nflverse": {"configured": True, "requires_key": False,
                         "schedule_loaded": schedule_ok},
            "kalshi_market_data": {"configured": True, "requires_key": False},
            "kalshi_trading": {"configured": kalshi_client.credentials_present(),
                               "requires_key": True},
            "open_meteo": {"configured": True, "requires_key": False},
            "odds_api": {"configured": bool(settings.ODDS_API_KEY),
                         "requires_key": True,
                         "note": "Optional. Used only as a cross-check on Kalshi prices."},
        },
        "database": counts,
        "jobs": scheduler.status(),
    }


@router.get("/api/status/jobs")
def job_status():
    return scheduler.status()
