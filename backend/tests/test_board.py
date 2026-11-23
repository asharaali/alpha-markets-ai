"""Board cards: calibrated hit %, after-fee edge from that hit %, and no stake until PROVEN."""
import time

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.main import app
from app.research.board import to_board_card
from app.research.calibration import Calibrator
from app.research.evidence import Evidence
from app.risk import fees
from app.strategies.recommendation import Recommendation


def make_card(**over):
    base = dict(recommendation_id="r1", game_id="2026_11_CIN_WAS", kickoff="2026-11-24T01:15:00+00:00",
                matchup="Bengals at Commanders", ticker="KXNFLGAME-26NOV23CINWAS-CIN", market_type="moneyline",
                selection="Bengals", side="yes", settlement="Pays $1 if the Bengals win.",
                raw_model_prob=0.62, final_prob=0.30, market_prob=0.24, cost=0.25, quote_age_seconds=30,
                depth_usd=4000, ev_gross=0.2, ev_after_fees=0.15, breakeven_prob=0.26, estimated_fee=0.5,
                max_entry_price=0.28, recommended_stake=40.0, contracts=100, sizing_basis="quarter Kelly",
                limits_applied=[], supporting=["QB injury not priced", "line moved 2 pts our way"],
                missing=["No kickoff weather forecast was available."], warnings=[], confidence="medium",
                robust=True, value_rating="good", win_likelihood="unlikely")
    base.update(over)
    return Recommendation(**base)


def ev(state, **over):
    base = dict(family=("ensemble", "moneyline"), state=state, calls=212, clusters=60, model_log_loss=0.6,
                market_log_loss=0.62, difference=-0.02, ci95=(-0.03, -0.01), roi_after_fees=0.04,
                reason="Beats the market on 60 games.")
    base.update(over)
    return Evidence(**base)


def test_watching_family_gets_zero_stake():
    card = to_board_card(make_card(), evidence=ev("WATCHING", reason="Only 14 games settled (needs 40)."),
                         calibrator=None, live_allowed=False)
    assert card["stake"] == 0
    assert card["evidence"]["state"] == "WATCHING"


def test_proven_family_keeps_the_sized_stake():
    card = to_board_card(make_card(), evidence=ev("PROVEN"), calibrator=None, live_allowed=False)
    assert card["stake"] == 40.0


def test_hit_is_calibrated_and_edge_uses_it():
    rows = [{"game_id": f"g{i}", "strategy": "ensemble", "market_type": "moneyline", "selection": "s",
             "created_at": 1.0, "outcome": int(i < 60), "fair_prob": 0.30, "model_prob": 0.30,
             "market_prob": 0.25} for i in range(300)]                       # 30% calls hit 20%
    cal = Calibrator().fit(rows)
    card = to_board_card(make_card(), evidence=ev("PROVEN"), calibrator=cal, live_allowed=False)
    expected_hit = cal.apply(0.30, 0.24)
    assert card["hit"] == pytest.approx(expected_hit, abs=1e-4)
    assert card["raw_hit"] == 0.30
    fee = fees.trading_fee(contracts=100, price=0.25) / 100
    assert card["edge_cents"] == pytest.approx(round((expected_hit - 0.25) * 100 - fee * 100, 1))
    assert card["edge_cents"] < (0.30 - 0.25) * 100


def test_no_calibrator_falls_back_to_market_price():
    card = to_board_card(make_card(), evidence=None, calibrator=None, live_allowed=False)
    assert card["hit"] == pytest.approx(0.24)


def test_unknown_family_is_watching():
    card = to_board_card(make_card(market_type="win_margin"), evidence=None, calibrator=None, live_allowed=False)
    assert card["evidence"]["state"] == "WATCHING"
    assert card["evidence"]["reason"] == "No settled history for this kind of bet yet."
    assert card["stake"] == 0


def test_kalshi_link_from_series_and_trade_mode():
    card = to_board_card(make_card(), evidence=ev("PROVEN"), calibrator=None, live_allowed=True)
    assert card["series"] == "KXNFLGAME"
    assert card["trade"] == {"url": "https://kalshi.com/markets/kxnflgame", "in_app": True}


def test_board_endpoint_is_private():
    assert TestClient(app).get("/api/board").status_code == 401


def test_build_cards_runs_on_an_empty_analysis():
    """Smoke: the extracted helper builds a real card (it lost an import once)."""
    import asyncio
    from types import SimpleNamespace

    from app.api import routes_trading
    from app import engine

    analysis = SimpleNamespace(contexts={}, quote_age_seconds=None, model_version="t")
    orig_best, orig_likely = engine.best_opportunities, engine.likely_candidates
    from tests.conftest import make_signal
    engine.best_opportunities = lambda a, limit=200: [make_signal("KXNFLGAME-26NOV23CINWAS-CIN")]
    engine.likely_candidates = lambda a: []
    try:
        value, likely = asyncio.run(routes_trading.build_cards(analysis, None))
        assert likely == [] and all(isinstance(c, Recommendation) for c in value)
    finally:
        engine.best_opportunities, engine.likely_candidates = orig_best, orig_likely
