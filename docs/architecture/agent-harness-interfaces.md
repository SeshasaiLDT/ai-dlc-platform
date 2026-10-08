# Shared Agent Harness interfaces

The public Python interface version is **1.4.0** (`INTERFACE_VERSION`); the initial interface version was 1.0.0. It currently ships inside the `ai-dlc-platform` distribution, whose version remains 0.1.0. The harness is a library imported by independently deployed agents. It does not run a central execution service or define an A2A wire schema.

`AgentLifecycle` owns initialize, execute, and shutdown. `AgentContext` is immutable, trusted in-process state: request, correlation, session, and trace IDs; the existing `ResolvedAuthorizationContext`; an optional aware deadline; cancellation state; and optional JSON metadata. Its `principal` property comes only from the resolved authorization snapshot. The authenticated runtime creates this context after authorization. Agent input must never supply or replace the principal, authorization snapshot, scopes, approval decisions, or trusted gateway references.

`Invocation` and `ExecutionResult` are JSON-safe application boundary values. `ExecutionStatus` is `succeeded`, `failed`, `cancelled`, or `timed_out`. Every unsuccessful result has an `ExecutionError` (`code`, `message`, `retryable`, optional JSON `details`), and every result carries correlation and trace IDs. Metadata is optional. Adapters must propagate those IDs without accepting an agent-generated identity. A deadline is an absolute, timezone-aware instant; a cancelled context or expired deadline must prevent new external calls. Implementations should return `cancelled` or `timed_out` when they can report the outcome, and must propagate task cancellation when their caller cancels them. The ports define these semantics; lifecycle and adapter components enforce deadlines at their own boundaries.

`ToolProvider` lists and invokes tools; its implementation must apply existing Gateway discovery and governed invocation checks. `ModelProvider` invokes a configured model role. `AgentDelegator` calls an independently deployed agent through an A2A adapter and preserves identity, authorization, correlation, trace, timeout, and cancellation semantics. `Validator` performs local input checks. `ApprovalProvider` requests and reads human approval through a trusted adapter; its `ApprovalIntent` is not a policy decision, and `ApprovalReference` is not permission to execute. The adapter must use the existing `ApprovalService` and recheck the established authorization, tool-policy, approval, and resource boundaries before a protected operation. `TelemetryProvider` records names, numeric metrics, and structured errors using the trusted context; implementations must exclude credentials and sensitive payloads.

These are structural Python `Protocol`s. An agent can implement only the ports it consumes. For example:

```python
from ai_dlc.application.agent_harness import (
    AgentContext,
    ExecutionResult,
    ExecutionStatus,
    Invocation,
)


class EchoAgent:
    async def initialize(self) -> None:
        pass

    async def execute(self, request: Invocation, *, context: AgentContext) -> ExecutionResult:
        return ExecutionResult(
            status=ExecutionStatus.SUCCEEDED,
            correlation_id=context.correlation_id,
            trace_id=context.trace_id,
            output=request.input,
        )

    async def shutdown(self) -> None:
        pass
```

Agents own prompts, workflow steps, validation rules, and deployment. Adapters own A2A serialization, model access, Gateway transport, approval access, and telemetry delivery. This package has no AWS, agent-framework, POS, or agent-specific dependency. Wire-protocol versions and independently published agent capability versions are separate from this SDK's Python package version.

## Compatibility policy

The public interface contract starts at 1.0.0. Follow semantic versioning for `INTERFACE_VERSION` and for any future separate SDK distribution: patches fix behavior without changing signatures; minors add optional fields or new ports while preserving existing valid calls and result parsing; majors may remove fields, change required fields, signatures, status meanings, or identity/authorization semantics. A release of the current platform distribution must state which interface version it contains. Deprecate a public member in a minor interface release, document its replacement, retain it through the next major release, and provide a migration note with before/after examples. Consumers should pin a compatible interface major and upgrade adapters and agents together for major changes. Contract tests with fake implementations check protocol use, JSON round trips, status/error invariants, and trusted context requirements; integration adapters must add their own A2A wire compatibility tests without treating `INTERFACE_VERSION` as an A2A protocol version. No version negotiation framework is introduced here.

