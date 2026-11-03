"""Conversation state lives OUTSIDE the agent, keyed by request_id.

Why: (1) sessions cannot see each other's data because the agent object holds no per-session
state; (2) if a worker dies or a request times out, the retry resumes from the saved history
instead of starting over (tools are idempotent, so replaying is safe); (3) any worker in a fleet
can continue any session once this is backed by DynamoDB / Redis.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from .models import Message, TriageResponse


@dataclass
class SessionState:
    messages: list[Message] = field(default_factory=list)
    turns: int = 0
    tool_calls: int = 0
    tokens: int = 0
    final: TriageResponse | None = None  # set only for successful sessions (enables replay)


class SessionStore(Protocol):
    async def load(self, key: str) -> SessionState | None: ...
    async def save(self, key: str, state: SessionState) -> None: ...


class InMemorySessionStore:
    """Bounded: sessions expire `ttl_s` after their last save; oldest evicted past `max_entries`.

    Keys are built by the caller as "<principal>:<request_id>" so users cannot read each other's
    sessions by guessing a request_id.
    """

    def __init__(
        self,
        max_entries: int = 10_000,
        ttl_s: float = 3_600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max = max_entries
        self._ttl = ttl_s
        self._clock = clock
        self._data: dict[str, tuple[float, SessionState]] = {}

    @staticmethod
    def _copy(state: SessionState) -> SessionState:
        # Hand out copies, like a real network store would (no shared mutable state).
        return SessionState(
            list(state.messages), state.turns, state.tool_calls, state.tokens, state.final
        )

    async def load(self, key: str) -> SessionState | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        saved_at, state = entry
        if self._clock() - saved_at > self._ttl:
            del self._data[key]
            return None
        return self._copy(state)

    async def save(self, key: str, state: SessionState) -> None:
        self._data.pop(key, None)  # re-insert so insertion order == recency
        self._data[key] = (self._clock(), self._copy(state))
        while len(self._data) > self._max:
            del self._data[next(iter(self._data))]
