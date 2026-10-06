# Centralized authorization service

Status: Application decision boundary implemented (AIDLC-27)

## Responsibility

```text
Authenticated Principal + selected Initiative + typed operation + logical target
                                  ↓
                         AuthorizationService
                                  ↓
                       audited ALLOW or DENY
```

Authentication (AIDLC-26) answers *who are you?* and returns a `Principal`.
Authorization answers *may this principal perform this operation in this
initiative?* The service takes the current trusted Initiative Profile, injected
role policy and membership repository, and optional trusted scope restriction.
It calls AIDLC-25 `resolve_authorization_context` rather than repeating
membership, grant aggregation, or scope intersection.

## Centralized server-side enforcement

`evaluate` returns an immutable `AuthorizationDecision` with a typed reason.
`require` records the same decision and raises `AuthorizationDeniedError` on
denial, so protected work after it cannot run. Future API, Orchestrator, Agent
Harness, MCP/tool gateway, and admin entrypoints must use this boundary before
protected operations. UI route hiding, disabled buttons, agent instructions,
and LLM reasoning cannot authorize an action.

Only trusted server code supplies the Initiative Profile, role policy,
membership repository, and optional member/policy scope restriction. The
request carries no roles, grants, permission sets, or resolved context for a
client or model to modify. A model cannot add capabilities, tools, memberships,
or logical scopes, or override a denial. Agents receive a trusted platform
execution context after control-plane authorization; they do not mint grants.
Later harness and tool integrations must also call the service at their action
boundaries rather than trusting a prior prompt or agent assertion.

## Capability authorization

`CapabilityAction` checks the named AIDLC-25 `Capability` grant. Generic role
names are resolved through `RolePolicy`; they have no hardcoded behavior in the
decision service. Missing membership, disabled membership, or absent grant
denies by default.

## Tool authorization

`ToolAction` checks the exact `ToolPermission`. Read does not imply write. Jira,
Git, and knowledge operations require a matching logical target; an omitted or
mismatched target is denied. This is base permission enforcement only. Detailed
tool operation rules, branch policies, and destructive action controls belong
to AIDLC-28.

## Administration

`AdminAction` checks the exact `AdminPermission`. Initiative membership/policy
administration and platform administration are distinct. Neither admin grant
implies capability execution or tool access. The current contract requires an
enabled membership in the selected initiative even for `platform.manage`; a
future platform-wide entrypoint would need an explicit, separate trust model.

## Logical resource scope

Jira project, repository, and knowledge source targets must appear in the
resolved `allowed_scopes`. AIDLC-25 intersects optional restrictions with
Initiative Profile configured scopes, so a restriction cannot expand access.
No physical Resource Binding, credential, database endpoint, or storage location
is resolved here. ServiceNow and artifact permissions currently have no
resource-specific logical target contract; their base grant is checked here,
with detailed policies deferred to later stories.

## Decisions and auditability

Each successful evaluation records one immutable `AuthorizationAuditEvent`
through `AuthorizationAuditSink`, for both allows and denials. Decision and
event carry a generated decision ID, UTC timestamp, stable principal ID,
initiative ID, typed action, logical target, allow/deny flag, reason code, and
optional Initiative Profile revision supplied by the trusted caller. They omit
email, display name, raw credentials, provider claims, and physical resources.
An in-memory sink supports deterministic tests. Durable audit persistence is
later work.

If the audit sink fails, the service raises `AuthorizationAuditError` rather
than returning an allow or pretending to deny. Thus protected work stops, and
callers can distinguish audit infrastructure failure from policy denial.
Unexpected membership repository failures also propagate as internal failures;
they are not mistaken for absent membership. This story does not implement a
retry or durable delivery policy.

## Future integration

Authentication adapters, tool-level policy (AIDLC-28), human approvals
(AIDLC-29), Resource Bindings, and concrete API/agent/tool integrations remain
separate. Future entrypoints must obtain the profile/revision and policy from
trusted platform state and call `require` immediately before protected work.