## Lifecycle execution (AIDLC-39)

`create_agent_context` is called only by a trusted entrypoint after authentication and authorization. It requires a trusted task ID and existing `ResolvedAuthorizationContext`; the initiative ID and principal are read from that snapshot. It generates missing request and trace IDs, defaults correlation and a stateless session ID to the request ID, and creates one cancellation event per invocation. Task ID is a frozen field, initiative ID is a read-only property of the frozen authorization snapshot, and metadata is copied and recursively frozen on construction. Neither identifier comes from `Invocation.input` or mutable metadata. Do not place credentials in the context. At process boundaries, reconstruct the trusted authorization snapshot using the existing authentication and authorization services rather than deserializing agent-returned claims.

`LifecycleRunner.run` executes one supplied agent instance for one invocation: check cancellation/deadline → initialize → execute → validate result lineage → shutdown → return. It calls `shutdown` once even for an early cancellation, expired deadline, or failed initialization. The runner has no shared invocation state and does not route agents or persist state. Callers should supply a separate agent instance when an agent has mutable per-invocation state.

The deadline is an absolute aware timestamp covering initialization and execution. Shutdown has a separate, configurable `cleanup_timeout_seconds` (default 5 seconds), measured by the event loop. When it expires, the runner cancels and awaits the local shutdown coroutine. This bounds cleanup under normal cooperative asyncio cancellation; a coroutine that suppresses cancellation indefinitely can still block its caller. The per-invocation `cancellation_event` can be set by the trusted caller; `cancellation_requested` also honors the original `cancelled` snapshot. Both stop new work and cancel the active coroutine. Agents and adapters must propagate cancellation to their own child work. Cancelling a Python coroutine does not prove that remote model, tool, or cleanup operations stopped. A cleanup timeout means remote cleanup is unconfirmed. An externally cancelled runner propagates `asyncio.CancelledError` after cancelling and awaiting local shutdown.

Every returned result has the trusted request, correlation, and trace IDs. Agent results with mismatched lineage or the wrong type become `invalid_result`. Exceptions become generic `initialization_failed` or `execution_failed` errors without exception text. Cancellation and execution timeout have distinct statuses and codes. A shutdown error or cleanup timeout replaces a successful result with structured `cleanup_failed` or `cleanup_timed_out`; if a primary failure already exists, it remains primary and result metadata records `cleanup_failed: true` plus `cleanup_error_code`. Future agents should return the existing structured `ExecutionResult` and keep sensitive exception details out of their own results.

Version 1.1.0 adds optional `AgentContext.task_id`, `AgentContext.cancellation_event`, and `ExecutionResult.request_id` fields to preserve 1.0.0 construction. The factory requires a task ID and the runner always fills request ID in results. Existing direct contexts without a task ID remain constructible, but cannot be passed to this runner.

## Common MCP client (AIDLC-40)

Agents inject `McpClient` as their existing `ToolProvider`. It accepts one or more trusted providers, calls each provider's governed `list_tools(context)`, checks the published JSON Schema, rejects duplicate names and external schema references, and exposes only name, description, input schema, and approval flag. `get_tool` reads the same filtered catalog. The client does not grant permissions: providers must filter discovery using the existing principal, initiative, authorization, policy, and approval boundaries. A hidden or unknown name has the same `tool_not_found` result. Discovery errors are sanitized as `ToolDiscoveryError`.

For the existing enterprise path, a trusted runtime constructs `GatewayToolProvider` with `GatewayRuntimeAccess` and its `TrustedGatewayContext` for one invocation. The adapter checks that the harness and Gateway contexts agree on principal, authorization, initiative, task, and correlation IDs. It then uses the existing filtered discovery and one-use invocation reference path; tool arguments contain only business data. Governed application services still enforce exact authorization, tool policy, approval, bindings, and strict Pydantic request validation. The adapter does not create a second MCP transport or provider client. Other providers must implement `ToolProvider` with equivalent governed behavior before registration.

