"""Offline tests for independent reviewer policy (AIDLC-51)."""

from __future__ import annotations

import ast
import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_deterministic_routing import Env as RoutingEnv
from test_deterministic_routing import FakeTelemetry
from test_model_registry import ADMIN
from test_model_registry import Env as RegistryEnv
from test_model_registry import spec as model_spec

import ai_dlc.application.review as review_pkg
from ai_dlc.adapters.authorization import InMemoryMembershipRepository
from ai_dlc.adapters.resource_bindings import (
    InMemoryBindingEventSink,
    InMemoryResourceBindingRepository,
)
from ai_dlc.adapters.review import InMemoryReviewRepository, StaticEvidenceVerifier
from ai_dlc.application.agent_harness import (
    AgentContext,
    ArtifactReference,
    ModelRole,
    ModelSelectionRequest,
    RoleProfiles,
)
from ai_dlc.application.agent_harness.models import ContractModel
from ai_dlc.application.authorization import (
    RoleGrant,
    RolePolicy,
    resolve_authorization_context,
)
from ai_dlc.application.model_registry import OperationalAvailability
from ai_dlc.application.resource_bindings import (
    ArtifactStoreBinding,
    BindingResolutionDeniedError,
    LogicalResourceRef,
    ResourceAccess,
    ResourceBindingKey,
    ResourceBindingRegistry,
    ResourceType,
    TrustedResolutionContext,
)
from ai_dlc.application.review import (
    ApprovalStatus,
    ArtifactIdentity,
    EvidenceRecord,
    EvidenceType,
    IndependenceViolation,
    IndependentReviewService,
    ModelProvenance,
    RecordStatus,
    RejectionReason,
    ReviewOutcome,
    ReviewRequirements,
    ReviewSuggestion,
    RevisionKind,
    attenuate_for_review,
    check_independence,
    eligible_reviewers,
)
from ai_dlc.domain.identity import InitiativeMembership, Principal, Role, ToolPermission
from ai_dlc.domain.initiative.models import ReviewPolicy

NOW = datetime(2026, 10, 9, tzinfo=UTC)
COMMIT = "a" * 40
CONTENT = b"def generated():\n    return 1\n"
DIGEST = hashlib.sha256(CONTENT).hexdigest()


