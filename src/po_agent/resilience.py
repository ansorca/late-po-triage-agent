"""Protection mechanisms that stop one slow/broken dependency from taking everything down.

TokenBucket     client-side rate limiter: stay under the provider's quota instead of
                discovering it through ThrottlingException.
CircuitBreaker  after N straight failures, stop calling for a while (fail fast), then let one
                probe through to test recovery.
RetryBudget     retries are only allowed as a fraction of real traffic, so a brownout cannot
                multiply load by max_attempts (a "retry storm").
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Literal

from .errors import CircuitOpenError
from .logging import get_logger
from .metrics import METRICS

log = get_logger(__name__)
Clock = Callable[[], float]


class TokenBucket:
    def __init__(
        self,
        rate_per_s: float,
        capacity: float,
        clock: Clock = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._rate = rate_per_s
        self._capacity = capacity
        self._tokens = capacity
        self._clock = clock
        self._sleep = sleep
        self._last = clock()
        self._lock = asyncio.Lock()  # waiters queue up in FIFO order

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self._capacity, self._tokens + (now - self._last) * self._rate)
        self._last = now

    async def acquire(self, n: float = 1.0) -> None:
        async with self._lock:
            while True:
                self._refill()
                if self._tokens >= n:
                    self._tokens -= n
                    return
                METRICS.incr("rate_limiter.waits")
                await self._sleep((n - self._tokens) / self._rate)


class CircuitBreaker:
    State = Literal["closed", "open", "half_open"]

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        reset_timeout_s: float = 10.0,
        clock: Clock = time.monotonic,
    ) -> None:
        self.name = name
        self._threshold = failure_threshold
        self._reset = reset_timeout_s
        self._clock = clock
        self.state: CircuitBreaker.State = "closed"
        self._failures = 0
        self._opened_at = 0.0
        self._probe_in_flight = False

    def allow(self) -> None:
        """Call before every attempt. Raises CircuitOpenError instead of calling a sick service."""
        if self.state == "closed":
            return
        if self.state == "open" and self._clock() - self._opened_at >= self._reset:
            self.state = "half_open"
            log.info("circuit_half_open", breaker=self.name)
        if self.state == "half_open" and not self._probe_in_flight:
            self._probe_in_flight = True  # exactly one trial call
            return
        METRICS.incr(f"circuit_rejected.{self.name}")
        raise CircuitOpenError(f"circuit '{self.name}' is {self.state}")

    def record_success(self) -> None:
        if self.state != "closed":
            log.info("circuit_closed", breaker=self.name)
        self.state, self._failures, self._probe_in_flight = "closed", 0, False

    def record_failure(self) -> None:
        self._probe_in_flight = False
        self._failures += 1
        if self.state == "half_open" or self._failures >= self._threshold:
            self.state, self._opened_at = "open", self._clock()
            METRICS.incr(f"circuit_opened.{self.name}")
            log.warning("circuit_opened", breaker=self.name, failures=self._failures)

    def release(self) -> None:
        """Attempt was cancelled: free the probe slot without judging the dependency."""
        self._probe_in_flight = False


class RetryBudget:
    """Each request earns `ratio` retry tokens; each retry costs 1. Starts with a small float."""

    def __init__(self, ratio: float = 0.2, initial: float = 10.0, max_tokens: float = 50.0) -> None:
        self._ratio = ratio
        self._max = max_tokens
        self._tokens = initial

    def on_request(self) -> None:
        self._tokens = min(self._max, self._tokens + self._ratio)

    def try_spend(self) -> bool:
        if self._tokens >= 1:
            self._tokens -= 1
            return True
        return False
