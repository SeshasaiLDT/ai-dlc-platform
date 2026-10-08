# Deterministic Capability Routing

`ai_dlc.application.routing` (AIDLC-48) decides, without any LLM call, **which AI-DLC capability** handles a request when the user or a configured workflow already says so. Anything it cannot decide deterministically is returned as `UNRESOLVED` for the classifier fallback (AIDLC-49).

## Two different routings

| | Capability routing (this) | Model routing (AIDLC-46/47/future) |
| --- | --- | --- |
| Question | Which capability/agent handles the request? | Which LLM deployment serves a model role? |
| Inputs | explicit selection, workflow IDs, initiative profile | `ModelRole`, `ModelSelectionRequest`, model registry |
| Output | `RoutingDecision` (logical `Capability`) | an eligible deployment |

The router never touches `ModelProvider`, the model registry, roles or requirements, and never calls A2A. Capabilities reuse the existing `Capability` enum; no second taxonomy.

## Architecture

`DeterministicRouter(authorizer, telemetry=None).route(request, context=AgentContext, profile=InitiativeProfile, initiative_revision=int)`

- **Trusted:** `AgentContext` (principal, initiative, request/correlation/trace/task IDs), and the initiative profile + revision supplied by the caller from the Initiative Registry. The context is read, never modified.
- **Untrusted:** `RoutingRequest` (`explicit_capability`, `workflow_ids`, `content`). Free text is never matched, logged or copied; only exact identifiers are used. A model-generated hint has no field here, so it cannot become a user selection.
- **Authorization:** the existing `AuthorizationService.evaluate` with a `CapabilityAction` (membership, role grants, initiative match). It audits each decision as before; an audit failure propagates (fail closed). No new RBAC.

## Configuration (backward-compatible profile extension)

`InitiativeProfile.routing` (optional, default empty; schema version unchanged):

```yaml
routing:
  capabilities:            # availability of a capability in this initiative (not a permission)
    - {capability: code_analysis, enabled: true}
  workflows:               # trusted, administrator-configured; exact IDs
    - {id: code-impact, capability: code_analysis, enabled: true, capability_locked: true}
```

Duplicate workflow IDs or capabilities are rejected at load and fail closed at routing. Workflows are initiative-scoped, so one initiative's workflow IDs are invisible to another. Each decision records the `initiative_revision` it used.

## Precedence

1. Malformed or empty request -> `INVALID_REQUEST`.
2. Explicit capability: validate id -> authorize -> configured & enabled -> locked-workflow conflict check -> `ROUTED`. Any failure is final.
3. Configured workflow(s): resolve -> enabled -> single capability -> authorize -> capability available -> `ROUTED`.
4. Otherwise `UNRESOLVED`.

Explicit selection beats workflows; a *locked* enabled workflow naming a different capability makes the request invalid rather than being overridden. Nothing lower in the list can replace a higher choice.

## Mixed-workflow policy

When a request lists several workflow IDs, none may be silently ignored. In order: any duplicate mapping -> `CONFIGURATION_ERROR`; any known **disabled** workflow -> `UNRESOLVED/workflow_disabled` (never a classifier candidate, even if others are enabled); any **unknown** ID alongside known ones -> `UNRESOLVED/unknown_workflow`; enabled workflows mapping to different capabilities -> `UNRESOLVED/ambiguous_workflow`; otherwise (all enabled, one capability) the capability is authorized and routed, and the reported `workflow_id` is the lowest ID so the result does not depend on request order. An explicit capability selection is unaffected by unrelated disabled workflows, but a locked, enabled workflow naming a different capability still makes it invalid.

## Decision table

| Situation | Outcome | Reason | Classifier candidate |
| --- | --- | --- | --- |
| Explicit, authorized, enabled | ROUTED | explicit_capability_selected | no |
| Explicit, not authorized | DENIED | capability_not_authorized | no |
| Explicit, unknown identifier | INVALID_REQUEST | unknown_capability | no |
| Explicit, not configured / disabled | CONFIGURATION_ERROR | capability_not_configured / capability_disabled | no |
| Explicit vs locked workflow conflict | INVALID_REQUEST | workflow_capability_conflict | no |
| Workflow matches, authorized | ROUTED | workflow_matched | no |
| Workflow, capability not authorized | DENIED | capability_not_authorized | no |
| Any listed workflow disabled (alone or mixed) | UNRESOLVED | workflow_disabled | no |
| Workflow maps to unavailable capability | CONFIGURATION_ERROR | workflow_capability_unavailable | no |
| Duplicate workflow mapping | CONFIGURATION_ERROR | duplicate_workflow_mapping | no |
| Unknown workflow ID (alone or mixed with known) | UNRESOLVED | unknown_workflow | **yes** |
| Workflows map to different capabilities | UNRESOLVED | ambiguous_workflow | **yes** |
| Only free text | UNRESOLVED | no_deterministic_match | **yes** |
| Malformed / empty | INVALID_REQUEST | malformed_request / empty_request | no |

`RoutingDecision.classifier_candidate` is the contract for AIDLC-49: true only for the three "yes" rows. Denied, invalid, misconfigured and administratively disabled outcomes can never reach a classifier.

## Decision schema

`outcome`, `rule`, `reason`, `initiative_id`, `initiative_revision`, `request_id`, `correlation_id`, `trace_id`, `task_id`, and (only when routed) `capability` and `workflow_id`. No prompts, content, identity details, endpoints or ARNs. Unknown workflow IDs are never echoed.

## Telemetry

Through the existing `TelemetryProvider`: events `routing.explicit_selected`, `routing.workflow_matched`, `routing.unresolved`, `routing.denied`, `routing.configuration_error`, `routing.invalid_request`, plus a metric `routing.reason.<reason_code>`. Names only (the port carries no payload), so no user content or identity is emitted. Telemetry errors are swallowed and cannot change an outcome.

## Orchestrator integration and limitations

A future Orchestrator builds `AgentContext`, loads the profile/revision, calls `route`, dispatches a `ROUTED` capability by logical name (endpoint resolution is not here), and sends only `classifier_candidate` decisions to AIDLC-49. The classifier's answer must be re-authorized through the same path. Routing is evaluated against the profile snapshot passed in; it does not guarantee the configuration is unchanged at dispatch.
