"""Thread-safe local approval store with atomic history and audit commits."""

from dataclasses import replace
from threading import Lock

from ai_dlc.application.approval.errors import ApprovalConflictError, ApprovalNotFoundError
from ai_dlc.application.approval.models import ApprovalAuditEvent, ApprovalRecord, ApprovalStatus


class InMemoryApprovalRepository:
    def __init__(self) -> None:
        self._lock = Lock()
        self._records: dict[str, ApprovalRecord] = {}
        self._keys: dict[str, str] = {}
        self._history: dict[str, tuple[ApprovalRecord, ...]] = {}
        self._events: dict[str, tuple[ApprovalAuditEvent, ...]] = {}

    def get(self, approval_id: str) -> ApprovalRecord:
        with self._lock:
            try:
                return self._records[approval_id]
            except KeyError:
                raise ApprovalNotFoundError from None

    def get_by_request_key(self, request_key: str) -> ApprovalRecord | None:
        with self._lock:
            approval_id = self._keys.get(request_key)
            return self._records[approval_id] if approval_id else None

    def commit(
        self,
        record: ApprovalRecord,
        event: ApprovalAuditEvent,
        *,
        expected_version: int | None,
    ) -> ApprovalRecord:
        with self._lock:
            current = self._records.get(record.approval_id)
            if expected_version is None:
                if current is not None or record.request_key in self._keys or record.version != 1:
                    raise ApprovalConflictError
            elif (
                current is None
                or current.version != expected_version
                or current.status is not ApprovalStatus.PENDING
                or record.version != expected_version + 1
                or record.request_key != current.request_key
                or replace(
                    record,
                    version=current.version,
                    status=current.status,
                    decided_at=current.decided_at,
                    decided_by=current.decided_by,
                )
                != current
            ):
                raise ApprovalConflictError
            if event != ApprovalAuditEvent.for_transition(event.event_id, current, record):
                raise ValueError("approval audit event does not match transition")
            self._records[record.approval_id] = record
            self._keys[record.request_key] = record.approval_id
            self._history[record.approval_id] = (*self._history.get(record.approval_id, ()), record)
            self._events[record.approval_id] = (*self._events.get(record.approval_id, ()), event)
            return record

    def history(self, approval_id: str) -> tuple[ApprovalRecord, ...]:
        self.get(approval_id)
        with self._lock:
            return self._history[approval_id]

    def audit_events(self, approval_id: str) -> tuple[ApprovalAuditEvent, ...]:
        self.get(approval_id)
        with self._lock:
            return self._events[approval_id]

    def list_pending(self, initiative_id: str) -> tuple[ApprovalRecord, ...]:
        return self._list(
            lambda r: r.initiative_id == initiative_id and r.status is ApprovalStatus.PENDING
        )

    def list_requested_by(self, principal_id: str) -> tuple[ApprovalRecord, ...]:
        return self._list(lambda r: r.principal_id == principal_id)

    def list_decided_by(self, principal_id: str) -> tuple[ApprovalRecord, ...]:
        return self._list(lambda r: r.decided_by == principal_id)

    def _list(self, predicate) -> tuple[ApprovalRecord, ...]:
        with self._lock:
            return tuple(
                sorted(
                    (r for r in self._records.values() if predicate(r)),
                    key=lambda r: r.approval_id,
                )
            )


class InMemoryHumanIdentityVerifier:
    """Test adapter: explicit allowlist stands in for a trusted human identity source."""

    def __init__(self, principal_ids: frozenset[str] = frozenset()) -> None:
        self._principal_ids = frozenset(principal_ids)

    def is_human(self, principal) -> bool:
        return principal.subject_id in self._principal_ids
