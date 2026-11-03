from helpers import build
from po_agent.models import ToolCall
from po_agent.tools.stub_backend import StubBackend


async def test_unknown_tool_and_bad_args_become_error_results():
    _, agent, _, _ = build()
    ex = agent._executor
    r = await ex.execute("r1", ToolCall("1", "nope", {}), principal="p")
    assert r.is_error and r.content["error"] == "unknown_tool"
    r = await ex.execute("r1", ToolCall("2", "get_inventory", {}), principal="p")
    assert r.is_error and r.content["error"] == "invalid_arguments"
    r = await ex.execute("r1", ToolCall("3", "get_inventory", {"sku": 5}), principal="p")
    assert r.is_error and r.content["error"] == "invalid_arguments"


async def test_permanent_backend_error_is_reported_not_raised():
    _, agent, _, _ = build()
    r = await agent._executor.execute(
        "r1", ToolCall("1", "get_inventory", {"sku": "NOPE"}), principal="p"
    )
    assert r.is_error and r.content["retryable"] is False


async def test_write_tool_runs_once_even_if_called_three_times_in_parallel():
    import asyncio

    backend, agent, _, _ = build()
    call = ToolCall("1", "propose_expedite", {"po_id": "PO-1001"})
    results = await asyncio.gather(
        *(agent._executor.execute("r1", call, principal="p") for _ in range(3))
    )
    assert all(not r.is_error for r in results)
    assert backend.expedite_executions == 1


async def test_lost_reply_after_commit_does_not_duplicate_the_write():
    # failure_rate=1 on the first call only: write commits, reply "lost", retry must not re-write.
    backend = StubBackend(failure_rate=0.0, seed=1)
    _, agent, _, _ = build(backend)
    original = backend._flaky
    state = {"n": 0}

    async def flaky_once(op):
        state["n"] += 1
        if op == "expedite" and state["n"] == 1:
            from po_agent.errors import TransientError

            raise TransientError("reply lost")
        await original(op)

    backend._flaky = flaky_once
    r = await agent._executor.execute(
        "r1", ToolCall("1", "propose_expedite", {"po_id": "PO-1001"}), principal="p"
    )
    assert not r.is_error
    assert backend.expedite_executions == 1


async def test_read_tools_are_cached_across_sessions():
    backend, agent, _, _ = build()
    for rid in ("a", "b", "c"):
        await agent._executor.execute(
            rid, ToolCall("1", "get_supplier_info", {"supplier_id": "SUP-A"}), principal="p"
        )
    assert backend.calls == 1


async def test_tool_circuit_opens_and_stops_hammering_backend():
    backend, agent, _, _ = build(StubBackend(failure_rate=1.0, seed=1), breaker_failure_threshold=3)
    for i in range(5):
        await agent._executor.execute(
            "r", ToolCall(str(i), "get_inventory", {"sku": f"S{i}"}), principal="p"
        )
    codes = []
    r = await agent._executor.execute(
        "r", ToolCall("x", "get_inventory", {"sku": "SKU-100"}), principal="p"
    )
    codes.append(r.content["error"])
    assert codes == ["circuit_open"]
    assert backend.calls <= 4  # not 5 requests * 4 attempts
