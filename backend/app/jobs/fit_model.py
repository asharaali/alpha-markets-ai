"""Fit the game model in its own process: `python -m app.jobs.fit_model`.

The scheduler launches this rather than fitting in-process. Fitting is tens of seconds of
pure-Python CPU work; on the event loop it starved /api/health until the host killed the
instance. In a separate, lower-priority process the web server keeps answering, and the
artifact it writes is picked up by every `calibration.load()` that follows.
"""
from __future__ import annotations

import asyncio
import os

from app.models import calibration


def main() -> None:
    try:
        os.nice(10)
    except OSError:
        pass
    artifact = asyncio.run(calibration.fit())
    print(f"fitted game model on {artifact.sample_games} games", flush=True)


if __name__ == "__main__":
    main()