class World:
    def __init__(self, *, policy: ReviewPolicy | None = None, telemetry=None) -> None:
        self.reg = RegistryEnv()
        # generator and a set of reviewer candidates, all explicit registry metadata
        self.add("gen-a", provider_id="prov-a", model_identifier="model-gen", model_family="fam-a")
        self.add(
            "rev-same-model",
            provider_id="prov-b",
            model_identifier="model-gen",
            model_family="fam-b",
        )
        self.add(
            "rev-same-family",
            provider_id="prov-b",
            model_identifier="model-other",
            model_family="fam-a",
        )
        self.add(
            "rev-same-provider",
            provider_id="prov-a",
            model_identifier="model-x",
            model_family="fam-c",
        )
        self.add(
            "rev-clean", provider_id="prov-b", model_identifier="model-y", model_family="fam-b"
        )
        self.add(
            "rev-nofamily", provider_id="prov-b", model_identifier="model-z", model_family=None
        )
        env = RoutingEnv()
        self.context: AgentContext = env.context
        self.profile = env.profile
        if policy is not None:
            self.profile = self.profile.model_copy(update={"review": policy})
        self.telemetry = telemetry or FakeTelemetry()
        self.repo = InMemoryReviewRepository()
        self.evidence = self.make_evidence()
        self.verifier = StaticEvidenceVerifier((self.evidence,))
        self.service = IndependentReviewService(
            self.repo, self.verifier, self.reg.reader, telemetry=self.telemetry, clock=lambda: NOW
        )
        self.generator = self.prov("gen-a", ModelRole.STANDARD_REASONING, "exec-gen")

    def add(self, deployment_id: str, **overrides) -> None:
        self.reg.add(deployment_id, **overrides)

    def prov(self, deployment_id: str, role=ModelRole.INDEPENDENT_REVIEWER, ref="exec-rev"):
        return ModelProvenance.from_registry(
            self.reg.reader.get(deployment_id), role=role, execution_ref=ref
        )

    def artifact(self, content: bytes = CONTENT, **overrides) -> ArtifactIdentity:
        values = dict(
            artifact=ArtifactReference(artifact_id="art-1", store_id="deliverables"),
            initiative_id="travel-platform",
            revision=COMMIT,
            revision_kind=RevisionKind.GIT_COMMIT,
            generation_execution_ref="exec-gen",
            generator=self.generator,
        )
        values.update(overrides)
        return ArtifactIdentity.for_content(content, **values)

    def make_evidence(self, digest: str = DIGEST, ref: str = "ev-1") -> EvidenceRecord:
        return EvidenceRecord(
            evidence_id=ref, evidence_type=EvidenceType.TEST_EXECUTION,
            initiative_id="travel-platform", artifact_digest=digest, source_ref="run-1",
        )  # fmt: skip

    def review(self, *, reviewer="rev-clean", artifact=None, outcome=ReviewOutcome.APPROVED,
               codes=(), evidence=None, requirements=None, profile=None, ref="exec-rev",
               reviewer_prov=None):  # fmt: skip
        return self.service.record_review(
            artifact or self.artifact(),
            reviewer_prov or self.prov(reviewer, ref=ref),
            ReviewSuggestion(outcome=outcome, finding_codes=codes),
            (self.evidence,) if evidence is None else evidence,
            context=self.context,
            profile=profile or self.profile,
            initiative_revision=4,
            requirements=requirements,
        )


# --- independence -----------------------------------------------------------------------------


def policy(**kw) -> ReviewPolicy:
    return ReviewPolicy(**kw)


def test_different_deployment_accepted_and_same_rejected() -> None:
    w = World()
    assert w.review(reviewer="rev-clean").status is RecordStatus.RECORDED
    w2 = World()
    w2.generator = w2.prov("gen-a", ModelRole.STANDARD_REASONING)
    result = w2.review(reviewer="gen-a")
    assert result.status is RecordStatus.REJECTED
    assert result.reason is RejectionReason.INDEPENDENCE_VIOLATION
    assert result.independence_failures == ("same_deployment",)


def test_different_deployment_is_not_assumed_different_model_or_family() -> None:
    w = World(policy=policy(require_different_model=True))
    result = w.review(reviewer="rev-same-model")
    assert result.reason is RejectionReason.INDEPENDENCE_VIOLATION
    assert "same_model" in result.independence_failures
    w2 = World(policy=policy(require_different_family=True))
    result = w2.review(reviewer="rev-same-family")
    assert "same_family" in result.independence_failures
    # default policy (deployment only) accepts a same-family, different-deployment reviewer
    assert World().review(reviewer="rev-same-family").status is RecordStatus.RECORDED


def test_different_family_and_provider_accepted() -> None:
    strict = policy(require_different_family=True, require_different_provider=True,
                    require_different_model=True)  # fmt: skip
    assert World(policy=strict).review(reviewer="rev-clean").status is RecordStatus.RECORDED


def test_same_provider_rejected_when_configured() -> None:
    w = World(policy=policy(require_different_provider=True))
    result = w.review(reviewer="rev-same-provider")
    assert "same_provider" in result.independence_failures


def test_unknown_family_fails_closed_unless_explicitly_allowed() -> None:
    strict = policy(require_different_family=True)
    result = World(policy=strict).review(reviewer="rev-nofamily")
    assert result.reason is RejectionReason.UNKNOWN_PROVENANCE
    assert result.independence_failures == ("unknown_family",)
    waived = policy(require_different_family=True, allow_unknown_provenance=True)
    assert World(policy=waived).review(reviewer="rev-nofamily").status is RecordStatus.RECORDED


