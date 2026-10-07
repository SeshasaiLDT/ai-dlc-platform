# ADR-004: Hybrid enterprise tool and MCP boundary

Status: Accepted

## Context

AI-DLC has reusable capability agents, initiative-scoped authorization,
operation-level tool policy, and an approval gate. Enterprise SaaS tools,
internal infrastructure adapters, and isolated code workspaces cross different
trust and execution boundaries. A universal transport would either add
unnecessary servers to internal functions or bypass governance for agent-facing
enterprise systems. Initiative Profiles contain logical scopes; AIDLC-100 will
resolve their physical environment bindings.

## Decision

Use a hybrid model. **MCP is an integration and governance mechanism, not a
requirement for every function in AI-DLC.** The governed tool entrypoint uses
the [enterprise tool contract](../architecture/enterprise-tool-contract.md)
for identity, initiative, logical scope, policy, approval, normalized results,
and audit regardless of execution mechanism.

| Mechanism | Intended use and examples | Security boundary and authorization | Discovery, credentials, and when not to use |
| --- | --- | --- | --- |
| MCP | Agent-facing enterprise SaaS/shared systems such as Jira and ServiceNow; optionally remote Git provider operations | Governed enterprise boundary with server-side AIDLC-27/28/29 checks and binding resolution | Registered, discoverable tools; credentials isolated in trusted adapter/gateway. Do not use merely to wrap internal code or local shell commands. |
| Direct SDK/API | Internal platform services, knowledge/persistence adapters, or infrastructure calls that are not agent-selected | Application service boundary with the same relevant server-side authorization and resource checks | Explicit dependency/port, not model discovery; credentials from runtime secret/configuration. Do not expose a direct client to agents to bypass tool policy. |
| Local execution | Checkout, status, diff, patch, file operations, build, and test in an isolated task workspace | Workspace ownership, containment, operation policy, approval where required, process limits | Governed local operation catalog; local workspace credentials, if any, stay outside prompts. Do not model as remote Git provider calls or a general enterprise MCP server. |

Remote Git provider operations are separate from local workspace operations.
The existing `GitOperation` enum classifies policy/risk; it does not collapse
these execution domains. Provider-specific behavior belongs behind adapters,
so new providers or initiatives do not change unrelated capability agents.

## Alternatives considered

1. **MCP for every operation:** uniform discovery, but internal functions and
   local execution gain no useful trust boundary and incur deployment and
   credential-handling overhead.
2. **Direct SDK/API everywhere:** simple for platform-internal calls, but each
   agent-facing enterprise integration would duplicate discovery, governance,
   audit, and credential isolation.
3. **Hybrid MCP + direct platform APIs + local execution (chosen):** aligns
   integration mechanism with the actual trust boundary while retaining one
   logical request, authorization, and result contract.

## Consequences

- Tools consume trusted, selected initiative context and logical resource IDs;
  agents cannot assert scopes or select physical endpoints.
- Read and write remain separately permissioned. A tool adapter cannot convert
  a denial or unsatisfied approval into execution.
- The tool entrypoint must normalize results/errors and retain sanitized
  diagnostics in server logs. Adapters require contract tests as they arrive.
- AIDLC-32 through AIDLC-36 and AIDLC-100 will implement adapters, local
  execution, runtime wiring, and Resource Bindings. This ADR deploys none of
  them.

## Revisit triggers

Revisit if a specific enterprise integration cannot meet required policy,
audit, or isolation guarantees through the chosen mechanism, or if measured
operational cost shows a different boundary is materially safer or simpler.
