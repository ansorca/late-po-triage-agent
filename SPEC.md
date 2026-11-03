# SPEC.md: Late Purchase Order Triage Agent

Status: skeleton v0.1. Runs fully offline; the real AWS Bedrock adapter exists but has only been tested against a fake client (no AWS account yet).

## 1. Goal

Build an AI agent that helps planners triage **late purchase orders (POs)**. A planner asks a question ("which late POs need attention?"); the agent consults business tools, decides what is risky, optionally requests an expedite from the supplier, and replies with a ranked, explained answer.

The project is a **reference skeleton**. The business logic is intentionally small; the point is the reliability engineering around it: concurrency, idempotency, retries, failure isolation, and observability. It also serves as practice for an assessment on Python, AWS Bedrock, agentic AI and AI usage quality.

## 2. Scope

| In scope | Out of scope (for now) |
|---|---|
| Agent loop (LLM <-> tools) with stub business tools | Real ERP/supplier integrations |
| Offline fake LLM + Bedrock Converse adapter | Prompt tuning, model evaluation with real models |
| Concurrent sessions with admission control | Authentication, API server, UI (hooks and threat model provided) |
| Idempotent writes, retries, circuit breaking, rate limiting | Durable stores (DynamoDB/Redis): only interfaces + in-memory versions |
| Structured logging, metrics, regression eval set | Metrics export to CloudWatch/Prometheus; distributed tracing |

## 3. Actors and use case

- **Planner** (human): submits `TriageRequest(request_id, planner_id, question)`; receives `TriageResponse`.
- **Agent**: runs the loop. **LLM**: decides which tools to call. **Tools**: `list_late_orders`, `get_supplier_info`, `get_inventory` (read-only) and `propose_expedite` (write).

Triage rules implemented by the fake LLM (a real model would be prompted to do the equivalent):
- stock cover (`on_hand / daily_demand`) below 5 days => **CRITICAL**, request expedite;
- otherwise 7+ days late => **WARNING**; otherwise **LOW**;
- missing data => say **UNKNOWN**; never invent numbers.

## 4. Functional requirements

| ID | Requirement |
|---|---|
| F1 | Given a question, the agent loops: call LLM -> execute requested tools -> feed results back -> until the LLM returns a final answer. |
| F2 | Tool calls requested in one LLM turn run concurrently. |
| F3 | Tool arguments from the model are validated against the tool's schema before execution. |
| F4 | Every tool failure is returned to the model as a structured error result (`error`, `message`, `retryable`), never as an exception that kills the session. |
| F5 | The LLM client is an interface (`LLMClient`); implementations: `FakeLLM` (offline) and `BedrockLLM` (Converse API). |
| F6 | The response carries a status: `ok`, `incomplete`, `failed`, `timeout`, `shed`, plus `trace_id`, turn/tool-call/token counts. |
| F7 | Resubmitting a finished `request_id` returns the stored answer; resubmitting an unfinished one resumes from its last checkpoint. |

## 5. Reliability requirements (failure modes)

Each failure mode has a mitigation, a place in the code, and a test.

| ID | Failure mode | Mitigation | Code | Test |
|---|---|---|---|---|
| R1 | Bedrock `ThrottlingException` / 5xx / timeouts | Exponential backoff with **full jitter**; **client-side token bucket** keeps us under quota | `retry.py`, `resilience.TokenBucket`, `llm/bedrock._classify` | `test_retry`, `test_resilience`, `test_bedrock_adapter` |
| R2 | Retry storms | **Circuit breaker** (fail fast), **retry budget** (retries capped as a fraction of traffic), **bounded queue + load shedding** | `resilience.py`, `runner.py` | `test_retry`, `test_agent_and_runner::overload` |
| R3 | Session cross-talk | Agent holds no per-session state; history lives in a `SessionStore` keyed by `request_id` (in-memory now, DynamoDB/Redis later) | `session_store.py`, `agent.py` | `test_concurrency_is_bounded_and_sessions_do_not_cross_talk` |
| R4 | Duplicate side effects | **Idempotency key** (hash of request_id + tool + args) on every write tool; in-flight de-duplication; key also sent to the backend | `idempotency.py`, `tools/executor.py` | `test_executor` (parallel + lost-reply) |
| R5 | Runaway loops and cost | `max_turns`, `max_tokens_per_session`, per-attempt LLM and tool timeouts, per-session timeout | `config.py`, `agent.py`, `runner.py` | `test_max_turns...`, `test_token_budget...` |
| R6 | Slow dependencies | **Bulkheads** (separate concurrency pool, breaker and retry budget per tool); **TTL cache with single-flight** for read-only lookups | `tools/executor.py`, `cache.py` | `test_executor` |
| R7 | Poor visibility | JSON logs with `request_id` + `trace_id` on every line; counters and latency histograms; regression **eval set** | `logging.py`, `metrics.py`, `tests/evals/` | `test_logging`, `test_evals` |

Further rules:
- R11: sessions and idempotency keys are scoped to the authenticated principal; ids are validated for shape and length.
- R12: tool arguments are validated strictly (types, patterns, lengths, ranges, no extra keys) and then authorized per principal via an `Authorizer` before execution; authorizer errors fail closed.
- R13: every write attempt (including denied ones) is recorded on the `po_agent.audit` logger.
- R14: one user cannot occupy more than `max_sessions_per_principal` slots; questions are capped at `max_question_chars`.
- R15: in-memory stores are bounded (TTL + max entries); error details are not exposed to the model or user.
- R8: a crashing session never affects other sessions (`run_many` never raises).
- R9: failures are never cached (idempotency store and read cache).
- R10: when a dependency is down the agent degrades (answers "unknown") instead of fabricating or crashing.

## 6. Non-functional requirements

- Python >= 3.12, fully `asyncio`; no runtime dependencies (only `boto3`, optional, for Bedrock).
- All limits configurable in one `Settings` object.
- Every external dependency (LLM, backend, stores) sits behind an interface so it can be swapped without touching the agent.
- Deterministic, offline test suite (`make test`) that runs in about a second; lint with `make lint`.

## 7. Acceptance criteria

1. `make test` passes (66 tests) and `make lint` is clean.
2. `make demo` runs 6 concurrent sessions against a 30%-failure backend; all finish `ok`, retries appear in metrics.
3. `python -m po_agent --duplicate` performs each expedite exactly once.
4. With `failure_rate=1.0` the agent returns a graceful answer, not an exception.
5. 20 requests against 2 workers and a queue of 3 produce 5 `ok` and 15 `shed`.

## 8. Known limitations / assumptions

- Circuit breaker, rate limiter, retry budget and caches are **per process**. A fleet needs shared limits (see ARCHITECTURE.md section 9).
- Security: see THREAT_MODEL.md. Authentication is not implemented here (the API layer must supply `planner_id`), and the default `Authorizer` allows everything.
- `BedrockLLM` is verified only against a fake client; real model ids, quotas and inference-profile rules need checking once an account exists.
- Token counts from `FakeLLM` are rough estimates; Bedrock reports real usage.
- Session checkpoints are saved after each full tool round, so a crash mid-round repeats that round (safe because tools are idempotent).
- Open questions for the real assessment: exact tool contracts, expected output format, and concurrency targets are unknown until the task text is available.
