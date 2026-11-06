# Durable workflow recovery and resume (AIDLC-57)

RecoveryService is the supported execution boundary. It stores versioned
ExecutionRecord snapshots separately from immutable TaskPlan records. The
existing single-agent and planning operations remain available. The legacy
WorkflowExecutor is an internal one-pass helper; the HTTP host no longer invokes
it directly, because doing so would repeat side effects on every submission.

## Operations and hosting

Authenticated `POST /invocations` accepts these strict operation envelopes:

```json
{"operation":"start_execution","request":{"plan_id":"accepted-plan","initiative_id":"travel-platform","idempotency_key":"submission-1"}}
```

```json
{"operation":"resume_execution","request":{"execution_id":"persisted-execution","initiative_id":"travel-platform"}}
```

Use the same execution request with `execution_status` or `cancel_execution`.
Start commits admission and returns queued state without launching work. Resume
is an awaited, bounded worker application entry point. It returns persisted
progress after one pass; retry backoff does not sleep inside the invocation.
An authenticated external worker/scheduler must call resume again at `retry_at`
or after a crashed worker's lease expires. It must retain execution IDs in a
durable queue; this change does not provision that scheduler. No untracked
background worker is launched or assumed to survive AgentCore shutdown.

Legacy `execute_plan` now durably starts with owner/initiative-scoped key
`legacy.<plan_id>` and awaits resume. Repeated submissions reuse that execution;
finished successes are not dispatched again. Creating another explicit start key
can intentionally run the plan again, including its side effects. Without a
RecoveryService dependency, all execution operations return 503, even if the old
one-pass executor is configured. Existing request shapes otherwise remain valid.

Compose the existing host with
`Dependencies(authentication, orchestrator, planning, recovery=recovery_service)`.
Construct `RecoveryService(planning, orchestrator, execution_repository,
policy=RetryPolicy(...))`. Inject the same registry, authorization and initiative
services used by planning/dispatch. SQLiteExecutionRepository takes a durable file
path, like the existing approval and plan repositories. Its ExecutionRepository
port requires atomic admission, claim/lease checks, revisions, cancellation
intent and immutable identities/history. SQLite works across processes sharing
that database filesystem; independent AgentCore ephemeral filesystems do not
provide this guarantee. Production requires shared transactional storage or a
managed durable filesystem, secure credential adapters, worker scheduling,
clock synchronization, access controls, encryption, retention and backup.
No AWS resources or identity provider are configured here.

## Persistence and concurrency

Admission is unique by (initiative, principal, identity provider, idempotency key).
Repeating the same plan returns the original execution and stored policy;
reusing the key for another plan conflicts. Execution IDs never silently create
state on resume/status/cancel. Ownership and active initiative membership are
checked on all operations. Start/resume recheck capability authorization through
the persisted-plan handoff; every new dispatch repeats the current initiative,
capability, manifest permission and registry gates. Cancellation requires ownership
and current initiative membership, without requiring all original capability
grants. Remote task controls still enforce current capability and endpoint gates;
revocation may prevent remote cleanup, producing an unknown outcome.

Records include schema version, plan/initiative/owner, request/correlation/trace,
execution and stable step task IDs, attempts, deadlines/times, policy, outputs,
attribution, sanitized errors, blocked dependencies, call/remote IDs, cancellation
state, revision and bounded worker lease. Successful evidence/output is immutable.
Steps transition queued -> active -> succeeded/failed/uncertain/cancelled; blocked
steps may become active after dependencies recover. Failed steps may become active
only through the bounded retry admission logic. Uncertain steps can become terminal
only through remote reconciliation, never a new send. Workflow states are queued,
active, succeeded, partial, failed, uncertain and cancelled. Unknown remote work
keeps overall state uncertain, even if cancellation was requested.

A worker claims the entire execution with a random fencing token (10-second lease
by default). A scoped monitor renews it and checks persisted cancellation/deadline
at most every 0.1 seconds. These tasks exist only while resume is awaited and are
cancelled/awaited on exit. A second worker gets `execution_busy` while the lease is
live. Expired leases can be claimed by a new token; stale workers cannot renew or
save through a newer token/revision. Expired intent is marked uncertain before
anything else happens. Lease expiry is not evidence that remote execution stopped.
Transactions are short and never span a remote call. UTC clocks must be synchronized;
the adapter's writes are process-safe SQLite transactions, not a distributed clock
or a remote fencing service. A stalled old request may still finish remotely.