On invocation, `McpClient` discovers the tool again, validates JSON arguments against its schema, applies the shorter of the configured tool timeout and remaining execution deadline, invokes the selected provider, checks correlation/trace/request lineage, and returns `ExecutionResult`. It maps missing tools, denials, explicit approval requirements, invalid arguments, unavailable providers, provider failures, timeout, and cancellation to stable codes with generic messages. Provider exception text and private metadata are not returned. The existing governed enterprise services currently report an unsatisfied approval gate as `PERMISSION_DENIED`; this client therefore reports `unauthorized_operation` for that path rather than inferring `approval_required` from discovery metadata. Providers that can distinguish the condition may return `APPROVAL_REQUIRED` explicitly.

`RetryPolicy` defaults to one attempt and caps attempts at ten. A retry requires **both** a provider-marked retryable transient/timeout result and trusted `retrySafe` tool metadata; the client never derives retry safety from agent arguments. The current Gateway adapter does not mark any tool retry-safe, so its operations are not automatically replayed. Backoff is bounded and stops at the invocation deadline or cancellation. Per-tool timeout cancels and awaits the adapter coroutine. The current synchronous Gateway facade runs through `asyncio.to_thread`: cancelling that await does not stop an already-running worker or prove remote execution stopped. Adapters must handle remote cancellation and idempotency according to their own transport contracts.

## Common A2A layer (AIDLC-41)

`A2AClient` implements the existing `AgentDelegator` port with the official `a2a-sdk` 1.2.x client and **A2A protocol 1.0** protobuf types. The repository had no prior A2A transport or SDK. `ConfiguredAgentDirectory` holds injected official Agent Cards and applies a required trusted allow callback before exposing a card or endpoint. The orchestrator still chooses the agent ID. An agent injects `A2AClient` and calls `delegate(agent_id, Invocation(...), context=trusted_context)`; it does not construct a provider-specific client. The required `call_context_factory` supplies transport authentication through the SDK's `ClientCallContext`. No credentials, principal, roles, authorization snapshot, or permission claims are placed in A2A data parts or metadata.

For a new delegation the client sends one official `SendMessageRequest` with JSON `Invocation` in a data part. A2A assigns the child task ID; the parent task ID goes in `reference_task_ids`. The request metadata carries task lineage, initiative, request, correlation, trace, session, and optional deadline as **untrusted hints**. The trusted server entrypoint authenticates the caller and resolves authorization using existing services, then constructs `AgentContext`; `A2AServerAdapter` compares wire lineage to that trusted context and rejects a deadline that the trusted context would extend. The entrypoint must bind the deadline to authenticated transport policy; a wire hint alone cannot authorize more time. The parent reference must be validated by the trusted entrypoint if used for access control. Message content never grants identity, initiative access, or parent-task ownership. Each server call gets an agent instance from a factory. The adapter emits an official terminal `Task` containing a JSON `ExecutionResult` artifact. `build_a2a_handler` attaches it to the official SDK request handler and an injected SDK task store; deployment owns the HTTP/AgentCore transport and store selection.

The client uses the shorter of its transport timeout and `AgentContext.deadline`; cancellation stops the local SDK call and propagates `CancelledError` from an externally cancelled caller. A timeout or cancellation before receiving a task includes the A2A message ID, but cannot assert a remote task ID. A received task ID is preserved in result metadata. Cancelling the local request does not prove remote execution stopped. Transport and protocol exceptions become sanitized `ExecutionError` codes; supported typed SDK errors distinguish invalid requests, unsupported methods, and remote timeouts. Authentication/authorization errors are distinguished when the transport exposes HTTP status 401/403; an SDK transport that erases the status is reported as `remote_unavailable`. Agent exceptions never expose their text.

This adapter supports Agent Cards, single message sends, terminal task responses, structured JSON results, and task ID handoff. A nonterminal submitted, working, input-required, or auth-required task returns `remote_task_pending` with its ID and state for a later polling adapter. It does not implement polling, resume, streaming, push notifications, persistent task storage, distributed cancellation, or AgentCore deployment. The SDK request handler may provide protocol operations beyond this adapter's terminal execution path; do not advertise those capabilities in the Agent Card until an agent implements them. This is a tested A2A 1.0 subset, not a claim of full protocol compliance. `INTERFACE_VERSION` remains the Python harness interface version and is independent of A2A's wire version and the `a2a-sdk` package version.

