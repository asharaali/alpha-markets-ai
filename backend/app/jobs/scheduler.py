"""Background jobs: keep the board warm, record market history, grade what has finished.

Three loops, all failure-tolerant. A background job that crashes the app is worse than one
that misses a cycle, so each iteration catches its own exceptions, logs them, and waits for
the next tick.

The market snapshot loop is the one that matters most. Line-movement analysis and
closing-line value are only possible if somebody was writing prices down all along, and
there is no way to backfill it later.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

from app.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

_tasks: List[asyncio.Task] = []
_status: Dict[str, Dict[str, Any]] = {}


def status() -> Dict[str, Any]:
    return {"enabled": settings.JOBS_ENABLED, "jobs": _status,
            "running": [t.get_name() for t in _tasks if not t.done()]}


def _record(name: str, **fields: Any) -> None:
    entry = _status.setdefault(name, {"runs": 0, "errors": 0})
    entry.update(fields)
    entry["last_run"] = time.time()


async def _loop(name: str, interval: float, body) -> None:
    """Run `body` forever on an interval, surviving its failures."""
    # Stagger startup so three jobs do not all hammer the venue on boot.
    await asyncio.sleep(min(interval * 0.1, 15))
    while True:
        started = time.time()
        try:
            result = await body()
            entry = _status.setdefault(name, {"runs": 0, "errors": 0})
            entry["runs"] = entry.get("runs", 0) + 1
            _record(name, last_result=result, last_error=None,
                    last_duration=round(time.time() - started, 2))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a job must never kill the app
            entry = _status.setdefault(name, {"runs": 0, "errors": 0})
            entry["errors"] = entry.get("errors", 0) + 1
            _record(name, last_error=str(exc)[:250],
                    last_duration=round(time.time() - started, 2))
            log.error("background job %s failed: %s", name, exc)
        await asyncio.sleep(interval)


async def _calibrate_once() -> Dict[str, Any]:
    """Fit the game model if it has not been fitted, on a worker thread.

    Fitting downloads several seasons of play-by-play and takes tens of seconds, so it must
    not block the event loop while the app is serving requests.
    """
    from app.models import calibration

    if calibration.load() is not None:
        return {"status": "already calibrated"}
    log.info("no game model artifact found — fitting in the background")
    artifact = await calibration.fit()
    return {"status": "fitted", "games": artifact.sample_games,
            "seasons": artifact.seasons_fitted}


async def _snapshot_once() -> Dict[str, Any]:
    """Refresh the slate and write every price we see to the snapshot table."""
    from app import engine
    from app.tracking import store

    analysis = await engine.analyze()
    written = store.record_snapshots(analysis.quotes)
    predictions = store.record_predictions(engine.prediction_rows(analysis))
    predictions += store.record_predictions(engine.per_strategy_rows(analysis))
    return {"quotes": len(analysis.quotes), "snapshots": written,
            "predictions_recorded": predictions,
            "week": analysis.week}


async def _grade_once() -> Dict[str, Any]:
    from app.tracking import grading, store

    result = await grading.grade_finished_games()
    result.update(await grading.settle_positions())
    removed = store.prune_snapshots()
    result["snapshots_pruned"] = removed
    return result


def start() -> None:
    """Launch the background jobs. Called once from the app's startup hook."""
    if not settings.JOBS_ENABLED:
        log.info("background jobs are disabled (JOBS_ENABLED=false)")
        return
    if _tasks:
        return

    specs = [
        ("calibration", 6 * 3600, _calibrate_once),
        ("market_snapshots", settings.MARKET_SNAPSHOT_INTERVAL, _snapshot_once),
        ("grading", settings.GRADING_INTERVAL, _grade_once),
    ]
    for name, interval, body in specs:
        task = asyncio.create_task(_loop(name, interval, body), name=name)
        _tasks.append(task)
    log.info("started %d background jobs", len(_tasks))


async def stop() -> None:
    for task in _tasks:
        task.cancel()
    for task in _tasks:
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _tasks.clear()
