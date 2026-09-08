"""Kalshi NFL market discovery, game mapping, and live pricing.

Three jobs, in order:

1. DISCOVER — pull open events for the series we cover and parse each contract into a
   structured (market type, team/player, line).
2. MAP — attach every event to a real nflverse game_id. Kalshi's event sub_title is
   consistently "AWAY vs HOME (Mon D)", which is a far safer key than the packed team codes
   in the ticker (LACHI is genuinely ambiguous between LAC+HI and LA+CHI).
3. PRICE — read each contract's order book, because Kalshi's market list reports every
   price as null.

Anything that fails to map or parse is counted and reported, never silently dropped: an
unmapped market means the model has an opinion nobody is pricing, and you want to know.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from app.config import settings
from app.core.cache import AsyncTTLCache
from app.core.http import gather_limited, make_client
from app.core.logging import get_logger
from app.core.types import Game, MarketQuote, MarketType, Side
from app.data import teams
from app.data.kalshi import client as kc
from app.data.kalshi import orderbook as ob
from app.data.kalshi import series as series_registry

log = get_logger(__name__)

_SUBTITLE_RE = re.compile(r"^\s*(?P<away>[A-Za-z .'-]+?)\s+vs\s+(?P<home>[A-Za-z .'-]+?)\s*"
                          r"(?:\((?P<date>[A-Za-z]{3}\s+\d{1,2})\))?\s*$")

_events_cache = AsyncTTLCache(ttl=settings.KALSHI_MARKET_TTL, stale_ttl=900,
                              name="kalshi-events")
_book_cache = AsyncTTLCache(ttl=settings.KALSHI_MARKET_TTL, stale_ttl=600,
                            name="kalshi-books")


@dataclass
class DiscoveredMarket:
    """A parsed Kalshi contract, before pricing."""

    series: str
    event_ticker: str
    ticker: str
    game_id: Optional[str]
    away: Optional[str]
    home: Optional[str]
    market_type: MarketType
    category: str
    label: str
    selection: str
    team: Optional[str] = None
    player: Optional[str] = None
    line: Optional[float] = None
    close_time: Optional[str] = None
    reference_only: bool = False


@dataclass
class BoardStats:
    """What the discovery pass actually saw. Surfaced in the UI so an empty board is
    explained rather than mysterious."""

    events_seen: int = 0
    events_unmapped: int = 0        # parsed fine, but the matchup is not on the schedule
    events_out_of_slate: int = 0    # a real game, just not one we asked about
    events_unparsed: int = 0        # we could not read the matchup at all
    contracts_seen: int = 0
    contracts_unparsed: int = 0
    contracts_priced: int = 0
    series_queried: List[str] = field(default_factory=list)
    stale: bool = False
    fetched_at: float = 0.0
    unmapped_examples: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return {
            "events_seen": self.events_seen,
            "events_unmapped": self.events_unmapped,
            "events_out_of_slate": self.events_out_of_slate,
            "events_unparsed": self.events_unparsed,
            "contracts_seen": self.contracts_seen,
            "contracts_unparsed": self.contracts_unparsed,
            "contracts_priced": self.contracts_priced,
            "series_queried": self.series_queried,
            "stale": self.stale,
            "fetched_at": self.fetched_at,
            "unmapped_examples": self.unmapped_examples[:5],
        }


def _parse_matchup(event: Dict) -> Optional[Tuple[str, str]]:
    """(away, home) from an event's sub_title, falling back to its title."""
    for text in (event.get("sub_title") or "", event.get("title") or ""):
        head = text.split(":", 1)[0]
        m = _SUBTITLE_RE.match(head)
        if not m:
            continue
        away = teams.resolve(m.group("away"))
        home = teams.resolve(m.group("home"))
        if away and home and away != home:
            return away, home
    return None


def build_game_index(games: Sequence[Game]) -> Dict[Tuple[str, str], List[Game]]:
    """(away, home) -> the scheduled games for that matchup, soonest first."""
    index: Dict[Tuple[str, str], List[Game]] = {}
    for g in games:
        index.setdefault((g.away, g.home), []).append(g)
    for bucket in index.values():
        bucket.sort(key=lambda g: g.kickoff)
    return index


def _match_game(away: str, home: str, close_time: Optional[str],
                index: Dict[Tuple[str, str], List[Game]]) -> Optional[Game]:
    """Pick which scheduled meeting of these two teams a market refers to.

    Two teams can meet twice a season, so the market's close time (kickoff, near enough)
    breaks the tie — we take the scheduled game whose kickoff is nearest to it.
    """
    candidates = index.get((away, home))
    if not candidates:
        return None
    if len(candidates) == 1 or not close_time:
        return candidates[0]
    try:
        close = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
    except ValueError:
        return candidates[0]

    def distance(g: Game) -> float:
        try:
            return abs((datetime.fromisoformat(g.kickoff) - close).total_seconds())
        except ValueError:
            return float("inf")

    return min(candidates, key=distance)


