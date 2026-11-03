import pytest

from helpers import RecordingSleep, seeded
from po_agent.errors import CircuitOpenError, PermanentError, TransientError
from po_agent.resilience import CircuitBreaker, RetryBudget
from po_agent.retry import RetryPolicy, backoff_delay, retry_async

POLICY = RetryPolicy(max_attempts=4, base_delay_s=0.2, max_delay_s=1.0)


def test_full_jitter_stays_within_exponential_cap():
    rng = seeded()
    for attempt, cap in [(1, 0.2), (2, 0.4), (3, 0.8), (4, 1.0), (9, 1.0)]:
        delays = [backoff_delay(POLICY, attempt, rng) for _ in range(200)]
        assert all(0 <= d <= cap for d in delays)
        assert max(delays) > cap * 0.8  # actually uses the range
        assert min(delays) < cap * 0.2  # ...including small values (that is the jitter)


async def test_retries_transient_then_succeeds():
    calls, sleep = [], RecordingSleep()

    async def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise TransientError("503")
        return "ok"

    assert await retry_async("t", flaky, policy=POLICY, sleep=sleep, rng=seeded()) == "ok"
    assert len(calls) == 3 and len(sleep.delays) == 2


async def test_permanent_error_is_not_retried():
    calls = []

    async def bad():
        calls.append(1)
        raise PermanentError("nope")

    with pytest.raises(PermanentError):
        await retry_async("t", bad, policy=POLICY, sleep=RecordingSleep())
    assert len(calls) == 1


async def test_gives_up_after_max_attempts():
    async def always():
        raise TransientError("503")

    with pytest.raises(TransientError):
        await retry_async("t", always, policy=POLICY, sleep=RecordingSleep())


async def test_timeout_counts_as_transient():
    import asyncio

    calls = []

    async def slow():
        calls.append(1)
        await asyncio.sleep(1)

    with pytest.raises(TransientError, match="timed out"):
        await retry_async("t", slow, policy=RetryPolicy(2, 0.001, 0.001), timeout_s=0.01)
    assert len(calls) == 2


async def test_retry_budget_stops_retry_storms():
    async def always():
        raise TransientError("503")

    budget = RetryBudget(ratio=0.0, initial=1.0)  # exactly one retry allowed in total
    attempts = []

    async def counted():
        attempts.append(1)
        await always()

    for _ in range(3):
        with pytest.raises(TransientError):
            await retry_async("t", counted, policy=POLICY, sleep=RecordingSleep(), budget=budget)
    # 3 first attempts + only 1 retry across all of them (not 3 * 3 retries)
    assert len(attempts) == 4


async def test_open_circuit_fails_fast_without_calling():
    breaker = CircuitBreaker("t", failure_threshold=2, reset_timeout_s=60)
    calls = []

    async def always():
        calls.append(1)
        raise TransientError("503")

    with pytest.raises((TransientError, CircuitOpenError)):
        await retry_async("t", always, policy=POLICY, sleep=RecordingSleep(), breaker=breaker)
    assert breaker.state == "open"
    n = len(calls)
    with pytest.raises(CircuitOpenError):
        await retry_async("t", always, policy=POLICY, sleep=RecordingSleep(), breaker=breaker)
    assert len(calls) == n  # the sick service was not touched
