"""Central configuration. Loaded once at startup."""
import os
from dotenv import load_dotenv

load_dotenv()


def _as_bool(value: str, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    ODDS_API_KEY: str = os.getenv("ODDS_API_KEY", "").strip()
    # Force demo mode if no key is present, regardless of the flag.
    DEMO_MODE: bool = _as_bool(os.getenv("DEMO_MODE", "true"), True) or not ODDS_API_KEY
    DEFAULT_BANKROLL: float = float(os.getenv("DEFAULT_BANKROLL", "1000"))
    KELLY_FRACTION: float = float(os.getenv("KELLY_FRACTION", "0.5"))

    # The Odds API sport key for the current World Cup.
    SOCCER_SPORT_KEY: str = os.getenv("SOCCER_SPORT_KEY", "soccer_fifa_world_cup")
    ODDS_API_BASE: str = "https://api.the-odds-api.com/v4"
    # Each region costs 1 quota credit per odds call. Keep it to one region to conserve.
    ODDS_REGIONS: str = os.getenv("ODDS_REGIONS", "us")
    # Odds change slowly pre-match — refetch about once an hour when nothing is live.
    ODDS_CACHE_TTL: int = int(os.getenv("ODDS_CACHE_TTL", "3600"))
    # Live scores refresh fast while a game is in play.
    SCORES_CACHE_TTL: int = int(os.getenv("SCORES_CACHE_TTL", "25"))
    # How often the browser re-polls: fast during a live game, hourly when idle.
    LIVE_POLL_SECONDS: int = int(os.getenv("LIVE_POLL_SECONDS", "30"))
    IDLE_POLL_SECONDS: int = int(os.getenv("IDLE_POLL_SECONDS", "3600"))

    # Phone push notifications via ntfy.sh (install the free 'ntfy' app, subscribe to this topic).
    NTFY_TOPIC: str = os.getenv("NTFY_TOPIC", "alpha-markets-ashar-x7k2").strip()
    NOTIFY_ENABLED: bool = _as_bool(os.getenv("NOTIFY_ENABLED", "true"), True)


settings = Settings()
