"""Decision journal: what was traded from the board, the card as it looked then, and how it did."""
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.main import app
from app.research import journal
from app.tracking import store


def _user():
    return f"jr{uuid.uuid4().hex[:10]}"


def _card(ticker, state="WATCHING", selection="Bengals"):
    return {"id": "c1", "ticker": ticker, "selection": selection, "side": "yes", "venue": "kalshi",
            "hit": 0.31, "price": 0.25, "edge_cents": 4.2, "reasons": ["QB injury not priced"],
            "evidence": {"state": state, "calls": 14, "clusters": 14, "reason": "Only 14 games"}}


def _settle(ticker, selection, outcome):
    store.init()
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO predictions (id, created_at, season, week, game_id, strategy, market_type, ticker,"
            " selection, model_prob, status, settled_at, outcome) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, time.time(), 2026, 11, "2026_11_X_Y", "ensemble", "moneyline", ticker,
             selection, 0.3, "settled", time.time(), outcome))


def test_round_trip_keeps_the_card_snapshot():
    user, ticker = _user(), f"KX-{uuid.uuid4().hex[:6]}"
    jid = journal.record(user, _card(ticker), price=0.25, contracts=10, note="liked the injury angle")
    [entry] = journal.entries(user)
    assert entry["id"] == jid
    assert entry["card"]["reasons"] == ["QB injury not priced"]
    assert entry["note"] == "liked the injury angle"
    assert entry["outcome"] == "open" and entry["pnl"] is None


def test_outcome_and_pnl_resolve_from_the_settled_prediction():
    user, ticker = _user(), f"KX-{uuid.uuid4().hex[:6]}"
    journal.record(user, _card(ticker), price=0.25, contracts=10)
    _settle(ticker, "Bengals", 1)
    [entry] = journal.entries(user)
    assert entry["outcome"] == "won"
    assert entry["pnl"] == pytest.approx(10 * (1 - 0.25))


def test_a_no_side_trade_wins_when_yes_loses():
    user, ticker = _user(), f"KX-{uuid.uuid4().hex[:6]}"
    journal.record(user, {**_card(ticker), "side": "no"}, price=0.70, contracts=5)
    _settle(ticker, "Bengals", 0)
    [entry] = journal.entries(user)
    assert entry["outcome"] == "won" and entry["pnl"] == pytest.approx(5 * 0.30)


def test_summary_groups_by_evidence_state_at_trade_time():
    user = _user()
    t1, t2 = f"KX-{uuid.uuid4().hex[:6]}", f"KX-{uuid.uuid4().hex[:6]}"
    journal.record(user, _card(t1, "WATCHING"), price=0.25, contracts=10)
    journal.record(user, _card(t2, "PROVEN"), price=0.40, contracts=10)
    _settle(t1, "Bengals", 0)
    summary = journal.summary(journal.entries(user))
    assert summary["WATCHING"] == {"trades": 1, "won": 0, "lost": 1, "open": 0, "pnl": pytest.approx(-2.5)}
    assert summary["PROVEN"]["open"] == 1


def test_other_users_entries_are_not_returned():
    a, b = _user(), _user()
    journal.record(a, _card(f"KX-{uuid.uuid4().hex[:6]}"), price=0.25, contracts=1)
    assert journal.entries(b) == []


def test_journal_endpoints_require_login_and_round_trip():
    name = _user()
    auth.create_user(name, "journal-password")
    client = TestClient(app)
    assert client.get("/api/journal").status_code == 401
    client.post("/api/login", json={"username": name, "password": "journal-password"})
    r = client.post("/api/journal", json={"card": _card("KX-ENDPT"), "price": 0.25, "contracts": 3, "note": ""})
    assert r.status_code == 200 and r.json()["id"]
    body = client.get("/api/journal").json()
    assert len(body["entries"]) == 1 and "WATCHING" in body["summary"]
    auth.disable_user(name)
