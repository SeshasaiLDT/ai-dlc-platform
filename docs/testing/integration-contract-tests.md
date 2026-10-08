# Integration contract tests

Status: Implemented in AIDLC-37.

A contract test composes the existing application services and tests their
observable guarantees across Jira, ServiceNow, remote Git, and Gateway. It
checks the path from trusted identity through policy, approval and Resource
Binding resolution to provider execution and a normalized result. Domain tests
remain in place for detailed provider and operation behavior.

## Organization and execution

The suite lives in `tests/contracts/`. Shared test composition lives in
`tests/support/`: one enterprise world uses the same principal, two initiatives,
membership/role service, exact policies, approval service, binding registry,
and real governed handlers. Domain-specific in-memory providers sit behind
call-recording spies. A loopback transport simulates AgentCore Lambda metadata
and exercises the real runtime facade, invocation registry, target, router,
and governed service.

Run all tests, or select only the contract layer:

```sh
.venv/bin/python -m pytest -q
.venv/bin/python -m pytest -q -m contract
.venv/bin/python -m pytest -q tests/contracts
.venv/bin/ruff check src tests infrastructure/agentcore
.venv/bin/ruff format --check src tests infrastructure/agentcore
git diff --check
```

The registered `contract` marker permits CI to run this subset independently.
The subset uses only local configuration, in-memory adapters, temporary paths,
finite executor fakes, and a deterministic fake DynamoDB client. No production
credentials or provider accounts are needed. An autouse fixture rejects socket
connections and also fails teardown if a service catches and normalizes a
network attempt. These tests neither call AWS nor require a deployed Gateway.

## Common contract matrix

| Guarantee | Jira | ServiceNow | Remote Git |
| --- | --- | --- | --- |
| Happy reads/search and approved writes | Issue detail/search and all four writes | Incident/request detail/search and all three writes | Repository/branch/diff/PR reads and all four writes |
| Authorization | Profile and narrowed projects | Profile and narrowed logical scopes/types | Profile and narrowed repository IDs |
| Read/write separation | Every write denied to read-only principal | Every write denied to read-only principal | Every write denied to read-only principal |
| Policy and approval | Exact deny; human approval bound to initiative/target/revision | Same, retaining record scope | Same, retaining branch target |
| Binding failures | Missing, disabled, wrong environment/initiative | Same | Same plus logical-ID/provider mismatch |
| Upstream errors | Normalized safe errors | Same | Same |
| Timeout/transient retryability | Read retryable; write non-retryable | Same | Same |
| Provider result revalidation | Project, issue identity, page and typed model | Scope, type, journal channel, page and typed model | Repository, branch/PR/diff identity, payload bounds and typed model |
| Audit and cancellation | Sensitive content omitted; audit failure blocks success; cancellation stops provider | Same | Same |

Tests assert that scope, permission, policy, approval, schema and binding denials
prevent provider execution at the applicable boundary. Binding existence does
not grant access. The same principal can select either initiative and receives
only that initiative's projects, ServiceNow scope, and repositories; Git
bindings also select different provider fakes without application conditionals.

Successful results contain normalized logical IDs and safe envelopes.
Malformed or cross-scope provider data returns
`MALFORMED_UPSTREAM_RESPONSE`, without unsafe data. Errors contain no raw
exception details, provider credentials, connection aliases or physical routing.
Timeout and transient-failure cases assert retryability and a single provider
attempt; these tests introduce no retries.

## Gateway and invocation storage

`test_gateway_contracts.py` covers filtered discovery, deterministic catalog
ordering/version, strict schema generation, excluded local/delete operations,
and direct invocation of hidden writes through each static target. Hidden calls
still reach the governed application service and receive permission denial.
Tool visibility remains separate from invocation authorization.

The runtime-to-target path tests one-use references, replay, explicit expiry,
missing/unknown references, wrong Gateway/target/tool/MCP message metadata,
argument mutation, unsupported/missing AgentCore versions, changed identity or
initiative during re-resolution, initiative revision changes, and broadened
permissions. A reference issued for another user/initiative cannot be injected
into model arguments. Narrowed scopes, trusted approvals, task identity,
approved commit pairs and cancellation pass through the trusted path.

These references are bearer capabilities confined to trusted runtime transport.
Tests prove the supported harness prevents an agent from submitting a reference
and that references do not appear in model schemas, results or telemetry. They
do not claim that a leaked reference plus its matching message ID independently
authenticates an end user; deployment must keep raw Gateway IAM access and
transport state inside the trusted harness.

Schema-projection cases deliberately pass AgentCore's basic property-type and
required-field shape while violating a Pydantic limit, enum or ref validator.
The real handlers still return `INVALID_ARGUMENT` before provider execution.

`test_invocation_storage_contracts.py` uses the production DynamoDB adapter with
a fake client to prove conditional `PutItem`, hashed reference keys, atomic
`DeleteItem` with `ALL_OLD`, concurrent one-use consumption, replay rejection,
and fail-closed handling of malformed stored state. Expiry is checked by the
application even when an expired item remains in the table; asynchronous
DynamoDB TTL deletion is cleanup rather than the authorization clock.

CloudFormation assertions verify IAM/MCP Gateway configuration, handler-derived
registrations, scoped runtime Gateway/write permissions, scoped Gateway Lambda
permissions, target delete permissions, function-name-derived ARNs in the same
partition/region/account, invocation-table billing/TTL/encryption, and safe
deployment outputs. Template shape is tested without AWS.

## Local workspace and remote Git handoff

`test_workspace_remote_handoff.py` composes the real workspace service with a
finite fake manager/executor and the existing handoff store. A local commit
creates a principal/task/workspace/initiative/repository-bound
`TrustedCommitHandoff`. Trusted runtime code reads approved repository/SHA pairs
from that store, carries them through Gateway correlation, and authorizes an
approved remote branch update. Agent-supplied handoff claims and wrong
task/principal/workspace/initiative/repository pairs fail.

Local operations work through their independent local handler and never invoke
a remote provider. Workspace names remain absent from enterprise discovery and
cannot be routed by Gateway. Local patch/build output and commit bodies remain
absent from audit. A local audit failure prevents publication of the handoff.
The existing workspace tests continue to cover real temporary-directory Git,
process controls, containment, and cleanup details.

## Regression found

The shared typed-provider-response contract found that Jira accepted an existing
Pydantic model modified through `model_copy(update=...)` without revalidating its
fields. Jira now dumps and revalidates provider models, matching ServiceNow and
remote Git. The contract test proves invalid fields fail closed even when the
provider returns an object with the expected model class.

## Deployment smoke tests

Local contracts prove schema, policy, normalization, correlation, handoff and
declarative IAM shape. A deployment must separately validate:

- Real AgentCore Lambda metadata, message version, tool prefix and MCP message
  ID preservation by the runtime transport.
- Actual IAM principal/role wiring, function/account/region identifiers, and
  shared invocation-table access across runtime and targets.
- Live provider authentication, managed connection/secret resolution, outbound
  networking and provider response translation.
- DynamoDB service semantics and production observability/redaction delivery.

Those checks require approved deployment sandboxes. The contract suite does not
provision infrastructure or add live provider clients.
