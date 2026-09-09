"""Sportsbook consensus from The Odds API.

Why this exists: comparing our model to Kalshi only ever tests whether the model is right.
Comparing KALSHI to eleven sportsbooks tests something much stronger — whether Kalshi's
price is out of line with the rest of the market — and that conclusion does not depend on
our model being any good at all.

Kalshi is an exchange with thin NFL books, especially early in the week. Sportsbooks are
deep, fast, and repriced by people who do this for a living. Where the two disagree, the
sportsbooks are usually closer to right.

The consensus is turned into a DISTRIBUTION rather than a set of prices, so it can be
evaluated at whatever line Kalshi happens to quote. A book offering a team at -3 with a 54%
de-vigged cover probability is stating an expected margin slightly above 3; running that
backwards for every book and averaging gives a market-implied margin and total that can be
compared against any contract on the Kalshi ladder.

One request pulls every game. Three markets is three credits against the quota.
"""
from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.config import settings
from app.core.cache import AsyncTTLCache
from app.core.errors import ConfigError, UpstreamError
from app.core.http import get_json, make_client
from app.core.logging import get_logger
from app.core.types import Game
from app.data import teams
from app.models import distributions as dist

log = get_logger(__name__)

_cache = AsyncTTLCache(ttl=settings.ODDS_CACHE_TTL, stale_ttl=6 * 3600, name="odds-api")

# Quota is per market per call, so ask for exactly the three we can use.
MARKETS = "h2h,spreads,totals"

# Books whose prices we trust enough to include. Everything The Odds API returns for the US
# region is a real book; this list exists so a single outlier cannot be added silently.
# Low-vig books are the most informative but all of them are averaged.
EXCLUDED_BOOKS: set = set()

# Spread of outcomes used to run a book's line backwards into an expected margin/total.
# These are the model's own fitted residual spreads, so both sides of the comparison are
# measured on the same scale.
DEFAULT_MARGIN_SIGMA = 13.1
DEFAULT_TOTAL_SIGMA = 13.2

QUOTA: Dict[str, Optional[int]] = {"remaining": None, "used": None, "last": None}


def configured() -> bool:
    return bool(settings.ODDS_API_KEY)


@dataclass
class BookLine:
    book: str
    line: Optional[float]
    home_prob: float          # de-vigged, from THIS book alone
    away_prob: float
    updated: Optional[str] = None


@dataclass
class GameConsensus:
    """What the sportsbook market collectively believes about one game."""

    game_id: Optional[str]
    home: str
    away: str
    commence: str
    book_count: int = 0
    # Expected home margin and game total implied by the books, in points.
    margin: Optional[float] = None
    total: Optional[float] = None
    # Straight moneyline consensus, de-vigged per book then averaged.
    home_win_prob: Optional[float] = None
    # Median quoted lines, for display.
    spread_line: Optional[float] = None
    total_line: Optional[float] = None
    # Disagreement between books, which is itself information: a market the books cannot
    # agree on is one nobody should be confident about.
    margin_spread_across_books: Optional[float] = None
    books: List[str] = field(default_factory=list)
    fetched_at: float = 0.0

    def margin_distribution(self, profile: Optional[Dict[int, float]] = None):
        if self.margin is None:
            return None
        return dist.margin_distribution(self.margin, DEFAULT_MARGIN_SIGMA, profile)

    def total_distribution(self, profile: Optional[Dict[int, float]] = None):
        if self.total is None:
            return None
        return dist.total_distribution(self.total, DEFAULT_TOTAL_SIGMA, profile)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "game_id": self.game_id, "home": self.home, "away": self.away,
            "book_count": self.book_count, "books": self.books,
            "margin": round(self.margin, 2) if self.margin is not None else None,
            "total": round(self.total, 2) if self.total is not None else None,
            "home_win_prob": round(self.home_win_prob, 4) if self.home_win_prob is not None else None,
            "spread_line": self.spread_line, "total_line": self.total_line,
            "book_disagreement": (round(self.margin_spread_across_books, 2)
                                  if self.margin_spread_across_books is not None else None),
            "fetched_at": self.fetched_at,
        }


def american_to_prob(odds: Optional[float]) -> Optional[float]:
    if odds is None:
        return None
    return 100.0 / (odds + 100.0) if odds > 0 else abs(odds) / (abs(odds) + 100.0)


def devig(a: Optional[float], b: Optional[float]) -> Optional[Tuple[float, float]]:
    """Strip one book's margin from a two-way market.

    Each book is de-vigged on its own before averaging. Averaging raw prices first and
    de-vigging the average would fold eleven different margins into one number and get the
    consensus wrong.
    """
    pa, pb = american_to_prob(a), american_to_prob(b)
    if pa is None or pb is None:
        return None
    total = pa + pb
    if total <= 0:
        return None
    return pa / total, pb / total


def _implied_centre(line: float, prob_over: float, sigma: float) -> Optional[float]:
    """Run a line backwards into the expectation that would produce it.

    P(X > line) = p  =>  mu = line + sigma * z(p)
    """
    p = min(max(prob_over, 1e-4), 1 - 1e-4)
    return line + sigma * dist.normal_ppf(p)