async def _fetch_series_events(series_ticker: str) -> List[Dict]:
    async def loader() -> List[Dict]:
        async with make_client() as client:
            return await kc.events(client, series_ticker)

    entry = await _events_cache.get(series_ticker, loader, empty_is_failure=False)
    return entry.value


async def discover(series_tickers: Sequence[str], games: Sequence[Game]
                   ) -> Tuple[List[DiscoveredMarket], BoardStats]:
    """Parse every open contract in the requested series and map it to a scheduled game."""
    index = build_game_index(games)
    stats = BoardStats(series_queried=list(series_tickers), fetched_at=time.time())
    out: List[DiscoveredMarket] = []

    batches = await gather_limited(
        [_fetch_series_events(s) for s in series_tickers], limit=4)

    for series_ticker, events in zip(series_tickers, batches):
        spec = series_registry.spec(series_ticker)
        if spec is None:
            continue
        for event in events:
            stats.events_seen += 1
            matchup = _parse_matchup(event)
            if not matchup:
                stats.events_unparsed += 1
                if len(stats.unmapped_examples) < 5:
                    stats.unmapped_examples.append(
                        f"{event.get('event_ticker')}: could not read matchup from "
                        f"{event.get('sub_title')!r}")
                continue
            away, home = matchup
            markets = event.get("markets") or []
            close_time = markets[0].get("close_time") if markets else None
            game = _match_game(away, home, close_time, index)
            if game is None:
                # Kalshi lists weeks ahead of the slate we were asked about. A matchup we
                # can read but did not ask for is out of scope, not a mapping failure —
                # conflating the two makes a healthy board look broken.
                if teams.get(away) and teams.get(home):
                    stats.events_out_of_slate += 1
                else:
                    stats.events_unmapped += 1
                    if len(stats.unmapped_examples) < 5:
                        stats.unmapped_examples.append(
                            f"{event.get('event_ticker')}: {away}@{home} is not a "
                            "recognised matchup")
                continue

            for market in markets:
                stats.contracts_seen += 1
                sub = (market.get("yes_sub_title") or market.get("subtitle") or "").strip()
                ticker = market.get("ticker")
                if not sub or not ticker:
                    stats.contracts_unparsed += 1
                    continue
                parsed = spec.parse(sub, away, home)
                if parsed is None:
                    stats.contracts_unparsed += 1
                    continue
                out.append(DiscoveredMarket(
                    series=series_ticker,
                    event_ticker=event.get("event_ticker", ""),
                    ticker=ticker,
                    game_id=game.game_id, away=away, home=home,
                    market_type=parsed.market_type,
                    category=spec.category,
                    label=parsed.label,
                    selection=parsed.selection,
                    team=parsed.team, player=parsed.player, line=parsed.line,
                    close_time=market.get("close_time"),
                    reference_only=spec.reference_only,
                ))
    return out, stats


async def price(markets: Sequence[DiscoveredMarket], *,
                max_markets: Optional[int] = None) -> Tuple[List[MarketQuote], BoardStats]:
    """Read live order books for the given contracts and build priced quotes.

    Order-book reads are the expensive part of a slate (one HTTP call per contract), so
    they are cached per ticker and issued with a concurrency ceiling. A book we could not
    read yields no quote at all rather than a guessed price.
    """
    stats = BoardStats(fetched_at=time.time())
    selected = list(markets)[:max_markets] if max_markets else list(markets)
    if not selected:
        return [], stats

    async with make_client() as client:
        async def read(ticker: str):
            async def loader():
                return await ob.fetch(client, ticker)
            entry = await _book_cache.get(ticker, loader, empty_is_failure=False)
            return entry.value

        books = await gather_limited([read(m.ticker) for m in selected],
                                     limit=settings.KALSHI_BOOK_CONCURRENCY)

    quotes: List[MarketQuote] = []
    now = time.time()
    for market, (yes_bid, yes_ask, depth) in zip(selected, books):
        stats.contracts_seen += 1
        if yes_bid is None and yes_ask is None:
            continue
        stats.contracts_priced += 1
        quotes.append(MarketQuote(
            venue="kalshi",
            ticker=market.ticker,
            event_ticker=market.event_ticker,
            market_type=market.market_type,
            label=market.label,
            side=Side.YES,
            yes_bid=yes_bid, yes_ask=yes_ask, depth_usd=depth,
            line=market.line, team=market.team, player=market.player,
            game_id=market.game_id, close_time=market.close_time,
            captured_at=now,
        ))
    return quotes, stats


