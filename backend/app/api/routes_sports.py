"""Sports page data. Reads the scheduler-built files only — no upstream calls on the request path."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.data.sports import refresh

router = APIRouter()


@router.get("/api/sports")
def sports_index():
    return refresh.read_index()


@router.get("/api/sports/{league}")
def sports_league(league: str):
    blob = refresh.read_league(league)
    if blob is None:
        raise HTTPException(status_code=404, detail="unknown league")
    return blob
