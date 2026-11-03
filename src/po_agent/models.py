"""Plain data types shared by every layer. Provider-neutral on purpose."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    side_effecting: bool = False  # True => writes somewhere, must be idempotent
    cache_ttl_s: float = 0.0  # >0 => read-only lookup whose result may be reused this long
    cache_per_principal: bool = False  # True => cached results are not shared across users


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    tool_call_id: str
    name: str
    content: dict[str, Any]
    is_error: bool = False


@dataclass(frozen=True)
class Message:
    role: Literal["user", "assistant"]
    text: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    tool_results: tuple[ToolResult, ...] = ()


@dataclass(frozen=True)
class LLMResponse:
    text: str | None
    tool_calls: tuple[ToolCall, ...] = ()
    input_tokens: int = 0
    output_tokens: int = 0
    truncated: bool = False  # model hit its output limit before finishing


@dataclass(frozen=True)
class ToolContext:
    principal: str  # authenticated caller; handlers must act on their behalf only
    request_id: str
    call_id: str
    idempotency_key: str | None = None


ToolHandler = Callable[[dict[str, Any], ToolContext], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class Tool:
    spec: ToolSpec
    run: ToolHandler


@dataclass(frozen=True)
class TriageRequest:
    request_id: str  # client-chosen; resubmitting the same id must be safe
    planner_id: (
        str  # the AUTHENTICATED principal; must be set by the API layer, never by the client
    )
    question: str


@dataclass(frozen=True)
class TriageResponse:
    request_id: str
    status: Literal["ok", "incomplete", "failed", "timeout", "shed", "rejected"]
    answer: str
    turns: int = 0
    tool_calls: int = 0
    tokens: int = 0
    trace_id: str = ""
    notes: list[str] = field(default_factory=list)
