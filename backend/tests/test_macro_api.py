"""Macro refresh job + /api/macro: one cached blob, built on the scheduler; the request path never
touches the network (the 2026-11-23 lesson: page loads must not do upstream or heavy work)."""
import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.data.macro import fred, kalshi_macro, quotes, refresh, sentiment
from app.jobs import scheduler
from app.main import app


def _ind(series, value):
    return fred.Indicator(id=series, label=fred.SERIES[series][0], group=fred.SERIES[series][1], value=value,
                          prev=value, change=0.0, mom=None, yoy=None, as_of="2026-11-23")


@pytest.fixture
def sources(monkeypatch):
    async def fred_fetch(series):
        return _ind(series, 4.0)

    async def yahoo(symbol):
        return quotes.Quote(symbol=symbol, label=symbol, price=100.0, prev_close=99.0, change_pct=1.0,
                            as_of=time.time(), market_open=False)

    async def btc():
        return quotes.Quote(symbol="BTC", label="Bitcoin", price=60000.0, prev_close=None, change_pct=None,
                            as_of=time.time(), market_open=True, source="Coinbase")

    async def fed(current_upper):
        return kalshi_macro.FedOdds(event="KXFED-26DEC", title="t", meeting_date="2026-12-09",
                                    current_upper=current_upper, outcomes=[{"label": "Hold", "prob": 1.0}])

    async def rec():
        return kalshi_macro.Odds(label="Recession this year?", prob=0.025, url="u")

    async def fng():
        return sentiment.Sentiment(name="Crypto Fear & Greed", value=11, label="Extreme Fear", as_of=1)

    monkeypatch.setattr(fred, "fetch", fred_fetch)
    monkeypatch.setattr(quotes, "fetch_yahoo", yahoo)
    monkeypatch.setattr(quotes, "fetch_btc", btc)
    monkeypatch.setattr(kalshi_macro, "fetch_fed", fed)
    monkeypatch.setattr(kalshi_macro, "fetch_recession", rec)
    monkeypatch.setattr(sentiment, "fetch", fng)


def test_refresh_builds_every_section(sources):
    blob = asyncio.run(refresh.build())
    assert blob["ready"] is True and blob["errors"] == {}
    assert {"rates", "fed", "inflation", "jobs_growth", "recession", "markets", "sentiment"} <= set(blob["sections"])
    assert blob["sections"]["fed"]["current_upper"] == 4.0          # fed odds use FRED's upper bound


def test_one_source_down_only_errors_its_own_section(sources, monkeypatch):
    async def down(symbol):
        raise RuntimeError("Yahoo 429")
    monkeypatch.setattr(quotes, "fetch_yahoo", down)
    blob = asyncio.run(refresh.build())
    assert "Yahoo 429" in blob["errors"]["markets"]
    assert blob["sections"]["markets"]["quotes"][0]["symbol"] == "BTC"     # BTC still there
    assert blob["sections"]["rates"]["items"]                              # other sections fine


def test_endpoint_reads_the_file_and_never_fetches(sources, tmp_path, monkeypatch):
    monkeypatch.setattr(refresh, "PATH", tmp_path / "macro.json")
    name = f"mc{int(time.time() * 1000) % 10**9}"
    auth.create_user(name, "macro-pw")
    client = TestClient(app)
    assert client.get("/api/macro").status_code == 401
    client.post("/api/login", json={"username": name, "password": "macro-pw"})
    assert client.get("/api/macro").json() == {"ready": False}            # nothing built yet: fast, not 500

    async def boom(*a, **k):
        raise AssertionError("the request path fetched upstream")
    asyncio.run(refresh.refresh())                                        # the scheduler job writes it
    monkeypatch.setattr(fred, "fetch", boom)
    monkeypatch.setattr(quotes, "fetch_yahoo", boom)
    body = client.get("/api/macro").json()
    auth.disable_user(name)
    assert body["ready"] is True and body["sections"]["sentiment"]["value"] == 11


def test_macro_refresh_is_scheduled_on_its_own_start_slot():
    names = [n for n, _, _ in scheduler.job_specs()]
    assert "macro_refresh" in names
    delays = [scheduler.start_delay(n) for n in names]
    assert len(set(delays)) == len(delays)