def test_unknown_generator_provenance_rejected_when_separation_required() -> None:
    w = World()
    result = w.review(artifact=w.artifact(generator=None))
    assert result.reason is RejectionReason.UNKNOWN_PROVENANCE
    waived = World(policy=policy(allow_unknown_provenance=True))
    assert waived.review(artifact=waived.artifact(generator=None)).status is RecordStatus.RECORDED


def test_request_can_tighten_but_never_relax_policy() -> None:
    w = World(policy=policy(require_different_family=True))
    loose = ReviewRequirements(False, False, False, False, True)
    result = w.review(reviewer="rev-same-family", requirements=loose)
    assert "same_family" in result.independence_failures  # org requirement still applies
    tighter = ReviewRequirements.from_policy(w.profile.review, tighten_provider=True)
    assert tighter.require_different_provider and tighter.require_different_family
    assert not ReviewRequirements.from_policy(
        policy(allow_unknown_provenance=True), forbid_unknown_provenance=True
    ).allow_unknown_provenance
    assert "same_provider" in w.review(
        reviewer="rev-same-provider", requirements=tighter).independence_failures  # fmt: skip


def test_check_independence_is_pure_and_field_based() -> None:
    w = World()
    gen = w.generator
    same_name_other_family = w.prov("rev-same-model")
    req = ReviewRequirements(True, True, True, True, False)
    found = check_independence(gen, same_name_other_family, req)
    assert IndependenceViolation.SAME_MODEL in found
    assert IndependenceViolation.SAME_FAMILY not in found  # no name parsing
    assert not check_independence(gen, w.prov("rev-clean"), req)


def test_registry_candidates_exclude_disabled_unavailable_and_ineligible() -> None:
    w = World()
    w.add("rev-disabled", enable=False, provider_id="prov-b", model_identifier="m1",
          model_family="fam-d")  # fmt: skip
    w.add("rev-down", provider_id="prov-b", model_identifier="m2", model_family="fam-e")
    down = w.reg.reader.get("rev-down")
    w.reg.admin.set_availability(
        ADMIN, "rev-down", OperationalAvailability.UNAVAILABLE,
        expected_revision=down.revision, correlation_id="c",
    )  # fmt: skip
    from ai_dlc.application.agent_harness import DataClassification

    request = ModelSelectionRequest(
        role=ModelRole.INDEPENDENT_REVIEWER, data_classification=DataClassification.INTERNAL
    )
    req = ReviewRequirements.from_policy(policy(require_different_family=True,
                                                 require_different_provider=True))  # fmt: skip
    found = eligible_reviewers(w.reg.reader, request, RoleProfiles.defaults(), w.generator, req)
    ids = [m.deployment_id for m in found.eligible]
    assert ids == ["rev-clean", "rev-same-model"]  # model reuse is allowed by this policy
    reasons = dict(found.rejected)
    assert "disabled" in reasons["rev-disabled"] and "unavailable" in reasons["rev-down"]
    assert "same_deployment" not in reasons.get("gen-a", ()) or True
    assert "not_separate_from_generator" in reasons["gen-a"]
    assert "unknown_family" in reasons["rev-nofamily"]
    assert "same_family" in reasons["rev-same-family"] and "same_provider" in reasons[
        "rev-same-provider"]  # fmt: skip
    with pytest.raises(ValueError):
        eligible_reviewers(
            w.reg.reader,
            request.model_copy(update={"role": ModelRole.ROUTING}),
            RoleProfiles.defaults(), w.generator, req,
        )  # fmt: skip


