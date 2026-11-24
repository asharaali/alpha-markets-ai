"""The Mac-side collector (mac/pnl_push.py): parsing, venue isolation, and the Topstep hands-off rule.

Payload shapes are trimmed from real responses captured 2026-11-24 (ids replaced, amounts and holdings made up).
"""
import importlib.util
import json
from datetime import datetime
from pathlib import Path

import pytest

from app.pnl import store

_SPEC = importlib.util.spec_from_file_location(
    "pnl_push", Path(__file__).resolve().parents[2] / "mac" / "pnl_push.py")
pp = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(pp)

ET = pp.ET

KMON_STATE = {
    "ready": True, "mode": "live", "ts": 1795548547.5,
    "connection": {"mode": "live", "positions_age_s": 9.6, "positions_error": None, "cash_balance": 100.5},
    "totals": {"invested": 4.0, "liquidation_value": 0, "exec_pnl": -4.0},
    "positions": [{"source": "live", "ticker": "KXEXAMPLE-26-T1", "side": "yes", "kind": "single",
                   "title": "Example market",
                   "metrics": {"qty": 10, "cost_basis": 4.0, "liquidation_value": 0.0,
                               "liquidation_complete": False, "exec_pnl": -4.0}},
                  {"source": "manual", "ticker": "MANUAL-1", "metrics": {}}],
}

PUBLIC_PF = {
    "accountId": "ACCT1", "cash": "250.00", "totalAccountValue": "1000.00",
    "positions": [
        {"instrument": {"symbol": "ACME", "name": "Acme Corp", "type": "EQUITY"}, "quantity": "3",
         "currentValue": "300.00", "positionDailyGain": {"gainValue": "-6.00"},
         "costBasis": {"totalCost": "330.00", "gainValue": "-30.00"}},
        {"instrument": {"symbol": "ZZZ270115C00100000", "name": "ZZZ $100 Call", "type": "OPTION"},
         "quantity": "1", "currentValue": "80.00", "positionDailyGain": {"gainValue": "-1.50"},
         "costBasis": {"totalCost": "90.00", "gainValue": "-10.00"}},
    ],
}


def _journal(tmp_path, *rows):
    p = tmp_path / "topstep.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return p


READY = {"at": "2026-11-24T09:25:02-05:00", "event": "ready", "account_id": 777, "balance": 50400.0}


# ---- Kalshi -------------------------------------------------------------------------

def test_kalshi_equity_is_cash_plus_executable_value_and_skips_manual():
    v = pp.parse_kalshi(KMON_STATE)
    assert v["equity"] == pytest.approx(100.5) and v["unrealized_pnl"] == pytest.approx(-4.0)
    assert [p["symbol"] for p in v["positions"]] == ["KXEXAMPLE-26-T1"]
    assert "unsellable" in v["positions"][0]["note"]
    assert v["as_of"] == pytest.approx(1795548537.9)


def test_kalshi_demo_mode_is_a_failure_not_fake_money():
    v = pp.kalshi(get=lambda *a, **k: {**KMON_STATE, "mode": "demo"})
    assert v["ok"] is False and "demo" in v["error"]


def test_kalshi_monitor_down_isolated():
    def boom(*a, **k):
        raise ConnectionRefusedError("8790")
    assert pp.kalshi(get=boom)["ok"] is False


# ---- Public -------------------------------------------------------------------------

def test_public_parse_sums_day_and_unrealized():
    v = pp.parse_public(PUBLIC_PF, now=1.0)
    assert v["equity"] == 1000.0 and v["cash"] == 250.0
    assert v["day_pnl"] == -7.5 and v["unrealized_pnl"] == -40.0
    assert {p["kind"] for p in v["positions"]} == {"EQUITY", "OPTION"}


def test_public_refuses_a_portfolio_for_another_account(tmp_path, monkeypatch):
    cfg = tmp_path / "public_lane.json"
    cfg.write_text(json.dumps({"account_id": "ACCT1"}))
    monkeypatch.setattr(pp, "PUBLIC_CONFIG", cfg)

    def req(method, url, body=None, headers=None, timeout=20):
        if "access-tokens" in url:
            return {"accessToken": "tok"}
        assert method == "GET"            # read-only after auth
        return {**PUBLIC_PF, "accountId": "OTHER"}
    v = pp.public(req=req, secret=lambda s: "secret")
    assert v["ok"] is False and "different account" in v["error"]


