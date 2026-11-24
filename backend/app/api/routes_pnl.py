"""P&L (module 3): the Mac pushes snapshots in, the terminal reads them out.

POST /api/pnl/push is the one machine route: no session, a bearer token instead
(PNL_PUSH_TOKEN, compared in constant time). With no token configured the route does not exist
as far as callers can tell (404), so a fresh deploy never accepts anonymous pushes.
GET /api/pnl is an ordinary private route (session required by the middleware).
"""
from __future__ import annotations

import hmac
import json
import os

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.pnl import store

router = APIRouter()


def _token() -> str:
    return os.getenv("PNL_PUSH_TOKEN", "").strip()


@router.post("/api/pnl/push", include_in_schema=False)
async def push(request: Request):
    expected = _token()
    if len(expected) < 32:   # unset or too short to be a real secret: behave as absent
        return JSONResponse({"error": "not_found", "message": "Not found."}, status_code=404)
    given = request.headers.get("authorization", "")
    if not hmac.compare_digest(given.encode(), f"Bearer {expected}".encode()):
        return JSONResponse({"error": "unauthorized", "message": "Bad push token."}, status_code=401)
    raw = await request.body()
    if len(raw) > store.MAX_BODY_BYTES:
        return JSONResponse({"error": "too_large", "message": "Snapshot too large."}, status_code=413)
    try:
        snapshot = store.validate(json.loads(raw))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse({"error": "invalid", "message": "Body must be JSON."}, status_code=422)
    except store.InvalidSnapshot as exc:
        return JSONResponse({"error": "invalid", "message": str(exc)}, status_code=422)
    return store.save(snapshot)


@router.get("/api/pnl")
def pnl():
    return store.read()
