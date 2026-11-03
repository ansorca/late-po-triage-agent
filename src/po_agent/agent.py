"""The agent loop: ask the model what to do, run the tools it asks for, repeat until it answers.

The agent object holds NO per-session state. Everything about a conversation lives in local
variables and in the SessionStore, so thousands of sessions can share one instance safely.
"""

from __future__ import annotations

import asyncio
import time
import uuid

from .config import Settings
from .errors import AgentError
from .llm.base import LLMClient
from .logging import get_logger, log_context
from .metrics import METRICS
from .models import Message, TriageRequest, TriageResponse
from .resilience import CircuitBreaker, RetryBudget, TokenBucket
from .retry import retry_async
from .session_store import InMemorySessionStore, SessionState, SessionStore
from .tools import ToolExecutor

log = get_logger(__name__)

SYSTEM_PROMPT = (
    "You are a purchasing assistant that triages late purchase orders for planners. "
    "Use the tools to gather facts; never invent data. Rank orders by risk, say why, and "
    "request an expedite only when stock cover is low. If a tool fails, say what is unknown."
)


class TriageAgent:
    def __init__(
        self,
        llm: LLMClient,
        executor: ToolExecutor,
        settings: Settings,
        sessions: SessionStore | None = None,
        breaker: CircuitBreaker | None = None,
        budget: RetryBudget | None = None,
        limiter: TokenBucket | None = None,
    ) -> None:
        self._llm = llm
        self._executor = executor
        self._settings = settings
        self._sessions = sessions or InMemorySessionStore()
        self._breaker = breaker or CircuitBreaker(
            "llm", settings.breaker_failure_threshold, settings.breaker_reset_s
        )
        self._budget = budget or RetryBudget()
        self._limiter = limiter or TokenBucket(settings.llm_requests_per_s, settings.llm_burst)

    async def run(self, request: TriageRequest) -> TriageResponse:
        trace_id = uuid.uuid4().hex[:16]
        with log_context(
            request_id=request.request_id, trace_id=trace_id, principal=request.planner_id
        ):
            started = time.perf_counter()
            log.info("session_started", planner_id=request.planner_id)
            response = await self._run(request, trace_id)
            elapsed = time.perf_counter() - started
            METRICS.incr(f"session.{response.status}")
            METRICS.observe("session_latency_s", elapsed)
            log.info(
                "session_finished",
                status=response.status,
                turns=response.turns,
                tool_calls=response.tool_calls,
                tokens=response.tokens,
                duration_s=round(elapsed, 3),
            )
            return response

    async def _run(self, request: TriageRequest, trace_id: str) -> TriageResponse:
        s = self._settings
        rid = request.request_id
        # Scoped by principal: a guessed request_id from another user finds nothing.
        skey = f"{request.planner_id}:{rid}"
        principal = request.planner_id
        state = await self._sessions.load(skey) or SessionState(
            messages=[Message("user", text=request.question)]
        )
        if state.final is not None:
            log.info("session_replayed")  # duplicate submission of a finished request
            METRICS.incr("session.replayed")
            return state.final

        def finish(status: str, answer: str, *notes: str) -> TriageResponse:
            return TriageResponse(
                rid,
                status,
                answer,
                state.turns,
                state.tool_calls,
                state.tokens,
                trace_id,
                list(notes),  # type: ignore[arg-type]
            )

        specs = self._executor.specs()
        while state.turns < s.max_turns:
            if state.tokens >= s.max_tokens_per_session:
                METRICS.incr("token_budget_exhausted")
                log.warning("token_budget_exhausted", tokens=state.tokens)
                return finish("incomplete", "Stopped: token budget for this request was used up.")

            llm_started = time.perf_counter()
            try:
                reply = await retry_async(
                    "llm",
                    lambda: self._llm.complete(SYSTEM_PROMPT, state.messages, specs),
                    policy=s.retry,
                    timeout_s=s.llm_timeout_s,
                    breaker=self._breaker,
                    budget=self._budget,
                    limiter=self._limiter,
                )
            except AgentError as exc:
                log.error("llm_failed", error=str(exc), kind=type(exc).__name__)
                return finish("failed", "The assistant is temporarily unavailable. Please retry.")
            except Exception:
                log.exception("llm_crashed")
                return finish("failed", "Unexpected internal error.")
            finally:
                METRICS.observe("llm_latency_s", time.perf_counter() - llm_started)

            state.turns += 1
            state.tokens += reply.input_tokens + reply.output_tokens
            state.messages.append(Message("assistant", reply.text, reply.tool_calls))

            if not reply.tool_calls and reply.truncated:
                log.warning("answer_truncated", tokens=state.tokens)
                METRICS.incr("answer_truncated")
                return finish("incomplete", reply.text or "", "answer cut off at the output limit")
            if not reply.tool_calls:
                response = finish("ok", reply.text or "")
                state.final = response
                await self._sessions.save(skey, state)
                return response

            # Independent tool calls run concurrently; execute() never raises.
            results = await asyncio.gather(
                *(
                    self._executor.execute(rid, call, principal=principal)
                    for call in reply.tool_calls
                )
            )
            state.tool_calls += len(results)
            state.messages.append(Message("user", tool_results=tuple(results)))
            await self._sessions.save(skey, state)  # checkpoint: a resubmit resumes from here

        METRICS.incr("max_turns_reached")
        log.warning("max_turns_reached", turns=state.turns)
        return finish("incomplete", "Stopped: too many steps without a final answer.")
