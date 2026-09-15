"""The calibration job must never stall the web server.

On Render's half-CPU instance the fit ran on the event loop, /api/health stopped answering
within the 5-second check, and the platform killed the instance mid-fit — forever, because
every restart began the same fit again.
"""
import asyncio
import sys
import time

from app.jobs import scheduler
from app.models import calibration


async def test_calibration_job_keeps_event_loop_responsive(monkeypatch):
    # A child process that burns CPU for a second stands in for the real fit.
    monkeypatch.setattr(scheduler, "_FIT_COMMAND",
                        [sys.executable, "-c", "import time; t=time.time()\n"
                         "while time.time() - t < 1.0: pass"])
    loaded = iter([None, object()])
    monkeypatch.setattr(calibration, "load", lambda: next(loaded))

    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.05)
            ticks += 1

    task = asyncio.create_task(ticker())
    started = time.time()
    await scheduler._calibrate_once()
    elapsed = time.time() - started
    task.cancel()

    assert elapsed >= 1.0
    # A blocked loop would tick once or twice; a free one ticks roughly every 50ms.
    assert ticks >= 10


async def test_calibration_job_reports_a_failed_fit(monkeypatch):
    monkeypatch.setattr(scheduler, "_FIT_COMMAND", [sys.executable, "-c", "raise SystemExit(3)"])
    monkeypatch.setattr(calibration, "load", lambda: None)
    try:
        await scheduler._calibrate_once()
    except RuntimeError as exc:
        assert "3" in str(exc)
    else:
        raise AssertionError("a failed fit must raise so the job records the error")
