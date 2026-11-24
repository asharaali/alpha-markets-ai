"""Keeping Render's half CPU free (2026-11-23 stalls): the settled-row scans use SQLite to pick
the latest forecast per contract, run on the scheduler, and a page load only ever reads the
newest finished result instead of starting a scan."""
import asyncio
import json
import time

from app.evaluation import protocol
from app.jobs import research, scheduler
from app.research import evidence
from app.tracking import store


def _fingerprint(groups):
    return {fam: sorted((protocol.forecast_key(r), r["created_at"]) for r in rows) for fam, rows in groups.items()}


def test_sql_latest_forecasts_match_the_python_dedupe_on_the_real_db():
    store.init()
    python = evidence.dedupe_by_family(store.iter_settled_predictions())
    sql = evidence.dedupe_by_family(store.latest_settled_forecasts())
    assert _fingerprint(sql) == _fingerprint(python)


def test_strategies_report_is_unchanged_by_the_sql_path():
    from app.api import routes_research
    from app.tracking import metrics
    stream, raw = protocol.tally(store.iter_settled_predictions(), ("strategy",))
    settled = protocol.deduplicate(stream)
    old = {}
    for row in settled:
        old.setdefault(row["strategy"], []).append(row)
    new = {s["key"]: s["record"] for s in routes_research.strategies_report()["strategies"]}
    for key, record in new.items():
        ref = metrics.summarise(old.get(key, []), label=key, raw_rows=raw["strategy"].get(key))
        assert record == ref, key


def test_page_load_serves_the_newest_finished_result_without_starting_a_scan(tmp_path, monkeypatch):
    monkeypatch.setattr(research, "RESULT_DIR", tmp_path)
    (tmp_path / "evidence_oldkey.json").write_text(json.dumps({"result": {"families": [], "stale": True}}))
    spawned = []

    async def no_spawn(*a, **k):
        spawned.append(a)
        raise AssertionError("a page load started a scan")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", no_spawn)
    out = asyncio.run(research.cached("evidence", bucket=int(time.time() // 1800)))
    assert out == {"families": [], "stale": True} and spawned == []


def test_page_load_computes_only_when_nothing_has_ever_been_computed(tmp_path, monkeypatch):
    monkeypatch.setattr(research, "RESULT_DIR", tmp_path)
    ran = []

    async def fake_run(kind, **params):
        ran.append(kind)
        return {"families": []}

    monkeypatch.setattr(research, "run", fake_run)
    assert asyncio.run(research.cached("evidence", bucket=1)) == {"families": []}
    assert ran == ["evidence"]


def test_the_scheduler_owns_the_scans():
    names = [name for name, _, _ in scheduler.job_specs()]
    assert "research_snapshots" in names


def test_jobs_really_stagger_on_boot_so_health_checks_pass_first():
    """min(interval*0.1, 15) gave every job the same 15s delay, so the cold analysis, grading and
    the research scans all hit Render's half CPU together and the new instance never answered
    its first health checks (deploy 546448c stuck 'in progress', 2026-11-23)."""
    delays = {name: scheduler.start_delay(name) for name, _, _ in scheduler.job_specs()}
    assert len(set(delays.values())) == len(delays)               # no two jobs start together
    heavy = ("market_snapshots", "grading", "research_snapshots")
    assert min(delays[n] for n in heavy) >= 60                    # health checks get a clear minute
    ordered = sorted(heavy, key=delays.get)
    gaps = [delays[b] - delays[a] for a, b in zip(ordered, ordered[1:])]
    assert min(gaps) >= 60                                        # and the heavy ones don't overlap
