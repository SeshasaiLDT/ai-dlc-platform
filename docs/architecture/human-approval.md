# Human approval policy engine

Status: Application contracts and local durable adapter (AIDLC-29)

## Layering

```text
Authentication → AIDLC-27 Base Authorization → AIDLC-28 Tool Policy
→ AIDLC-29 Human Approval → Resource Binding → Tool Execution
```

AIDLC-28 determines `REQUIRE_APPROVAL` by operation and initiative. The approval
service invokes that trusted server-side service itself; callers cannot submit a
fabricated `ToolPolicyDecision`. Approval never grants missing base permission or
overrides a tool-policy denial. The service does not call any tool.

## Lifecycle and identity

```text
PENDING ──→ APPROVED
        └─→ REJECTED
```

Only one transition from pending is permitted. The requester and approver are
separate stable `Principal.subject_id` values. The approver must be attested as
human by an injected trusted identity boundary and must pass AIDLC-27 for
`initiative.approval.manage` in the same initiative. This grant is explicit in
`RolePolicy`; `platform.manage` alone does not grant it. Self approval is denied.
The test human verifier uses an allowlist and is not a production identity source.

## Persistence and session independence

`ApprovalRepository` owns approvals as operational state, independent of browser
or chat sessions. It supports ID lookup, pending-by-initiative, requested-by,
decided-by, version history, and audit history. An in-memory adapter supports
tests. A file-backed SQLite adapter demonstrates pending approvals surviving
new service instances and process restarts without AWS. DynamoDB is the intended
production store under ADR-006; no AWS resource or SDK is introduced here.

Each commit atomically writes the current snapshot, an immutable history version,
and an audit event. The repository rejects a stale `expected_version`. Concurrent
approve/reject attempts cannot both win. A failed audit write rolls back the
state change. Persistence and audit failures are distinct from authorization
denials, and neither returns successful execution permission.

## Idempotency

The caller supplies a stable `request_key` per protected action. Repeating the
same request key for the same principal, initiative, operation, logical target,
and configuration revision returns the existing approval without appending
history. Reusing it for a different action is a conflict. An exact retry of the
same terminal decision by the same approver returns the terminal snapshot;
opposing or later decisions fail. The repository enforces unique request keys.

## Decision lineage and replay protection

The record captures the AIDLC-27 base decision ID, AIDLC-28 tool-policy decision
ID, operation, deterministic risk, logical target, requester, initiative, and
configuration revision. An approval for a Git feature branch cannot be reused
for another branch or a Jira action. `approval_gate_satisfied` checks exact
request/revision binding and re-evaluates current AIDLC-27/AIDLC-28 gates before
returning true. Protected execution must use a trusted current Initiative Profile
and revision from the Registry. If either earlier gate now denies, or policy no
longer requires approval, the stored approval cannot make the action executable.
Pinned revision attribution survives rollback; future execution integration
should compare its selected revision to this record and re-request approval when
it changes.

`APPROVED` means only that the human gate was satisfied. It does not execute a
tool or authorize a different operation. No bearer approval token is issued.

## Audit

Request, approval, and rejection append immutable events with event ID, time,
status transition, requester/approver stable IDs, initiative, operation, logical
target, configuration revision, and both upstream decision IDs. They omit
credentials, provider claims, email, ticket bodies, code contents, and physical
resource bindings or free-text comments. The injected repository is also the
transactional audit boundary, so state and audit cannot commit separately.

## DynamoDB production access patterns

A production adapter should use conditional/transactional writes for current
state, append-only version history, and audit events. A possible partition is
`APPROVAL#<approval_id>` with `CURRENT`, `VERSION#000001`, and `EVENT#...` sort
keys. A unique request-key item prevents duplicate workflows. Projected indexes
should support pending approvals by initiative, requested-by principal, and
decided-by principal; task/workspace indexes can be added when those contracts
exist. Approval history is queried by ID and ordered version. A transaction or
outbox with equivalent durable guarantees must ensure audit failure cannot
leave a successful state transition. Optimistic version checks must be atomic.

## Agent and execution boundary

Agents may request operations through trusted server entrypoints but cannot
approve them, classify risk, change tool policy, or grant themselves membership.
The approval API accepts an authenticated `Principal`; the human verifier must
deny service/agent identities. The future tool gateway must check current base
authorization, tool policy, exact approval binding, and Resource Binding before
execution. No Jira, Git, ServiceNow, UI, notification, or deployment integration
is implemented here.
