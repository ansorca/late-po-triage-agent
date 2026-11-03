"""Retry with exponential backoff and full jitter, guarded by limiter / breaker / budget.

Why jitter: if 100 sessions fail at the same moment and all wait exactly 1s, they all retry at
the same moment and fail again (a "thundering herd"). Picking a random wait in [0, cap] spreads
them out. This is the "Full Jitter" strategy from the AWS Architecture Blog.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .errors import TransientError
from .logging import get_logger
from .metrics import METRICS
from .resilience import CircuitBreaker, RetryBudget, TokenBucket

log = get_logger(__name__)


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 4
    base_delay_s: float = 0.2
    max_delay_s: float = 5.0

    def delay_cap(self, attempt: int) -> float:
        """Upper bound of the wait after failed attempt number `attempt` (1-based)."""
        return min(self.max_delay_s, self.base_delay_s * 2 ** (attempt - 1))


def backoff_delay(policy: RetryPolicy, attempt: int, rng: random.Random) -> float:
    return rng.uniform(0, policy.delay_cap(attempt))


_default_rng = random.Random()


async def retry_async[T](
    op: str,
    fn: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy,
    timeout_s: float | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rng: random.Random = _default_rng,
    breaker: CircuitBreaker | None = None,
    budget: RetryBudget | None = None,
    limiter: TokenBucket | None = None,
) -> T:
    """Run `fn`, retrying TransientError / timeouts.

    PermanentError and unknown exceptions propagate at once. CircuitOpenError (raised by the
    breaker) also propagates at once: retrying against an open circuit defeats its purpose.
    """
    if budget:
        budget.on_request()
    for attempt in range(1, policy.max_attempts + 1):
        if limiter:
            await limiter.acquire()
        if breaker:
            breaker.allow()
        try:
            if timeout_s is None:
                result = await fn()
            else:
                async with asyncio.timeout(timeout_s):
                    result = await fn()
        except TransientError as exc:
            err = exc
        except TimeoutError:
            err = TransientError(f"{op} timed out after {timeout_s}s")
        except Exception:
            if breaker:
                breaker.record_success()  # the dependency answered; the request was just bad
            raise
        except BaseException:
            if breaker:
                breaker.release()
            raise
        else:
            if breaker:
                breaker.record_success()
            return result

        if breaker:
            breaker.record_failure()
        METRICS.incr(f"transient_error.{op}")
        if attempt == policy.max_attempts:
            log.error("retry_exhausted", op=op, attempts=attempt, error=str(err))
            raise err
        if budget and not budget.try_spend():
            METRICS.incr(f"retry_budget_exhausted.{op}")
            log.warning("retry_budget_exhausted", op=op, attempt=attempt, error=str(err))
            raise err
        delay = backoff_delay(policy, attempt, rng)
        METRICS.incr(f"retries.{op}")
        log.warning(
            "retry_scheduled", op=op, attempt=attempt, delay_s=round(delay, 3), error=str(err)
        )
        await sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover
