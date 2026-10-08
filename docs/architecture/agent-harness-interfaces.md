# Shared Agent Harness interfaces

The public Python interface version is **1.0.0** (`INTERFACE_VERSION`). It currently ships inside the `ai-dlc-platform` distribution, whose version remains 0.1.0. The harness is a library imported by independently deployed agents. It does not run a central execution service or define an A2A wire schema.

`AgentLifecycle` owns initialize, execute, and shutdown. `AgentContext` is immutable, trusted in-process state: request, correlation, session, and trace IDs; the existing `ResolvedAuthorizationContext`; an optional aware deadline; cancellation state; and optional JSON metadata. Its `principal` property comes only from the resolved authorization snapshot. The authenticated runtime creates this context after authorization. Agent input must never supply or replace the principal, authorization snapshot, scopes, approval decisions, or trusted gateway references.

`Invocation` and `ExecutionResult` are JSON-safe application boundary values. `ExecutionStatus` is `succeeded`, `failed`, `cancelled`, or `timed_out`. Every unsuccessful result has an `ExecutionError` (`code`, `message`, `retryable`, optional JSON `details`), and every result carries correlation and trace IDs. Metadata is optional. Adapters must propagate those IDs without accepting an agent-generated identity. A deadline is an absolute, timezone-aware instant; a cancelled context or expired deadline must prevent new external calls. Implementations should return `cancelled` or `timed_out` when they can report the outcome, and must propagate task cancellation when their caller cancels them. This library does not schedule or enforce deadlines.

`ToolProvider` lists and invokes tools; its implementation must apply existing Gateway discovery and governed invocation checks. `ModelProvider` invokes a configured model role. `AgentDelegator` calls an independently deployed agent through an A2A adapter and preserves identity, authorization, correlation, trace, timeout, and cancellation semantics. `Validator` performs local input checks. `ApprovalProvider` requests and reads human approval through a trusted adapter; its `ApprovalIntent` is not a policy decision, and `ApprovalReference` is not permission to execute. The adapter must use the existing `ApprovalService` and recheck the established authorization, tool-policy, approval, and resource boundaries before a protected operation. `TelemetryProvider` records names, numeric metrics, and structured errors using the trusted context; implementations must exclude credentials and sensitive payloads.

These are structural Python `Protocol`s. An agent can implement only the ports it consumes. For example:

```python
from ai_dlc.application.agent_harness import (
    AgentContext, ExecutionResult, ExecutionStatus, Invocation,
)

class EchoAgent:
    async def initialize(self) -> None:
        pass

    async def execute(self, request: Invocation, *, context: AgentContext) -> ExecutionResult:
        return ExecutionResult(
            status=ExecutionStatus.SUCCEEDED,
            correlation_id=context.correlation_id,
            trace_id=context.trace_id,
            output=request.input,
        )

    async def shutdown(self) -> None:
        pass
```

Agents own prompts, workflow steps, validation rules, and deployment. Future adapters own A2A serialization, model access, Gateway transport, approval access, and telemetry delivery. This package has no AWS, agent-framework, POS, or agent-specific dependency. Existing A2A behavior is unchanged; wire-protocol versions and independently published agent capability versions are separate from this SDK's Python package version.

## Compatibility policy

The public interface contract starts at 1.0.0. Follow semantic versioning for `INTERFACE_VERSION` and for any future separate SDK distribution: patches fix behavior without changing signatures; minors add optional fields or new ports while preserving existing valid calls and result parsing; majors may remove fields, change required fields, signatures, status meanings, or identity/authorization semantics. A release of the current platform distribution must state which interface version it contains. Deprecate a public member in a minor interface release, document its replacement, retain it through the next major release, and provide a migration note with before/after examples. Consumers should pin a compatible interface major and upgrade adapters and agents together for major changes. Contract tests with fake implementations check protocol use, JSON round trips, status/error invariants, and trusted context requirements; integration adapters must add their own A2A wire compatibility tests without treating `INTERFACE_VERSION` as an A2A protocol version. No version negotiation framework is introduced here.