def test_public_never_sends_account_id_in_snapshot(tmp_path, monkeypatch):
    cfg = tmp_path / "public_lane.json"
    cfg.write_text(json.dumps({"account_id": "ACCT1"}))
    monkeypatch.setattr(pp, "PUBLIC_CONFIG", cfg)
    req = lambda m, u, b=None, headers=None, timeout=20: {"accessToken": "t"} if "access-tokens" in u else PUBLIC_PF
    v = pp.public(req=req, secret=lambda s: "secret")
    assert v["ok"] and "ACCT1" not in json.dumps(v)


# ---- Topstep ------------------------------------------------------------------------

def test_runner_window_bounds():
    at = lambda h, m, d=24: datetime(2026, 11, d, h, m, tzinfo=ET)
    assert not pp.runner_window(at(9, 19)) and pp.runner_window(at(9, 20))
    assert pp.runner_window(at(16, 4)) and not pp.runner_window(at(16, 5))
    assert not pp.runner_window(at(12, 0, d=28))     # Saturday


def test_topstep_never_logs_in_during_runner_window(tmp_path):
    j = _journal(tmp_path, READY, {"at": "2026-11-24T14:00:00-05:00", "event": "checkpoint", "have": 0})

    def no_network(*a, **k):
        raise AssertionError("must not call TopstepX while the runner is live")
    v = pp.topstep(now=lambda: datetime(2026, 11, 24, 14, 30, tzinfo=ET), req=no_network,
                   secret=no_network, journal=j)
    assert v["ok"] and v["equity"] == 50400.0 and v["day_pnl"] is None and "Runner is live" in v["note"]
    assert v["positions"] == []


def test_topstep_runner_window_shows_runner_position(tmp_path):
    j = _journal(tmp_path, READY, {"at": "2026-11-24T10:00:00-05:00", "event": "checkpoint", "have": 3})
    v = pp.topstep(now=lambda: datetime(2026, 11, 24, 10, 5, tzinfo=ET), req=None, secret=None, journal=j)
    assert v["positions"][0]["qty"] == 3.0


def test_topstep_runner_window_without_journal_is_a_failure(tmp_path):
    j = _journal(tmp_path, {**READY, "at": "2026-11-23T09:25:00-05:00"})
    v = pp.topstep(now=lambda: datetime(2026, 11, 24, 11, 0, tzinfo=ET), req=None, secret=None, journal=j)
    assert v["ok"] is False


def test_topstep_after_hours_reads_balance_and_day_pnl(tmp_path):
    j = _journal(tmp_path, READY)
    calls = []

    def req(method, url, body=None, headers=None, timeout=20):
        calls.append(url.rsplit("/api/", 1)[1])
        if url.endswith("loginKey"):
            return {"success": True, "token": "jwt"}
        if url.endswith("Account/search"):
            return {"accounts": [{"id": 1, "name": "PRAC-1", "balance": 150000},
                                 {"id": 777, "name": "50KTC-X", "balance": 50555.25}]}
        if url.endswith("Position/searchOpen"):
            assert body == {"accountId": 777}
            return {"positions": [{"contractId": "CON.F.US.MNQ.Z26", "type": 2, "size": 1, "averagePrice": 26300}]}
        raise AssertionError(url)
    v = pp.topstep(now=lambda: datetime(2026, 11, 24, 17, 0, tzinfo=ET), req=req,
                   secret=lambda s: "x", journal=j)
    assert calls == ["Auth/loginKey", "Account/search", "Position/searchOpen"]   # reads only
    assert v["equity"] == 50555.25 and v["day_pnl"] == 155.25
    assert v["positions"][0]["symbol"] == "MNQ" and v["positions"][0]["qty"] == -1.0
    assert "50KTC" not in json.dumps(v) and "777" not in json.dumps(v)


def test_topstep_login_failure_isolated(tmp_path):
    v = pp.topstep(now=lambda: datetime(2026, 11, 24, 20, 0, tzinfo=ET),
                   req=lambda *a, **k: {"success": False, "errorCode": 3}, secret=lambda s: "x",
                   journal=_journal(tmp_path))
    assert v["ok"] is False and "login failed" in v["error"]


# ---- end to end: what the collector builds, the server accepts ----------------------

def test_collector_output_passes_server_validation(tmp_path):
    venues = [pp.parse_kalshi(KMON_STATE), pp.parse_public(PUBLIC_PF, now=1.8e9),
              pp.topstep(now=lambda: datetime(2026, 11, 24, 14, 30, tzinfo=ET), req=None, secret=None,
                         journal=_journal(tmp_path, READY))]
    clean = store.validate({"schema": 1, "venues": venues})
    assert [v["venue"] for v in clean["venues"]] == ["kalshi", "public", "topstep"]
