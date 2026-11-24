"""Market browser parsing against real payload shapes captured 2026-11-23 (no network)."""
import asyncio
import json
from pathlib import Path

from app.data import polymarket_us
from app.data.kalshi import browse as kalshi_browse
from app.api import routes_markets

FIX = Path(__file__).parent / "fixtures"


def test_kalshi_events_parse_to_bid_ask_with_category_and_link():
    markets = kalshi_browse.parse_events(json.loads((FIX / "kalshi_events.json").read_text()))
    mars = next(m for m in markets if m.id == "KXELONMARS-99")
    assert (mars.yes_bid, mars.yes_ask, mars.last) == (0.10, 0.13, 0.13)
    assert mars.category == "Science and Technology"
    assert mars.venue == "kalshi"
    assert mars.url == "https://kalshi.com/markets/kxelonmars"
    assert mars.mid == 0.115


def test_kalshi_skips_markets_with_no_book():
    payload = {"events": [{"event_ticker": "E", "series_ticker": "S", "title": "t", "category": "c",
                           "markets": [{"ticker": "T1", "title": "x", "yes_bid_dollars": "0.0000",
                                        "yes_ask_dollars": "0.0000", "last_price_dollars": "0.0000"}]}]}
    assert kalshi_browse.parse_events(payload) == []


def test_polymarket_reads_yes_book_and_links_to_event():
    markets = polymarket_us.parse_markets(json.loads((FIX / "polymarket_us_markets.json").read_text()))
    caps = next(m for m in markets if m.id == "7974")
    assert (caps.yes_bid, caps.yes_ask) == (0.243, 0.264)
    assert caps.title == "Major League Soccer Champion — Vancouver Whitecaps FC"
    assert caps.url == "https://polymarket.us/event/tec-mls-winner-2026-11-07-vwh"
    assert caps.venue == "polymarket"


def test_polymarket_skips_a_market_it_cannot_price():
    markets = polymarket_us.parse_markets(json.loads((FIX / "polymarket_us_markets.json").read_text()))
    assert "broken" not in {m.id for m in markets}
    assert polymarket_us.parse_markets({"unexpected": True}) == []


def test_one_venue_down_still_returns_the_other(monkeypatch):
    async def kalshi_ok():
        return kalshi_browse.parse_events(json.loads((FIX / "kalshi_events.json").read_text()))

    async def poly_down():
        raise RuntimeError("gateway timeout")

    monkeypatch.setattr(routes_markets, "_FETCHERS", {"kalshi": kalshi_ok, "polymarket": poly_down})
    routes_markets._CACHE.clear()
    markets, warnings = asyncio.run(routes_markets.fetch_all())
    assert markets and all(m.venue == "kalshi" for m in markets)
    assert warnings == ["Polymarket US unavailable: gateway timeout"]


def test_kalshi_multi_outcome_rows_name_the_outcome():
    payload = {"events": [{"event_ticker": "KXPOPE", "series_ticker": "KXPOPE", "title": "Who will the next Pope be?",
                           "category": "Elections", "markets": [
        {"ticker": "KXPOPE-A", "title": "Who will the next Pope be?", "yes_sub_title": "Pietro Parolin",
         "yes_bid_dollars": "0.0300", "yes_ask_dollars": "0.0500"},
        {"ticker": "KXPOPE-B", "title": "Will Elon visit Mars?", "yes_sub_title": "Mars",
         "yes_bid_dollars": "0.1000", "yes_ask_dollars": "0.1300"}]}]}
    a, b = kalshi_browse.parse_events(payload)
    assert a.title == "Who will the next Pope be? — Pietro Parolin"
    assert b.title == "Will Elon visit Mars?"          # sub-title already in the title: unchanged
