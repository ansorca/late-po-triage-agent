import asyncio

import pytest

from po_agent.cache import TTLCache
from po_agent.errors import TransientError
from po_agent.idempotency import InMemoryIdempotencyStore


async def test_concurrent_same_key_runs_once():
    store, runs = InMemoryIdempotencyStore(), []

    async def work():
        runs.append(1)
        await asyncio.sleep(0.01)
        return {"v": 1}

    results = await asyncio.gather(*(store.run_once("k", work) for _ in range(10)))
    assert len(runs) == 1
    assert sum(1 for _, replayed in results if not replayed) == 1


async def test_failures_are_not_remembered():
    store, n = InMemoryIdempotencyStore(), []

    async def work():
        n.append(1)
        if len(n) == 1:
            raise TransientError("boom")
        return {"v": 1}

    with pytest.raises(TransientError):
        await store.run_once("k", work)
    assert (await store.run_once("k", work)) == ({"v": 1}, False)


async def test_cache_single_flight_and_ttl():
    now, loads = [0.0], []
    cache = TTLCache(clock=lambda: now[0])

    async def load():
        loads.append(1)
        await asyncio.sleep(0.01)
        return {"v": len(loads)}

    await asyncio.gather(*(cache.get_or_load("k", 10, load) for _ in range(20)))
    assert len(loads) == 1  # stampede protection
    assert (await cache.get_or_load("k", 10, load))[1] is True
    now[0] = 11
    assert (await cache.get_or_load("k", 10, load))[1] is False  # expired
    assert len(loads) == 2
