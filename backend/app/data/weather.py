"""Game-time weather from Open-Meteo (free, no API key, no account).

Only outdoor games get a forecast — asking for the wind speed at a domed stadium is a
category error, and quietly applying it would put a phantom penalty on every indoor total.

Forecasts are only meaningful inside the model horizon (about 16 days), so a game further
out reports no forecast rather than a fabricated one.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

from app.config import settings
from app.core.cache import AsyncTTLCache
from app.core.http import get_json, make_client
from app.core.logging import get_logger
from app.core.types import Game
from app.data import teams

log = get_logger(__name__)

_cache = AsyncTTLCache(ttl=settings.WEATHER_CACHE_TTL, stale_ttl=6 * 3600, name="weather")

FORECAST_HORIZON_DAYS = 16


def is_indoor(game: Game) -> bool:
    roof = (game.roof or "").lower()
    if roof in {"dome", "closed"}:
        return True
    if roof in {"outdoors", "open"}:
        return False
    home = teams.get(game.home)
    return bool(home and home.roof == "dome")


async def _forecast(lat: float, lon: float) -> Dict[str, List]:
    async with make_client() as client:
        return await get_json(
            client, settings.WEATHER_BASE, source="open-meteo",
            params={
                "latitude": round(lat, 3), "longitude": round(lon, 3),
                "hourly": "temperature_2m,wind_speed_10m,precipitation_probability,"
                          "snowfall,relative_humidity_2m",
                "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
                "precipitation_unit": "inch", "timezone": "UTC",
                "forecast_days": FORECAST_HORIZON_DAYS,
            })


async def for_game(game: Game) -> Optional[Dict[str, object]]:
    """Kickoff-hour conditions for one game, or None when it does not apply."""
    if is_indoor(game):
        return {"indoor": True, "source": "stadium roof",
                "note": "Indoor game — weather is not a factor."}
    home = teams.get(game.home)
    if not home or not game.kickoff:
        return None
    try:
        kickoff = datetime.fromisoformat(game.kickoff)
    except ValueError:
        return None
    days_out = (kickoff - datetime.now(timezone.utc)).total_seconds() / 86400.0
    if days_out > FORECAST_HORIZON_DAYS or days_out < -1:
        return None

    key = f"{round(home.lat, 2)},{round(home.lon, 2)}"
    try:
        entry = await _cache.get(key, lambda: _forecast(home.lat, home.lon))
    except Exception as exc:  # noqa: BLE001 - weather is an enhancement, never a blocker
        log.warning("weather lookup failed for %s: %s", game.game_id, exc)
        return None

    hourly = (entry.value or {}).get("hourly") or {}
    times = hourly.get("time") or []
    if not times:
        return None
    target = kickoff.strftime("%Y-%m-%dT%H:00")
    try:
        i = times.index(target)
    except ValueError:
        # Nearest available hour, so a kickoff on a half hour still resolves.
        i = min(range(len(times)),
                key=lambda j: abs(_parse(times[j]) - kickoff.timestamp()))

    def at(name: str) -> Optional[float]:
        values = hourly.get(name) or []
        return values[i] if i < len(values) else None

    return {
        "indoor": False,
        "temperature_f": at("temperature_2m"),
        "wind_mph": at("wind_speed_10m"),
        "precipitation_pct": at("precipitation_probability"),
        "snowfall_in": at("snowfall"),
        "humidity_pct": at("relative_humidity_2m"),
        "forecast_hour": times[i] if i < len(times) else None,
        "stale": entry.stale,
        "source": "open-meteo",
        "fetched_at": time.time(),
    }


def _parse(iso: str) -> float:
    try:
        return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return 0.0


async def for_games(games: Sequence[Game]) -> Dict[str, Optional[Dict[str, object]]]:
    out: Dict[str, Optional[Dict[str, object]]] = {}
    for game in games:
        out[game.game_id] = await for_game(game)
    return out