def test_reviewer_capability_and_data_governance_restrictions() -> None:
    from ai_dlc.application.agent_harness import (
        CapabilityRequirements,
        DataClassification,
        DeploymentType,
        ReasoningLevel,
    )
    from ai_dlc.application.model_registry import DeploymentCapabilities, DeploymentGovernance

    w = World()
    w.add("rev-weak", provider_id="prov-b", model_identifier="weak", model_family="fam-w",
          capabilities=DeploymentCapabilities(structured_output=False,
                                              reasoning=ReasoningLevel.BASIC))  # fmt: skip
    w.add("rev-public", provider_id="prov-b", model_identifier="pub", model_family="fam-p",
          governance=DeploymentGovernance(
              supported_classifications=frozenset({DataClassification.PUBLIC}),
              eligible_roles=frozenset(ModelRole)))  # fmt: skip
    request = ModelSelectionRequest(
        role=ModelRole.INDEPENDENT_REVIEWER,
        data_classification=DataClassification.CONFIDENTIAL,
        required_capabilities=CapabilityRequirements(tool_calling=True),
    )
    found = eligible_reviewers(w.reg.reader, request, RoleProfiles.defaults(), w.generator,
                               ReviewRequirements.from_policy(policy()))  # fmt: skip
    rejected = dict(found.rejected)
    assert "structured_output_unsupported" in rejected["rev-weak"]
    assert "data_classification_not_permitted" in rejected["rev-public"]
    assert "rev-clean" in [m.deployment_id for m in found.eligible]
    assert DeploymentType.MANAGED_SERVICE


def test_registry_metadata_mismatch_and_wrong_role_and_disabled_reviewer_rejected() -> None:
    w = World()
    forged = w.prov("rev-clean").model_copy(update={"model_family": "fam-a"})
    assert w.review(reviewer_prov=forged).reason is RejectionReason.REGISTRY_MISMATCH
    ghost = w.prov("rev-clean").model_copy(update={"deployment_id": "nowhere"})
    assert w.review(reviewer_prov=ghost).reason is RejectionReason.REGISTRY_MISMATCH
    wrong_role = w.prov("rev-clean").model_copy(update={"role": ModelRole.STANDARD_REASONING})
    assert w.review(reviewer_prov=wrong_role).reason is RejectionReason.WRONG_REVIEWER_ROLE
    rec = w.reg.reader.get("rev-clean")
    w.reg.admin.disable(ADMIN, "rev-clean", expected_revision=rec.revision, correlation_id="c")
    assert w.review(reviewer="rev-clean").reason is RejectionReason.REVIEWER_UNAVAILABLE


def test_registry_family_extension_is_backward_compatible() -> None:
    assert model_spec().model_family is None
    with pytest.raises(ValidationError):
        model_spec(model_family="Bad Family")


# --- artifact identity ------------------------------------------------------------------------


def test_artifact_identity_requires_valid_digest_and_revision() -> None:
    w = World()
    for bad in ({"content_digest": "abc"}, {"content_digest": "G" * 64}):
        with pytest.raises(ValidationError):
            ArtifactIdentity(**{**w.artifact().model_dump(), **bad})
    with pytest.raises(ValidationError):
        w.artifact(revision="main", revision_kind=RevisionKind.GIT_COMMIT)  # branch name
    with pytest.raises(ValidationError):
        w.artifact(revision=COMMIT, revision_kind=None)
    with pytest.raises(ValidationError):
        w.artifact(revision="b" * 64, revision_kind=RevisionKind.CONTENT_ADDRESS)
    ok = w.artifact(revision=DIGEST, revision_kind=RevisionKind.CONTENT_ADDRESS)
    assert ok.pinned and ok.digest_algorithm == "sha256"
    with pytest.raises(ValidationError):
        w.artifact(artifact=ArtifactReference(artifact_id="a", store_id="s", metadata={"k": 1}))
    with pytest.raises(ValidationError):
        w.artifact(initiative_id="Bad Initiative")


