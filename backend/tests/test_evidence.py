"""The evidence gate: a signal may call itself PROVEN only after beating the market price on
enough independent games to rule out luck, and making money after fees doing it."""
import random
import time

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.main import app
from app.research import evidence


def _rows(games, *, model_skill, market_skill, per_game=5, refreshes=1, seed=7,
          cost_from="market"):
    """Synthetic settled forecasts. True probability t per contract; a forecaster with skill s
    reports 0.5 + s * (t - 0.5), so skill 1.0 is perfect and 0.0 is a coin flip."""
    rng = random.Random(seed)
    rows = []
    for g in range(games):
        for k in range(per_game):
            t = rng.uniform(0.05, 0.95)
            outcome = int(rng.random() < t)
            model = 0.5 + model_skill * (t - 0.5)
            market = 0.5 + market_skill * (t - 0.5)
            cost = market if cost_from == "market" else cost_from
            for r in range(refreshes):
                rows.append({
                    "game_id": f"2026_{g:03d}", "strategy": "ensemble", "market_type": "spread",
                    "selection": f"sel{k}", "created_at": 1_790_000_000 + r, "horizon_hours": 24 - r * 0.01,
                    "outcome": outcome, "model_prob": model, "fair_prob": model,
                    "market_prob": market, "cost": cost, "ev_per_dollar": model / market - 1,
                })
    return rows


def test_clearly_better_than_market_over_60_games_is_proven():
    ev = evidence.assess(_rows(60, model_skill=1.0, market_skill=0.3))
    assert ev.state == "PROVEN", ev.reason
    assert ev.clusters == 60 and ev.calls == 300
    assert ev.ci95[1] < 0
    assert ev.roi_after_fees > 0


def test_identical_to_market_is_watching():
    ev = evidence.assess(_rows(60, model_skill=0.8, market_skill=0.8))
    assert ev.state == "WATCHING"
    assert ev.reason == "Not distinguishable from the market yet."


def test_clearly_worse_is_failing():
    ev = evidence.assess(_rows(80, model_skill=0.3, market_skill=1.0))
    assert ev.state == "FAILING"
    assert ev.reason == "Significantly worse than the market price."


def test_refreshes_do_not_count_as_evidence():
    ev = evidence.assess(_rows(14, model_skill=1.0, market_skill=0.3, refreshes=500))
    assert ev.clusters == 14
    assert ev.calls == 70
    assert ev.state == "WATCHING"
    assert ev.reason == "Only 14 games settled (needs 40)."


def test_too_few_calls_is_watching_even_with_enough_games():
    ev = evidence.assess(_rows(45, model_skill=1.0, market_skill=0.3, per_game=1))
    assert ev.state == "WATCHING"
    assert ev.reason == "Only 45 calls (needs 150)."


def test_better_but_unprofitable_after_fees_is_watching():
    # Model is sharper than the market, but every contract was bought at 99c.
    ev = evidence.assess(_rows(60, model_skill=1.0, market_skill=0.3, cost_from=0.99))
    assert ev.state == "WATCHING"
    assert ev.reason == "Beats the market but loses money after fees."


def test_empty_family_is_watching():
    ev = evidence.assess([])
    assert ev.state == "WATCHING" and ev.calls == 0


@pytest.fixture
def account():
    name = f"ev{int(time.time() * 1000) % 10**9}"
    auth.create_user(name, "evidence-password")
    yield name
    auth.disable_user(name)


def test_endpoint_is_private_and_lists_thresholds(account):
    client = TestClient(app)
    assert client.get("/api/research/evidence").status_code == 401
    client.post("/api/login", json={"username": account, "password": "evidence-password"})
    body = client.get("/api/research/evidence").json()
    assert body["thresholds"] == {"min_calls": 150, "min_clusters": 40}
    assert isinstance(body["families"], list)


def test_endpoint_includes_each_familys_calibration_table(account, monkeypatch):
    from app.research import calibration
    rows = _rows(60, model_skill=1.0, market_skill=0.3)
    monkeypatch.setattr(evidence, "group_settled", lambda **kw: {("ensemble", "spread"): rows})

    async def child(kind, **params):          # stands in for the out-of-process scan
        return evidence.snapshot()
    monkeypatch.setattr("app.jobs.research.cached", child)
    evidence._LOADED.clear()
    evidence._CACHE.update(value=None)
    calibration._CACHE.update(value=None)
    client = TestClient(app)
    client.post("/api/login", json={"username": account, "password": "evidence-password"})
    [fam] = client.get("/api/research/evidence").json()["families"]
    assert fam["calibration"] and {"bucket", "n", "predicted", "observed"} <= set(fam["calibration"][0])
    evidence._LOADED.clear()
    evidence._CACHE.update(value=None)
    calibration._CACHE.update(value=None)


def test_grouping_streams_and_keeps_only_the_latest_forecast():
    """The 2026-11-23 outage: group_settled() materialised every settled row (~300k on prod),
    the 512MB instance was OOM-killed. Only the latest forecast per contract is ever used,
    so that is all the grouping may hold."""
    consumed = 0

    def stream():
        nonlocal consumed
        for row in _rows(14, model_skill=1.0, market_skill=0.3, refreshes=500):
            consumed += 1
            yield row

    groups = evidence.dedupe_by_family(stream())
    assert consumed == 14 * 5 * 500                       # it read everything...
    assert sum(len(v) for v in groups.values()) == 70     # ...but kept one row per forecast
    kept = groups[("ensemble", "spread")]
    assert all(r["created_at"] == 1_790_000_000 + 499 for r in kept)   # the latest refresh
    assert evidence.assess(kept).calls == 70
