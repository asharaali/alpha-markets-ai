"""Shared fixtures. Builds real model objects from synthetic inputs, so the tests exercise
the production code paths rather than mocks of them."""
from __future__ import annotations

import pytest

from app.core.types import (Confidence, Game, MarketQuote, MarketType, Signal, Side)
from app.models import distributions as D
from app.models.game_model import GameProjection


def make_game(game_id="2026_01_AAA_BBB", home="KC", away="BUF", week=1):
    return Game(game_id=game_id, season=2026, week=week, game_type="REG",
                kickoff="2026-09-13T17:00:00+00:00", home=home, away=away,
                home_rest=7, away_rest=7, roof="outdoors")


def make_projection(game, margin=3.0, total=45.0, sigma=13.0, total_sigma=13.0):
    margin_dist = D.margin_distribution(margin, sigma)
    total_dist = D.total_distribution(total, total_sigma)
    home, tie, away = D.win_probability(margin_dist)
    return GameProjection(
        game=game, expected_margin=margin, expected_total=total,
        margin_sigma=sigma, total_sigma=total_sigma,
        margin=margin_dist, total=total_dist,
        home_win=home, tie=tie, away_win=away, confidence=0.8)


def make_quote(ticker, market_type=MarketType.MONEYLINE, bid=0.55, ask=0.57,
               game_id="2026_01_AAA_BBB", team=None, line=None, depth=5000.0):
    return MarketQuote(venue="kalshi", ticker=ticker, event_ticker="E",
                       market_type=market_type, label=ticker, side=Side.YES,
                       yes_bid=bid, yes_ask=ask, depth_usd=depth, line=line,
                       team=team, game_id=game_id)


def make_signal(ticker, *, game_id="2026_01_AAA_BBB", market_type=MarketType.MONEYLINE,
                team=None, line=None, model_prob=0.60, market_prob=0.56, cost=0.56,
                confidence=Confidence.HIGH, label=None, value=True, depth=5000.0):
    quote = make_quote(ticker, market_type=market_type, bid=cost - 0.01, ask=cost,
                       game_id=game_id, team=team, line=line, depth=depth)
    signal = Signal(strategy="ensemble", game_id=game_id, market_type=market_type,
                    label=label or ticker, selection=label or ticker,
                    model_prob=model_prob, confidence=confidence, line=line, team=team)
    signal.quote = quote
    signal.market_prob = market_prob
    signal.edge = model_prob - market_prob
    signal.ev_per_dollar = (model_prob / cost) - 1.0
    signal.features.update({"fair_prob": model_prob, "cost": cost, "liquid": True,
                            "value": value, "depth_usd": depth})
    return signal


@pytest.fixture
def game():
    return make_game()


@pytest.fixture
def projection(game):
    return make_projection(game)
