"""Shared async HTTP client with retries, backoff, and rate-limit handling.

Every outbound call in the app goes through here so retry policy, timeouts, and the
User-Agent live in exactly one place. Kalshi in particular rate-limits bursts, and the old
code's habit of reading a 429 as "no price" is what made entire boards read as untradeable.
"""
from __future__ import annotations

import asyncio
import random
from typing import Any, Dict, Optional

import httpx

from app.core.errors import RateLimited, UpstreamError
from app.core.logging import get_logger

log = get_logger(__name__)

USER_AGENT = "AlphaMarkets/2.0 (+nfl-research)"

DEFAULT_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
# Kalshi tolerates roughly this much concurrency before it starts shedding with 429s.
DEFAULT_CONCURRENCY = 8

_RETRY_STATUS = {429, 500, 502, 503, 504}


def make_client(*, timeout: httpx.Timeout | float = DEFAULT_TIMEOUT,
                headers: Optional[Dict[str, str]] = None,
                follow_redirects: bool = True) -> httpx.AsyncClient:
    hdrs = {"User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)
    return httpx.AsyncClient(timeout=timeout, headers=hdrs,
                             follow_redirects=follow_redirects,
                             limits=httpx.Limits(max_connections=20,
                                                 max_keepalive_connections=10))


async def request(client: httpx.AsyncClient, method: str, url: str, *,
                  attempts: int = 4, base_delay: float = 0.5,
                  source: str = "upstream", **kwargs: Any) -> httpx.Response:
    """One HTTP call with exponential backoff + jitter on retryable statuses.

    Raises RateLimited if we exhaust retries against a 429, UpstreamError otherwise.
    A non-retryable 4xx comes back to the caller as a Response so it can read the body.
    """
    last_exc: Optional[Exception] = None
    last_status: Optional[int] = None
    for attempt in range(attempts):
        try:
            resp = await client.request(method, url, **kwargs)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_exc = exc
            last_status = None
        else:
            if resp.status_code not in _RETRY_STATUS:
                return resp
            last_status = resp.status_code
            last_exc = None
            # Honour an explicit Retry-After when the server sends one.
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                try:
                    await asyncio.sleep(min(float(retry_after), 10.0))
                    continue
                except ValueError:
                    pass
        if attempt < attempts - 1:
            delay = base_delay * (2 ** attempt) + random.uniform(0, base_delay)
            log.debug("%s %s %s -> retry %d/%d in %.2fs (status=%s exc=%s)",
                      source, method, url, attempt + 1, attempts, delay,
                      last_status, last_exc)
            await asyncio.sleep(delay)

    if last_status == 429:
        raise RateLimited(f"{source} rate-limited after {attempts} attempts",
                          detail=f"{method} {url}")
    raise UpstreamError(f"{source} unreachable after {attempts} attempts",
                        detail=f"{method} {url} (last status={last_status}, exc={last_exc})")


async def get_json(client: httpx.AsyncClient, url: str, *, source: str = "upstream",
                   **kwargs: Any) -> Any:
    resp = await request(client, "GET", url, source=source, **kwargs)
    if resp.status_code >= 400:
        raise UpstreamError(f"{source} returned {resp.status_code}",
                            detail=resp.text[:300])
    try:
        return resp.json()
    except ValueError as exc:
        raise UpstreamError(f"{source} returned non-JSON", detail=str(exc)[:200]) from exc


async def gather_limited(coros, limit: int = DEFAULT_CONCURRENCY):
    """asyncio.gather with a concurrency ceiling, so we never burst a feed into 429s."""
    sem = asyncio.Semaphore(limit)

    async def _run(coro):
        async with sem:
            return await coro

    return await asyncio.gather(*[_run(c) for c in coros])
