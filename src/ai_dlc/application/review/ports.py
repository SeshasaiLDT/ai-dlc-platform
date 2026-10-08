"""Narrow ports for review persistence and trusted evidence verification."""

from typing import Protocol

from .models import ArtifactIdentity, EvidenceRecord, ReviewRecord


class ReviewRepository(Protocol):
    """Append-only store. There is deliberately no update or delete.

    A durable adapter MUST make ``append`` atomic with respect to the duplicate check and the
    attempt-limit count (one conditional write or transaction), persist across runtimes, and keep
    records immutable. An in-memory adapter proves none of the durability properties.
    """

    def append(self, record: ReviewRecord, *, max_attempts: int) -> ReviewRecord:
        """Append the record, or return the identical existing record for a duplicate submission.

        Raises DuplicateReviewConflictError if the same review ID exists with different content,
        and ReviewAttemptLimitError if this exact artifact version already has ``max_attempts``
        records. Must raise rather than overwrite.
        """

    def get(self, review_id: str) -> ReviewRecord | None: ...

    def list_for_artifact(self, artifact: ArtifactIdentity) -> tuple[ReviewRecord, ...]:
        """Records for this exact artifact version, in append order."""

    def list_for_lineage(self, artifact: ArtifactIdentity) -> tuple[ReviewRecord, ...]:
        """Records for any version of the same artifact (used to detect stale approvals)."""


class EvidenceVerifier(Protocol):
    """Trusted check that an evidence record exists in the trusted evidence source."""

    def verify(self, evidence: EvidenceRecord, artifact: ArtifactIdentity) -> bool: ...
