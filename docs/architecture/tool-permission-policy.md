# Initiative tool-permission policy

Status: Application contracts implemented (AIDLC-28)

## Layering

```text
Authentication → AIDLC-27 base authorization → AIDLC-28 tool policy
               → AIDLC-29 approval → Resource Binding → execution
```

`ToolPolicyService` constructs the required AIDLC-27 `ToolAction` from the
typed operation and calls `AuthorizationService.evaluate` itself. A caller
cannot submit a base allow decision, a permission flag, a role, or a policy
effect in `ToolPolicyRequest`. The base decision must allow the same principal,
initiative, permission, and logical target before any tool rule is considered.
The base decision ID is retained in the tool-policy audit trail. A base denial
always becomes a tool-policy denial; a later layer never expands it.

Having `git.write` or `jira.write` is necessary but does **not** authorize all
writes. An exact matching initiative policy must also permit the operation.
Existing Initiative Profile write switches and Git repository access modes are
upper bounds: a disabled write switch or read-only repository denies even when
an operation rule says `ALLOW`. A Profile write approval flag upgrades an
explicit operation `ALLOW` to `REQUIRE_APPROVAL`; it never weakens a deny.

## Tool operations and risk

Jira, Git, and ServiceNow operations use separate typed enums. A fixed
platform-owned mapping classifies each operation as `READ`, `WRITE`, or
`DESTRUCTIVE` and maps it to the corresponding AIDLC-25 read/write permission.
Destructive operations require the existing write permission plus an explicit
operation policy. A model or agent cannot reclassify risk through prompt text.

## Policy outcomes and enforcement

- `ALLOW` permits the tool-policy layer to continue.
- `DENY` stops execution.
- `REQUIRE_APPROVAL` stops immediate execution and signals a later approval
  gate; it is not an approval or execution authorization.

`require_allowed` returns only on `ALLOW`. It raises a typed denial or
approval-required error for the other outcomes. AIDLC-29 will implement
approval requests, state, decisions, and persistence. No approval workflow is
created here.

## Initiative-specific rules and default deny

Immutable `ToolPolicy` records contain an initiative ID, typed operation,
effect, optional exact logical target ID, and optional Git branch glob. The
repository port loads policies for one initiative and tool. Different
initiatives may configure different effects for the same operation without
changing evaluator code. Role-to-base permission differences remain in
AIDLC-25/AIDLC-27; this layer does not inspect roles or memberships.

Matching requires the exact initiative and operation, then the optional
logical target and branch pattern. There is no implicit inheritance from a
read or write operation to another operation. **No matching policy means
`DENY`.** If policies exist for the operation but none matches the target,
the reason is `TARGET_NOT_ALLOWED`. If more than one rule matches, the outcome
is `DENY` with `AMBIGUOUS_POLICY`. Repository order never chooses a winner.

## Git policy

Git requests contain a logical repository ID and optional branch name.
Policies may match an exact repository ID and a deterministic glob-style
branch pattern. `*` is the only wildcard; matching is case-sensitive and
does not run arbitrary regular expressions. For example, configuration can
allow `feature/*`, deny `main`, and require approval for `release/*`.
These names are examples, not built-in protected branches. Overlapping rules
deny as ambiguous. Branch creation, push, deletion, and PR operations are
represented but never executed here. Branch-sensitive operations require a
branch target; omitting it cannot bypass a protected-branch rule.

## Jira and ServiceNow policy

Jira requests carry a logical project ID already checked against AIDLC-27
allowed scope. Rules can distinguish read/search, comments, updates,
transitions, creation, and deletion. This story does not inspect Jira fields
or transition schemas.

ServiceNow requests carry a logical scope ID. AIDLC-27 checks the base
ServiceNow read/write grant; AIDLC-28 additionally requires the scope to be
enabled in the trusted Initiative Profile before matching operation rules.
This story does not inspect ServiceNow record schemas or call its API.

## Audit and failure behavior

Each completed tool-policy evaluation emits an immutable event with decision
ID, UTC timestamp, stable principal/initiative IDs, tool, operation, risk,
logical target, effect, reason, and base authorization decision ID. Audit
events omit credentials, provider claims, ticket/file contents, and physical
Resource Bindings. An audit sink failure raises `ToolPolicyAuditError` and
returns no allow result. Policy repository failure raises a distinct
`ToolPolicyConfigurationError`; it does not fall back to allow.

## Agent and resource boundaries

Agents may request a typed operation and logical target. They cannot supply
the permission mapping, risk classification, policy rule, protected branch
set, approval result, or base authorization outcome. Future tool gateways
must call `require_allowed` before executing a tool, then apply AIDLC-29
approval and Resource Binding. This layer knows no Jira site, Git host,
credential, database location, or storage bucket.
