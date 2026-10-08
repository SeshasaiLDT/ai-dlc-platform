"""Deterministic in-memory review store and evidence verifier for tests and local use.

Process-local only: it demonstrates append-only semantics, not cross-runtime durability.
"""

from threading import Lock

from ai_dlc.application.review.errors import DuplicateReviewConflictError, ReviewAttemptLimitError
from ai_dlc.application.review.models import ArtifactIdentity, EvidenceRecord, ReviewRecord


class InMemoryReviewRepository:
    def __init__(self) -> None:
        self._records: dict[str, ReviewRecord] = {}
        self._order: list[str] = []
        self._lock = Lock()

    def append(self, record: ReviewRecord, *, max_attempts: int) -> ReviewRecord:
        with self._lock:
            existing = self._records.get(record.review_id)
            if existing is not None:
                if existing == record:
                    return existing
                # Same submission identity, different content (e.g. another outcome or time).
                if self._same_submission(existing, record):
                    return existing
                raise DuplicateReviewConflictError("review ID already recorded differently")
            attempts = sum(
                1 for r in self._records.values() if r.artifact.same_version(record.artifact)
            )
            if attempts >= max_attempts:
                raise ReviewAttemptLimitError("review attempt limit reached")
            self._records[record.review_id] = record
            self._order.append(record.review_id)
            return record

    @staticmethod
    def _same_submission(existing: ReviewRecord, new: ReviewRecord) -> bool:
        """A retried submission differs only in bookkeeping, not in the review's substance."""
        return (
            existing.outcome == new.outcome
            and existing.finding_codes == new.finding_codes
            and existing.evidence == new.evidence
            and existing.reviewer == new.reviewer
            and existing.artifact == new.artifact
        )

    def get(self, review_id: str) -> ReviewRecord | None:
        with self._lock:
            return self._records.get(review_id)

    def list_for_artifact(self, artifact: ArtifactIdentity) -> tuple[ReviewRecord, ...]:
        with self._lock:
            return tuple(
                self._records[i]
                for i in self._order
                if self._records[i].artifact.same_version(artifact)
            )

    def list_for_lineage(self, artifact: ArtifactIdentity) -> tuple[ReviewRecord, ...]:
        with self._lock:
            return tuple(
                self._records[i]
                for i in self._order
                if self._records[i].artifact.lineage_key() == artifact.lineage_key()
            )

    @property
    def all_records(self) -> tuple[ReviewRecord, ...]:
        with self._lock:
            return tuple(self._records[i] for i in self._order)


class StaticEvidenceVerifier:
    """Trusted-fixture verifier: only pre-registered evidence passes."""

    def __init__(self, trusted: tuple[EvidenceRecord, ...] = ()) -> None:
        self._trusted = {item.evidence_id: item for item in trusted}

    def verify(self, evidence: EvidenceRecord, artifact: ArtifactIdentity) -> bool:
        return (
            self._trusted.get(evidence.evidence_id) == evidence
            and evidence.artifact_digest == artifact.content_digest
            and evidence.initiative_id == artifact.initiative_id
        )
