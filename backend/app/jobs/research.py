"""Run the heavy research reports in their own process, and remember the answer.

The walk-forward evaluation and the standing backtest are 10-15s of pure-Python CPU on a
laptop and about a minute on the half-CPU host. Run inside a request they held the event
loop the whole time: /api/health went unanswered past Render's 5-second check and the
instance was killed — just from opening the Decisions or Model page.

So, like the model fit (see fit_model.py), they run in a separate lower-priority process
the web server merely awaits, and the result is cached on disk. Both reports cover
finished seasons only, so the answer changes when the code or the fitted model changes,
not from one page view to the next.

Child entry point: `python -m app.jobs.research <kind> <params-json> <out-path>`.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict

from app.config import settings
from app.core import errors
from app.core.logging import get_logger

log = get_logger(__name__)

_BACKEND_DIR = str(Path(__file__).resolve().parents[2])
RESULT_DIR = Path(settings.CACHE_DIR) / "research"
# A backstop, not the main invalidation: the key already moves with code and model.
MAX_AGE = 24 * 3600.0

_locks: Dict[str, asyncio.Lock] = {}


async def _compute(kind: str, params: Dict[str, Any]) -> Any:
    if kind == "evaluation":
        from app.evaluation import report
        return await report.run(**params)
    if kind == "backtest":
        from app.backtest import engine
        return await engine.run(**params)
    raise ValueError(f"unknown research job {kind!r}")


def _key(kind: str, params: Dict[str, Any]) -> str:
    from app.models import calibration

    model_stamp = max((p.stat().st_mtime_ns for p in calibration.ARTIFACT_DIR.glob("*.json")),
                      default=0)
    raw = json.dumps([kind, params, settings.VERSION, model_stamp], sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _read(path: Path) -> Any:
    blob = json.loads(path.read_text())
    if "error" in blob:
        err = blob["error"]
        cls = getattr(errors, err.get("type", ""), None)
        if isinstance(cls, type) and issubclass(cls, Exception):
            raise cls(err.get("message", ""))
        raise RuntimeError(err.get("message", "research job failed"))
    return blob["result"]


async def run(kind: str, **params: Any) -> Any:
    """The report for these parameters: from disk if fresh, else computed out of process."""
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    key = _key(kind, params)
    path = RESULT_DIR / f"{kind}_{key}.json"
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:                      # a second viewer waits for the same run
        if path.exists() and time.time() - path.stat().st_mtime < MAX_AGE:
            return _read_once(path)
        log.info("research: computing %s in a background process", kind)
        started = time.time()
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "app.jobs.research", kind,
            json.dumps(params), str(path), cwd=_BACKEND_DIR)
        code = await proc.wait()
        if code != 0 or not path.exists():
            raise RuntimeError(f"{kind} job exited with status {code}")
        log.info("research: %s ready in %.1fs", kind, time.time() - started)
        return _read_once(path)


def _read_once(path: Path) -> Any:
    """Read a result; an error result is reported once, never cached."""
    try:
        return _read(path)
    except Exception:
        path.unlink(missing_ok=True)
        raise


def main() -> None:
    from fastapi.encoders import jsonable_encoder

    kind, params, out = sys.argv[1], json.loads(sys.argv[2]), Path(sys.argv[3])
    try:
        os.nice(10)
    except OSError:
        pass
    try:
        blob: Dict[str, Any] = {"result": jsonable_encoder(asyncio.run(_compute(kind, params)))}
    except errors.AlphaError as exc:      # expected and user-facing, e.g. not calibrated yet
        blob = {"error": {"type": type(exc).__name__, "message": str(exc)}}
    tmp = out.with_suffix(f".part{os.getpid()}")
    tmp.write_text(json.dumps(blob))
    tmp.replace(out)


if __name__ == "__main__":
    main()
