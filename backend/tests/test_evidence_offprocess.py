"""The 2026-11-23 crash loop: the evidence/calibration scan is pure-Python CPU. In a thread it
still holds the GIL, so /api/health starved past Render's 5s check and the instance was killed
on every boot. The web process must only ever read the scan's result, never run it."""
import asyncio

import pytest

from app.research import calibration, evidence
from tests.test_evidence import _rows


@pytest.fixture
def groups(monkeypatch):
    g = {("ensemble", "spread"): _rows(60, model_skill=1.0, market_skill=0.3)}
    monkeypatch.setattr(evidence, "group_settled", lambda **kw: g)
    return g


def test_snapshot_round_trips_evidence_and_calibration(groups):
    snap = evidence.snapshot()
    ev_map, cal_map = evidence.from_snapshot(snap)
    fam = ("ensemble", "spread")
    direct = evidence.assess(groups[fam])
    assert ev_map[fam].state == direct.state == "PROVEN"
    assert ev_map[fam].ci95 == direct.ci95 and ev_map[fam].calls == direct.calls
    direct_cal = calibration.Calibrator().fit(groups[fam])
    assert cal_map[fam].table() == direct_cal.table()
    assert cal_map[fam].apply(0.35, 0.3) == pytest.approx(direct_cal.apply(0.35, 0.3))


def test_load_reads_the_child_result_and_never_scans_in_process(groups, monkeypatch):
    snap = evidence.snapshot()
    calls = []

    async def fake_run(kind, **params):
        calls.append((kind, params))
        return snap

    def boom(**kw):
        raise AssertionError("the web process ran the settled-row scan")

    monkeypatch.setattr(evidence, "group_settled", boom)
    monkeypatch.setattr("app.jobs.research.cached", fake_run)
    evidence._LOADED.clear()
    ev_map, cal_map = asyncio.run(evidence.load())
    assert ev_map[("ensemble", "spread")].state == "PROVEN"
    assert calls and calls[0][0] == "evidence" and "bucket" in calls[0][1]
    asyncio.run(evidence.load())
    assert len(calls) == 1          # same 30-minute bucket: served from memory
    evidence._LOADED.clear()


def test_strategies_endpoint_reads_the_child_result_and_never_scans_in_process(monkeypatch):
    """Same crash loop, second cause: /api/strategies scanned every settled row on the event
    loop (1.1s locally, ~10s on Render) and the new Home page calls it on every load."""
    import time as _t
    from fastapi.testclient import TestClient
    from app import auth
    from app.main import app
    from app.tracking import store

    calls = []

    async def fake_run(kind, **params):
        calls.append(kind)
        return {"strategies": [{"key": "moneyline"}], "ensemble": {"weights": []}}

    def boom(*a, **k):
        raise AssertionError("the web process scanned the predictions table")

    name = f"st{int(_t.time() * 1000) % 10**9}"
    auth.create_user(name, "strategies-pw")
    client = TestClient(app)
    client.post("/api/login", json={"username": name, "password": "strategies-pw"})
    monkeypatch.setattr("app.jobs.research.cached", fake_run)
    monkeypatch.setattr(store, "iter_settled_predictions", boom)
    r = client.get("/api/strategies")
    auth.disable_user(name)
    assert r.status_code == 200 and r.json()["strategies"] == [{"key": "moneyline"}]
    assert calls == ["strategies"]
