import pytest

from po_agent.errors import CircuitOpenError
from po_agent.resilience import CircuitBreaker, TokenBucket


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_breaker_half_open_probe_then_close_or_reopen():
    clock = Clock()
    b = CircuitBreaker("x", failure_threshold=2, reset_timeout_s=10, clock=clock)
    b.allow()
    b.record_failure()
    b.allow()
    b.record_failure()
    assert b.state == "open"
    with pytest.raises(CircuitOpenError):
        b.allow()
    clock.now = 11
    b.allow()  # the single probe
    assert b.state == "half_open"
    with pytest.raises(CircuitOpenError):
        b.allow()  # second caller is rejected while the probe is in flight
    b.record_failure()  # probe failed -> open again
    assert b.state == "open"
    clock.now = 25
    b.allow()
    b.record_success()
    assert b.state == "closed"


async def test_token_bucket_waits_when_empty():
    clock, slept = Clock(), []

    async def sleep(d):
        slept.append(d)
        clock.now += d

    bucket = TokenBucket(rate_per_s=2, capacity=2, clock=clock, sleep=sleep)
    await bucket.acquire()
    await bucket.acquire()
    assert slept == []  # burst allowed
    await bucket.acquire()
    assert slept == [pytest.approx(0.5)]  # 2 tokens/s => 0.5s for the next one
