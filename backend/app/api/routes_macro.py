"""Macro page data. Reads the scheduler-built file only — no upstream calls on the request path."""
from __future__ import annotations

from fastapi import APIRouter

from app.data.macro import refresh

router = APIRouter()


@router.get("/api/macro")
def macro():
    return refresh.read()
