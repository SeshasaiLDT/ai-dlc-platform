# Standard vs Deep Reasoning Policy

`ai_dlc.application.reasoning` (AIDLC-50) decides which **logical reasoning tier**, `ModelRole.STANDARD_REASONING` or `ModelRole.DEEP_REASONING`, a task should use. It is a deterministic, configuration-driven function of trusted inputs. It never calls a model, never picks a deployment, and does not replace capability routing (AIDLC-48/49).

## Selection vs escalation

- **Initial selection:** no tier has been used yet (`prior_role` absent). Triggers pick deep immediately (`deep_selected`); this is *not* an escalation.
- **Escalation:** a standard attempt was in use and triggers fire; the policy may move to deep (`escalated`, count + 1), subject to limits.
- **Retained:** the prior tier is kept. Deep is the highest defined tier: it never escalates further and is never automatically downgraded.

## Triggers (stable codes, reported in this order)

| Code | Condition (all plain comparisons; `None` disables) |
| --- | --- |
| `explicit_governed_policy` | initiative `always_deep` |
| `explicit_request` | trusted caller's `requested_role` is deep |
| `capability_requirement` | required reasoning level >= `deep_reasoning_level`, or a required capability is in `deep_capabilities` |
| `context_size_threshold` | estimated context tokens >= `context_size_threshold_tokens` |
| `task_complexity_threshold` | trusted complexity score >= `complexity_score_threshold` |
| `task_breadth_threshold` | components / repositories / dependency relationships >= their thresholds |
| `failed_attempt_threshold` | count of `insufficient_reasoning` failures >= `failed_attempt_threshold` |

No keyword matching and no LLM is used to infer complexity.

## Configuration (`reasoning` in the Initiative Profile, all optional)

```yaml
reasoning:
  escalation_enabled: true
  deep_reasoning_allowed: true     # false: deep is never selected for this initiative
  max_escalations_per_task: 1      # 0..3
  context_size_threshold_tokens: 64000
  complexity_score_threshold: 0.7
  failed_attempt_threshold: 2
  deep_capabilities: [implementation]
```

Defaults are conservative starting points, **not tuned production thresholds**; they need calibration against real tasks. Existing profiles load unchanged. Policies are per initiative, read-only to the evaluator, and a profile whose initiative differs from the trusted context fails closed (`blocked`). Request text cannot change a policy.

## Trusted signals and trust boundary

`ReasoningSignals` are validated (non-negative counts, bounded tokens, finite 0..1 score, known levels/roles/failure kinds, consistent history) but their **values are trusted** as supplied. Callers must derive them from workflow instrumentation or platform context, never from user text, model output, or model claims that a bigger model is needed. Failure kinds and the escalation history come from trusted execution logic.

## Failure classification

`classify_failure(result)` maps harness `ErrorClassifier` classes to kinds: transient, throttled, timeout, invalid output, context limitation, authentication, authorization, policy validation, cancelled, unknown. It never returns `insufficient_reasoning`; only trusted logic that judged answer quality may assign that kind.

| Last failure | Effect |
| --- | --- |
| transient / throttled / timeout | follow retry policy; no escalation |
| invalid output | bounded validation/repair policy; no escalation |
| context limitation | handled by context budgeting; no escalation |
| unknown / cancelled | no escalation |
| authentication / authorization / policy validation | `blocked`, no tier selected (fail closed) |
| insufficient reasoning (counted) | may trigger `failed_attempt_threshold` |

## Limits

`max_escalations_per_task` bounds approved escalations. The count is part of the caller-supplied history and can only grow: a history with escalations whose prior tier is not deep is invalid and blocks. With two tiers the structural bound is one escalation; `0` forbids escalation. Rejections carry an explicit reason: `escalation_disabled`, `deep_not_allowed`, `escalation_limit_reached`. **Limitation:** no durable task state exists yet; the counter must come from trusted storage (Epic 8), not a process-local variable.

## Decision record and inspection

`ReasoningDecision` (`to_dict`/`from_dict`, JSON-safe) holds kind, `selected_role`, `previous_role`, `requested_role`, `escalated`, `trigger_codes`, `rejection_reason`, `escalation_count`, `policy_revision` (the initiative configuration revision), initiative/request/correlation/trace/task IDs, and a UTC timestamp. No prompts, outputs, credentials or model IDs.

**Requested vs selected vs invoked:** `requested_role` is what the trusted caller asked for, `selected_role` is the policy outcome, and `invoked_role` is empty with `execution_status=not_observed` until trusted execution calls `record_invocation(role)`. A decision never proves a model ran.

## Decision table

| Prior tier | Triggers | Policy | Result |
| --- | --- | --- | --- |
| none | none | any | standard selected |
| none | yes | deep allowed | **deep selected** (not an escalation) |
| none | yes | deep not allowed | standard selected, reason `deep_not_allowed` |
| standard | none | any | standard retained |
| standard | yes | allowed, enabled, under limit | **escalated** to deep |
| standard | yes | deep not allowed / escalation disabled / limit reached | escalation rejected (reason) |
| deep | any | deep allowed | deep retained |
| any | n/a | auth/policy failure, initiative mismatch, invalid history | blocked |

## Observability

Events: `reasoning.standard_selected`, `reasoning.deep_selected`, `reasoning.escalation_requested`, `_approved`, `_rejected`, `_limit_reached`, `reasoning.blocked`. Metrics: `reasoning.decisions.standard|deep`, `reasoning.escalations`, `reasoning.escalation_rejections`, `reasoning.trigger.<code>`. Codes and counts only; telemetry errors never change a decision.

## Model registry boundary and integration

The policy outputs a role; the future router resolves it to an eligible deployment via the model registry (AIDLC-47) and owns availability handling. If no deep deployment is available, the policy does **not** downgrade; that is a governed-fallback decision elsewhere. The Orchestrator will call `ReasoningTierPolicy.evaluate` per task step with trusted signals and persist the record in Epic 8 task state.
