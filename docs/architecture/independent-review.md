# Independent Reviewer Policy

`ai_dlc.application.review` (AIDLC-51) lets generation and review be assigned to independent models, binds a review to one exact artifact version, validates the outcome and its evidence, and persists it append-only. It does **not** implement the AI Pre-Review agent, invoke any model, or grant any approval.

## Architecture

```
trusted execution infra ──► ModelProvenance (generator) ─► ArtifactIdentity (digest, revision)
reviewer model (untrusted) ─► ReviewSuggestion (outcome + finding codes only)
IndependentReviewService.record_review:
   policy → artifact pinned → reviewer role & registry cross-check → independence
   → outcome/evidence rules → trusted evidence verification → append-only ReviewRepository
```

Registry integration is read-only (`ModelRegistryReader`). `eligible_reviewers(...)` returns registry-eligible `INDEPENDENT_REVIEWER` deployments that also satisfy independence, with explicit rejection reasons; there is no ranking or fallback.

## Independence: four distinct notions

| Requirement | Compares | Note |
| --- | --- | --- |
| different deployment | `deployment_id` | default **on**; two deployments may be the same model |
| different model | `model_identifier` | a different identifier may still share a family |
| different family | `model_family` | explicit registry metadata; **never parsed from names** |
| different provider | `provider_id` | |

Unknown generator provenance or unknown family, when a rule needs it, **fails closed** unless the initiative explicitly sets `allow_unknown_provenance`. A request may only tighten (`ReviewRequirements.from_policy(..., tighten_*)`); the service re-applies organizational policy so a looser requirement object cannot weaken it. The registry gained an optional, backward-compatible `model_family` field.

## Trusted provenance

`ModelProvenance` = deployment, model identifier, family, provider, role, registry revision, execution reference. Build it with `ModelProvenance.from_registry(...)` from the registry record used for the invocation, never from model output or request payloads. The service re-checks the reviewer provenance against the registry (matching identifier/provider/family, reviewer role eligible, enabled, not unavailable). **Pending:** recording provenance automatically at invocation time (needs the model-invocation record store); until then trusted callers/fixtures supply it.

## Artifact identity

`ArtifactIdentity` reuses the harness `ArtifactReference` plus initiative, SHA-256 content digest, and a pinned revision of an explicit kind: `git_commit` (full commit id, never a branch), `object_version` (versioned object id) or `content_address` (equals the digest). Unpinned artifacts are rejected unless the initiative sets `require_immutable_artifact: false` (the digest is always required). Reviews match only the **exact** version: a different digest or revision needs a new review, and `approval_status(...)` reports `current`, `stale` (approved only for other versions; emits `review.approval_invalidated`) or `none`. It never modifies the artifact.

## Reviewer write isolation

`attenuate_for_review(context)` derives a new `AgentContext` from the trusted one with all write tool permissions (Jira, Git, ServiceNow, artifact) and admin permissions removed, regardless of what the user holds elsewhere; the original is unchanged. Components that check the context (resource bindings, gateway) then deny writes, which is tested against the real `ResourceBindingRegistry`. **Limit:** this is in-process authorization. Production must also run reviewer tools with read-only credentials and protect the artifact (branch protection, object lock/versioning); a Python interface alone cannot stop external writes. No second RBAC is introduced.

## Review result and evidence

`ReviewSuggestion` (what the model may say): `outcome` (`approved`, `changes_requested`, `rejected`, `inconclusive`) and short `finding_codes`; unknown fields (provenance, IDs, reasoning, approvals) are rejected and no chain-of-thought is stored. Trusted validation decides recordability: evidence is required (policy) and **always** for approval (approval is never inferred from absence of findings); changes-requested/rejected need finding codes; approved carries none. `EvidenceRecord` is a reference (type, ID, initiative, artifact digest, opaque source reference, optional result code), never a payload, and must be confirmed by a trusted `EvidenceVerifier` and match the reviewed artifact and initiative.

## Persistence

`ReviewRepository` is append-only (no update/delete). `append` atomically dedupes and enforces `maximum_review_attempts` per exact artifact version. The review ID is deterministic (version + reviewer deployment + execution reference), so an identical retry returns the existing record (`duplicate`) and a conflicting one cannot overwrite it. Records hold outcome, artifact identity, reviewer provenance, evidence references, timestamp, policy (initiative configuration) revision and request/correlation/trace/task IDs. Retrieval by ID and by artifact. The in-memory adapter is process-local; a durable adapter needs a shared store with conditional/transactional append, immutability and retention.

## Separation from human approval

An AI review is evidence only. It does not satisfy the human approval engine, merge, push, deploy or alter protected branches, and the review package imports none of those components.

## Configuration (`review` in the Initiative Profile, optional)

```yaml
review:
  require_different_deployment: true
  require_different_family: true
  require_different_provider: false
  allow_unknown_provenance: false
  require_immutable_artifact: true
  require_review_evidence: true
  maximum_review_attempts: 3
```

## Policy decision table

| Situation | Result |
| --- | --- |
| Review disabled | rejected `policy_disabled` |
| Artifact/evidence from another initiative | rejected `initiative_mismatch` / `evidence_invalid` |
| Unpinned artifact, immutability required | rejected `artifact_not_pinned` |
| Reviewer not in registry / mismatched metadata | rejected `registry_mismatch` |
| Reviewer disabled or unavailable | rejected `reviewer_unavailable` |
| Required separation violated | rejected `independence_violation` |
| Required family/generator unknown | rejected `unknown_provenance` |
| Approval without evidence; unverified or foreign evidence | rejected `evidence_required` / `evidence_invalid` |
| Attempt limit reached | rejected `attempt_limit_reached` |
| Valid and persisted | `recorded` (identical retry: `duplicate`) |

Telemetry: `review.artifact_validated`, `review.independence_checked`, `review.independence_rejected`, `review.record_persisted`, `review.approval_invalidated`, `review.policy_rejected`; metrics for requests, outcomes, independence failures, artifact mismatches, persistence failures. Names and counts only; telemetry failures never alter results.