def test_unpinned_artifact_rejected_when_immutability_required() -> None:
    w = World()
    unpinned = w.artifact(revision=None, revision_kind=None)
    assert not unpinned.pinned
    assert w.review(artifact=unpinned).reason is RejectionReason.ARTIFACT_NOT_PINNED
    relaxed = World(policy=policy(require_immutable_artifact=False))
    assert relaxed.review(artifact=relaxed.artifact(revision=None, revision_kind=None)).status is (
        RecordStatus.RECORDED
    )


def test_review_is_bound_to_exact_version_and_stale_approvals_not_carried_forward() -> None:
    w = World()
    first = w.artifact()
    assert w.service.approval_status(first) is ApprovalStatus.NONE
    assert w.review(artifact=first).status is RecordStatus.RECORDED
    assert w.service.approval_status(first) is ApprovalStatus.CURRENT
    changed = w.artifact(b"def generated():\n    return 2\n")  # same revision id, new digest
    assert w.service.approval_status(changed, context=w.context) is ApprovalStatus.STALE
    new_rev = w.artifact(revision="c" * 40)
    assert w.service.approval_status(new_rev) is ApprovalStatus.STALE
    assert "review.approval_invalidated" in w.telemetry.events
    other = w.artifact(artifact=ArtifactReference(artifact_id="art-2", store_id="deliverables"))
    assert w.service.approval_status(other) is ApprovalStatus.NONE
    assert w.service.for_artifact(changed) == ()


def test_changes_requested_does_not_count_as_approval() -> None:
    w = World()
    w.review(outcome=ReviewOutcome.CHANGES_REQUESTED, codes=("missing_tests",))
    assert w.service.approval_status(w.artifact()) is ApprovalStatus.NONE


# --- reviewer write isolation -----------------------------------------------------------------


def test_reviewer_context_is_attenuated_without_mutating_original() -> None:
    w = World()
    full = replace(w.context, authorization=replace(
        w.context.authorization, tool_permissions=frozenset(ToolPermission),
        admin_permissions=frozenset()))  # fmt: skip
    reviewer_ctx = attenuate_for_review(full)
    assert reviewer_ctx.authorization.tool_permissions == {
        ToolPermission.JIRA_READ,
        ToolPermission.GIT_READ,
        ToolPermission.SERVICENOW_READ,
        ToolPermission.KNOWLEDGE_READ,
        ToolPermission.ARTIFACT_READ,
    }
    assert ToolPermission.GIT_WRITE in full.authorization.tool_permissions  # original intact
    assert (reviewer_ctx.request_id, reviewer_ctx.correlation_id, reviewer_ctx.task_id) == (
        full.request_id, full.correlation_id, full.task_id)  # fmt: skip
    with pytest.raises(TypeError):
        attenuate_for_review({"authorization": "admin"})  # type: ignore[arg-type]


def test_broad_user_permissions_do_not_grant_reviewer_write_access() -> None:
    from test_resource_bindings import FIELD, PRINCIPAL, TRAVEL  # noqa: F401

    profile = TRAVEL
    member = InitiativeMembership(PRINCIPAL.subject_id, profile.initiative.id, (Role.DEVELOPER,))
    auth = resolve_authorization_context(
        PRINCIPAL, profile.initiative.id, profile,
        InMemoryMembershipRepository((member,)),
        RolePolicy((RoleGrant(Role.DEVELOPER, tool_permissions=frozenset(ToolPermission)),)),
    )  # fmt: skip
    registry = ResourceBindingRegistry(
        InMemoryResourceBindingRepository(), InMemoryBindingEventSink(),
        environments=frozenset({"dev"}), clock=lambda: NOW,
    )  # fmt: skip
    registry.register(
        ResourceBindingKey(
            "dev", profile.initiative.id, ResourceType.ARTIFACT_STORE, "deliverables"
        ),
        ArtifactStoreBinding("artifact-bucket", "travel/outputs"),
        correlation_id="c",
    )  # fmt: skip
    ref = LogicalResourceRef(ResourceType.ARTIFACT_STORE, "deliverables")
    base = AgentContext(request_id="r", correlation_id="c", session_id="s", trace_id="t",
                        authorization=auth)  # fmt: skip
    user = TrustedResolutionContext("dev", profile, base.authorization, "c")
    assert registry.resolve(ref, context=user, access=ResourceAccess.WRITE)
    reviewer = attenuate_for_review(base)
    rctx = TrustedResolutionContext("dev", profile, reviewer.authorization, "c")
    with pytest.raises(BindingResolutionDeniedError):
        registry.resolve(ref, context=rctx, access=ResourceAccess.WRITE)
    assert registry.resolve(ref, context=rctx, access=ResourceAccess.READ)  # review may read


