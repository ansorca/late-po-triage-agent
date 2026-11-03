"""Idempotency: doing the same thing twice has the same effect as doing it once.

`run_once(key, fn)` guarantees that for a given key, `fn` has a single successful execution:
  * first caller runs `fn`; concurrent callers with the same key wait for that result;
  * later callers get the stored result (replayed=True) without running `fn`;
  * failures are NOT stored, so a retry can run `fn` again.

The store is an interface so the in-memory version can be swapped for DynamoDB / Redis
(conditional put + TTL) without touching the callers.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from .errors import TransientError

Result = dict[str, Any]


class IdempotencyStore(Protocol):
    async def run_once(
        self, key: str, fn: Callable[[], Awaitable[Result]]
    ) -> tuple[Result, bool]: ...


def fail_future(future: asyncio.Future[Result], exc: BaseException) -> None:
    """Propagate a leader's failure to waiters. Cancellation becomes a retryable error."""
    future.set_exception(exc if isinstance(exc, Exception) else TransientError("leader cancelled"))
    future.exception()  # mark retrieved so asyncio does not warn when nobody waits


class InMemoryIdempotencyStore:
    """Bounded: completed entries expire after `ttl_s` and the oldest are evicted past
    `max_entries`. Keep ttl_s longer than any client's retry window."""

    def __init__(
        self,
        max_entries: int = 100_000,
        ttl_s: float = 86_400.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max = max_entries
        self._ttl = ttl_s
        self._clock = clock
        # key -> future that holds the result (pending while the leader is running)
        self._entries: dict[str, asyncio.Future[Result]] = {}
        self._done_at: dict[str, float] = {}  # completion order == insertion order

    def _drop(self, key: str) -> None:
        self._entries.pop(key, None)
        self._done_at.pop(key, None)

    async def run_once(self, key: str, fn: Callable[[], Awaitable[Result]]) -> tuple[Result, bool]:
        existing = self._entries.get(key)
        if existing is not None:
            done_at = self._done_at.get(key)
            if done_at is not None and self._clock() - done_at > self._ttl:
                self._drop(key)  # expired: treat as never seen
            else:
                return await asyncio.shield(existing), True

        future: asyncio.Future[Result] = asyncio.get_running_loop().create_future()
        self._entries[key] = future
        try:
            result = await fn()
        except BaseException as exc:
            self._drop(key)  # do not remember failures
            fail_future(future, exc)
            raise
        future.set_result(result)
        self._done_at[key] = self._clock()
        while len(self._done_at) > self._max:
            self._drop(next(iter(self._done_at)))
        return result, False
