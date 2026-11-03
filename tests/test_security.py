import asyncio
import json

import pytest

from helpers import build
from po_agent.idempotency import InMemoryIdempotencyStore
from po_agent.models import LLMResponse, ToolCall, TriageRequest
from po_agent.security import PoOwnershipAuthorizer
from po_agent.session_store import InMemorySessionStore, SessionState
from po_agent.tools.stub_backend import StubBackend


def req(rid="r1", planner="alice", q="triage"):
    return TriageRequest(rid, planner, q)


# --- Spoofing: sessions are scoped to the authenticated principal -----------------------------
async def test_other_user_cannot_read_or_resume_my_session_by_guessing_request_id():
    backend, _, runner, _ = build()
    mine = await runner.handle(req("shared-id", "alice"))
    calls = backend.calls
    theirs = await runner.handle(req("shared-id", "mallory"))
    assert mine.status == theirs.status == "ok"
    assert backend.calls > calls  # mallory got a fresh run, not alice's stored answer
    assert backend.expedite_executions == 4  # distinct writes per principal, not shared


@pytest.mark.parametrize(
    "bad",
    [req("a:b"), req("x" * 65), req("has space"), req(planner="a:b"), req(planner=""), req(q="  ")],
)
async def test_malformed_requests_are_rejected_before_any_work(bad):
    backend, _, runner, _ = build()
    r = await runner.handle(bad)
    assert r.status == "rejected" and backend.calls == 0


async def test_oversized_question_is_rejected():
    _, _, runner, _ = build(max_question_chars=100)
    assert (await runner.handle(req(q="x" * 101))).status == "rejected"


# --- Tampering: tool arguments are strictly validated -----------------------------------------
@pytest.mark.parametrize(
    "tool,args",
    [
        ("propose_expedite", {"po_id": "PO-1001; DROP TABLE"}),
        ("propose_expedite", {"po_id": "PO-1001", "force": True}),
        ("propose_expedite", {"po_id": "A" * 33}),
        ("list_late_orders", {"min_days_late": -1}),
        ("list_late_orders", {"min_days_late": 10**6}),
    ],
)
async def test_hostile_arguments_never_reach_the_backend(tool, args):
    backend, agent, _, _ = build()
    r = await agent._executor.execute("r", ToolCall("1", tool, args), principal="alice")
    assert r.is_error and r.content["error"] == "invalid_arguments"
    assert backend.calls == 0


# --- Elevation of privilege: per-user authorization on tool calls -----------------------------
async def test_injected_expedite_for_someone_elses_po_is_blocked_and_audited(caplog):
    # Simulates prompt injection: the model is steered into expediting a PO alice doesn't own.
    class Injected:
        async def complete(self, system, messages, tools):
            if any(m.tool_results for m in messages):
                return LLMResponse("done")
            return LLMResponse(None, (ToolCall("c1", "propose_expedite", {"po_id": "PO-9999"}),))

    backend, agent, runner, settings = build(llm=Injected())
    agent._executor._authorizer = PoOwnershipAuthorizer({"alice": frozenset({"PO-1001"})})
    with caplog.at_level("INFO", logger="po_agent.audit"):
        r = await runner.handle(req())
    assert r.status == "ok"
    assert backend.expedite_executions == 0
    audits = [x for x in caplog.records if x.getMessage() == "write_attempt"]
    assert audits and audits[0].fields["outcome"] == "forbidden"
    assert audits[0].fields["principal"] == "alice"


async def test_owner_can_still_expedite_and_write_is_audited(caplog):
    backend, agent, _, _ = build()
    agent._executor._authorizer = PoOwnershipAuthorizer({"alice": frozenset({"PO-1001"})})
    with caplog.at_level("INFO", logger="po_agent.audit"):
        r = await agent._executor.execute(
            "r", ToolCall("1", "propose_expedite", {"po_id": "PO-1001"}), principal="alice"
        )
    assert not r.is_error and backend.expedite_executions == 1
    rec = [x for x in caplog.records if x.getMessage() == "write_attempt"][0]
    assert rec.fields["outcome"] == "ok" and rec.fields["idempotency_key"]


async def test_authorizer_crash_fails_closed():
    class Broken:
        async def authorize(self, *a):
            raise RuntimeError("policy service down")

    backend, agent, _, _ = build()
    agent._executor._authorizer = Broken()
    call = ToolCall("1", "propose_expedite", {"po_id": "PO-1001"})
    r = await agent._executor.execute("r", call, principal="alice")
    assert r.is_error and backend.expedite_executions == 0


# --- Information disclosure ---------------------------------------------------------------------
async def test_backend_error_details_are_not_exposed_to_the_model():
    _, agent, _, _ = build(StubBackend(failure_rate=1.0, seed=1))
    r = await agent._executor.execute(
        "r", ToolCall("1", "get_inventory", {"sku": "SKU-100"}), principal="alice"
    )
    assert r.is_error and "simulated" not in json.dumps(r.content)


async def test_per_principal_cache_scope_does_not_leak_between_users():
    from dataclasses import replace

    backend, agent, _, _ = build()
    tool = agent._executor._registry.get("get_inventory")
    agent._executor._registry._tools["get_inventory"] = type(tool)(
        replace(tool.spec, cache_per_principal=True), tool.run
    )
    for who in ("alice", "bob", "alice"):
        await agent._executor.execute(
            "r", ToolCall("1", "get_inventory", {"sku": "SKU-100"}), principal=who
        )
    assert backend.calls == 2  # one per user; alice's second call hit her own cache


# --- Denial of service ------------------------------------------------------------------------
async def test_one_user_cannot_fill_the_queue():
    class Slow:
        async def complete(self, *a, **k):
            await asyncio.sleep(0.05)
            return LLMResponse("done")

    _, _, runner, _ = build(llm=Slow(), max_sessions_per_principal=2, max_queue_depth=50)
    noisy = [req(f"n{i}", "noisy") for i in range(10)]
    polite = [req("p1", "polite")]
    out = await runner.run_many(noisy + polite)
    assert [r.status for r in out[:10]].count("shed") == 8
    assert out[-1].status == "ok"  # the other user is unaffected


async def test_user_quota_is_released_after_each_session():
    _, _, runner, _ = build(max_sessions_per_principal=1)
    for i in range(3):
        assert (await runner.handle(req(f"s{i}", "alice"))).status == "ok"
    assert runner._per_principal == {}


async def test_session_store_expires_and_evicts():
    now = [0.0]
    store = InMemorySessionStore(max_entries=2, ttl_s=10, clock=lambda: now[0])
    for k in ("a", "b", "c"):
        await store.save(k, SessionState())
    assert await store.load("a") is None  # evicted (oldest past the cap)
    assert await store.load("c") is not None
    now[0] = 11
    assert await store.load("c") is None  # expired


async def test_idempotency_store_expires_and_evicts():
    now, runs = [0.0], []
    store = InMemoryIdempotencyStore(max_entries=2, ttl_s=10, clock=lambda: now[0])

    async def work():
        runs.append(1)
        return {"v": 1}

    for k in ("a", "b", "c"):
        await store.run_once(k, work)
    assert (await store.run_once("c", work))[1] is True  # replay
    await store.run_once("a", work)  # evicted => runs again
    assert len(runs) == 4
    now[0] = 11
    assert (await store.run_once("c", work))[1] is False  # expired => runs again
