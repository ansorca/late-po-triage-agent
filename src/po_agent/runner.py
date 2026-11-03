"""Admission control for many simultaneous sessions.

    request -> [ queue (bounded) ] -> [ N workers (semaphore) ] -> agent.run
                    |
                    +-- full? shed immediately with status="shed" (fail fast, tell the client)

Shedding early is kinder than letting every request wait forever and time out together.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable

from .agent import TriageAgent
from .config import Settings
from .logging import get_logger, log_context
from .metrics import METRICS
from .models import TriageRequest, TriageResponse
from .security import validate_request

log = get_logger(__name__)


class SessionRunner:
    def __init__(self, agent: TriageAgent, settings: Settings) -> None:
        self._agent = agent
        self._settings = settings
        self._slots = asyncio.Semaphore(settings.max_concurrent_sessions)
        self._waiting = 0
        self._per_principal: dict[str, int] = {}  # sessions queued or running per user

    async def handle(self, request: TriageRequest) -> TriageResponse:
        problem = validate_request(request, self._settings)
        if problem:
            METRICS.incr("session.rejected")
            log.warning("request_rejected", reason=problem)  # no client-supplied ids in the log
            return TriageResponse(request.request_id[:64], "rejected", f"Rejected: {problem}.")

        principal = request.planner_id
        with log_context(request_id=request.request_id, principal=principal):
            if self._per_principal.get(principal, 0) >= self._settings.max_sessions_per_principal:
                METRICS.incr("session.shed_user")
                log.warning("session_shed", reason="per_user_limit")
                return TriageResponse(
                    request.request_id, "shed", "Too many requests in progress for this user."
                )
            if self._slots.locked() and self._waiting >= self._settings.max_queue_depth:
                METRICS.incr("session.shed")
                log.warning("session_shed", reason="queue_full", queue_depth=self._waiting)
                return TriageResponse(
                    request.request_id, "shed", "System is busy. Please retry shortly."
                )

            self._per_principal[principal] = self._per_principal.get(principal, 0) + 1
            try:
                return await self._admit_and_run(request)
            finally:
                self._per_principal[principal] -= 1
                if not self._per_principal[principal]:
                    del self._per_principal[principal]

    async def _admit_and_run(self, request: TriageRequest) -> TriageResponse:
        queued_at = time.perf_counter()
        self._waiting += 1
        try:
            await self._slots.acquire()
        finally:
            self._waiting -= 1
        METRICS.observe("queue_wait_s", time.perf_counter() - queued_at)
        try:
            async with asyncio.timeout(self._settings.session_timeout_s):
                return await self._agent.run(request)
        except TimeoutError:
            METRICS.incr("session.timeout")
            log.error("session_timeout", limit_s=self._settings.session_timeout_s)
            return TriageResponse(
                request.request_id, "timeout", "Took too long; retry to resume where it stopped."
            )
        except Exception:
            log.exception("session_crashed")
            METRICS.incr("session.crashed")
            return TriageResponse(request.request_id, "failed", "Unexpected internal error.")
        finally:
            self._slots.release()

    async def run_many(self, requests: Iterable[TriageRequest]) -> list[TriageResponse]:
        # handle() never raises, so one bad session cannot cancel its siblings.
        return list(await asyncio.gather(*(self.handle(r) for r in requests)))