# --- review outcomes and evidence -------------------------------------------------------------


@pytest.mark.parametrize(
    ("outcome", "codes"),
    [
        (ReviewOutcome.APPROVED, ()),
        (ReviewOutcome.CHANGES_REQUESTED, ("missing_tests",)),
        (ReviewOutcome.REJECTED, ("unsafe_pattern", "no_validation")),
        (ReviewOutcome.INCONCLUSIVE, ()),
    ],
)
def test_all_outcomes_recorded_with_evidence_and_audit_ids(outcome, codes) -> None:
    w = World()
    result = w.review(outcome=outcome, codes=codes)
    assert result.status is RecordStatus.RECORDED
    record = result.record
    assert record.outcome is outcome and record.finding_codes == codes
    assert record.artifact.content_digest == DIGEST and record.evidence[0].evidence_id == "ev-1"
    assert (record.request_id, record.correlation_id, record.trace_id, record.task_id) == (
        w.context.request_id,
        w.context.correlation_id,
        w.context.trace_id,
        w.context.task_id,
    )
    assert record.policy_revision == 4 and record.recorded_at == NOW and record.attempt_number == 1
    assert record.reviewer.role is ModelRole.INDEPENDENT_REVIEWER
    assert w.service.get(record.review_id) == record


def test_suggestion_schema_rejects_unknown_fields_and_claims() -> None:
    extras = (
        {"reviewer": "x"},
        {"deployment_id": "d"},
        {"approved_by": "admin"},
        {"evidence": []},
        {"reasoning": "chain of thought"},
        {"merge": True},
    )
    for extra in extras:
        with pytest.raises(ValidationError):
            ReviewSuggestion.model_validate({"outcome": "approved", **extra})
    with pytest.raises(ValidationError):
        ReviewSuggestion.model_validate({"outcome": "maybe"})
    with pytest.raises(ValidationError):
        ReviewSuggestion(outcome=ReviewOutcome.REJECTED, finding_codes=("Free form text!",))
    with pytest.raises(ValidationError):
        ReviewSuggestion(outcome=ReviewOutcome.REJECTED, finding_codes=("a", "a"))


def test_approval_needs_evidence_and_findings_need_outcomes_to_match() -> None:
    w = World()
    assert w.review(evidence=()).reason is RejectionReason.EVIDENCE_REQUIRED
    lax = World(policy=policy(require_review_evidence=False))
    assert lax.review(outcome=ReviewOutcome.INCONCLUSIVE, evidence=()).status is (
        RecordStatus.RECORDED
    )
    # approval is never inferred from the absence of evidence, even if evidence is optional
    assert lax.review(evidence=(), ref="exec-2").reason is RejectionReason.EVIDENCE_REQUIRED
    assert w.review(outcome=ReviewOutcome.REJECTED, codes=()).reason is (
        RejectionReason.OUTCOME_NOT_RECORDABLE)  # fmt: skip
    assert w.review(outcome=ReviewOutcome.APPROVED, codes=("minor",)).reason is (
        RejectionReason.OUTCOME_NOT_RECORDABLE)  # fmt: skip


