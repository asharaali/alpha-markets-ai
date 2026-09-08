"""On-disk cache for large upstream data files.

nflverse ships whole seasons as CSV (play-by-play is ~19MB gzipped). Refetching those on
every request would be absurd, and holding them in memory would blow the Render dyno, so
they land on disk once and are re-read as streams.

Downloads are atomic (temp file + rename) so a killed process can never leave a truncated
CSV that then parses as a silently short season.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional

import httpx

from app.config import settings
from app.core.errors import UpstreamError
from app.core.http import make_client, request
from app.core.logging import get_logger

log = get_logger(__name__)

CACHE_DIR = Path(settings.CACHE_DIR)


def _safe_name(url: str) -> str:
    return url.rsplit("/", 1)[-1].replace("?", "_").replace("&", "_") or "download"


def cached_path(url: str) -> Path:
    return CACHE_DIR / _safe_name(url)


def age_seconds(path: Path) -> float:
    try:
        return time.time() - path.stat().st_mtime
    except OSError:
        return float("inf")


async def fetch_file(url: str, *, ttl: float, source: str = "nflverse",
                     required: bool = True) -> Optional[Path]:
    """Download `url` into the feed cache unless a fresh copy already exists.

    Returns the local path, or None when the resource does not exist upstream (a 404 is a
    normal, expected answer for a season whose files have not been published yet — the
    caller degrades instead of crashing).

    On a download failure a stale cached copy is used if we have one; only a failure with
    nothing on disk propagates.
    """
    path = cached_path(url)
    if path.exists() and age_seconds(path) < ttl and path.stat().st_size > 0:
        return path

    tmp = path.with_suffix(path.suffix + f".part{os.getpid()}")
    try:
        async with make_client(timeout=httpx.Timeout(180.0, connect=15.0)) as client:
            resp = await request(client, "GET", url, source=source, attempts=3)
            if resp.status_code == 404:
                log.info("%s: %s not published upstream (404)", source, _safe_name(url))
                return None
            if resp.status_code >= 400:
                raise UpstreamError(f"{source} returned {resp.status_code} for {_safe_name(url)}")
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_bytes(resp.content)
            tmp.replace(path)
            log.info("%s: cached %s (%.1f MB)", source, _safe_name(url),
                     path.stat().st_size / 1e6)
            return path
    except Exception as exc:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        if path.exists() and path.stat().st_size > 0:
            log.warning("%s: refresh of %s failed (%s); using cached copy %.0f min old",
                        source, _safe_name(url), exc, age_seconds(path) / 60)
            return path
        if required:
            raise
        log.warning("%s: %s unavailable and not cached (%s)", source, _safe_name(url), exc)
        return None
