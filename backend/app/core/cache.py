"""TTL cache with stale-if-error semantics.

The single most damaging failure mode in the old build was a transient upstream blip
emptying a live board: one 429 from Kalshi and the combo builder showed nothing. So every
cached fetch here keeps the last GOOD value and serves it (clearly marked stale) when a
refresh fails, rather than caching the failure.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Generic, Optional, TypeVar

from app.core.logging import get_logger

log = get_logger(__name__)

T = TypeVar("T")


@dataclass
class Entry(Generic[T]):
    value: T
    fetched_at: float
    stale: bool = False


@dataclass
class _Slot:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    entry: Optional[Entry] = None


class AsyncTTLCache:
    """Per-key async cache. Coalesces concurrent misses behind one lock per key."""

    def __init__(self, ttl: float, stale_ttl: float = 900.0, name: str = "cache"):
        self.ttl = ttl
        self.stale_ttl = stale_ttl
        self.name = name
        self._slots: Dict[str, _Slot] = {}

    def _slot(self, key: str) -> _Slot:
        slot = self._slots.get(key)
        if slot is None:
            slot = _Slot()
            self._slots[key] = slot
        return slot

    def peek(self, key: str) -> Optional[Entry]:
        slot = self._slots.get(key)
        return slot.entry if slot else None

    def invalidate(self, key: Optional[str] = None) -> None:
        if key is None:
            self._slots.clear()
        else:
            self._slots.pop(key, None)

    async def get(self, key: str, loader: Callable[[], Awaitable[T]],
                  *, empty_is_failure: bool = False) -> Entry:
        """Return a cached Entry, refreshing through `loader` when stale.

        `empty_is_failure=True` treats an empty result (no rows) the same as an exception:
        the previous good value stands in. Use it for live boards, where "no markets" is
        almost always a feed blip rather than the truth.
        """
        slot = self._slot(key)
        now = time.time()
        cur = slot.entry
        if cur is not None and (now - cur.fetched_at) < self.ttl and not cur.stale:
            return cur

        async with slot.lock:
            # Another waiter may have refreshed while we queued.
            cur = slot.entry
            now = time.time()
            if cur is not None and (now - cur.fetched_at) < self.ttl and not cur.stale:
                return cur
            try:
                value = await loader()
                empty = empty_is_failure and _is_empty(value)
                if empty and cur is not None and (now - cur.fetched_at) < self.stale_ttl:
                    log.warning("%s[%s] refresh returned empty; serving last good value "
                                "(%.0fs old)", self.name, key, now - cur.fetched_at)
                    stale = Entry(cur.value, cur.fetched_at, stale=True)
                    slot.entry = stale
                    return stale
                entry = Entry(value, time.time(), stale=False)
                slot.entry = entry
                return entry
            except Exception as exc:  # noqa: BLE001 - deliberate: any loader failure
                if cur is not None and (time.time() - cur.fetched_at) < self.stale_ttl:
                    log.warning("%s[%s] refresh failed (%s); serving last good value "
                                "(%.0fs old)", self.name, key, exc,
                                time.time() - cur.fetched_at)
                    stale = Entry(cur.value, cur.fetched_at, stale=True)
                    slot.entry = stale
                    return stale
                log.error("%s[%s] refresh failed with no usable cache: %s",
                          self.name, key, exc)
                raise


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, (list, tuple, dict, set, str)):
        return len(value) == 0
    return False