Each attempt intent is committed **before** dispatch. The A2A message ID and
selected agent/endpoint checkpoint is committed before send. Known remote task
IDs are checkpointed on response, before outcome processing. Successful output
and attribution are committed before dependents can run. Storage/monitor failures
abort further dispatch and retain recoverable intent. Status returns persisted
state; it hides fencing tokens and endpoint addresses. It does not return raw
transport diagnostics. Successful agent evidence remains explicitly untrusted.

## Retry, deadlines and crash windows

The default stored policy bounds attempts to 3 per step, backoff to 2 seconds
exponentially increasing (capped at 300), and execution duration to 3600 seconds.
Hard caps are 10 attempts and 86400 seconds. Current configuration can tighten
stored attempt/time budgets, slow backoff, or remove retry-safe declarations.
Stored/current plan size/depth/delegation limits also apply on resume.
Every attempt, including a retry, consumes the total plan delegation budget;
reconciliation lookups/cancellation are control operations rather than new sends. No nested
plan expansion occurs. Each retry has a new message/call ID and attempt number,
while execution, trace, request/correlation and step task IDs stay stable.

Only no-agent (no send) and explicit remote rejection are eligible by default.
Terminal remote execution failures additionally require the capability to be
listed as retry-safe in **both** stored and current trusted configuration. This
is an operator assertion about the agent's effects, not an idempotency guarantee.
Authorization, malformed input/output, protocol, and permanent errors are not
retried. A retry-safe declaration never permits blind retry of uncertain work.

A crash before attempt intent permits recovery of queued work. A crash after
intent, even before an actual send, is conservatively uncertain. A crash after
remote effects but before local outcome commit cannot be distinguished from a
lost response. With a remote task ID, resume invokes the installed official
`a2a-sdk` Client.get_task(GetTaskRequest(id=...)); results pass existing lineage,
status and output-schema validation. Without an ID, or with an unavailable,
unsupported or changed remote endpoint, status remains reconciliation-required
(`uncertain`). There is no automatic redispatch or manual force-success endpoint.
Remote state without a trustworthy terminal result cannot enable dependencies.
A remote idempotency key would only help if the receiving agent enforces it;
this implementation makes no exactly-once claim and sends no invented dedupe key.

Each attempt deadline is bounded by configured dispatch timeout and execution
deadline. Elapsed execution budgets prevent new work but still permit bounded
lookup/cancel of existing remote work. A local timeout or coroutine cancellation
after send leaves an uncertain outcome, not a safely retryable failure. Independent
successes/evidence survive; blocked dependencies are reevaluated after a safe retry
or reconciliation succeeds. User-driven repeated resume is idempotent for queued,
active and terminal successes; an eligible failed step can deliberately advance
to its next attempt after persisted backoff.

## Cancellation

Cancellation commits intent before acknowledgement, preventing further steps and
retries. A live worker sees it through shared storage, signals AgentContext's
cancellation event and awaits local cleanup. A fresh service can process intent
when no worker owns the lease. Known remote tasks receive the official
Client.cancel_task(CancelTaskRequest(id=...)) through the same authenticated A2A
transport boundary and pinned original agent/endpoint checks.

Outcomes distinguish requested, confirmed terminal cancellation, unsupported
remote control, unknown remote outcome, and completed-before-cancellation.
A successful cancellation RPC alone does not confirm cancellation: only terminal
protocol evidence does. A cancelled task may already have caused effects.
With no remote ID, local cancellation cannot establish remote termination and
remains unknown/uncertain. Completed steps and evidence are preserved. Repeated
cancel requests are deterministic; terminal completion wins a final-step race,
while intent prevents starting the next step. Normal external asyncio cancellation
propagates after child tasks are cancelled/awaited; the lease and intent remain for
safe recovery. Cancellation latency depends on store availability, polling and
bounded transport cleanup; it is not a distributed hard-stop guarantee.
