"""The store must atomically commit a snapshot, history version, and audit event."""

from typing import Protocol

from ai_dlc.domain.identity import Principal

from .models import ApprovalAuditEvent, ApprovalRecord


class ApprovalRepository(Protocol):
    def get(self, approval_id: str) -> ApprovalRecord: ...

    def get_by_request_key(self, request_key: str) -> ApprovalRecord | None: ...

    def commit(
        self,
        record: ApprovalRecord,
        event: ApprovalAuditEvent,
        *,
        expected_version: int | None,
    ) -> ApprovalRecord:
        """Atomic append with compare-and-swap. None means new approval."""

    def history(self, approval_id: str) -> tuple[ApprovalRecord, ...]: ...

    def audit_events(self, approval_id: str) -> tuple[ApprovalAuditEvent, ...]: ...

    def list_pending(self, initiative_id: str) -> tuple[ApprovalRecord, ...]: ...

    def list_requested_by(self, principal_id: str) -> tuple[ApprovalRecord, ...]: ...

    def list_decided_by(self, principal_id: str) -> tuple[ApprovalRecord, ...]: ...


class HumanIdentityVerifier(Protocol):
    def is_human(self, principal: Principal) -> bool:
        """Trusted authentication boundary attests an interactive human identity."""