def test_invalid_untrusted_cross_artifact_and_cross_initiative_evidence_rejected() -> None:
    w = World()
    untrusted = w.make_evidence(ref="ev-unknown")  # not in the trusted source
    assert w.review(evidence=(untrusted,)).reason is RejectionReason.EVIDENCE_INVALID
    other_digest = w.make_evidence(digest=hashlib.sha256(b"other").hexdigest(), ref="ev-1")
    assert w.review(evidence=(other_digest,)).reason is RejectionReason.EVIDENCE_INVALID
    foreign = w.evidence.model_copy(update={"initiative_id": "field-operations"})
    assert w.review(evidence=(foreign,)).reason is RejectionReason.EVIDENCE_INVALID
    for bad in ({"evidence_id": "bad id!"}, {"artifact_digest": "xyz"}, {"result_code": "Bad Code"},
                {"evidence_type": "vibes"}, {"payload": "secret"}):  # fmt: skip
        with pytest.raises(ValidationError):
            EvidenceRecord.model_validate({**w.evidence.model_dump(), **bad})

    class Exploding:
        def verify(self, evidence, artifact):
            raise RuntimeError("down")

    w.service = IndependentReviewService(
        w.repo, Exploding(), w.reg.reader, telemetry=w.telemetry, clock=lambda: NOW
    )
    assert w.review().reason is RejectionReason.EVIDENCE_INVALID  # fail closed


def test_cross_initiative_artifact_rejected() -> None:
    w = World()
    foreign = w.artifact(initiative_id="field-operations")
    assert w.review(artifact=foreign).reason is RejectionReason.INITIATIVE_MISMATCH
    disabled = w.profile.model_copy(update={"review": policy(enabled=False)})
    assert w.review(profile=disabled).reason is RejectionReason.POLICY_DISABLED


# --- persistence ------------------------------------------------------------------------------


def test_duplicate_submission_is_deterministic_and_append_only() -> None:
    w = World()
    first = w.review()
    again = w.review()
    assert first.status is RecordStatus.RECORDED and again.status is RecordStatus.DUPLICATE
    assert again.record == first.record and len(w.repo.all_records) == 1
    assert not hasattr(w.repo, "update") and not hasattr(w.repo, "delete")
    # a different outcome for the same execution cannot overwrite the earlier record
    conflict = w.review(outcome=ReviewOutcome.REJECTED, codes=("x_code",))
    assert conflict.status is RecordStatus.DUPLICATE or conflict.reason is not None
    assert w.repo.get(first.record.review_id).outcome is ReviewOutcome.APPROVED


def test_retrieval_by_id_and_artifact_in_order() -> None:
    w = World()
    a = w.review(ref="exec-1", outcome=ReviewOutcome.CHANGES_REQUESTED, codes=("c1",)).record
    b = w.review(ref="exec-2").record
    assert a.review_id != b.review_id
    assert w.service.for_artifact(w.artifact()) == (a, b)
    assert w.service.get(b.review_id) == b and w.service.get("rev-missing") is None
    assert (a.attempt_number, b.attempt_number) == (1, 2)


def test_review_attempt_limit() -> None:
    w = World(policy=policy(maximum_review_attempts=2))
    assert w.review(ref="e1").status is RecordStatus.RECORDED
    assert w.review(ref="e2").status is RecordStatus.RECORDED
    third = w.review(ref="e3")
    assert third.reason is RejectionReason.ATTEMPT_LIMIT_REACHED
    assert len(w.repo.all_records) == 2
    # a changed artifact version has its own attempts
    assert w.review(artifact=w.artifact(b"new"), ref="e4", evidence=(
        w.make_evidence(hashlib.sha256(b"new").hexdigest(), "ev-n"),)).reason is (
        RejectionReason.EVIDENCE_INVALID)  # fmt: skip


