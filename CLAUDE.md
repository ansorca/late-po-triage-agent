# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Uses `uv` (Python >= 3.12). No AWS account or network is needed for anything below.

- `make install` - `uv sync --all-extras`
- `make test` - full suite (`uv run pytest -q`); single test: `uv run pytest tests/test_executor.py::test_write_tool_runs_once_even_if_called_three_times_in_parallel -q`
- `make lint` / `make fmt` - ruff check + format (line length 100)
- `make demo` - offline multi-session run against a flaky stub backend; `uv run python -m po_agent --help` for flags (`--duplicate`, `--failure-rate`, `--seed`, `--concurrency`, `--queue`)

## Architecture

Read SPEC.md (requirements, failure-mode table R1-R15) and ARCHITECTURE.md (diagrams, retry stack, production swaps) before changing behaviour. README.md is the learner-facing explanation.

Request path: `SessionRunner` (queue, slots, shedding, timeout) -> `TriageAgent` (loop, turn/token budgets, checkpoints in `SessionStore`) -> `LLMClient` and `ToolExecutor`. `ToolExecutor` is where most reliability logic meets: schema validation, read cache, idempotency for `side_effecting` tools, per-tool bulkhead/breaker/retry budget, then `retry_async`.

Invariants to preserve:
- `ToolExecutor.execute` and `SessionRunner.handle` never raise; failures become error `ToolResult`s / non-`ok` `TriageResponse`s.
- Errors must be classified `TransientError` (retry) or `PermanentError` (don't). `CircuitOpenError` is transient but is never retried inside the same call.
- Failures are never cached (idempotency store, TTL cache).
- The agent object holds no per-session state; conversation state lives in `SessionStore` keyed by `request_id`.
- Idempotency key = hash(request_id, tool, canonical args); write tools must forward `ctx.idempotency_key` to the backend.
- `TriageRequest.planner_id` is the authenticated principal. Session keys are `"<principal>:<request_id>"`, idempotency keys include the principal, and `ToolExecutor.execute(..., principal=)` requires it. Never derive the principal from model output.
- Tool args are validated (strict schemas) then authorized (`Authorizer`, fail closed) before running; write attempts go to the `po_agent.audit` logger. See THREAT_MODEL.md.
- Logging context (`request_id`, `trace_id`) is set via `log_context` (a ContextVar); use `get_logger(...).info("event_name", **fields)`, not f-string messages.

Everything external (LLM, stores, backend) is behind an interface with an in-memory/stub implementation. `llm/bedrock.py` is only tested against a fake client (and real botocore config), never real AWS; it must keep boto3's own retries disabled (`total_max_attempts=1`) and must never return a tool call from a `max_tokens`-truncated response. Circuit breaker, limiter and retry budget are per-process.

## Testing notes

Tests use `tests/helpers.py::build()` (tiny retry delays, unlimited rate limiter) and an autouse fixture resetting the global `METRICS`. Regression scenarios live in `tests/evals/cases.json`.
