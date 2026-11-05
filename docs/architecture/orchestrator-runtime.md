# Enterprise Orchestrator runtime (AIDLC-54)

The single-agent control component accepts `POST /invocations` with JSON:

```json
{"initiative_id":"travel-platform","capability":"investigation","invocation":{"input":{"question":"Investigate this issue"}}}
```

The ASGI host implements the AgentCore HTTP container contract: port 8080,
`GET /ping`, `POST /invocations`. See the
[AWS HTTP contract](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-http-protocol-contract.html).
There was no existing runtime container in this repository; the Gateway
CloudFormation stack remains separate. Build the container from the repository
root using `docker buildx build --platform linux/arm64 -f
infrastructure/agentcore/orchestrator/Dockerfile .`.

Set `AIDLC_ORCHESTRATOR_FACTORY=deployment.composition:dependencies`. This is a
trusted deployment configuration value, never request input. That factory must
return `ai_dlc.adapters.orchestrator.host.Dependencies(authentication,
orchestrator)`. The host fails startup without the factory. A deployment package
must include its composition module and credential/repository adapters in the
image (extend the supplied Dockerfile). No permissive fallback is installed.

Composition follows existing constructor injection:

```python
registry = AgentRegistry(agent_repository)
contexts = GatewayContextResolver(initiatives, memberships, role_policy)
authorization = AuthorizationService(memberships, role_policy, authorization_audit)
service = Orchestrator(
    registry,
    contexts,
    authorization,
    a2a_transport(call_context_factory=outbound_authenticated_call_context),
)
return Dependencies(AuthenticationService(identity_provider_adapter), service)
```

These names are deployment-owned dependencies, not global application objects.
`agent_repository` implements the AIDLC-53 `AgentRegistryRepository` port.
Use the same shared repository as agent registration. No shared adapter exists
in this repository yet. Its in-memory adapter supports local tests in one
process only; separately hosted services require a durable shared adapter.
Initiative and membership repositories, role policy, audit sink, validated
identity provider adapter, and outbound A2A credential factory must also be
supplied by the deployment. Identity-provider provisioning is external.

The HTTP host authenticates its Bearer credential using AuthenticationService;
caller-supplied identity, grants or permission fields are rejected. Configure
AgentCore's authorizer and header forwarding so the validated bearer credential
reaches this boundary; do not treat arbitrary identity headers as authenticated.
The service resolves active initiative configuration and current membership,
then calls the existing audited AuthorizationService for the requested capability.
It derives manifest permission eligibility from resolved tool permissions. These
labels do not authorize tool execution: receiving agents still enforce exact
resource scope, operation policy and approval through existing governed services.

Discovery uses the capability enum value as the registry capability name.
Exactly one enabled, eligible, non-unhealthy manifest is required. Zero returns
`agent_not_found`; multiple candidates return `ambiguous_agent`, without dispatch.
Administrators must adjust registrations/requirements to resolve ambiguity.
There is no built-in agent catalog or arbitrary first-match selection.
Input is checked against the selected manifest schema. The endpoint becomes an
official A2A Agent Card in ConfiguredAgentDirectory and the existing A2AClient
performs delegation. No endpoint can be supplied by the request.

The server generates task/request/correlation/trace identifiers and a bounded
deadline. Trusted AgentContext reaches the A2A client; only existing lineage
hints go onto the wire, with transport credentials supplied separately. The
receiving A2A boundary must authenticate and re-resolve initiative authorization;
permission snapshots are never serialized as authority. ExecutionResult is
returned with lineage intact. Authorization/context failures become
`authorization_failed`; malformed HTTP payloads return 400, missing/invalid
credentials 401. Transport errors are sanitized; timeout returns
`deadline_exceeded`. Existing A2A result errors are preserved. Cancellation
propagates; a local timeout cannot prove remote execution stopped.

Provisioning still required: publish the ARM64 image to ECR, supply the runtime
execution role, network access, AgentCore HTTP runtime authorizer, forwarded
Authorization header, environment factory setting, and dependency adapters.
Use AgentCore HTTP protocol configuration with the container's default 8080
port and invocation path. This change creates no AWS resources and deploys
nothing. Model selection remains with agents and existing model-governance
services; no model calls or multi-agent planning occur here (AIDLC-55).
