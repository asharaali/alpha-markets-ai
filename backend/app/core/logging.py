"""Structured application logging.

One configured root logger for the whole app so every module can `get_logger(__name__)`
instead of scattering print() calls (which the old codebase did, and which meant data-feed
failures vanished into Render's stdout with no level, timestamp, or module).
"""
from __future__ import annotations

import logging
import os
import sys

_CONFIGURED = False

_FMT = "%(asctime)s %(levelname)-7s %(name)-28s %(message)s"
_DATEFMT = "%H:%M:%S"


def configure(level: str | None = None) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    lvl = (level or os.getenv("LOG_LEVEL") or "INFO").upper()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_FMT, datefmt=_DATEFMT))
    root = logging.getLogger("alpha")
    root.setLevel(getattr(logging, lvl, logging.INFO))
    root.handlers[:] = [handler]
    root.propagate = False
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    configure()
    # Everything hangs off the "alpha" root so one level switch controls the whole app.
    short = name.replace("app.", "", 1) if name.startswith("app.") else name
    return logging.getLogger(f"alpha.{short}")
