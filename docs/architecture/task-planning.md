# Bounded task planning (AIDLC-55)

Planning compiles an explicit capability graph into a validated, immutable,
accepted snapshot. It uses no model, agent catalog, dispatch or recursive expansion.
Callers supply the decomposition; this version does not infer tasks from prose.

The existing AgentCore `POST /invocations` supports an additional strict envelope:

```json
{
  "operation": "plan",
  "request": {
    "initiative_id": "travel-platform",
    "request_id": "planning-request-1",
    "correlation_id": "trace-1",
    "steps": [
      {
        "step_id": "investigate",
        "capability": "investigation",
        "invocation": {"input": {"question": "Investigate the change"}},
        "expected_output": {"type": "object"}
      },
      {
        "step_id": "verify",
        "capability": "verification",
        "dependencies": ["investigate"],
        "upstream_outputs": {"evidence": "investigate"},
        "invocation": {"input": {}},
        "expected_output": {"type": "object"}
      }
    ]
  }
}
```

The normal single-agent request remains supported. The response is a persisted
`TaskPlan`, schema version `1.0.0`, with generated plan identity, authenticated
principal/provider ownership, initiative, request/correlation lineage, creation
time, effective limits, canonical steps and initial `accepted` state. Lineage
identifiers are correlation/idempotency data, never grants. Input uses existing
`Invocation`; capabilities use the existing authorization enum. Output contracts
use the harness's Draft 2020-12 local-reference schema validator. No new task
execution result or identity model is introduced.

Validation rejects empty graphs, duplicate IDs/dependencies, missing/self/cyclic
dependencies, malformed output schemas, external schema references and conflicting
input bindings. Each upstream binding names a direct dependency's **whole output**;
JSON pointers and implicit transitive references are unsupported. Literal input
keys cannot overlap upstream bindings. Unknown plan/step fields, including nested
steps, endpoints and permission grants, are rejected. Invocation business JSON
is opaque data: embedding a plan there does not expand or authorize anything.

Ordering proceeds in dependency levels with lexicographic step IDs within each
level. Independent steps are explicitly represented and ordered without adding
dependencies; a future executor may schedule them together. Defaults allow 32
steps, depth 8 (roots count as 1), 32 total delegations, and no nested delegation.
Trusted configuration may tighten these or raise them to hard caps of 64 steps,
depth 16, and 64 delegations. Requests cannot set limits. Request JSON is bounded
to 256 KiB in addition to the HTTP host's existing 1 MiB limit. Each step represents
one leaf delegation. AIDLC-56 must enforce these stored budgets and reject agent
requests to expand or replan; AIDLC-55 has no executor or recursive expansion path.

`PlanningService` reuses GatewayContextResolver and audited AuthorizationService
for active initiative membership and every distinct requested capability. Registry
discovery checks capability eligibility using policy-derived tool permissions.
Multiple candidates are allowed at planning time; no endpoint/agent is pinned.
Execution must reselect from the current registry and re-enforce authorization,
resource scope, operation policy and approval. A saved plan grants no permissions.

Inject `PlanningService(registry, contexts, authorization, repository)` as the
optional third field of the existing host's `Dependencies`. With no planning
service the planning operation returns 503; the single-agent path remains usable.
Use `SQLitePlanRepository(path)` for durable local acceptance, following the
existing SQLite approval adapter pattern. The repository requires a file path,
uses an immediate transaction and FULL synchronization, and returns only after
commit. Snapshots are immutable: there is no update/delete or silent overwrite.
The unique (initiative, principal, provider, request_id) key serializes concurrent
submissions. Identical canonical content returns the original plan identity and
time; changes to steps, lineage or effective limits conflict. Different owners
have separate keys. Validation/authorization/eligibility failures write nothing;
storage failures never return execution eligibility.

AIDLC-56 consumes `PlanningService.load_for_execution(plan_id, initiative_id,
principal)`, never an in-memory plan object. It loads and revalidates accepted
storage, verifies ownership, reapplies current bounds, and rechecks capability
authorization. Missing, corrupted, mismatched or unsaved plans fail closed. This
handoff is not a permission lease or an execution claim. Execution concurrency,
step dispatch/results, retries and recovery state belong to AIDLC-56/57.

SQLite persists across adapter/process restarts on the same durable filesystem;
it is not shared storage for independent AgentCore microVMs. Production composition
must supply a durable shared transactional implementation of `PlanRepository`,
with equivalent atomic idempotency and immutable snapshot semantics, or an
appropriately managed persistent filesystem. The supplied container has no
persistent volume configured. Plan payloads can contain business data: storage
access controls, retention, encryption and backup are deployment responsibilities.
No AWS provisioning or deployment is included.