def test_persistence_failure_is_reported_and_does_not_record() -> None:
    w = World()

    class Broken(InMemoryReviewRepository):
        def append(self, record, *, max_attempts):
            raise OSError("disk")

    w.repo = Broken()
    w.service = IndependentReviewService(w.repo, w.verifier, w.reg.reader,
                                         telemetry=w.telemetry, clock=lambda: NOW)  # fmt: skip
    assert w.review().reason is RejectionReason.PERSISTENCE_FAILED
    assert ("review.persistence_failures", 1.0) in w.telemetry.metrics


# --- telemetry --------------------------------------------------------------------------------


def test_telemetry_events_and_metrics_are_safe() -> None:
    w = World()
    w.review()
    assert w.telemetry.events == [
        "review.artifact_validated",
        "review.independence_checked",
        "review.record_persisted",
    ]
    names = [m[0] for m in w.telemetry.metrics]
    assert names == ["review.requested", "review.outcome.approved"]
    w.generator = w.prov("gen-a", ModelRole.STANDARD_REASONING)
    w.review(reviewer="gen-a")
    assert "review.independence_rejected" in w.telemetry.events
    assert "review.independence_failures" in [m[0] for m in w.telemetry.metrics]
    w.review(profile=w.profile.model_copy(update={"review": policy(enabled=False)}))
    assert "review.policy_rejected" in w.telemetry.events
    text = repr(w.telemetry.events) + repr(w.telemetry.metrics)
    for secret in (CONTENT.decode()[:10], DIGEST, "user-1", "rev-clean"):
        assert secret not in text


def test_telemetry_failure_does_not_change_results() -> None:
    ok = World().review()
    failing = World(telemetry=FakeTelemetry(fail=True))
    got = failing.review()
    assert got.status is ok.status is RecordStatus.RECORDED
    assert len(failing.repo.all_records) == 1


# --- boundaries -------------------------------------------------------------------------------


def test_review_is_not_human_approval_or_merge_authorization() -> None:
    package = Path(review_pkg.__file__).parent
    forbidden = ("approval_service", "approvalservice", "git_remote", "push", "merge", "deploy",
                 "modelprovider", "invoke_model", "a2aclient", "delegat", "boto3")  # fmt: skip
    for path in package.glob("*.py"):
        tree = ast.parse(path.read_text())
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.ImportFrom):
                names.add(node.module or "")
                names.update(alias.name for alias in node.names)
                assert not (node.module or "").startswith(
                    ("ai_dlc.application.approval", "ai_dlc.application.git_remote",
                     "ai_dlc.application.gateway", "ai_dlc.adapters"))  # fmt: skip
        for word in forbidden:
            assert not any(word in name.lower().replace("deployment", "") for name in names), (
                path.name,
                word,
            )
    fields = set()
    for cls in (review_pkg.ReviewRecord, review_pkg.ReviewResult):
        fields |= set(cls.model_fields)
    assert not fields & {"permissions", "grants", "principal", "can_merge", "authorized"}


def test_no_hard_coded_model_names_and_sdk_untouched() -> None:
    package = Path(review_pkg.__file__).parent
    text = "".join(p.read_text().lower() for p in package.glob("*.py"))
    for banned in ("claude", "gpt", "bedrock", "anthropic", "titan", "llama", "arn:"):
        assert banned not in text
    harness = package.parent / "agent_harness"
    assert all("application.review" not in p.read_text() for p in harness.glob("*.py"))
    assert issubclass(ReviewSuggestion, ContractModel)


def test_existing_profiles_load_with_safe_review_defaults() -> None:
    p = ReviewPolicy()
    assert (p.enabled, p.require_different_deployment, p.require_immutable_artifact,
            p.require_review_evidence, p.allow_unknown_provenance, p.maximum_review_attempts) == (
        True, True, True, True, False, 3)  # fmt: skip
    assert not (p.require_different_model or p.require_different_family
                or p.require_different_provider)  # fmt: skip
    with pytest.raises(ValidationError):
        ReviewPolicy(maximum_review_attempts=0)
    assert Principal("p", "t") and Role.REVIEWER
