"""Immutable approval snapshots and audit events for logical tool operations."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from ai_dlc.application.authorization import JiraProjectTarget
from ai_dlc.application.tool_policy import (
    GitTarget,
    ServiceNowTarget,
    ToolKind,
    ToolOperationRisk,
    ToolPolicyDecision,
    ToolPolicyEffect,
    ToolPolicyReason,
)
from ai_dlc.application.tool_policy.models import ToolTarget
from ai_dlc.application.tool_policy.operations import ToolOperation, operation_risk, tool_kind


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    approval_id: str
    request_key: str
    version: int
    status: ApprovalStatus
    principal_id: str
    initiative_id: str
    tool: ToolKind
    operation: ToolOperation
    risk: ToolOperationRisk
    target: ToolTarget
    base_decision_id: str
    tool_policy_decision_id: str
    initiative_revision: int | None
    requested_at: datetime
    decided_at: datetime | None = None
    decided_by: str | None = None

    def __post_init__(self) -> None:
        for value in (
            self.approval_id,
            self.request_key,
            self.principal_id,
            self.initiative_id,
            self.base_decision_id,
            self.tool_policy_decision_id,
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("approval identifiers must be nonblank")
        if type(self.version) is not int or self.version < 1:
            raise ValueError("approval version must be positive")
        if self.initiative_revision is not None and (
            type(self.initiative_revision) is not int or self.initiative_revision < 1
        ):
            raise ValueError("initiative revision must be positive")
        if self.tool is not tool_kind(self.operation) or self.risk is not operation_risk(
            self.operation
        ):
            raise ValueError("approval operation classification mismatch")
        expected_target = {
            ToolKind.JIRA: JiraProjectTarget,
            ToolKind.GIT: GitTarget,
            ToolKind.SERVICENOW: ServiceNowTarget,
        }[self.tool]
        if not isinstance(self.target, expected_target):
            raise ValueError("approval operation and target do not match")
        if self.requested_at.tzinfo is None or self.requested_at.utcoffset() is None:
            raise ValueError("approval time must be timezone-aware")
        if self.status is ApprovalStatus.PENDING:
            if self.version != 1 or self.decided_at is not None or self.decided_by is not None:
                raise ValueError("pending approval has decision fields")
        elif self.status in (ApprovalStatus.APPROVED, ApprovalStatus.REJECTED):
            if self.version < 2 or self.decided_at is None or not self.decided_by:
                raise ValueError("terminal approval requires decision metadata")
            if self.decided_at.tzinfo is None or self.decided_at.utcoffset() is None:
                raise ValueError("decision time must be timezone-aware")
        else:
            raise ValueError("unknown approval status")

    @property
    def approval_gate_satisfied(self) -> bool:
        """Only the human gate; execution must recheck earlier gates."""
        return self.status is ApprovalStatus.APPROVED

    @classmethod
    def from_decision(
        cls, decision: ToolPolicyDecision, approval_id: str, request_key: str, revision: int | None
    ) -> ApprovalRecord:
        if decision.effect is not ToolPolicyEffect.REQUIRE_APPROVAL or (
            decision.reason is not ToolPolicyReason.APPROVAL_REQUIRED
        ):
            raise ValueError("tool decision does not require approval")
        return cls(
            approval_id=approval_id,
            request_key=request_key,
            version=1,
            status=ApprovalStatus.PENDING,
            principal_id=decision.principal_id,
            initiative_id=decision.initiative_id,
            tool=decision.tool,
            operation=decision.operation,
            risk=decision.risk,
            target=decision.target,
            base_decision_id=decision.base_decision_id,
            tool_policy_decision_id=decision.decision_id,
            initiative_revision=revision,
            requested_at=decision.occurred_at,
        )

    def decided(self, status: ApprovalStatus, approver_id: str, at: datetime) -> ApprovalRecord:
        if self.status is not ApprovalStatus.PENDING:
            raise ValueError("approval is terminal")
        if status not in (ApprovalStatus.APPROVED, ApprovalStatus.REJECTED):
            raise ValueError("decision must be approve or reject")
        return replace(
            self,
            version=self.version + 1,
            status=status,
            decided_at=at,
            decided_by=approver_id,
        )


@dataclass(frozen=True, slots=True)
class ApprovalAuditEvent:
    event_id: str
    occurred_at: datetime
    approval_id: str
    from_status: ApprovalStatus | None
    to_status: ApprovalStatus
    requester_id: str
    approver_id: str | None
    initiative_id: str
    tool: ToolKind
    operation: ToolOperation
    target: ToolTarget
    base_decision_id: str
    tool_policy_decision_id: str
    initiative_revision: int | None

    @classmethod
    def for_transition(
        cls, event_id: str, previous: ApprovalRecord | None, current: ApprovalRecord
    ) -> ApprovalAuditEvent:
        return cls(
            event_id=event_id,
            occurred_at=current.decided_at or current.requested_at,
            approval_id=current.approval_id,
            from_status=previous.status if previous else None,
            to_status=current.status,
            requester_id=current.principal_id,
            approver_id=current.decided_by,
            initiative_id=current.initiative_id,
            tool=current.tool,
            operation=current.operation,
            target=current.target,
            base_decision_id=current.base_decision_id,
            tool_policy_decision_id=current.tool_policy_decision_id,
            initiative_revision=current.initiative_revision,
        )
