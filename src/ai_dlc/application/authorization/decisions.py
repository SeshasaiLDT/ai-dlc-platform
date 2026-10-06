"""Typed authorization requests, decisions, and audit-safe events."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from ai_dlc.domain.identity import AdminPermission, Capability, Principal, ToolPermission


class AuthorizationReason(StrEnum):
    ALLOWED = "allowed"
    MEMBERSHIP_NOT_FOUND = "membership_not_found"
    MEMBERSHIP_DISABLED = "membership_disabled"
    CAPABILITY_NOT_GRANTED = "capability_not_granted"
    TOOL_PERMISSION_NOT_GRANTED = "tool_permission_not_granted"
    ADMIN_PERMISSION_NOT_GRANTED = "admin_permission_not_granted"
    TARGET_REQUIRED = "target_required"
    TARGET_OUT_OF_SCOPE = "target_out_of_scope"
    INITIATIVE_MISMATCH = "initiative_mismatch"
    INVALID_REQUEST = "invalid_request"


@dataclass(frozen=True, slots=True)
class CapabilityAction:
    capability: Capability

    def __post_init__(self) -> None:
        if not isinstance(self.capability, Capability):
            raise ValueError("unknown capability")


@dataclass(frozen=True, slots=True)
class ToolAction:
    permission: ToolPermission

    def __post_init__(self) -> None:
        if not isinstance(self.permission, ToolPermission):
            raise ValueError("unknown tool permission")


@dataclass(frozen=True, slots=True)
class AdminAction:
    permission: AdminPermission

    def __post_init__(self) -> None:
        if not isinstance(self.permission, AdminPermission):
            raise ValueError("unknown admin permission")


AuthorizationAction = CapabilityAction | ToolAction | AdminAction


def _logical_id(value: str, *, jira: bool = False) -> None:
    pattern = r"[A-Z][A-Z0-9_]*" if jira else r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*"
    limit = 20 if jira else 80
    if not isinstance(value, str) or len(value) > limit or re.fullmatch(pattern, value) is None:
        raise ValueError("logical target ID has an invalid format")


@dataclass(frozen=True, slots=True)
class JiraProjectTarget:
    project_id: str

    def __post_init__(self) -> None:
        _logical_id(self.project_id, jira=True)


@dataclass(frozen=True, slots=True)
class RepositoryTarget:
    repository_id: str

    def __post_init__(self) -> None:
        _logical_id(self.repository_id)


@dataclass(frozen=True, slots=True)
class KnowledgeSourceTarget:
    source_id: str

    def __post_init__(self) -> None:
        _logical_id(self.source_id)


AuthorizationTarget = JiraProjectTarget | RepositoryTarget | KnowledgeSourceTarget


@dataclass(frozen=True, slots=True)
class AuthorizationRequest:
    principal: Principal
    initiative_id: str
    action: AuthorizationAction
    target: AuthorizationTarget | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.principal, Principal):
            raise TypeError("principal must be a Principal")
        if not isinstance(self.initiative_id, str) or not self.initiative_id.strip():
            raise ValueError("initiative_id must be nonblank")
        if not isinstance(self.action, (CapabilityAction, ToolAction, AdminAction)):
            raise ValueError("unknown authorization action")
        if self.target is not None and not isinstance(
            self.target, (JiraProjectTarget, RepositoryTarget, KnowledgeSourceTarget)
        ):
            raise ValueError("unknown authorization target")


@dataclass(frozen=True, slots=True)
class AuthorizationDecision:
    decision_id: str
    occurred_at: datetime
    principal_id: str
    initiative_id: str
    action: AuthorizationAction
    target: AuthorizationTarget | None
    allowed: bool
    reason: AuthorizationReason
    initiative_revision: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.reason, AuthorizationReason):
            raise ValueError("unknown authorization reason")
        if self.allowed != (self.reason is AuthorizationReason.ALLOWED):
            raise ValueError("allowed flag and reason disagree")


@dataclass(frozen=True, slots=True)
class AuthorizationAuditEvent:
    decision_id: str
    occurred_at: datetime
    principal_id: str
    initiative_id: str
    action: AuthorizationAction
    target: AuthorizationTarget | None
    allowed: bool
    reason: AuthorizationReason
    initiative_revision: int | None = None

    @classmethod
    def from_decision(cls, decision: AuthorizationDecision) -> AuthorizationAuditEvent:
        return cls(
            decision_id=decision.decision_id,
            occurred_at=decision.occurred_at,
            principal_id=decision.principal_id,
            initiative_id=decision.initiative_id,
            action=decision.action,
            target=decision.target,
            allowed=decision.allowed,
            reason=decision.reason,
            initiative_revision=decision.initiative_revision,
        )