async def board(games: Sequence[Game], *,
                series_tickers: Optional[Sequence[str]] = None,
                max_markets: Optional[int] = None
                ) -> Tuple[List[DiscoveredMarket], List[MarketQuote], BoardStats]:
    """Discovery + pricing in one call. The entry point every strategy uses."""
    tickers = list(series_tickers or series_registry.GAME_LINE_SERIES)
    discovered, stats = await discover(tickers, games)
    quotes, price_stats = await price(discovered, max_markets=max_markets)
    stats.contracts_priced = price_stats.contracts_priced
    return discovered, quotes, stats


def index_quotes(quotes: Iterable[MarketQuote]) -> Dict[str, MarketQuote]:
    return {q.ticker: q for q in quotes}


def complements(quotes: Sequence[MarketQuote]) -> Dict[str, List[MarketQuote]]:
    """Group quotes that form a mutually-exclusive, collectively-exhaustive set.

    Vig removal only makes sense across a complete set of outcomes: the two sides of a
    moneyline, or the whole winning-margin ladder. Grouping by (game, market type, line)
    gives us those sets, and anything that does not form one is priced on its own mid —
    correctly, since a single Kalshi contract's YES and NO already bracket the fair price.
    """
    groups: Dict[str, List[MarketQuote]] = {}
    for q in quotes:
        if q.market_type is MarketType.MONEYLINE:
            key = f"{q.game_id}:moneyline"
        elif q.market_type is MarketType.WIN_MARGIN:
            key = f"{q.game_id}:win_margin"
        else:
            key = f"{q.game_id}:{q.market_type.value}:{q.line}:{q.team or ''}:{q.player or ''}"
        groups.setdefault(key, []).append(q)
    return groups


# How many rungs of a ladder are worth pricing per game. Kalshi lists spreads and totals
# in long ladders that stretch far past any plausible outcome; reading every book on a
# 16-game slate is roughly 800 HTTP calls, which is both slow and enough to trip the venue's
# rate limiter. Pricing the rungs nearest the projection covers everything we could
# realistically bet and cuts the work by two thirds.
LADDER_WINDOW = 7


def select_for_pricing(discovered: Sequence[DiscoveredMarket],
                       projections: Dict[str, Dict[str, float]],
                       *, window: int = LADDER_WINDOW,
                       price_all: bool = False) -> List[DiscoveredMarket]:
    """Choose which contracts to read order books for.

    Moneylines, winning-margin bands and player props are always priced — they are few, and
    a prop the user asked to see must actually have a price. Spread, total and team-total
    ladders are trimmed to the rungs nearest the model's projection for that game.

    `projections` maps game_id -> {"margin": expected home margin, "total": expected total,
    "home_score": .., "away_score": ..}.
    """
    if price_all:
        return list(discovered)

    always: List[DiscoveredMarket] = []
    ladders: Dict[Tuple[str, str, str], List[DiscoveredMarket]] = {}

    for market in discovered:
        if market.market_type in (MarketType.MONEYLINE, MarketType.WIN_MARGIN):
            always.append(market)
            continue
        if market.category == "Player Prop":
            always.append(market)
            continue
        if market.line is None or market.game_id is None:
            always.append(market)
            continue
        key = (market.game_id, market.market_type.value, market.team or "")
        ladders.setdefault(key, []).append(market)

    picked: List[DiscoveredMarket] = list(always)
    for (game_id, market_type, team), rungs in ladders.items():
        projection = projections.get(game_id)
        if projection is None:
            picked.extend(sorted(rungs, key=lambda m: m.line or 0)[:window])
            continue
        centre = _ladder_centre(market_type, team, projection)
        rungs.sort(key=lambda m: abs((m.line or 0) - centre))
        picked.extend(rungs[:window])
    return picked


def _ladder_centre(market_type: str, team: str, projection: Dict[str, float]) -> float:
    """The line this ladder's outcome is expected to land nearest."""
    if market_type == MarketType.TOTAL.value:
        return projection.get("total", 44.0)
    if market_type == MarketType.TEAM_TOTAL.value:
        if team and team == projection.get("home_team"):
            return projection.get("home_score", 22.0)
        return projection.get("away_score", 22.0)
    if market_type == MarketType.FIRST_HALF_TOTAL.value:
        return projection.get("total", 44.0) * 0.47
    if market_type == MarketType.SPREAD.value:
        # Spread contracts are "team wins by over N", so the relevant centre is how much
        # THAT team is expected to win by (negative if they are the underdog).
        margin = projection.get("margin", 0.0)
        if team and team == projection.get("home_team"):
            return max(margin, 0.0)
        return max(-margin, 0.0)
    return 0.0
