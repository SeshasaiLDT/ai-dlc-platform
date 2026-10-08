"""Contracts for independent AI review: provenance, artifact identity, evidence, outcomes.

Everything here is secret-free and carries no permissions. A review record is evidence for a
human or workflow decision; it never authorizes a merge, push or deployment.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal

from pydantic import field_validator, model_validator

from ai_dlc.application.agent_harness import ArtifactReference, ModelRole
from ai_dlc.application.agent_harness.models import ContractModel
from ai_dlc.application.model_registry import RegisteredModel

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_LOWER_ID = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_GIT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_OBJECT_VERSION = re.compile(r"^[A-Za-z0-9._~-]{1,256}$")
_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
MAX_FINDINGS = 20
MAX_EVIDENCE = 20


def plain_id(value: str, label: str) -> str:
    if not isinstance(value, str) or not _ID.match(value):
        raise ValueError(f"{label} must be a plain identifier")
    return value


def _utc(value: datetime) -> datetime:
    if value.utcoffset() != timedelta(0):
        raise ValueError("timestamps must be timezone-aware UTC")
    return value


class ModelProvenance(ContractModel):
    """Which deployment produced or reviewed something.

    Must come from trusted model-execution infrastructure (here: the registry record the
    invocation used). Never populate it from model output or request payloads.
    """

    deployment_id: str
    model_identifier: str
    model_family: str | None = None  # None = unknown
    provider_id: str
    role: ModelRole
    registry_revision: int
    execution_ref: str  # correlation/execution reference of the invocation

    @field_validator("deployment_id", "model_identifier", "execution_ref")
    @classmethod
    def ids(cls, value: str) -> str:
        return plain_id(value, "provenance identifier")

    @field_validator("provider_id", "model_family")
    @classmethod
    def lower_ids(cls, value: str | None) -> str | None:
        if value is not None and not _LOWER_ID.match(value):
            raise ValueError("provider/family must be lowercase kebab-case")
        return value

    @field_validator("registry_revision")
    @classmethod
    def positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("registry_revision must be positive")
        return value

    @classmethod
    def from_registry(
        cls, model: RegisteredModel, *, role: ModelRole, execution_ref: str
    ) -> ModelProvenance:
        spec = model.spec
        return cls(
            deployment_id=spec.deployment_id,
            model_identifier=spec.model_identifier,
            model_family=spec.model_family,
            provider_id=spec.provider_id,
            role=role,
            registry_revision=model.revision,
            execution_ref=execution_ref,
        )


class RevisionKind(StrEnum):
    GIT_COMMIT = "git_commit"  # immutable commit id; never a branch name
    OBJECT_VERSION = "object_version"  # versioned object-store id
    CONTENT_ADDRESS = "content_address"  # revision equals the content digest


class ArtifactIdentity(ContractModel):
    """The exact artifact version a review applies to."""

    artifact: ArtifactReference
    initiative_id: str
    content_digest: str
    digest_algorithm: Literal["sha256"] = "sha256"
    revision: str | None = None
    revision_kind: RevisionKind | None = None
    generation_execution_ref: str | None = None
    generator: ModelProvenance | None = None

    @field_validator("artifact")
    @classmethod
    def bare_reference(cls, value: ArtifactReference) -> ArtifactReference:
        plain_id(value.artifact_id, "artifact_id")
        plain_id(value.store_id, "store_id")
        if value.metadata:
            raise ValueError("artifact identity references carry no metadata")
        return value

    @field_validator("initiative_id")
    @classmethod
    def initiative(cls, value: str) -> str:
        if not _LOWER_ID.match(value):
            raise ValueError("invalid initiative_id")
        return value

    @field_validator("content_digest")
    @classmethod
    def digest(cls, value: str) -> str:
        if not _HEX64.match(value):
            raise ValueError("content_digest must be 64 lowercase hex characters")
        return value

    @field_validator("generation_execution_ref")
    @classmethod
    def exec_ref(cls, value: str | None) -> str | None:
        return None if value is None else plain_id(value, "generation_execution_ref")

    @model_validator(mode="after")
    def revision_consistent(self) -> ArtifactIdentity:
        if (self.revision is None) != (self.revision_kind is None):
            raise ValueError("revision and revision_kind are set together")
        if self.revision is not None:
            pattern = {
                RevisionKind.GIT_COMMIT: _GIT_ID,
                RevisionKind.OBJECT_VERSION: _OBJECT_VERSION,
                RevisionKind.CONTENT_ADDRESS: _HEX64,
            }[self.revision_kind]
            if not pattern.match(self.revision):
                raise ValueError("revision does not match its kind")
            if self.revision_kind is RevisionKind.CONTENT_ADDRESS and (
                self.revision != self.content_digest
            ):
                raise ValueError("content-addressed revision must equal the digest")
        return self

    @classmethod
    def for_content(cls, content: bytes, **fields: object) -> ArtifactIdentity:
        return cls(content_digest=hashlib.sha256(content).hexdigest(), **fields)

    @property
    def pinned(self) -> bool:
        return self.revision is not None

    def same_version(self, other: ArtifactIdentity) -> bool:
        """Exact-version equality; a different digest or revision is a different artifact."""
        return (
            self.artifact == other.artifact
            and self.initiative_id == other.initiative_id
            and self.content_digest == other.content_digest
            and self.digest_algorithm == other.digest_algorithm
            and self.revision == other.revision
            and self.revision_kind == other.revision_kind
        )

    def version_key(self) -> str:
        parts = (
            self.initiative_id,
            self.artifact.store_id,
            self.artifact.artifact_id,
            self.digest_algorithm,
            self.content_digest,
            self.revision_kind.value if self.revision_kind else "",
            self.revision or "",
        )
        return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()

    def lineage_key(self) -> tuple[str, str, str]:
        """Identifies the artifact across versions (for stale-approval detection)."""
        return (self.initiative_id, self.artifact.store_id, self.artifact.artifact_id)


class EvidenceType(StrEnum):
    TEST_EXECUTION = "test_execution"
    STATIC_ANALYSIS = "static_analysis"
    CODE_INSPECTION = "code_inspection"
    POLICY_CHECK = "policy_check"
    ARTIFACT_COMPARISON = "artifact_comparison"
    REVIEWER_FINDING = "reviewer_finding"


class EvidenceRecord(ContractModel):
    """A reference to evidence produced by trusted tooling. Payloads are never copied here."""

    evidence_id: str
    evidence_type: EvidenceType
    initiative_id: str
    artifact_digest: str
    source_ref: str  # opaque trusted reference (test run, scan id, finding id)
    result_code: str | None = None

    @field_validator("evidence_id", "source_ref")
    @classmethod
    def ids(cls, value: str) -> str:
        return plain_id(value, "evidence identifier")

    @field_validator("initiative_id")
    @classmethod
    def initiative(cls, value: str) -> str:
        if not _LOWER_ID.match(value):
            raise ValueError("invalid initiative_id")
        return value

    @field_validator("artifact_digest")
    @classmethod
    def digest(cls, value: str) -> str:
        if not _HEX64.match(value):
            raise ValueError("artifact_digest must be 64 lowercase hex characters")
        return value

    @field_validator("result_code")
    @classmethod
    def code(cls, value: str | None) -> str | None:
        if value is not None and not _CODE.match(value):
            raise ValueError("result_code must be a short code")
        return value


class ReviewOutcome(StrEnum):
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    REJECTED = "rejected"
    INCONCLUSIVE = "inconclusive"


class ReviewSuggestion(ContractModel):
    """What the reviewer model may say (untrusted). It carries no provenance, IDs or evidence.

    Trusted platform validation decides whether the suggestion can be recorded.
    """

    outcome: ReviewOutcome
    finding_codes: tuple[str, ...] = ()

    @field_validator("finding_codes")
    @classmethod
    def codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > MAX_FINDINGS or len(set(value)) != len(value):
            raise ValueError("finding codes must be unique and bounded")
        if any(not _CODE.match(item) for item in value):
            raise ValueError("finding codes must be short codes")
        return value


class ReviewRecord(ContractModel):
    """Immutable, append-only review outcome bound to one exact artifact version."""

    review_id: str
    artifact: ArtifactIdentity
    reviewer: ModelProvenance
    outcome: ReviewOutcome
    finding_codes: tuple[str, ...]
    evidence: tuple[EvidenceRecord, ...]
    recorded_at: datetime
    policy_revision: int
    request_id: str
    correlation_id: str
    trace_id: str
    task_id: str | None = None
    attempt_number: int

    @field_validator("recorded_at")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("review_id", "request_id", "correlation_id", "trace_id", "task_id")
    @classmethod
    def ids(cls, value: str | None) -> str | None:
        return None if value is None else plain_id(value, "identifier")

    @field_validator("policy_revision", "attempt_number")
    @classmethod
    def positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("value must be positive")
        return value

    @model_validator(mode="after")
    def consistent(self) -> ReviewRecord:
        if self.reviewer.role is not ModelRole.INDEPENDENT_REVIEWER:
            raise ValueError("reviewer provenance must use the independent reviewer role")
        if len(self.evidence) > MAX_EVIDENCE:
            raise ValueError("too many evidence records")
        for item in self.evidence:
            if (
                item.artifact_digest != self.artifact.content_digest
                or item.initiative_id != self.artifact.initiative_id
            ):
                raise ValueError("evidence must reference the reviewed artifact")
        return self

    @property
    def approves(self) -> bool:
        return self.outcome is ReviewOutcome.APPROVED


class RecordStatus(StrEnum):
    RECORDED = "recorded"
    DUPLICATE = "duplicate"  # identical submission; the existing record is returned
    REJECTED = "rejected"


class RejectionReason(StrEnum):
    POLICY_DISABLED = "policy_disabled"
    INITIATIVE_MISMATCH = "initiative_mismatch"
    ARTIFACT_NOT_PINNED = "artifact_not_pinned"
    WRONG_REVIEWER_ROLE = "wrong_reviewer_role"
    REGISTRY_MISMATCH = "registry_mismatch"
    REVIEWER_UNAVAILABLE = "reviewer_unavailable"
    INDEPENDENCE_VIOLATION = "independence_violation"
    UNKNOWN_PROVENANCE = "unknown_provenance"
    EVIDENCE_REQUIRED = "evidence_required"
    EVIDENCE_INVALID = "evidence_invalid"
    OUTCOME_NOT_RECORDABLE = "outcome_not_recordable"
    ATTEMPT_LIMIT_REACHED = "attempt_limit_reached"
    CONFLICTING_DUPLICATE = "conflicting_duplicate"
    PERSISTENCE_FAILED = "persistence_failed"


class ReviewResult(ContractModel):
    status: RecordStatus
    reason: RejectionReason | None = None
    record: ReviewRecord | None = None
    independence_failures: tuple[str, ...] = ()