## Structured output validation (AIDLC-42)

An agent selects one schema for each output contract: a Pydantic `BaseModel` class or a local JSON Schema Draft 2020-12 document. `StructuredOutputValidator.validate(Invocation, context=...)` implements the existing synchronous `Validator` port for already parsed input. Its async `parse(raw_json, context=...)` strictly parses a JSON object, validates it, and returns the existing `ExecutionResult` with `output.data` and `output.artifacts`. The result metadata includes `schema_id`, trusted `task_id`, and repair attempt count; request, correlation, and trace IDs come from `AgentContext`. Pydantic validation uses strict mode; model authors set `extra="forbid"` when unexpected properties must fail. JSON Schema uses the existing `jsonschema` dependency, supports local references, and rejects external references. Missing fields, types, enums, nested objects, extra properties, and malformed JSON map to stable sanitized `ExecutionError` codes without raw values or exception text.

```python
from typing import Literal

from pydantic import BaseModel, ConfigDict

from ai_dlc.application.agent_harness import StructuredOutputValidator


class ReviewOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdict: Literal["pass", "fail"]


validator = StructuredOutputValidator("review.v1", model=ReviewOutput)
result = await validator.parse(raw_model_json, context=trusted_context)
```

Optional `ArtifactReference` values carry only logical `store_id`, `artifact_id`, and small JSON metadata. An optional artifact metadata schema validates them before inclusion; malformed references and inline payload fields fail closed. Storage, resolution, and access authorization stay with trusted artifact services. Repair is disabled by default and requires both an explicit maximum of 1–3 attempts and an injected async callback (for example, an adapter around `ModelProvider`). Every candidate is revalidated. Cancellation and the execution deadline stop local repair work, but cancellation cannot prove remote model work stopped. Artifact failures are not sent to repair because changing model text cannot fix references. Telemetry uses the existing `TelemetryProvider`: event names include a stable schema ID, metrics use fixed names, the trusted context carries correlation and trace IDs, and errors contain only category and schema information. Telemetry exceptions do not change the validation outcome. This layer does not choose prompts, authorize artifact access, store artifacts, or migrate agents.

Version 1.2.0 adds `StructuredOutputValidator` and `ArtifactReference` without changing existing harness ports or result fields. It does not alter the A2A protocol version.

## Context and token budgeting (AIDLC-43)

`ContextAssembler` turns caller-supplied `ContextSegment` values into a deterministic, budgeted prompt input. Segments are inputs to prompt assembly, not a second execution context: identity and initiative come only from the trusted `AgentContext` passed to `assemble`. The assembler does no retrieval, summarization, model calls, or persistence.

**Categories and authority.** `system_instructions`, `task_instructions`, `initiative_context`, `retrieved_evidence`, `conversation_history`, `supporting_metadata`. Authority is derived from category and cannot be configured: system/task are `instruction`, initiative context is `trusted_context`, everything else is `untrusted_data`. Instruction segments are always required and never truncatable. Evidence cannot be required and must carry a `SourceReference` with an `initiative_id`. Each segment is rendered in a delimited block naming its category and authority; untrusted text has `<<` neutralized so it cannot forge delimiters. Callers must authorize and initiative-filter retrieval *before* building evidence segments; the assembler additionally excludes any segment whose source initiative differs from the context's (`initiative_mismatch`, fail-closed for required ones) and exact duplicates of evidence (same source and normalized content).

**Prioritization.** Three separate concepts: *authority* (category-derived), *relevance* (0–1, orders evidence at equal priority), and *allocation priority* (0–1000, lower is allocated first within a category). `ContextPolicy.category_order` reorders only the four non-instruction categories; instructions always rank first, so untrusted content cannot outrank them. Default: initiative, evidence, history, metadata. Ties keep input order. Required segments are admitted first; optional ones follow in rank order. Output order is category order, caller order within a category (so history stays chronological), and relevance order for evidence.

