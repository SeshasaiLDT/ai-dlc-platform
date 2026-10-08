# Model Registry

The model registry (`ai_dlc.application.model_registry`, AIDLC-47) is the governed source of *model deployment* metadata and availability. Later Epic 6 stories consume it; this one only stores, governs and filters.

## Terminology

| Term | Meaning |
| --- | --- |
| **Model role** | A logical need (`ModelRole`, SDK). Contains no model names. |
| **Model deployment** | A concrete configured endpoint recorded in the registry (provider, model identifier, region, type). |
| **Model registry** | The governed store of deployments, their capabilities, role eligibility and state. |
| **Model selection** | Choosing among eligible deployments (AIDLC-48, not implemented). |
| **Model invocation** | Calling a model through the existing `ModelProvider`; untouched here. |

## Architecture

- Platform-side only. It reuses SDK contracts (`ModelRole`, `ModelRequirements`, `RoleProfiles`, `ModelSelectionRequest`, `ModelCapability`) and is **not** imported by the SDK, which stays independently installable. No interface or SDK version change.
- `ModelRegistryRepository` (port) -> `InMemoryModelRegistryRepository` (test/local adapter).
- `ModelRegistryReader` (read-only, no privilege) and `ModelRegistryAdmin` (mutations).
- Follows the Resource Binding Registry conventions: revisions, expected-revision replacement, correlation IDs, injected clock.

## Deployment metadata (`ModelDeploymentSpec`)

Identity (`deployment_id`, `provider_id`, `model_identifier`, optional `inference_profile_id`, `region`, `deployment_type`); `capabilities` (structured output, tool calling, reasoning level, input modalities, response capabilities); `context` (max context/output tokens, tool-schema and protocol reservations; `to_model_capability()` adapts it to the budgeting contract); `latency` (typical latency, performance class); `pricing` (per-million input/output prices, currency, effective date); `governance` (supported data classifications, eligible roles).

`RegisteredModel` adds `enabled`, `availability` (`unknown/available/degraded/unavailable`), `revision`, `updated_at`. Enablement is an administrative decision; availability is an operational signal. They are independent fields.

Identifiers are validated plain tokens: ARNs, URLs and credential-like strings are rejected, and records have no credential fields. Unknown enum values and extra fields fail validation.

## Eligibility (`evaluate_eligibility`)

A deployment is eligible only if all hold, and every failure reason is reported: enabled; availability is not `unavailable`; role declared; structured output / tool calling / reasoning level / modalities / response capabilities meet the effective requirements; context and output capacity sufficient; data classification supported by both deployment and role profile; deployment type, provider and region permitted; pricing present when required, same currency and within ceilings; typical latency known and within any ceiling (unknown fails closed); not in the request's `separate_from_deployments`. Declaring a role is necessary, never sufficient. Requirements come from `ModelSelectionRequest.effective_requirements(profile)`, which only tightens, so user provider preferences cannot override organizational restrictions. Results are ordered by deployment ID; there is no ranking.

## Administration

All mutations take an authenticated `Principal` and call the existing `AuthorizationService.require(PlatformAuthorizationRequest(...))` (platform administrator + `platform.manage`); denial or a non-`Principal` fails closed before any read or write. No new authorization system, and registry data never grants tool or RBAC permissions. Operations: `register` (starts **disabled**), `update_metadata`, `enable`, `disable`, `set_availability`. Reads (`get`, `list`, `evaluate`, `query_eligible`) need no privilege and cannot mutate.

## Revisions, concurrency, refresh

Each record has a monotonically increasing `revision`. Mutations carry `expected_revision`; a mismatch raises `RevisionConflictError` and leaves the record untouched. Enable/disable and availability changes that are already in the requested state return the current record with no revision bump or audit event. Readers read through the repository on every call and hold no cache, so a disable is visible to the next selection; the revision lets consumers tell versions apart. Any future cache must key on `revision` and be bounded by a short TTL; there is no distributed cache or broker.

## Audit

Each successful mutation yields a `ModelRegistryAuditEvent`: deployment ID, operation, actor ID, timestamp, previous/new revision, **changed field names only**, correlation ID and the authorization decision ID. No values, credentials or prompts. The port's single `commit(record, event, expected_revision)` must apply both atomically; if the audit cannot be written the change is not applied (`ModelRegistryAuditError`, with no store detail leaked).

## Durable adapter requirements and limitations

The in-memory adapter is atomic only within one process (a lock). It does **not** prove cross-runtime configuration updates. A production adapter must: share state across runtimes, perform the revision compare and the record+audit write in one transaction or conditional batch, keep audit append-only, and give read-your-writes after commit. No DynamoDB table or other infrastructure is added here. Availability is set by an administrator call; automatic health integration is future work. Pricing and latency are metadata declared by operators, not measured.

## Future routing (AIDLC-48)

The router will call `query_eligible` (or `evaluate`) with a `ModelSelectionRequest`, then rank and apply fallback itself, and invoke through `ModelProvider`.
