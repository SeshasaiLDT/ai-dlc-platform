# Classifier Fallback

`ClassifierFallback` (`ai_dlc.application.routing`, AIDLC-49) uses a low-cost LLM, via the logical `ModelRole.ROUTING`, **only** when deterministic capability routing could not decide. Its output is untrusted: it is validated, then re-authorized and configuration-checked by the same router before it can become a route. It never executes tools or delegates to agents.

## Flow

1. `DeterministicRouter.route` returns a `RoutingDecision`.
2. `classify(request, decision=..., context=..., profile=..., initiative_revision=...)` runs the **gate**: `outcome == UNRESOLVED` and `decision.classifier_candidate`. Nothing else opens it; there is no override parameter. Other decisions return `NOT_PERMITTED` without a model call.
3. Consistency: the decision's initiative, revision, request, correlation, trace and task IDs must equal the trusted `AgentContext`/profile, else `NOT_PERMITTED/decision_context_mismatch` (no model call).
4. Initiative policy must enable the classifier (default **off**).
5. Prompt is built from trusted instructions, the capability names (with optional trusted descriptions) that are enabled for the initiative **and** allowed by the user's resolved snapshot, and the user text, escaped, wrapped as untrusted data, and bounded by `max_input_chars` (optionally by a `ContextAssembler` token budget). No identity, resource IDs or authorization data are included.
6. One `ModelProvider.invoke_model(ModelRole.ROUTING, ...)` call through `ResilientExecutor` (max 2 attempts, transient errors only; permanent errors fail fast). No model names, ARNs or keys appear in code.
7. `StructuredOutputValidator.parse` enforces `ClassifierOutput` with repair disabled (no re-asking).
8. Confidence/ambiguity policy, then `DeterministicRouter.route_suggested`.

## Output schema (`routing-classification-v1`)

`capability` (platform `Capability` or null), `confidence` (finite 0..1), `ambiguous` (bool), `alternative_capabilities` (known, unique, not the selected one; only when ambiguous), `reason_code` (`clear_match | partial_match | multiple_matches | insufficient_information | out_of_scope`; the last two only with null capability). Extra fields (identity, roles, tools, endpoints, authorization claims), unknown values, NaN/Infinity, booleans as numbers and malformed JSON are rejected. No free-text reasoning is accepted.

## Policy (`routing.classifier` in the Initiative Profile, all optional)

| Field | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | opt in per initiative |
| `min_confidence` | `0.8` | accept at or above; the score is **not calibrated** |
| `allow_ambiguous` | `false` | allow auto-routing of an ambiguous answer (still enforced) |
| `max_alternatives` | `3` | more is invalid output |
| `max_input_chars` | `4000` | user text bound |

Capabilities may carry a short trusted `description` (no `<`, `>` or newlines).

```yaml
routing:
  capabilities:
    - {capability: investigation, description: "Investigate defects and incidents"}
  classifier: {enabled: true, min_confidence: 0.85}
```

## Reauthorization

`route_suggested` applies the existing `AuthorizationService` capability check (audited) and initiative availability check to the suggested capability. The result carries `rule=MODEL_SUGGESTED`, never `EXPLICIT_CAPABILITY`, so a model suggestion cannot pass as a user selection. Denied -> `DENIED` (no alternatives tried, no second model call); disabled/unconfigured -> `CONFIGURATION_ERROR`. Alternatives are advisory for a clarification prompt only and are filtered to offered choices.

## Decision table

| Situation | Outcome | Model called |
| --- | --- | --- |
| Decision not UNRESOLVED-candidate (routed, denied, invalid, config error, disabled workflow) | NOT_PERMITTED | no |
| Decision belongs to another context/revision | NOT_PERMITTED | no |
| Classifier disabled | NOT_PERMITTED | no |
| No user text | CLARIFICATION_REQUIRED | no |
| No enabled capability / none allowed to user | CONFIGURATION_ERROR / DENIED | no |
| Prompt exceeds budget | MODEL_FAILED | no |
| Provider failure after bounded retries | MODEL_FAILED | yes |
| Malformed or non-conforming output | INVALID_OUTPUT | yes |
| Null capability, ambiguous (default), or confidence < threshold | CLARIFICATION_REQUIRED | yes |
| Suggestion not authorized | DENIED | yes |
| Suggestion disabled/unconfigured | CONFIGURATION_ERROR | yes |
| Suggestion valid and authorized | CLASSIFIED (`model_suggested`) | yes |

## Telemetry

Events: `routing.classifier_invoked`, `_accepted`, `_ambiguous`, `_low_confidence`, `_invalid_output`, `_denied`, `_failed`, plus `_not_permitted` and `_clarification`. Metrics: `routing.classifier.invocations`, `.accepted`/`.rejected`, `.confidence.<lt_50|50_79|80_100>`. Names and counts only; no text, reasoning or identities. Telemetry errors are swallowed.

## Limitations

Confidence is an uncalibrated model self-report; thresholds need calibration against labelled traffic. A prompt-injected answer can still pick an allowed capability, so the control is authorization plus clarification, not the prompt. Decision identity is checked by fields, not cryptographically. Clarification UX, deep-reasoning escalation, reviewer policy and cost governance are out of scope. Model selection for the ROUTING role (the registry) is done by the `ModelProvider` adapter.
