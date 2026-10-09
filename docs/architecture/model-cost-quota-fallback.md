# Model Cost, Quota and Fallback Controls

`ai_dlc.application.model_governance` (AIDLC-52) governs model calls with budgets, quotas, usage/cost accounting and requirement-preserving fallback. It is platform-side, provider-independent, and keeps policy logic free of model calls: only `GovernedModelClient` calls `ModelProvider`; the service, ledger, cost and fallback modules make decisions only. Nothing here grants authorization.

## Architecture

```
selection (role + requirements) ─► FallbackPlanner ─► ordered eligible candidates (registry-backed)
candidate ─► ModelGovernanceService.admit ─► registry recheck → budget → quota → reservation (ledger)
admitted ─► ResilientExecutor ─► ModelProvider ─► usage ─► record_usage / release / record_ambiguous
failure ─► classify_model_failure ─► fallback only if permitted ─► next candidate (same requirements)
```

Reused: `ModelRegistryReader`/eligibility (AIDLC-47), `ModelSelectionRequest`/`RoleProfiles` (AIDLC-46), reviewer independence (AIDLC-51), `ResilientExecutor`/`ErrorClassifier`, `ApprovalProvider`/`ApprovalIntent`, `TelemetryProvider`. The ledger is a narrow port; there is no new database, cache, approval engine or authorization system.

## Configuration (`inference_controls` in the Initiative Profile, optional)

```yaml
inference_controls:
  budget:
    currency: USD
    period: monthly
    initiative_limit: "500.00"
    role_limits: [{role: deep_reasoning, amount: "200.00"}]
    request_limit: "2.00"
    warning_threshold: 0.8
    on_exhausted: require_approval   # block | require_approval | allow (audit-only)
    revision: 3
  quota:
    period: daily
    initiative: {max_invocations: 5000, max_tokens: 20000000, max_concurrent: 20, max_requests_per_minute: 120}
    role_limits: [{role: deep_reasoning, limits: {max_concurrent: 4}}]
  fallback:
    enabled: true
    max_fallback_attempts: 2
    failure_categories: [transient, throttled, provider_unavailable]
    ordering: [priority, cost, latency]
    priorities: [{deployment_id: primary-deployment, priority: 1}]
```

Defaults: no budget or quota limit (that scope is not enforced), action `block`, fallback **off**. A request may lower the request limit, never raise it.

## Usage and cost

`UsageRecord` carries IDs, role, deployment, registry revision, invocation reference, `TokenUsage` (input, output, cache read/write, bounded other categories), source (`provider_reported` / `estimated` / `missing`), pricing effective date, estimated and actual cost, currency, timestamp and status (`committed`, `unknown_cost`, `unreconciled`, `released`). **Missing usage is never zero**; **unknown pricing is never free**. Cost uses trusted registry per-million prices (`Decimal`, rounded **up** to 6 places), is unknown on currency mismatch, and prices cache tokens only when the registry carries cache prices (`input_tokens` excludes cache tokens so nothing is counted twice). No vendor prices are in code. Calculated cost is an estimate until reconciled with authoritative provider billing.

## Reservation lifecycle (example)

1. `admit` estimates `(input+max_output)` cost = 0.60 and reserves it under a stable ID (`res-` + hash of invocation reference and deployment). Budget scopes (initiative, role) and quotas are checked and the hold made in **one atomic ledger operation**.
2. Call succeeds with usage → `record_usage` replaces the 0.60 hold with the actual 0.006; replays return the stored record (no double counting).
3. Definitive failure with no usage → `release` frees the hold.
4. Timeout/cancel → `record_ambiguous`: usage and cost unknown, the estimate **stays held**, no refund, no retry, no fallback → state `unreconciled` → `reconcile(executed=…, usage, billed_cost)` from authoritative execution/billing data resolves it.
5. Abandoned `reserved` entries past their TTL become `unreconciled`: the money stays held (outcome unknown) but the concurrency slot is freed.

