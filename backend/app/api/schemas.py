"""Request bodies. Validation lives in the types, so a bad request fails at the edge."""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class AuthRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=8, max_length=200)


class PlaceOrderRequest(BaseModel):
    ticker: str = Field(min_length=3, max_length=120)
    side: Literal["yes", "no"] = "yes"
    stake: float = Field(gt=0, le=10000)
    mode: Literal["paper", "live"] = "paper"
    game_id: Optional[str] = None
    label: Optional[str] = None
    market_type: Optional[str] = None
    model_prob: Optional[float] = Field(default=None, ge=0, le=1)


class ParlayLegRequest(BaseModel):
    ticker: str
    side: Literal["yes", "no"] = "yes"
    game_id: Optional[str] = None
    label: Optional[str] = None
    market_type: Optional[str] = None
    model_prob: Optional[float] = Field(default=None, ge=0, le=1)


class PlaceParlayRequest(BaseModel):
    legs: List[ParlayLegRequest] = Field(min_length=2, max_length=8)
    stake: float = Field(gt=0, le=10000)
    mode: Literal["paper", "live"] = "paper"
    category: str = "custom"
    combined_prob: Optional[float] = Field(default=None, ge=0, le=1)
    combined_odds: Optional[float] = None
    ev_per_dollar: Optional[float] = None
    risk_rating: Optional[str] = None


class ClosePositionRequest(BaseModel):
    position_id: str = Field(min_length=4, max_length=64)


class BankrollRequest(BaseModel):
    starting: Optional[float] = Field(default=None, ge=0, le=10_000_000)
    current: Optional[float] = Field(default=None, ge=0, le=10_000_000)
    mode: Optional[Literal["flat", "percentage", "kelly"]] = None
    flat_stake: Optional[float] = Field(default=None, gt=0, le=100000)
    percentage: Optional[float] = Field(default=None, gt=0, le=1)
    kelly_fraction: Optional[float] = Field(default=None, gt=0, le=1)
    max_stake_pct: Optional[float] = Field(default=None, gt=0, le=1)
    max_per_game_pct: Optional[float] = Field(default=None, gt=0, le=1)
    max_per_team_pct: Optional[float] = Field(default=None, gt=0, le=1)
    max_daily_pct: Optional[float] = Field(default=None, gt=0, le=1)


class BacktestRequest(BaseModel):
    seasons: List[int] = Field(min_length=1, max_length=8)
    start_week: int = Field(default=1, ge=1, le=18)
    markets: Optional[List[str]] = None
    blend_weight: Optional[float] = Field(default=None, ge=0, le=1)
