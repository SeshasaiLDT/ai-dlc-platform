"""Independent review recording. Validates, persists and reports; it never invokes a model.

Recording a review is evidence only. It does not approve a merge, push or deployment, and it
touches neither the reviewed artifact nor human-approval state.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum

from ai_dlc.application.agent_harness import AgentContext, ModelRole, TelemetryProvider
from ai_dlc.application.model_registry import ModelRegistryReader, OperationalAvailability
from ai_dlc.domain.initiative import InitiativeProfile

from .errors import DuplicateReviewConflictError, ReviewAttemptLimitError
from .independence import ReviewRequirements, check_independence
from .models import (
    ArtifactIdentity,
    EvidenceRecord,
    ModelProvenance,
    RecordStatus,
    RejectionReason,
    ReviewOutcome,
    ReviewRecord,
    ReviewResult,
    ReviewSuggestion,
)
from .ports import EvidenceVerifier, ReviewRepository

_REJECTION_EVENTS = {
    RejectionReason.INDEPENDENCE_VIOLATION: "review.independence_rejected",
    RejectionReason.UNKNOWN_PROVENANCE: "review.independence_rejected",
}


class ApprovalStatus(StrEnum):
    CURRENT = "current"  # an approving review exists for this exact version
    STALE = "stale"  # approvals exist only for other versions of this artifact
    NONE = "none"


class IndependentReviewService:
    def __init__(
        self,
        repository: ReviewRepository,
        evidence: EvidenceVerifier,
        registry: ModelRegistryReader,
        *,
        telemetry: TelemetryProvider | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._repository = repository
        self._evidence = evidence
        self._registry = registry
        self._telemetry = telemetry
        self._clock = clock

    def record_review(
        self,
        artifact: ArtifactIdentity,
        reviewer: ModelProvenance,
        suggestion: ReviewSuggestion,
        evidence: tuple[EvidenceRecord, ...],
        *,
        context: AgentContext,
        profile: InitiativeProfile,
        initiative_revision: int,
        requirements: ReviewRequirements | None = None,
    ) -> ReviewResult:
        """Validate a (model-suggested) review and persist it append-only.

        ``reviewer`` and ``artifact`` (including its generator provenance) must come from trusted
        execution infrastructure. ``requirements`` may only tighten the initiative policy.
        """
        if not (
            isinstance(artifact, ArtifactIdentity)
            and isinstance(reviewer, ModelProvenance)
            and isinstance(suggestion, ReviewSuggestion)
            and isinstance(context, AgentContext)
        ):
            raise TypeError("typed review inputs and trusted AgentContext required")
        if type(initiative_revision) is not int or initiative_revision < 1:
            raise ValueError("initiative_revision must be a positive integer")
        self._emit(context, None, "review.requested")
        result = self._validate_and_store(
            artifact, reviewer, suggestion, tuple(evidence), context, profile,
            initiative_revision, requirements,
        )  # fmt: skip
        self._report(result, context)
        return result

    def approval_status(
        self, artifact: ArtifactIdentity, *, context: AgentContext | None = None
    ) -> ApprovalStatus:
        """Whether an approving review applies to *this exact* version. Read-only.

        Approvals never carry forward: a different digest or revision needs a new review.
        """
        if any(r.approves for r in self._repository.list_for_artifact(artifact)):
            return ApprovalStatus.CURRENT
        others = [
            r
            for r in self._repository.list_for_lineage(artifact)
            if r.approves and not r.artifact.same_version(artifact)
        ]
        if others:
            self._emit(context, "review.approval_invalidated", "review.artifact_mismatches")
            return ApprovalStatus.STALE
        return ApprovalStatus.NONE

    def get(self, review_id: str) -> ReviewRecord | None:
        return self._repository.get(review_id)

    def for_artifact(self, artifact: ArtifactIdentity) -> tuple[ReviewRecord, ...]:
        return self._repository.list_for_artifact(artifact)

    # -- internals ----------------------------------------------------------------------------

    def _reject(self, reason: RejectionReason, *failures: str) -> ReviewResult:
        return ReviewResult(
            status=RecordStatus.REJECTED, reason=reason, independence_failures=tuple(failures)
        )

    def _validate_and_store(
        self,
        artifact: ArtifactIdentity,
        reviewer: ModelProvenance,
        suggestion: ReviewSuggestion,
        evidence: tuple[EvidenceRecord, ...],
        context: AgentContext,
        profile: InitiativeProfile,
        revision: int,
        requirements: ReviewRequirements | None,
    ) -> ReviewResult:
        policy = profile.review
        initiative = context.authorization.initiative_id
        if not (profile.initiative.id == initiative == artifact.initiative_id):
            return self._reject(RejectionReason.INITIATIVE_MISMATCH)
        if not policy.enabled:
            return self._reject(RejectionReason.POLICY_DISABLED)
        if policy.require_immutable_artifact and not artifact.pinned:
            return self._reject(RejectionReason.ARTIFACT_NOT_PINNED)
        self._emit(context, "review.artifact_validated")
        if reviewer.role is not ModelRole.INDEPENDENT_REVIEWER:
            return self._reject(RejectionReason.WRONG_REVIEWER_ROLE)

        # Trusted registry cross-check of the reviewer's provenance.
        try:
            registered = self._registry.get(reviewer.deployment_id)
        except Exception:
            return self._reject(RejectionReason.REGISTRY_MISMATCH)
        spec = registered.spec
        if (
            spec.model_identifier != reviewer.model_identifier
            or spec.provider_id != reviewer.provider_id
            or spec.model_family != reviewer.model_family
            or ModelRole.INDEPENDENT_REVIEWER not in spec.governance.eligible_roles
        ):
            return self._reject(RejectionReason.REGISTRY_MISMATCH)
        if not registered.enabled or registered.availability is OperationalAvailability.UNAVAILABLE:
            return self._reject(RejectionReason.REVIEWER_UNAVAILABLE)

        effective = requirements or ReviewRequirements.from_policy(policy)
        effective = self._not_weaker(effective, policy)
        violations = check_independence(artifact.generator, reviewer, effective)
        self._emit(context, "review.independence_checked")
        if violations:
            names = tuple(v.value for v in violations)
            unknown = all(n.startswith("unknown_") for n in names)
            return self._reject(
                RejectionReason.UNKNOWN_PROVENANCE
                if unknown
                else RejectionReason.INDEPENDENCE_VIOLATION,
                *names,
            )

        outcome_reason = self._outcome_problem(suggestion, evidence, policy.require_review_evidence)
        if outcome_reason is not None:
            return self._reject(outcome_reason)
        for item in evidence:
            if (
                item.artifact_digest != artifact.content_digest
                or item.initiative_id != artifact.initiative_id
                or not self._verified(item, artifact)
            ):
                return self._reject(RejectionReason.EVIDENCE_INVALID)

        key = "\x1f".join((artifact.version_key(), reviewer.deployment_id, reviewer.execution_ref))
        record = ReviewRecord(
            review_id="rev-" + hashlib.sha256(key.encode()).hexdigest()[:32],
            artifact=artifact,
            reviewer=reviewer,
            outcome=suggestion.outcome,
            finding_codes=suggestion.finding_codes,
            evidence=evidence,
            recorded_at=self._now(),
            policy_revision=revision,
            request_id=context.request_id,
            correlation_id=context.correlation_id,
            trace_id=context.trace_id,
            task_id=context.task_id,
            attempt_number=len(self._repository.list_for_artifact(artifact)) + 1,
        )
        try:
            stored = self._repository.append(record, max_attempts=policy.maximum_review_attempts)
        except ReviewAttemptLimitError:
            return self._reject(RejectionReason.ATTEMPT_LIMIT_REACHED)
        except DuplicateReviewConflictError:
            return self._reject(RejectionReason.CONFLICTING_DUPLICATE)
        except Exception:
            return self._reject(RejectionReason.PERSISTENCE_FAILED)
        status = RecordStatus.RECORDED if stored == record else RecordStatus.DUPLICATE
        return ReviewResult(status=status, record=stored)

    @staticmethod
    def _not_weaker(requested: ReviewRequirements, policy) -> ReviewRequirements:
        """Organizational requirements always apply, whatever the caller passed."""
        return ReviewRequirements(
            requested.require_different_deployment or policy.require_different_deployment,
            requested.require_different_model or policy.require_different_model,
            requested.require_different_family or policy.require_different_family,
            requested.require_different_provider or policy.require_different_provider,
            requested.allow_unknown_provenance and policy.allow_unknown_provenance,
        )

    @staticmethod
    def _outcome_problem(
        suggestion: ReviewSuggestion, evidence: tuple[EvidenceRecord, ...], required: bool
    ) -> RejectionReason | None:
        outcome = suggestion.outcome
        if (required or outcome is ReviewOutcome.APPROVED) and not evidence:
            # Approval is never inferred from the absence of findings.
            return RejectionReason.EVIDENCE_REQUIRED
        if outcome in (ReviewOutcome.CHANGES_REQUESTED, ReviewOutcome.REJECTED) and (
            not suggestion.finding_codes
        ):
            return RejectionReason.OUTCOME_NOT_RECORDABLE
        if outcome is ReviewOutcome.APPROVED and suggestion.finding_codes:
            return RejectionReason.OUTCOME_NOT_RECORDABLE
        return None

    def _verified(self, item: EvidenceRecord, artifact: ArtifactIdentity) -> bool:
        try:
            return self._evidence.verify(item, artifact) is True
        except Exception:
            return False

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise ValueError("review clock must be timezone-aware")
        return value.astimezone(UTC)

    def _report(self, result: ReviewResult, context: AgentContext) -> None:
        if result.status is RecordStatus.REJECTED:
            reason = result.reason
            event = _REJECTION_EVENTS.get(reason, "review.policy_rejected")
            metrics = ["review.rejected"]
            if event == "review.independence_rejected":
                metrics.append("review.independence_failures")
            if reason is RejectionReason.PERSISTENCE_FAILED:
                metrics.append("review.persistence_failures")
            self._emit(context, event, *metrics)
            return
        self._emit(
            context, "review.record_persisted", f"review.outcome.{result.record.outcome.value}"
        )

    def _emit(self, context: AgentContext | None, event: str | None, *metrics: str) -> None:
        """Names and counts only; failures never alter validation or persistence results."""
        if self._telemetry is None or context is None:
            return
        try:
            if event is not None:
                self._telemetry.record_event(event, context=context)
            for name in metrics:
                self._telemetry.record_metric(name, 1.0, context=context)
        except Exception:
            return
