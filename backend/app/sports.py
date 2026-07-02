"""
Sport registry — the single place that knows which model + config powers each sport.

Everything downstream (analysis, the API, the data adapters) dispatches through here so
adding a sport is "register it once" instead of threading if/else soccer-vs-baseball
branches through the whole codebase. Soccer stays the default so existing behaviour and
URLs are unchanged when no ?sport= is given.
"""
from __future__ import annotations
from typing import Dict

from app.config import settings
from app import soccer_model
from app import baseball_model

# key -> config. `model` is the module (both expose the same public functions:
# match_probabilities / extended_markets / live_match_probabilities /
# live_leg_probability / update_after_result / MODEL_INFO).
_SPORTS: Dict[str, Dict] = {
    "soccer": {
        "key": "soccer",
        "model": soccer_model,
        "label": "FIFA World Cup",
        "sport_key": settings.SOCCER_SPORT_KEY,   # The Odds API key
        "has_draw": True,
        "unit": "goals",
        "icon": "⚽",
        # markets eligible for a Kalshi parlay leg (regex over the market name)
        "parlay_markets": r"match result|winning margin|spread|total goals|both teams|goalscorer|corners",
    },
    "mlb": {
        "key": "mlb",
        "model": baseball_model,
        "label": "MLB",
        "sport_key": settings.MLB_SPORT_KEY,
        "has_draw": False,
        "unit": "runs",
        "icon": "⚾",
        "parlay_markets": r"moneyline|run line|total runs|team total|first 5",
    },
}

DEFAULT_SPORT = "soccer"


def normalize(sport: str | None) -> str:
    s = (sport or "").strip().lower()
    return s if s in _SPORTS else DEFAULT_SPORT


def config(sport: str | None) -> Dict:
    return _SPORTS[normalize(sport)]


def model(sport: str | None):
    return _SPORTS[normalize(sport)]["model"]


def all_sports() -> list[Dict]:
    return [{"key": c["key"], "label": c["label"], "icon": c["icon"],
             "has_draw": c["has_draw"], "unit": c["unit"]} for c in _SPORTS.values()]
