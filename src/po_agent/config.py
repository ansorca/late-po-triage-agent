"""All tunables in one place. Every limit here is a reliability knob (see ARCHITECTURE.md)."""

from dataclasses import dataclass, field

from .retry import RetryPolicy


@dataclass(frozen=True)
class Settings:
    # --- load control -------------------------------------------------------------------
    max_concurrent_sessions: int = 8  # conversations running at once
    max_queue_depth: int = 32  # waiting beyond this => new requests are shed immediately
    session_timeout_s: float = 30.0  # hard ceiling for one whole conversation
    max_sessions_per_principal: int = 4  # fairness: one user cannot fill the queue
    max_question_chars: int = 4_000  # input size cap (cost and abuse control)

    # --- cost / runaway protection ------------------------------------------------------
    max_turns: int = 8  # LLM round trips per session
    max_tokens_per_session: int = 20_000  # input+output tokens per session

    # --- LLM ----------------------------------------------------------------------------
    llm_timeout_s: float = 20.0  # per attempt
    llm_requests_per_s: float = 20.0  # client-side limiter; set below your Bedrock quota
    llm_burst: float = 20.0

    # --- tools --------------------------------------------------------------------------
    tool_timeout_s: float = 5.0  # per attempt
    default_tool_concurrency: int = 8  # bulkhead size per tool
    tool_concurrency: dict[str, int] = field(default_factory=dict)  # per-tool overrides

    # --- retries & circuit breaking -----------------------------------------------------
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    breaker_failure_threshold: int = 5
    breaker_reset_s: float = 10.0
