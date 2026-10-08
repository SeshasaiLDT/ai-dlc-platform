# AgentCore enterprise Gateway configuration

Status: AIDLC-36 application catalog, runtime adapter, and CloudFormation configuration implemented. Deployment requires existing authenticated agent runtime and target Lambda deployments.

## Boundary

```mermaid
flowchart LR
  A[Agent] --> H[Authenticated agent harness]
  H --> L[Trusted initiative-aware discovery]
  H --> G[AgentCore Gateway MCP]
  H --> W[Local workspace capability]
  G --> T[Domain Lambda target adapter]
  T --> C[Trusted runtime context lookup]
  T --> J[Jira handler]
  T --> S[ServiceNow handler]
  T --> R[Remote Git handler]
  J --> P[Existing governed services]
  S --> P
  R --> P
  P --> B[Resource Binding resolution]
  B --> X[Managed provider adapters]
```

The `GatewayCatalog` composes the three existing MCP-facing handler catalogs. It registers 6 Jira, 5 ServiceNow, and 9 remote Git operations. It excludes all delete operations, local Git/build/test operations, and arbitrary provider calls. Names and input schemas come from the strict handler request models. A deterministic hash identifies catalog changes. The CloudFormation synthesis projects those schemas to AgentCore's smaller `SchemaDefinition` shape; required fields and property types derive from the handlers, while the existing Pydantic models retain full validation (patterns, limits, enums, and extra-field rejection). Regenerate `infrastructure/agentcore/gateway.json` with `PYTHONPATH=src .venv/bin/python infrastructure/agentcore/generate.py`.

Gateway target names prefix tool calls (`Jira___jira_get_issue`, for example). The thin `GatewayLambdaTarget` checks that prefix, reads only AWS-injected invocation metadata, asks a trusted session lookup for `TrustedGatewayContext`, and passes the event's business arguments to `GatewayRouter`. The router translates centrally to `TrustedJiraContext`, `TrustedServiceNowContext`, or `TrustedGitContext` and invokes the existing handler. It has no provider client, credential lookup, Resource Binding lookup, or business-policy implementation. Unknown names fail closed.

## Discovery and authorization

`GatewayContextResolver` starts with an authenticated principal and selected initiative supplied by the trusted runtime session. It reads the existing Initiative Registry and membership/role policy, constructs the narrowed `ResolvedAuthorizationContext`, then forms `TrustedResolutionContext`. Agent tool arguments cannot set principal, initiative, environment, permissions, scopes, revision, approval, or approved commit handoffs.

`GatewayDiscovery.available_tools(context)` is read-only and deterministic. It requires the base read/write permission, enabled integration, at least one authorized logical scope, and an applicable exact operation policy. Explicit deny or ambiguous matching rules hide the operation. Git writes additionally require a `READ_WRITE` repository. Profile-disabled writes are hidden. Approval-required operations remain visible with `approvalRequired=true`; discovery never grants approval. Branch-specific policies are conservatively evaluated against configured default branches for operation-level discovery. Exact project, record, repository, branch, and approval checks still run on every invocation in the existing application service.

AgentCore Lambda targets publish a **static superset** of the approved enterprise operation names. AgentCore's Lambda target discovery does not receive AI-DLC's selected Initiative authorization context. `GatewayRuntimeAccess` is the harness facade: it exposes only `GatewayDiscovery` results, rejects hidden names, and sends eligible invocations through an injected Gateway transport using runtime-held credentials. The authenticated harness must be the only IAM principal allowed to invoke this Gateway. The model must not receive raw Gateway credentials or raw `tools/list` output. Direct calls to a hidden registered name still reach the governed service and are denied there. This distinction is material: static Gateway registration alone is not per-user discovery authorization.

## CloudFormation and IAM

The checked-in AWS-native CloudFormation template creates:

- An `AWS_IAM` AgentCore MCP Gateway and three Lambda targets for the approved domains.
- A Gateway execution role that may invoke only the three supplied target Lambda ARNs. Its trust is restricted to AgentCore in the deploying account and Gateway ARN namespace.
- A scoped `bedrock-agentcore:InvokeGateway` policy attached to the supplied existing runtime/harness role name.
- Safe outputs: Gateway ARN/ID, environment, and catalog version.

There is no second IaC framework in this repository. CloudFormation parameters supply the environment, existing runtime role name, and deployed Jira/ServiceNow/remote Git target Lambda ARNs. These are trusted deployment identifiers, not provider endpoints or secrets. No physical Initiative Resource Bindings, Jira projects, ServiceNow instances, repository coordinates, passwords, or tokens are in the Gateway template. The target Lambdas require their own execution roles, provider-port composition, authenticated session lookup, approved egress, and log delivery. The template does not provision them or create live Jira/ServiceNow/Git HTTP clients. Deployment should restrict Gateway IAM access to the runtime role and validate that no other principal can invoke the Gateway. The target Lambda session lookup must be populated by authenticated runtime infrastructure from verified user/session state; it must fail closed if no session exists. AWS invocation metadata is a lookup key, never an authorization grant.

AgentCore Gateway itself is managed. `JsonGatewayTelemetrySink` emits only correlation ID, principal/initiative, action, tool name, outcome, and latency to the trusted runtime logger. Existing governed services retain their operation audit. Deployments should deliver Gateway/target logs and CloudTrail data events to approved stores and set retention/metrics there. Tool arguments, issue descriptions, journal bodies, PR text, diffs, provider credentials, and Resource Binding details are excluded from Gateway telemetry. Existing AWS topology requires controlled outbound access; this stack does not assume public provider egress or claim VPC isolation.

## Local workspace and follow-up

AIDLC-35 `workspace.local` remains a separate harness/local runtime capability with task isolation and sandbox requirements. It is absent from the enterprise Gateway catalog and CloudFormation targets.

This ticket provides declarative Gateway registration and the application/runtime transport contracts. A live deployment still needs a packaged AgentCore runtime/harness, deployed target Lambdas, a trusted session lookup, production provider adapters/managed connection resolution, egress configuration, and observability delivery. Those components are deployment prerequisites because this repository currently has no executable AgentCore runtime or live provider transport. AIDLC-37 owns broader cross-integration contract and failure testing.

AWS references: [Gateway CloudFormation resource](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-bedrockagentcore-gateway.html), [Gateway target CloudFormation resource](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-bedrockagentcore-gatewaytarget.html), [Lambda target event format](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-add-target-lambda.html), [IAM inbound authorization](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-inbound-auth.html).
