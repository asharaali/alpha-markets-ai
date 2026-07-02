"""Central configuration. Loaded once at startup."""
import os
from pathlib import Path
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
    # The Odds API sport key for MLB regular season.
    MLB_SPORT_KEY: str = os.getenv("MLB_SPORT_KEY", "baseball_mlb")
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

    # ---- Auto-bet (autonomous order placement) ----
    # Conservative hard ceilings the user CANNOT exceed even if they crank their own limits.
    AUTOBET_HARD_MAX_STAKE: float = float(os.getenv("AUTOBET_HARD_MAX_STAKE", "10"))
    AUTOBET_HARD_DAILY_CAP: float = float(os.getenv("AUTOBET_HARD_DAILY_CAP", "50"))
    # Live trading is triple-gated: this flag, a Kalshi key, AND the user must be the
    # single account the Kalshi key belongs to. Everyone else is paper-only — nobody can
    # ever place real orders on your Kalshi account but you.
    AUTOBET_LIVE_ALLOWED: bool = _as_bool(os.getenv("AUTOBET_LIVE_ALLOWED", "false"), False)
    # Must equal the username you LOG IN with (lowercase). On Render set this env var to
    # your actual login name, or live trading silently falls back to paper for you.
    AUTOBET_LIVE_USER: str = os.getenv("AUTOBET_LIVE_USER", "asharaali").strip().lower()
    KALSHI_KEY_ID: str = os.getenv("KALSHI_KEY_ID", "").strip()
    KALSHI_PRIVATE_KEY: str = os.getenv("KALSHI_PRIVATE_KEY", "").strip()

    # Where per-user accounts + bet logs live. Auto-uses the Render persistent disk
    # (/var/data) when present, else a local folder — no env var needed.
    DATA_DIR: str = os.getenv("DATA_DIR") or (
        "/var/data" if Path("/var/data").is_dir() else str(Path(__file__).parent.parent / "data"))
    # Secret for signing login cookies. Use env if given, else generate+persist one on disk
    # (stable across restarts, never the insecure default).
    SITE_SECRET: str = ""  # set just below


def _resolve_secret() -> str:
    env = os.getenv("SITE_SECRET", "").strip()
    if env:
        return env
    import secrets
    p = Path(Settings.DATA_DIR) / ".secret"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists():
            return p.read_text().strip()
        s = secrets.token_hex(32)
        p.write_text(s)
        return s
    except Exception:
        return secrets.token_hex(32)


Settings.SITE_SECRET = _resolve_secret()


settings = Settings()
