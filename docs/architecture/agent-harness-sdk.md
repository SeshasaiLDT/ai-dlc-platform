# Agent Harness SDK (AIDLC-45)

`ai-dlc-agent-harness` lets independently deployed agent runtimes (including AWS Bedrock AgentCore Runtime) share one harness. There is **one implementation**: the platform tree under `src/ai_dlc/`. `sdk/agent-harness/build_sdk.py` copies a fixed set of modules into a temporary staging directory and builds a wheel and sdist from it. Nothing is duplicated in git.

## Package layout and import path

The import path is unchanged: `ai_dlc.application.agent_harness`. The wheel contains only the harness and the trusted-context contracts it imports:

| Module | Why |
| --- | --- |
| `ai_dlc.application.agent_harness.*` | The SDK |
| `ai_dlc.domain.identity.*` | `Principal`, memberships, permission enums |
| `ai_dlc.domain.authorization` | `ResolvedAuthorizationContext`, `LogicalScopes` (moved from `application.authorization.models`) |
| `ai_dlc.domain.approval` | `ApprovalStatus` (moved from `application.approval.models`) |

The only refactor was moving those pure contracts so the SDK does not pull in the authorization, approval, tool-policy, or initiative services. The old import paths re-export the same objects. Authorization, approval, and Gateway **services and adapters stay in `ai-dlc-platform`**; the SDK only defines ports for them. `ai_dlc`, `ai_dlc.application`, and `ai_dlc.domain` are namespace packages in the wheel (no `__init__.py` shipped), so the SDK and platform can coexist. A test enforces the import boundary.

## Dependencies

| Group | Packages | Install |
| --- | --- | --- |
| Core | `pydantic>=2.12,<3`, `jsonschema>=4.23,<5` | `ai-dlc-agent-harness` |
| `a2a` extra | `a2a-sdk>=1.2,<1.3`, `httpx>=0.28,<1` | `ai-dlc-agent-harness[a2a]` |

No AWS, YAML, or model-provider packages are required. `A2AClient`, `A2AServerAdapter`, `ConfiguredAgentDirectory`, and `build_a2a_handler` load lazily; without the extra they raise an `ImportError` naming it. Model, Gateway, and Bedrock adapters belong to the agent runtime. `sdk/agent-harness/constraints-tested.txt` lists the exact versions the tests ran with; install with `pip install -c constraints-tested.txt` for reproducible builds.

## Versions

Four versions move independently:

- **SDK distribution** (`ai-dlc-agent-harness`, currently `0.1.0`): semantic version of the Python distribution. Pre-1.0 until the packaging and registry flow have been exercised by a real consumer.
- **Harness interface** (`INTERFACE_VERSION`, currently `1.4.0`): the public contract policy in [agent-harness-interfaces.md](agent-harness-interfaces.md).
- **A2A wire protocol** (1.0) and `a2a-sdk` version: unchanged.
- **Agent capability version**: each agent's own Agent Card.

| SDK | Interface | Notes |
| --- | --- | --- |
| 0.1.0 | 1.4.0 | First package; no behavior change |

A new SDK release that only changes packaging or fixes bugs does not bump the interface. Agents should check the interface at startup:

```python
from ai_dlc.application.agent_harness import INTERFACE_VERSION

major, minor, _ = (int(p) for p in INTERFACE_VERSION.split("."))
assert major == 1 and minor >= 4, INTERFACE_VERSION
```

## Quick start

```bash
pip install "ai-dlc-agent-harness[a2a]==0.1.0" -c constraints-tested.txt
python sdk/agent-harness/examples/reference_agent.py   # offline demo
```

```python
from ai_dlc.application.agent_harness import LifecycleRunner, Invocation, create_agent_context

context = create_agent_context(
    task_id=task_id, authorization=resolved_snapshot
)  # trusted boundary only
result = await LifecycleRunner().run(MyAgent(...), Invocation(input={...}), context=context)
```

## Reference agent

`sdk/agent-harness/examples/reference_agent.py` (not in the wheel) is one small workflow built from SDK interfaces; every capability is injected.

```
trusted AgentContext -> LifecycleRunner -> ReferenceAgent
   tool read (McpClient -> governed ToolProvider)      via ResilientExecutor
   context assembly (ContextAssembler, untrusted evidence)
   model call (ModelProvider)                          via ResilientExecutor
   structured output (StructuredOutputValidator)
   optional A2A delegation (AgentDelegator)
   optional write: ApprovalProvider -> idempotent tool write
   telemetry (TelemetryProvider)
```

