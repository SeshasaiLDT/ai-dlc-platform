# A2A workflow delegation and aggregation (AIDLC-56)

The AgentCore host accepts an additional authenticated operation on its existing
`POST /invocations` endpoint:

```json
{"operation":"execute_plan","request":{"plan_id":"persisted-plan-id","initiative_id":"travel-platform"}}
```

The strict envelope accepts neither a plan object, agent endpoint, result nor
permission claim. `WorkflowExecutor.execute` loads through
`PlanningService.load_for_execution(plan_id, initiative_id, authenticated_principal)`.
Missing, unsaved, cross-initiative and unauthorized plans fail before dispatch.
The snapshot is revalidated and stays immutable. Effective bounds are the
intersection of stored budgets and current planning limits; each planned leaf
gets at most one dispatch, with no expansion, recursion or automatic retries.

The scheduler walks the canonical dependency order sequentially. Only successful
direct dependencies supply whole outputs to their declared input keys. Literal
inputs are preserved; conflicting bindings are rejected. Input/output payloads
are copied, so later steps cannot mutate an earlier result or the accepted plan.
A failed or blocked dependency blocks the step without dispatch, recording the
immediate dependency IDs and `dependency_failed`. Independent branches continue.

Single-agent and workflow dispatch share `Orchestrator.dispatch`. Each call
resolves the active initiative/current membership, performs audited capability
authorization, discovers through the current registry with policy-derived tool
permissions, and rejects zero or multiple eligible agents explicitly. No agent
is pinned at planning time. Manifest permission eligibility is rechecked and
resolved inputs must satisfy the selected input schema. Successful outputs must
satisfy both the selected manifest output schema and the planned expected-output
schema. Invalid output becomes a failed step and cannot feed dependents. Governed
tool resource scope, operation policy and approval still apply at receiving
agents. A plan or serialized authorization hint grants no rights.

The shared dispatch uses the existing A2AClient and official Agent Card transport.
Its injected call-context factory supplies credentials outside business data.
Every call has a configured finite deadline (the orchestrator timeout, default
30 seconds). Remote pending tasks are failures for this one-pass executor; no
polling, recovery, retry or cancellation-propagation system is introduced.

Lineage contracts:

- `plan_id` identifies the immutable accepted snapshot.
- A new `execution_id` and trace ID identify each explicit invocation. The plan's
  request ID and correlation ID are unchanged across every call and aggregation.
- A step task ID is deterministically derived from execution ID + step ID, is
  distinct from other steps, and is recorded with the result. It is the trusted
  AgentContext task ID and the A2A `reference_task_ids`/`parent_task_id` value.
- A2A assigns its own remote child task ID. That separate ID is retained as
  `remote_task_id`; it does not replace the stable local step task identity.
- The A2A message ID is retained as `call_id` for completed exchanges as well as
  supported transport failures. Failures before dispatch have no call ID.
- The existing A2A parser and shared dispatch validate returned request,
  correlation and trace IDs and terminal status. Mismatch is `protocol_error`.
  Lineage on the wire is an untrusted hint: receiving runtimes must authenticate
  and bind trusted context using existing server mechanisms.

The normalized `WorkflowResult` schema 1.0.0 includes plan/execution/initiative,
request/correlation/trace IDs, overall outcome and a result for **every** step.
Each `StepResult` has step/task/capability identity, selected agent where known,
call and remote task IDs where available, and succeeded/failed/blocked status.
All steps succeeded means `succeeded`; some succeeded means `partial_failure`;
none succeeded means `failed`. Blocked steps are never counted as success.
Errors retain only recognized stable codes and a generic message, not remote
exception text, details or arbitrary failure metadata.

Aggregation is structured, with no model synthesis, flattening or deduplication.
Successful output JSON is retained in its producer's envelope, including source
IDs, evidence, citations and references. Successful metadata is preserved as
`evidence_metadata` and is explicitly untrusted. `upstream_sources` maps input
keys to their producing step IDs; the aggregate retains those producers and
thus their task, capability, agent and source metadata even when downstream
agents omit citations. Attribution records origin, not verification. Agent
output is never treated as verified evidence or new control instructions.

Composition extends the existing factory's `Dependencies` with an optional fourth
field, `executor=WorkflowExecutor(planning_service, orchestrator)`. Use the same
registry/context/authorization dependencies for planning and dispatch. Without
an executor, execution requests return 503. Planning and single-agent requests
remain supported. Admission errors return 403/404/503; admitted workflow partial
or complete failures return HTTP 200 with explicit structured outcomes.

Execution results are returned in memory and do not alter accepted plans. Repeating
an execution request performs a new execution and may repeat side effects. AIDLC-57
must add durable execution/progress records, idempotency, retry/resume semantics,
avoiding unnecessary reruns and user cancellation propagation. This ticket does
not claim exactly-once execution. Task IDs are stable within one execution,
not across new executions. Local deadlines do not prove remote work stopped.
Production composition still requires the shared plan/agent stores, credential
adapters and hosting prerequisites documented in task-planning and orchestrator
runtime docs. No AWS resources are deployed here.
