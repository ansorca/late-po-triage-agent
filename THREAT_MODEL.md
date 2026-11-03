# THREAT_MODEL.md (STRIDE)

Scope: the intended production shape (an authenticated API in front of the service, a shared state store, real Bedrock, real business systems). Today the repo is a library/CLI with no network surface. This is a design-level review, not a penetration test.

**Assets:** supplier/inventory data; the ability to trigger supplier expedites (a business action); Bedrock spend; session history.
**Trust boundaries:** client -> API layer; service -> Bedrock; service -> business systems; service -> state stores. **Untrusted input:** the planner's question, *and everything the model reads from tools* (supplier names, PO notes), because the model may then choose tool arguments based on attacker-controlled text.

## Findings and status

| | Threat | Mitigation | Status |
|---|---|---|---|
| **S** | Guessing another user's `request_id` to read or resume their session | Sessions are keyed `"<principal>:<request_id>"`; idempotency keys include the principal; ids are validated to `[A-Za-z0-9_-]{1,64}` so keys cannot collide. `test_other_user_cannot_read_or_resume...` | Done |
| **S** | Client forges `planner_id` | `planner_id` **is** the authenticated principal and must be set by the API layer from the verified token, never from the request body | **Open: needs the API layer** |
| **S** | Static AWS keys | Use an IAM role for the service; no keys in code or env | Doc only |
| **T** | Prompt injection steers the model into a harmful write (`propose_expedite` on someone else's PO) | `Authorizer` hook runs on every tool call with the caller's identity; denial is a non-retryable `forbidden` result; failure of the authorizer fails closed. `PoOwnershipAuthorizer` is a worked example. `test_injected_expedite...` | Hook done; **real policy open** (default is `AllowAll`) |
| **T** | Hostile tool arguments (injection strings, oversized values, extra keys, out-of-range numbers) | Schemas use `additionalProperties: false`, `pattern`, `maxLength`, `minimum`/`maximum`, enforced before authorization or execution. Parametrised test. | Done |
| **T** | Poisoned shared session store replayed into the model | Restrict write access to the store to the service role; integrity depends on the real store (IAM, encryption) | Open (production store) |
| **R** | No record of who triggered a write | Every side-effecting call emits a `write_attempt` event on the `po_agent.audit` logger: principal, tool, arguments (truncated), idempotency key, outcome, plus `request_id`/`trace_id` from context. Includes denied attempts. | Done in code; **open: route to an append-only, tamper-evident sink** |
| **I** | Backend error text reaching the model and user | Tool errors to the model are generic (`service temporarily unavailable`); details stay in logs. Prompts and answers are not logged. | Done |
| **I** | Cross-user leakage through the shared read cache | `ToolSpec.cache_per_principal=True` scopes cache entries to the user. Default is shared, which is fine for reference data only. | Done (opt-in per tool) |
| **I** | Data at rest and in transit | Encrypt the production session store, set TTLs (in-memory store has them); review Bedrock data handling; consider Guardrails for PII | Open (production) |
| **D** | Queue filled by one user | `max_sessions_per_principal` (default 4) sheds the excess for that user only. `test_one_user_cannot_fill_the_queue` | Done |
| **D** | Oversized input / cost abuse | `max_question_chars`; `max_turns`; `max_tokens_per_session`; LLM output cap; rate limiter | Done |
| **D** | Memory growth | In-memory session and idempotency stores have TTL and a max-entries cap | Done |
| **D** | Overload, slow or broken dependencies, retry storms | Bounded queue + shedding, timeouts, bulkheads, circuit breakers, retry budget (see SPEC R1-R6) | Done |
| **D** | Denial of wallet from a flood of valid requests | Per-user quota and token budget limit it per session; **a spend alarm / AWS Budget and API-level rate limiting are still needed** | Open (infrastructure) |
| **E** | Model acting as a confused deputy with the service's credentials | Principal is passed to every tool (`ToolContext.principal`) and authorized before execution. Model can only call registered tools (unknown names are rejected). | Hook done; real policy open |
| **E** | Over-broad IAM | Service role limited to `bedrock:InvokeModel` on the one model; separate, narrower credential for write tools | Open (infrastructure) |

Supply chain: the core has no runtime dependencies; `boto3` is optional; `uv.lock` pins versions. Run `uv pip audit` / Dependabot in CI.

## Before exposing this to real users

1. Authenticate at the API layer and set `planner_id` from the verified identity.
2. Replace `AllowAll` with a real `Authorizer` (ownership/role checks against the business system) and add policy conditions inside write tools.
3. Route the `po_agent.audit` stream to an append-only store.
4. Add API-level rate limiting and a Bedrock spend alarm.
5. Move state to an encrypted shared store with IAM-scoped access.
6. Consider human approval for expedites, and Bedrock Guardrails.

## Residual risk

Prompt injection cannot be fully prevented in the model, so the design assumes the model *will* sometimes be fooled and limits the damage in code: strict schemas, per-user authorization, idempotent and audited writes, and per-session budgets.