Fakes (`FakeModel`, `PolicyToolProvider`, `InMemoryApprovals`, `PrintTelemetry`) stand in for Bedrock, the Gateway, the approval service, and telemetry, so it runs without credentials. Replace them with real adapters; the agent code does not change. Security properties shown and tested:

- Identity, initiative, and permissions come only from the trusted `AgentContext`; model output with extra identity fields fails validation.
- The tool provider, not the agent, enforces permissions and approval. In production that is `GatewayToolProvider`, which calls the platform authorization, tool-policy, and approval services. The SDK does not reimplement them.
- Writes need a human-approved `ApprovalReference` verified by the provider, a derived idempotency key, and an `IdempotencyStore`; there is no bypass.
- Retrieved text is untrusted evidence, scoped to the initiative; no credentials or identity are put in prompts or A2A payloads.

## AgentCore Runtime

Each runtime builds its own container image or package, pins `ai-dlc-agent-harness[a2a]==X.Y.Z` with the constraints file, and supplies configuration at start-up:

- **Trusted entrypoint:** authenticate the caller using the platform authentication boundary, resolve `ResolvedAuthorizationContext` with the platform authorization service, then `create_agent_context(...)`. Never build it from request payloads.
- **Model:** inject a `ModelProvider` adapter and a `ModelCapability` (model ID and limits) from environment-specific configuration. The SDK hard-codes no model limits, Runtime ARNs, or credentials.
- **Tools:** inject `McpClient((GatewayToolProvider(...),))` from the platform package, or another governed `ToolProvider`.
- **A2A:** use `A2AServerAdapter` with an agent factory and `build_a2a_handler` behind the runtime's HTTP entrypoint, and `A2AClient` with a `ConfiguredAgentDirectory` of allowed Agent Cards. Credentials go in the `call_context_factory`, never in message data.
- **Persistence:** supply a durable `IdempotencyStore` and an `ApprovalProvider` adapter; the shipped in-memory store is single-process only.

Agents that use the Gateway adapters also depend on `ai-dlc-platform`; that is an agent-side choice, not an SDK requirement. Do not install `ai-dlc-platform` and the SDK from different sources in one environment: until the platform wheel consumes the SDK as a dependency, it still contains the same harness files.

## Build and internal publishing

```bash
pip install -e ".[dev]"
python sdk/agent-harness/build_sdk.py --out dist/sdk      # wheel + sdist; --no-isolation for offline
pytest tests/test_sdk_package.py tests/test_reference_agent.py
```

No registry is configured and nothing is published to public PyPI. Releasing is an explicit manual step once an approved internal registry exists:

1. Bump `version` in `sdk/agent-harness/pyproject.toml` and the compatibility table; update release notes.
2. Run `ruff check .`, `ruff format --check .`, `pytest -q`, then the build above on a clean checkout.
3. Tag `agent-harness-sdk-vX.Y.Z` and record the artifact SHA-256.
4. Upload with the registry's own tooling and credentials (for example `twine upload --repository-url <approved-registry> dist/sdk/*`); credentials come from the CI secret store, never the repository.

## Upgrade and rollback

1. Read the release notes (SDK changes, interface version, dependency changes, migrations).
2. Pin the exact version in the agent (`ai-dlc-agent-harness[a2a]==X.Y.Z`) with the matching constraints file; no floating ranges.
3. Upgrade one agent at a time, starting with non-production. Agents stay independently deployable: interface minors are backward compatible and A2A wire compatibility is unaffected, so mixed versions can run side by side. Upgrade across an interface **major** only agent by agent following the migration note.
4. Run the agent's unit tests, its contract tests with fakes, and an integration run against its real adapters (model, Gateway, A2A peer, idempotency store, approval).
5. Deploy, watch telemetry (`resilience.*`, `context_budget.*`), and promote.
6. **Rollback:** redeploy the previous pinned version. Interface minors add only; do not rely on new members until the rollout is complete. If a release changed stored idempotency or approval data, the release notes must say so and the rollback must be tested first.

Breaking changes (removing members, changing required fields or signatures, or identity/authorization semantics) require an interface major bump, a deprecation in the preceding minor, and a before/after migration note. Release notes must list SDK version, interface version, dependency range changes, behavior changes, and deprecations.

## Known limitations

- The platform wheel still includes the harness files; it does not yet depend on the SDK distribution.
- `ai-dlc-platform` services (Gateway adapters) are not part of the SDK, and no production agent is migrated here.
- No internal registry, signing, or release automation is configured.
- Offline tests install the wheel with `--no-deps` and expose only the core dependencies; a real resolver run (including `[a2a]`) was checked manually and is not part of CI.
- Dependency upper bounds follow what was tested and may need widening as consumers adopt newer releases.
