"""TTL cache with single-flight loading for read-only lookups.

100 sessions asking for the same supplier at once cause ONE backend call, not 100. Failures are
never cached. Cached values are shared between sessions: treat them as read-only.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from .idempotency import fail_future

Loader = Callable[[], Awaitable[dict[str, Any]]]


class TTLCache:
    def __init__(
        self, max_entries: int = 1024, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._max = max_entries
        self._clock = clock
        self._data: dict[str, tuple[float, dict[str, Any]]] = {}
        self._inflight: dict[str, asyncio.Future[dict[str, Any]]] = {}

    async def get_or_load(
        self, key: str, ttl_s: float, loader: Loader
    ) -> tuple[dict[str, Any], bool]:
        """Return (value, was_cached)."""
        entry = self._data.get(key)
        if entry and entry[0] > self._clock():
            return entry[1], True
        pending = self._inflight.get(key)
        if pending is not None:
            return await asyncio.shield(pending), True

        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._inflight[key] = future
        try:
            value = await loader()
        except BaseException as exc:
            self._inflight.pop(key, None)
            fail_future(future, exc)
            raise
        self._inflight.pop(key, None)
        self._data[key] = (self._clock() + ttl_s, value)
        if len(self._data) > self._max:
            self._data.pop(next(iter(self._data)))  # evict oldest inserted
        future.set_result(value)
        return value, False
