import asyncio

from helpers import build
from po_agent.errors import PermanentError
from po_agent.llm.fake import FakeLLM
from po_agent.models import LLMResponse, ToolCall, TriageRequest
from po_agent.tools.stub_backend import StubBackend


def req(i: int | str = 0, q: str = "triage") -> TriageRequest:
    return TriageRequest(f"req-{i}", f"planner-{i}", q)


async def test_happy_path_expedites_critical_orders_only():
    backend, _, runner, _ = build()
    r = await runner.handle(req())
    assert r.status == "ok" and r.trace_id
    assert "[CRITICAL] PO-1001" in r.answer and "[CRITICAL] PO-1003" in r.answer
    assert "[WARNING] PO-1004" in r.answer
    assert backend.expedite_executions == 2


async def test_survives_flaky_backend_and_flaky_llm():
    backend, _, runner, _ = build(
        StubBackend(failure_rate=0.2, seed=3), llm=FakeLLM(transient_failures=2)
    )
    r = await runner.handle(req())
    assert r.status == "ok"


async def test_permanent_llm_failure_returns_failed_not_exception():
    class Denied:
        async def complete(self, *a, **k):
            raise PermanentError("AccessDenied")

    _, _, runner, _ = build(llm=Denied())
    r = await runner.handle(req())
    assert r.status == "failed" and "unavailable" in r.answer


async def test_max_turns_stops_runaway_loop():
    class Loop:
        async def complete(self, system, messages, tools):
            n = len(messages)
            return LLMResponse(None, (ToolCall(f"c{n}", "get_inventory", {"sku": "SKU-100"}),))

    _, _, runner, _ = build(llm=Loop(), max_turns=3)
    r = await runner.handle(req())
    assert r.status == "incomplete" and r.turns == 3


async def test_token_budget_stops_session():
    class Hungry:
        async def complete(self, system, messages, tools):
            return LLMResponse(
                None,
                (ToolCall(f"c{len(messages)}", "get_inventory", {"sku": "SKU-100"}),),
                900,
                200,
            )

    _, _, runner, _ = build(llm=Hungry(), max_tokens_per_session=2000)
    r = await runner.handle(req())
    assert r.status == "incomplete" and r.turns == 2 and "token" in r.answer


async def test_resubmitting_finished_request_replays_without_new_side_effects():
    backend, agent, runner, _ = build()
    first = await runner.handle(req(1))
    calls_after_first = backend.calls
    second = await runner.handle(req(1))
    assert second.answer == first.answer
    assert backend.calls == calls_after_first  # nothing re-executed


async def test_resubmit_after_timeout_resumes_from_checkpoint():
    backend, agent, runner, settings = build(
        StubBackend(latency_s=0.05, seed=1), session_timeout_s=0.12
    )
    first = await runner.handle(req(7))
    assert first.status == "timeout"
    runner._settings = settings.__class__(**{**settings.__dict__, "session_timeout_s": 5})
    second = await runner.handle(req(7))
    assert second.status == "ok"
    assert backend.expedite_executions == 2  # each write happened exactly once overall


async def test_concurrency_is_bounded_and_sessions_do_not_cross_talk():
    running, peak = 0, 0

    class Echo:
        """Answers with its own question; a mixed-up history would echo someone else's."""

        async def complete(self, system, messages, tools):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.01)
            running -= 1
            return LLMResponse(f"echo:{messages[0].text}")

    _, _, runner, _ = build(llm=Echo(), max_concurrent_sessions=5, max_queue_depth=100)
    reqs = [req(i, f"question-{i}") for i in range(40)]
    out = await runner.run_many(reqs)
    assert peak <= 5
    assert all(r.status == "ok" and r.answer == f"echo:question-{i}" for i, r in enumerate(out))


async def test_overload_is_shed_not_queued_forever():
    class Slow:
        async def complete(self, *a, **k):
            await asyncio.sleep(0.05)
            return LLMResponse("done")

    _, _, runner, _ = build(llm=Slow(), max_concurrent_sessions=2, max_queue_depth=3)
    out = await runner.run_many([req(i) for i in range(20)])
    statuses = [r.status for r in out]
    assert statuses.count("ok") == 5 and statuses.count("shed") == 15


async def test_one_crashing_session_does_not_affect_others():
    class Sometimes:
        async def complete(self, system, messages, tools):
            if "boom" in messages[0].text:
                raise RuntimeError("bug")
            return LLMResponse("fine")

    _, _, runner, _ = build(llm=Sometimes())
    out = await runner.run_many([req(0, "ok"), req(1, "boom"), req(2, "ok")])
    assert [r.status for r in out] == ["ok", "failed", "ok"]


async def test_truncated_final_answer_is_reported_as_incomplete():
    class Cut:
        async def complete(self, *a, **k):
            return LLMResponse("The most urgent PO is", truncated=True)

    _, _, runner, _ = build(llm=Cut())
    r = await runner.handle(req())
    assert r.status == "incomplete" and r.notes
