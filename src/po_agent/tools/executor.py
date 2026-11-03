"""Runs one tool call safely. The contract: `execute` NEVER raises (except cancellation).

Every failure becomes a ToolResult(is_error=True) the model can read and react to. That keeps
one broken backend from crashing a whole conversation.

Per-tool isolation: each tool gets its own bulkhead (semaphore), circuit breaker and retry
budget, so a slow `get_inventory` cannot starve `get_supplier_info`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass

from ..cache import TTLCache
from ..config import Settings
from ..errors import CircuitOpenError, PermanentError, PermissionDenied, TransientError
from ..idempotency import IdempotencyStore
from ..logging import get_logger
from ..metrics import METRICS
from ..models import ToolCall, ToolContext, ToolResult, ToolSpec
from ..resilience import CircuitBreaker, RetryBudget
from ..retry import retry_async
from ..security import AllowAll, Authorizer
from .registry import ToolRegistry, validate_arguments

log = get_logger(__name__)
audit = get_logger("po_agent.audit")  # who did what; ship to an append-only sink in production


def _canonical(arguments: dict) -> str:
    return json.dumps(arguments, sort_keys=True, separators=(",", ":"))


def idempotency_key(principal: str, request_id: str, call: ToolCall) -> str:
    """Same user + request + tool + arguments => same key, even if the model re-asks."""
    raw = f"{principal}|{request_id}|{call.name}|{_canonical(call.arguments)}"
    digest = hashlib.sha256(raw.encode())
    return digest.hexdigest()[:32]


@dataclass
class _Dependency:
    bulkhead: asyncio.Semaphore
    breaker: CircuitBreaker
    budget: RetryBudget


class ToolExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        store: IdempotencyStore,
        settings: Settings,
        cache: TTLCache | None = None,
        authorizer: Authorizer | None = None,
    ) -> None:
        self._registry = registry
        self._store = store
        self._settings = settings
        self._cache = cache or TTLCache()
        self._authorizer = authorizer or AllowAll()
        self._deps: dict[str, _Dependency] = {}

    def specs(self) -> list[ToolSpec]:
        return self._registry.specs()

    def _dep(self, name: str) -> _Dependency:
        if name not in self._deps:
            s = self._settings
            self._deps[name] = _Dependency(
                asyncio.Semaphore(s.tool_concurrency.get(name, s.default_tool_concurrency)),
                CircuitBreaker(f"tool:{name}", s.breaker_failure_threshold, s.breaker_reset_s),
                RetryBudget(),
            )
        return self._deps[name]

    async def execute(self, request_id: str, call: ToolCall, *, principal: str) -> ToolResult:
        """Run one tool call on behalf of `principal`. Never raises."""
        result = await self._execute(request_id, call, principal)
        tool = self._registry.get(call.name)
        if tool is not None and tool.spec.side_effecting:
            audit.info(
                "write_attempt",
                principal=principal,
                tool=call.name,
                arguments=str(call.arguments)[:500],
                idempotency_key=idempotency_key(principal, request_id, call),
                outcome=result.content["error"] if result.is_error else "ok",
            )
        return result

    async def _execute(self, request_id: str, call: ToolCall, principal: str) -> ToolResult:
        tool = self._registry.get(call.name)
        if tool is None:
            return self._error(call, "unknown_tool", f"no tool named '{call.name}'", False)
        try:
            validate_arguments(tool.spec, call.arguments)
        except PermanentError as exc:
            return self._error(call, "invalid_arguments", str(exc), False)

        try:
            await self._authorizer.authorize(principal, call.name, call.arguments)
        except PermissionDenied:
            METRICS.incr(f"authz_denied.{call.name}")
            return self._error(call, "forbidden", "not permitted for this user", False)
        except Exception:
            log.exception("authorizer_crashed", tool=call.name)  # fail closed
            return self._error(call, "internal_error", "authorization check failed", False)

        key = idempotency_key(principal, request_id, call) if tool.spec.side_effecting else None
        ctx = ToolContext(
            principal=principal, request_id=request_id, call_id=call.id, idempotency_key=key
        )
        ttl = tool.spec.cache_ttl_s

        async def run_tool() -> dict:
            return await tool.run(call.arguments, ctx)

        async def attempt() -> dict:
            if key is not None:
                result, replayed = await self._store.run_once(key, run_tool)
                if replayed:
                    METRICS.incr(f"idempotent_replay.{call.name}")
                    log.info("tool_replayed", tool=call.name, idempotency_key=key)
                return result
            if ttl > 0:
                scope = principal if tool.spec.cache_per_principal else "*"
                value, hit = await self._cache.get_or_load(
                    f"{scope}|{call.name}|{_canonical(call.arguments)}", ttl, run_tool
                )
                METRICS.incr(f"cache.{'hit' if hit else 'miss'}.{call.name}")
                return value
            return await run_tool()

        dep = self._dep(call.name)
        started = time.perf_counter()
        try:
            async with dep.bulkhead:
                content = await retry_async(
                    f"tool:{call.name}",
                    attempt,
                    policy=self._settings.retry,
                    timeout_s=self._settings.tool_timeout_s,
                    breaker=dep.breaker,
                    budget=dep.budget,
                )
        except CircuitOpenError as exc:
            log.warning("circuit_open", tool=call.name, detail=str(exc))
            return self._error(call, "circuit_open", "service temporarily unavailable", True)
        except TransientError as exc:
            # Backend details stay in the logs; the model (and so the user) gets a generic message.
            log.warning("tool_backend_error", tool=call.name, detail=str(exc))
            return self._error(call, "backend_unavailable", "service temporarily unavailable", True)
        except PermanentError as exc:
            return self._error(call, "rejected", str(exc)[:200], False)
        except Exception:
            log.exception("tool_crashed", tool=call.name)
            return self._error(call, "internal_error", "tool crashed unexpectedly", False)
        finally:
            METRICS.observe(f"tool_latency_s.{call.name}", time.perf_counter() - started)

        METRICS.incr(f"tool_ok.{call.name}")
        log.info("tool_ok", tool=call.name, call_id=call.id)
        return ToolResult(call.id, call.name, content)

    @staticmethod
    def _error(call: ToolCall, code: str, message: str, retryable: bool) -> ToolResult:
        METRICS.incr(f"tool_error.{call.name}.{code}")
        log.warning("tool_failed", tool=call.name, code=code, error=message, retryable=retryable)
        body = {"error": code, "message": message, "retryable": retryable}
        return ToolResult(call.id, call.name, body, is_error=True)
