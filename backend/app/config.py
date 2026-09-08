"""Central configuration, loaded once at startup from the environment.

Nothing secret is ever defaulted to a real value. If a credential is missing the feature
that needs it reports itself as unconfigured (and the UI says so) rather than silently
falling back to fake data.
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

_BACKEND_DIR = Path(__file__).resolve().parent.parent


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


class Settings:
    # ---------------- app ----------------
    APP_NAME = "Alpha Markets"
    APP_TAGLINE = "NFL quantitative market research"
    VERSION = "2.0.0"
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    # Where the SQLite database, cached feeds, and user accounts live. Uses the Render
    # persistent disk automatically when it exists.
    DATA_DIR: str = os.getenv("DATA_DIR") or (
        "/var/data" if Path("/var/data").is_dir() else str(_BACKEND_DIR / "data")
    )
    DB_PATH: str = ""            # resolved below
    CACHE_DIR: str = ""          # resolved below

    # ---------------- season ----------------
    # The season the app treats as current. Auto-derived (NFL seasons are labelled by the
    # calendar year they START in, and roll over in March) unless pinned by env.
    SEASON: int = 0              # resolved below
    # How many prior seasons of play-by-play feed the ratings prior.
    HISTORY_SEASONS: int = _int("HISTORY_SEASONS", 3)
    # How many seasons back to probe when fitting the game model. More history means
    # a steadier fit but a slower first boot (each season is a ~19MB download).
    CALIBRATION_SEASONS: int = _int("CALIBRATION_SEASONS", 6)

    # ---------------- nflverse (free, no key) ----------------
    NFLVERSE_BASE: str = os.getenv(
        "NFLVERSE_BASE", "https://github.com/nflverse/nflverse-data/releases/download")
    NFLVERSE_CACHE_TTL: int = _int("NFLVERSE_CACHE_TTL", 6 * 3600)
    SCHEDULE_CACHE_TTL: int = _int("SCHEDULE_CACHE_TTL", 3600)
    INJURY_CACHE_TTL: int = _int("INJURY_CACHE_TTL", 1800)

    # ---------------- Kalshi ----------------
    KALSHI_BASE: str = os.getenv("KALSHI_BASE", "https://api.elections.kalshi.com/trade-api/v2")
    KALSHI_ORDER_BASE: str = os.getenv(
        "KALSHI_ORDER_BASE", "https://external-api.kalshi.com/trade-api/v2").rstrip("/")
    KALSHI_KEY_ID: str = os.getenv("KALSHI_KEY_ID", "").strip()
    KALSHI_PRIVATE_KEY: str = os.getenv("KALSHI_PRIVATE_KEY", "").strip()
    KALSHI_MARKET_TTL: int = _int("KALSHI_MARKET_TTL", 90)
    KALSHI_BOOK_CONCURRENCY: int = _int("KALSHI_BOOK_CONCURRENCY", 8)
    # A price level needs this many dollars resting to count as tradeable. Thin Kalshi books
    # are littered with $1 stale orders you cannot actually trade against.
    KALSHI_MIN_DEPTH: float = _float("KALSHI_MIN_DEPTH", 20.0)

    # ---------------- execution ----------------
    # Paper trading is the default and the only mode reachable without all three gates below.
    EXECUTION_MODE: str = os.getenv("EXECUTION_MODE", "paper").strip().lower()
    LIVE_TRADING_ENABLED: bool = _bool("LIVE_TRADING_ENABLED", False)
    # Live orders require this username to match the logged-in user AND a Kalshi key.
    LIVE_TRADING_USER: str = os.getenv("LIVE_TRADING_USER", "").strip().lower()
    # Hard ceilings a user cannot raise from the UI.
    HARD_MAX_STAKE: float = _float("HARD_MAX_STAKE", 25.0)
    HARD_DAILY_CAP: float = _float("HARD_DAILY_CAP", 100.0)

    # ---------------- sportsbook consensus (optional) ----------------
    ODDS_API_KEY: str = os.getenv("ODDS_API_KEY", "").strip()
    ODDS_API_BASE: str = "https://api.the-odds-api.com/v4"
    ODDS_SPORT_KEY: str = os.getenv("ODDS_SPORT_KEY", "americanfootball_nfl")
    ODDS_REGIONS: str = os.getenv("ODDS_REGIONS", "us")
    ODDS_CACHE_TTL: int = _int("ODDS_CACHE_TTL", 900)

    # ---------------- weather (free, no key) ----------------
    WEATHER_BASE: str = os.getenv("WEATHER_BASE", "https://api.open-meteo.com/v1/forecast")
    WEATHER_CACHE_TTL: int = _int("WEATHER_CACHE_TTL", 3600)

    # ---------------- risk defaults ----------------
    DEFAULT_BANKROLL: float = _float("DEFAULT_BANKROLL", 1000.0)
    KELLY_FRACTION: float = _float("KELLY_FRACTION", 0.25)
    MAX_STAKE_PCT: float = _float("MAX_STAKE_PCT", 0.05)
    MAX_EXPOSURE_PER_GAME_PCT: float = _float("MAX_EXPOSURE_PER_GAME_PCT", 0.10)
    MAX_EXPOSURE_PER_TEAM_PCT: float = _float("MAX_EXPOSURE_PER_TEAM_PCT", 0.15)
    MAX_DAILY_EXPOSURE_PCT: float = _float("MAX_DAILY_EXPOSURE_PCT", 0.20)

    # ---------------- edge thresholds ----------------
    # Absolute probability disagreement below this is noise on a market that ticks in
    # whole cents, whatever the price.
    MIN_EDGE: float = _float("MIN_EDGE", 0.015)
    # The threshold that actually decides a bet: expected return per dollar staked. A flat
    # probability threshold is the wrong gate, because 3 points of edge on a 10c contract
    # is a 30% return while the same 3 points on an 85c contract is under 4% — filtering on
    # probability alone systematically discards the best-paying bets and waves through the
    # worst-paying ones.
    MIN_EV: float = _float("MIN_EV", 0.04)
    # How far outside this implied-probability band we refuse to call something value:
    # longshots and near-locks are where model error is largest and payout is worst.
    LONGSHOT_FLOOR: float = _float("LONGSHOT_FLOOR", 0.08)
    HEAVY_FAV_CAP: float = _float("HEAVY_FAV_CAP", 0.92)
    # A wider bid/ask than this means the "price" is a guess; we won't call an edge off it.
    MAX_QUOTE_SPREAD: float = _float("MAX_QUOTE_SPREAD", 0.12)

    # ---------------- background jobs ----------------
    JOBS_ENABLED: bool = _bool("JOBS_ENABLED", True)
    MARKET_SNAPSHOT_INTERVAL: int = _int("MARKET_SNAPSHOT_INTERVAL", 600)
    GRADING_INTERVAL: int = _int("GRADING_INTERVAL", 1800)

    # ---------------- auth ----------------
    SITE_SECRET: str = ""        # resolved below
    SESSION_DAYS: int = _int("SESSION_DAYS", 60)


def _current_season() -> int:
    pinned = os.getenv("SEASON", "").strip()
    if pinned.isdigit():
        return int(pinned)
    from datetime import date
    today = date.today()
    # A season labelled 2026 runs Sep 2026 -> Feb 2027. Before March, we're still in the
    # previous label's season.
    return today.year if today.month >= 3 else today.year - 1


def _resolve_secret(data_dir: str) -> str:
    env = os.getenv("SITE_SECRET", "").strip()
    if env:
        return env
    path = Path(data_dir) / ".secret"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            existing = path.read_text().strip()
            if existing:
                return existing
        generated = secrets.token_hex(32)
        path.write_text(generated)
        return generated
    except OSError:
        # Read-only disk: still never fall back to a guessable constant.
        return secrets.token_hex(32)


Settings.SEASON = _current_season()
Settings.DB_PATH = os.getenv("DB_PATH") or str(Path(Settings.DATA_DIR) / "alpha.db")
Settings.CACHE_DIR = os.getenv("CACHE_DIR") or str(Path(Settings.DATA_DIR) / "feed_cache")
Settings.SITE_SECRET = _resolve_secret(Settings.DATA_DIR)

Path(Settings.DATA_DIR).mkdir(parents=True, exist_ok=True)
Path(Settings.CACHE_DIR).mkdir(parents=True, exist_ok=True)

settings = Settings()


def live_trading_available(username: str | None) -> tuple[bool, str]:
    """Triple gate on real-money orders: flag + credentials + the one authorised user.

    Returns (allowed, reason). Everything not explicitly allowed is paper.
    """
    if not settings.LIVE_TRADING_ENABLED:
        return False, "live trading is disabled (LIVE_TRADING_ENABLED=false)"
    if not (settings.KALSHI_KEY_ID and settings.KALSHI_PRIVATE_KEY):
        return False, "no Kalshi API credentials configured"
    if not settings.LIVE_TRADING_USER:
        return False, "LIVE_TRADING_USER is not set"
    if (username or "").strip().lower() != settings.LIVE_TRADING_USER:
        return False, "this account is not the authorised live-trading user"
    return True, "live trading enabled"