**Budget.** `ModelCapability` is injected by deployment (`model_id`, `max_context_tokens`, `reserved_output_tokens`, `reserved_tool_schema_tokens`, `reserved_protocol_tokens`); no model limits are built in. `available_input_tokens = max_context - output - tool/schema - protocol`; configurations leaving no input are rejected. `assemble(..., tool_schema_tokens=n)` overrides the tool reservation for one call. `ContextPolicy` adds `fixed_overhead_tokens` and `per_segment_overhead_tokens` for message framing. The advertised window is not assumed to be usable input.

**Counting.** `TokenCounter.count(text, model_id=...)` returns `TokenCount(tokens, exact)`. Inject a model-specific exact counter where one exists. `EstimatingTokenCounter` (characters per token and a safety factor, both configurable) always reports `exact=False`. Counting covers the rendered text including delimiters and citations. Any inexact count makes the report `counting_method="estimated"` and `provider_validation_required=True`; set `require_exact_counting` to fail instead. An estimate does not guarantee provider acceptance: validate provider-side where supported.

**Truncation.** Only optional segments with `truncatable=True` can be shortened, and only when `truncation_enabled`. Allocation is greedy in rank order: higher-priority segments are admitted whole first, the first optional segment that does not fit is truncated (or dropped as `over_budget`), and later, smaller segments may still fill the remainder. Text is cut at a character boundary and ends with ` [truncated]`; JSON arrays/objects drop trailing items/keys so the result stays valid JSON, and scalars are never truncated. Truncated blocks carry `truncated="true"`; source references stay in the header. A result below `min_truncated_tokens` is dropped instead. `AssembledSegment` records original and retained token counts. If required segments do not fit, `assemble` returns a `BudgetFailure` (`to_execution_error()` gives a sanitized `ExecutionError`).

**Telemetry.** Through the existing `TelemetryProvider`: event `context_budget.assembled|failed.model.<id>.counting.<method>`, metrics `context_budget.{available_input_tokens,reserved_tokens,used_tokens,utilization,included_segments,excluded_segments,truncated_segments,overflow_events,estimated_counting}`, and `record_error` on failure. No prompt text, evidence, citations, or identity is emitted, and telemetry errors are swallowed.

```python
assembler = ContextAssembler(
    ModelCapability(
        model_id="deployment-model", max_context_tokens=32_000,
        reserved_output_tokens=2_000, reserved_tool_schema_tokens=1_000,
    ),
    counter=EstimatingTokenCounter(),
)
result = assembler.assemble(
    [
        ContextSegment("sys", ContextCategory.SYSTEM_INSTRUCTIONS, trusted_prompt),
        ContextSegment("task", ContextCategory.TASK_INSTRUCTIONS, task_prompt),
        ContextSegment(
            "doc-1", ContextCategory.RETRIEVED_EVIDENCE, text, relevance=0.9, truncatable=True,
            source=SourceReference("doc-1", initiative_id=ctx.initiative_id),
        ),
    ],
    context=ctx,
)
if isinstance(result, BudgetFailure):
    return ExecutionResult(...error=result.to_execution_error()...)
prompt = result.render()
```

**Limitations.** Token sums are additive per segment and assume counters are monotonic in length; real tokenizers may differ at boundaries. Only top-level JSON arrays/objects are truncated. No summarization, nested JSON pruning, or provider-side validation is performed here. Version 1.3.0 adds these types without changing existing ports, results, or A2A wire contracts.

## Retries, resilience, and idempotency (AIDLC-44)

`ResilientExecutor.run(operation, spec=..., context=..., identity=...)` wraps one logical operation (`operation(AttemptInfo)` returns the existing `ExecutionResult`). It is a library primitive, not a workflow engine or scheduler. The AIDLC-40 `RetryPolicy` moved to `resilience.py` and gained optional `jitter` and `retry_on_timeout`; its defaults and `from ...mcp import RetryPolicy` still work, and `McpClient` computes backoff with the shared `backoff_delay`. `McpClient` keeps its tool-metadata `retrySafe` gate and is otherwise unchanged. The existing Gateway path keeps its per-invocation one-use references; the repository has no durable idempotency coordination, so none is implied by it.

