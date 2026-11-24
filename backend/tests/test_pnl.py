"""Module 3 P&L: the Mac pushes a snapshot with a bearer token; the terminal reads it privately.

Review focus: the push route is the only /api route reachable without a session, so it must
refuse everything that is not exactly right, and must not exist at all until a token is set.
"""
import json
import time

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.api.privacy import MACHINE_API_PATHS, PUBLIC_API_PATHS
from app.main import app
from app.pnl import store

TOKEN = "t" * 40


def _snap(**over):
    venue = {"venue": "public", "ok": True, "as_of": time.time(), "equity": 1000.0, "cash": 250.0,
             "day_pnl": -7.5, "unrealized_pnl": -40.0,
             "positions": [{"symbol": "ACME", "name": "Acme Corp", "kind": "EQUITY", "qty": 3,
                            "value": 300.0, "cost": 330.0, "pnl": -30.0, "day_pnl": -6.0}]}
    venue.update(over)
    return {"schema": 1, "venues": [venue]}


@pytest.fixture(autouse=True)
def tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "LATEST", tmp_path / "latest.json")
    monkeypatch.setattr(store, "HISTORY", tmp_path / "history.jsonl")


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setenv("PNL_PUSH_TOKEN", TOKEN)


def _push(body, tok=TOKEN):
    headers = {"Authorization": f"Bearer {tok}"} if tok is not None else {}
    data = body if isinstance(body, (bytes, str)) else json.dumps(body)
    return TestClient(app).post("/api/pnl/push", content=data, headers=headers)


# ---- the push route's locks ----------------------------------------------------------

def test_push_route_is_machine_not_public():
    assert "/api/pnl/push" in MACHINE_API_PATHS
    assert "/api/pnl/push" not in PUBLIC_API_PATHS


def test_push_does_not_exist_without_a_token(monkeypatch):
    monkeypatch.delenv("PNL_PUSH_TOKEN", raising=False)
    assert _push(_snap()).status_code == 404


def test_short_token_counts_as_unset(monkeypatch):
    monkeypatch.setenv("PNL_PUSH_TOKEN", "short")
    assert _push(_snap(), tok="short").status_code == 404


def test_push_refuses_missing_and_wrong_token(token):
    assert _push(_snap(), tok=None).status_code == 401
    assert _push(_snap(), tok="x" * 40).status_code == 401
    assert not store.LATEST.exists()


def test_push_refuses_a_session_cookie_instead_of_the_token(token):
    name = f"pnl{int(time.time() * 1000) % 10**9}"
    auth.create_user(name, "terminal-password")
    try:
        c = TestClient(app)
        assert c.post("/api/login", json={"username": name, "password": "terminal-password"}).status_code == 200
        assert c.post("/api/pnl/push", content=json.dumps(_snap())).status_code == 401
    finally:
        auth.disable_user(name)


def test_push_stores_with_the_right_token(token):
    r = _push(_snap())
    assert r.status_code == 200 and r.json()["stored"] is True
    assert json.loads(store.LATEST.read_text())["venues"][0]["equity"] == 1000.0


def test_push_rejects_bad_bodies(token):
    assert _push(b"not json").status_code == 422
    assert _push({"schema": 2, "venues": []}).status_code == 422
    assert _push({"schema": 1, "venues": [{"venue": "robinhood", "ok": True, "as_of": 1}]}).status_code == 422
    assert _push(_snap(equity=float("inf"))).status_code == 422
    assert _push(_snap(equity=True)).status_code == 422
    assert _push(b'{"schema":1,"venues":[{"venue":"public","ok":true,"as_of":1,"equity":NaN}]}').status_code == 422
    two = _snap()
    two["venues"].append(dict(two["venues"][0]))
    assert _push(two).status_code == 422
    assert _push(b"x" * (store.MAX_BODY_BYTES + 1)).status_code == 413
    assert not store.LATEST.exists()


def test_get_on_push_is_not_a_read_path(token):
    # A GET to the machine path must not leak anything (FastAPI: 405 method not allowed).
    assert TestClient(app).get("/api/pnl/push").status_code in (401, 404, 405)


def test_pnl_read_is_private():
    assert TestClient(app).get("/api/pnl").status_code == 401


# ---- validation keeps only whitelisted, safe fields ---------------------------------

def test_validate_drops_unknown_keys_like_account_ids():
    s = _snap()
    s["venues"][0]["account_id"] = "ACCT-FAKE-1"
    s["venues"][0]["positions"][0]["account_id"] = "ACCT-FAKE-1"
    s["account"] = "secret"
    clean = store.validate(s)
    assert "ACCT-FAKE-1" not in json.dumps(clean) and "secret" not in json.dumps(clean)


def test_validate_caps_strings_and_positions():
    s = _snap(note="n" * 5000)
    assert len(store.validate(s)["venues"][0]["note"]) == 300
    s = _snap(positions=[{"symbol": "X"}] * (store.MAX_POSITIONS + 1))
    with pytest.raises(store.InvalidSnapshot):
        store.validate(s)


def test_failed_venue_needs_no_as_of():
    clean = store.validate({"schema": 1, "venues": [{"venue": "topstep", "ok": False, "error": "login failed"}]})
    assert clean["venues"][0]["ok"] is False


# ---- read: staleness, totals, history -----------------------------------------------

def test_read_before_any_push_is_not_ready():
    assert store.read()["ready"] is False


def test_totals_skip_failed_venues_instead_of_counting_zero():
    now = 1_800_000_000.0
    snap = {"schema": 1, "venues": [
        {"venue": "public", "ok": True, "as_of": now, "equity": 800.0, "day_pnl": -5.0},
        {"venue": "kalshi", "ok": True, "as_of": now, "equity": 150.0, "day_pnl": None},
        {"venue": "topstep", "ok": False, "error": "TopstepX timeout"}]}
    store.save(store.validate(snap), now=now)
    out = store.read(now=now + 60)
    assert out["totals"]["equity"] == 950.0
    assert out["totals"]["day_pnl"] == -5.0
    assert out["totals"]["venues_missing"] == ["topstep"]
    by = {v["venue"]: v for v in out["venues"]}
    assert by["topstep"]["stale"] is True and by["public"]["stale"] is False
    assert out["history"][-1]["by_venue"]["topstep"] is None   # no fake crash on the curve


def test_old_snapshot_reads_as_stale():
    now = 1_800_000_000.0
    store.save(store.validate(_snap(as_of=now)), now=now)
    assert store.read(now=now + 3 * 3600)["venues"][0]["stale"] is True


def test_history_spacing_and_cap(monkeypatch):
    monkeypatch.setattr(store, "HISTORY_KEEP", 3)
    t = 1_800_000_000.0
    assert store.save(store.validate(_snap(as_of=t)), now=t)["history_point"] is True
    assert store.save(store.validate(_snap(as_of=t + 60)), now=t + 60)["history_point"] is False
    for i in range(1, 6):
        store.save(store.validate(_snap(as_of=t + i * 700)), now=t + i * 700)
    hist = store.read(now=t + 5000)["history"]
    assert len(hist) == 3 and hist[-1]["ts"] == t + 3500