async def _fetch() -> List[Dict[str, Any]]:
    if not configured():
        raise ConfigError("ODDS_API_KEY is not set")
    url = f"{settings.ODDS_API_BASE}/sports/{settings.ODDS_SPORT_KEY}/odds/"
    async with make_client() as client:
        from app.core.http import request
        resp = await request(client, "GET", url, source="odds-api", params={
            "apiKey": settings.ODDS_API_KEY,
            "regions": settings.ODDS_REGIONS,
            "markets": MARKETS,
            "oddsFormat": "american",
        })
    for header, key in (("x-requests-remaining", "remaining"),
                        ("x-requests-used", "used"),
                        ("x-requests-last", "last")):
        value = resp.headers.get(header)
        if value is not None:
            try:
                QUOTA[key] = int(value)
            except ValueError:
                pass
    if resp.status_code >= 400:
        raise UpstreamError(f"Odds API returned {resp.status_code}", detail=resp.text[:200])
    if QUOTA["remaining"] is not None and QUOTA["remaining"] < 500:
        log.warning("Odds API quota is down to %s requests", QUOTA["remaining"])
    return resp.json()


def _consensus_for(event: Dict[str, Any]) -> Optional[GameConsensus]:
    home = teams.resolve(event.get("home_team"))
    away = teams.resolve(event.get("away_team"))
    if not home or not away:
        return None

    margins: List[float] = []
    totals: List[float] = []
    h2h_home: List[float] = []
    spread_lines: List[float] = []
    total_lines: List[float] = []
    books: List[str] = []

    for book in event.get("bookmakers") or []:
        key = book.get("key")
        if not key or key in EXCLUDED_BOOKS:
            continue
        seen = False
        for market in book.get("markets") or []:
            outcomes = {o.get("name"): o for o in market.get("outcomes") or []}
            kind = market.get("key")

            if kind == "h2h":
                pair = devig(_price(outcomes, event.get("home_team")),
                             _price(outcomes, event.get("away_team")))
                if pair:
                    h2h_home.append(pair[0])
                    seen = True

            elif kind == "spreads":
                home_out = outcomes.get(event.get("home_team"))
                away_out = outcomes.get(event.get("away_team"))
                if not home_out or not away_out:
                    continue
                line = home_out.get("point")
                pair = devig(home_out.get("price"), away_out.get("price"))
                if line is None or not pair:
                    continue
                # The book quotes the home team at `line` (negative when favoured); the
                # home side covers when the margin exceeds -line.
                threshold = -float(line)
                centre = _implied_centre(threshold, pair[0], DEFAULT_MARGIN_SIGMA)
                if centre is not None:
                    margins.append(centre)
                    spread_lines.append(threshold)
                    seen = True

            elif kind == "totals":
                over, under = outcomes.get("Over"), outcomes.get("Under")
                if not over or not under:
                    continue
                line = over.get("point")
                pair = devig(over.get("price"), under.get("price"))
                if line is None or not pair:
                    continue
                centre = _implied_centre(float(line), pair[0], DEFAULT_TOTAL_SIGMA)
                if centre is not None:
                    totals.append(centre)
                    total_lines.append(float(line))
                    seen = True
        if seen:
            books.append(key)

    if not books:
        return None

    return GameConsensus(
        game_id=None, home=home, away=away,
        commence=event.get("commence_time", ""),
        book_count=len(books), books=sorted(books),
        # Median, not mean: one book with a stale line should not drag the consensus.
        margin=statistics.median(margins) if margins else None,
        total=statistics.median(totals) if totals else None,
        home_win_prob=statistics.median(h2h_home) if h2h_home else None,
        spread_line=statistics.median(spread_lines) if spread_lines else None,
        total_line=statistics.median(total_lines) if total_lines else None,
        margin_spread_across_books=(max(margins) - min(margins)) if len(margins) > 1 else None,
        fetched_at=time.time(),
    )


def _price(outcomes: Dict[str, Any], name: Optional[str]) -> Optional[float]:
    out = outcomes.get(name) if name else None
    return out.get("price") if out else None


async def consensus(games: Sequence[Game]) -> Dict[str, GameConsensus]:
    """game_id -> sportsbook consensus, for the games we were asked about.

    Never raises: the cross-check is an enhancement, and a research page must not go blank
    because a third-party odds feed is down or out of quota.
    """
    if not configured():
        return {}
    try:
        entry = await _cache.get("nfl", _fetch, empty_is_failure=True)
        events = entry.value
    except Exception as exc:  # noqa: BLE001
        log.warning("sportsbook consensus unavailable: %s", exc)
        return {}

    by_matchup: Dict[Tuple[str, str], GameConsensus] = {}
    for event in events or []:
        row = _consensus_for(event)
        if row:
            by_matchup.setdefault((row.away, row.home), row)

    out: Dict[str, GameConsensus] = {}
    for game in games:
        row = by_matchup.get((game.away, game.home))
        if row:
            row.game_id = game.game_id
            out[game.game_id] = row
    return out


def quota() -> Dict[str, Any]:
    return {
        "configured": configured(),
        "remaining": QUOTA["remaining"],
        "used": QUOTA["used"],
        "last_call_cost": QUOTA["last"],
        "note": ("One refresh costs 3 requests (one per market) and covers every game."
                 if configured() else "No ODDS_API_KEY set."),
    }