**Four separate concepts.** *Retryable failure* is an error class. *Idempotent operation* is declared by trusted adapter code through `OperationSpec(category, ReplaySafety)` (`read_only`, `idempotent_write`, `non_idempotent_write`), never by provider metadata or model output. *Safe to replay* combines that declaration with recorded state (`resume_action`). *Ambiguous completion* means the outcome cannot be established, so nothing is replayed automatically. Model invocations and tool reads must be read-only.

**Retry policy.** `ResilientExecutor(policies={OperationCategory: RetryPolicy})` configures tool reads, tool writes, model invocations, and agent delegation separately; unconfigured categories make one attempt. Backoff is `min(max, base * 2**n)`, optionally shortened (never lengthened) by jitter. A provider `retry_after_seconds` detail raises the delay, and a hint beyond `max_delay_seconds` stops retrying. No retry starts if the remaining deadline is shorter than the delay; the wait is interrupted by `cancellation_event`; each attempt is bounded by the deadline and cancelled locally with it. There is no built-in rate limiter: only provider hints are honored.

**Error classification.** `ErrorClassifier` maps structured result codes (and `CANCELLED`/`TIMED_OUT` status) to `transient`, `permanent`, `authorization`, `approval_required`, `validation`, `timeout`, `cancellation`, or `ambiguous`. Adapters add their provider codes through `overrides`; unknown codes are `permanent`. Messages and exceptions are never parsed. Only `transient` retries by default; `timeout` retries only when `retry_on_timeout` is set and never after our own deadline. Authorization, approval, validation (including `malformed_json`/`repair_exhausted`, left to the AIDLC-42 repair layer), permanent, and cancellation fail fast. Returned errors keep the code but get a generic message, no details, and `retryable=False`.

**Identity.** `derive_operation_identity(context, category, operation_name, logical_key, payload)` is called at a trusted boundary with a stable step label. The key is a SHA-256 of initiative, task, category, name, and label (length-delimited), so it is stable across attempts, resumes, and request IDs, and differs for any other operation, task, or initiative. Payloads are reduced to a fingerprint digest; no secret or raw payload is stored. The same key with a different fingerprint is an `idempotency_conflict`. Never take the key from tool arguments.

**Writes.** A write fails closed without an identity (`idempotency_required`) or an `IdempotencyStore` (`idempotency_store_unavailable`). `begin` records the operation before dispatch. `idempotent_write` retries transient failures with the same key; `non_idempotent_write` is never retried. A timeout, local cancellation, raised exception, or non-definitive failure after dispatch ends as `outcome_unknown` with `reconciliation_required` and state `unknown`. Authorization, approval, and validation rejections are recorded as `failed` and may be replayed later (replay re-enters governance). Success records `completed` plus an optional `metadata["result_ref"]`.

**Resume.** `resume_action` returns `skip_completed` (a deduplicated success carrying the stored reference), `replay` (previously rejected), `await_in_progress` (reported as `operation_in_progress`, never restarted), `reconcile` (unknown), or `conflict`. An optional adapter-supplied `Reconciler` may resolve unknown state by querying the remote system with the key; otherwise the result is `outcome_unknown`. Resume re-runs the governed operation, so authorization and approval are checked again; identity must match the context's initiative, task, and category.

**Persistence.** `IdempotencyStore` is a two-method port (`begin`, `record`) scoped by initiative. Only `InMemoryIdempotencyStore` ships, for tests and single-process use; it provides no cross-process or restart guarantees. Durable, atomic adapters are a deployment requirement before relying on write deduplication across processes. An idempotency key is not exactly-once delivery.

**Telemetry.** Existing `TelemetryProvider` events `resilience.<category>.{retry.<class>,retry_exhausted,permanent_failure.<class>,ambiguous_outcome,deduplicated,reconciliation_required}` and metrics `resilience.<category>.{attempts,backoff_seconds}`. No payloads, prompts, details, or keys are emitted; telemetry errors are swallowed.

**Limitations.** A cancelled or timed-out attempt cannot prove remote work stopped. Cancellation before dispatch of a write is also recorded as unknown (conservative). Retry-after handling and attempt limits do not replace provider-side rate limiting. Idempotent-write retries are only as safe as the adapter's attestation that the remote enforces the key. Version 1.4.0 adds these types additively and changes no A2A or Gateway contract.