## Quotas

Invocation count, tokens (committed actual + held estimates), concurrent in-flight and requests per minute, at initiative and role scope, within a period. Quota exhaustion, budget exhaustion, provider throttling and unavailability are distinct outcomes (`blocked_quota` with the dimension, `blocked_budget`, failure categories). Slots free on commit, release, or TTL expiry.

## Budget breach and approval

`block` returns `blocked_budget`. `require_approval` returns `approval_required` with an `ApprovalIntent` (`model.budget_exception`, bound to the invocation and period) and reserves nothing; the caller submits it through the existing `ApprovalProvider`. A later call with the `approval_id` is admitted only if the trusted provider reports `approved`; the approval is **single-use** (bound to one invocation reference by the ledger) and covers the budget only, never quotas. Model output or classifier confidence is never approval. `allow` records the breach and admits.

## Fallback

Eligibility is the original request's effective requirements (capabilities, context/output, data class, provider/region/deployment-type rules, cost and latency ceilings, enablement, availability) evaluated by the registry, plus AIDLC-51 independence for the reviewer role. The **role is never changed**: a deep request with no deep deployment yields no candidate rather than a standard downgrade. Order = configured keys in order (`priority` → `cost` → `latency` by default), then deployment ID. Unlisted priority, unknown price and unknown latency sort **last**. No LLM chooses.

Fallback applies only to configured categories (default transient, throttled, provider unavailable; optionally permanent model error, invalid output, context capacity). **Never** eligible: authentication, authorization, policy, budget, quota, timeout/cancel. Dispatches are bounded by `1 + max_fallback_attempts`, a deployment is never tried twice, and budget/quota blocks stop the loop.

| Situation | Outcome |
| --- | --- |
| Budget exhausted (`block`) | blocked; no model call; no fallback |
| Budget exhausted (`require_approval`) | approval_required; no model call |
| Quota exhausted | blocked; no fallback |
| Registry revision changed / deployment disabled before dispatch | not dispatched; reselect |
| Transient / throttled / unavailable, fallback permitted | next eligible candidate |
| Auth / authz / policy failure | failed; no fallback |
| Timeout | ambiguous; hold kept; reconcile |
| Candidate lacks data class/region/capability/independence | rejected with reasons |

**Safe fallback:** primary returns `provider_unavailable`; `dep-b` has the same role, data classification, region rules and capabilities, so it is dispatched under its own reservation and the first hold is released. **Denied fallback:** the primary returns an authorization error, or the only other deployment supports only public data for a confidential request, or a reviewer candidate shares the generator's family: no candidate is selected and the failure is returned.

## Registry revision and dispatch

Candidates carry the registry revision they were evaluated at. `admit` re-reads the registry, requires the deployment to be currently eligible, and refuses a changed revision (`registry_changed`) so the caller revalidates or reselects. This narrows but cannot close the race: a revision check is not a distributed execution lease.

## Telemetry

Events: `model.budget_checked`, `budget_exhausted`, `quota_exhausted`, `reservation_created`, `usage_recorded`, `cost_calculated`, `fallback_considered`, `fallback_selected`, `fallback_rejected`, `deployment_unavailable`, `reconciliation_required`. Metrics: tokens, invocations, estimated/recorded cost, budget utilization, fallback counts, unknown accounting. No prompts, code, identities or authorization data; telemetry errors never change a decision.

## Production limitations

The in-memory ledger is process-local and proves no distributed budget/quota enforcement; a durable ledger must provide atomic cross-scope check-and-hold, idempotent keyed records, single-use approvals, monotonic budget revisions and shared state across runtimes. Provider adapters must normalise usage (input excluding cache tokens) and report it in trusted result metadata. Retried attempts inside one executor call are reserved for but only the final response's usage is recorded. Concurrency slots rely on trustworthy outcomes or TTL. Cost is calculated, not billed.
