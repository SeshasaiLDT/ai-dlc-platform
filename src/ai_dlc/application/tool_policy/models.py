"""Immutable logical tool-policy requests, rules, and decisions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from fnmatch import fnmatchcase

from ai_dlc.application.authorization import JiraProjectTarget, RepositoryTarget
from ai_dlc.domain.identity import Principal

from .operations import (
    GitOperation,
    ToolKind,
    ToolOperation,
    ToolOperationRisk,
    tool_kind,
)


def _identifier(value: str, *, jira: bool = False) -> None:
    pattern = r"[A-Z][A-Z0-9_]*" if jira else r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*"
    limit = 20 if jira else 80
    if not isinstance(value, str) or len(value) > limit or re.fullmatch(pattern, value) is None:
        raise ValueError("invalid logical identifier")


def _servicenow_scope_id(value: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) > 80
        or re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]*", value) is None
    ):
        raise ValueError("invalid ServiceNow scope identifier")


@dataclass(frozen=True, slots=True)
class GitTarget:
    repository_id: str
    branch_name: str | None = None

    def __post_init__(self) -> None:
        RepositoryTarget(self.repository_id)
        if self.branch_name is not None:
            branch = self.branch_name
            if (
                not isinstance(branch, str)
                or len(branch) > 128
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", branch) is None
                or ".." in branch
                or branch.endswith(("/", "."))
            ):
                raise ValueError("invalid logical branch name")


@dataclass(frozen=True, slots=True)
class ServiceNowTarget:
    scope_id: str

    def __post_init__(self) -> None:
        _servicenow_scope_id(self.scope_id)


ToolTarget = JiraProjectTarget | GitTarget | ServiceNowTarget


@dataclass(frozen=True, slots=True)
class ToolPolicyRequest:
    principal: Principal
    initiative_id: str
    operation: ToolOperation
    target: ToolTarget

    def __post_init__(self) -> None:
        if not isinstance(self.principal, Principal):
            raise TypeError("principal must be a Principal")
        _identifier(self.initiative_id)
        kind = tool_kind(self.operation)
        expected = {
            ToolKind.JIRA: JiraProjectTarget,
            ToolKind.GIT: GitTarget,
            ToolKind.SERVICENOW: ServiceNowTarget,
        }[kind]
        if not isinstance(self.target, expected):
            raise ValueError("operation and logical target do not match")
        if (
            kind is ToolKind.GIT
            and self.operation
            not in (
                GitOperation.READ_REPOSITORY,
                GitOperation.LIST_BRANCHES,
                GitOperation.READ_DIFF,
                GitOperation.READ_PR,
            )
            and self.target.branch_name is None
        ):
            raise ValueError("Git operation requires a branch target")


class ToolPolicyEffect(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class ToolPolicyReason(StrEnum):
    ALLOWED_BY_POLICY = "allowed_by_policy"
    NO_POLICY = "no_policy"
    OPERATION_DENIED = "operation_denied"
    DESTRUCTIVE_OPERATION_DENIED = "destructive_operation_denied"
    APPROVAL_REQUIRED = "approval_required"
    TARGET_NOT_ALLOWED = "target_not_allowed"
    BASE_AUTHORIZATION_REQUIRED = "base_authorization_required"
    INITIATIVE_POLICY_DISABLED = "initiative_policy_disabled"
    AMBIGUOUS_POLICY = "ambiguous_policy"


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    initiative_id: str
    operation: ToolOperation
    effect: ToolPolicyEffect
    target_id: str | None = None
    branch_pattern: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.initiative_id)
        kind = tool_kind(self.operation)
        if not isinstance(self.effect, ToolPolicyEffect):
            raise ValueError("unknown policy effect")
        if self.target_id is not None:
            if kind is ToolKind.SERVICENOW:
                _servicenow_scope_id(self.target_id)
            else:
                _identifier(self.target_id, jira=kind is ToolKind.JIRA)
        if self.branch_pattern is not None:
            if kind is not ToolKind.GIT:
                raise ValueError("branch patterns require a Git operation")
            pattern = self.branch_pattern
            if (
                not isinstance(pattern, str)
                or len(pattern) > 128
                or re.fullmatch(r"[A-Za-z0-9*][A-Za-z0-9._/*-]*", pattern) is None
                or ".." in pattern
            ):
                raise ValueError("invalid branch pattern")

    @property
    def tool(self) -> ToolKind:
        return tool_kind(self.operation)

    def matches(self, request: ToolPolicyRequest) -> bool:
        if (
            self.initiative_id != request.initiative_id
            or type(self.operation) is not type(request.operation)
            or self.operation != request.operation
        ):
            return False
        target = request.target
        target_id = (
            target.project_id
            if isinstance(target, JiraProjectTarget)
            else target.repository_id
            if isinstance(target, GitTarget)
            else target.scope_id
        )
        if self.target_id is not None and self.target_id != target_id:
            return False
        if self.branch_pattern is not None:
            return (
                isinstance(target, GitTarget)
                and target.branch_name is not None
                and fnmatchcase(target.branch_name, self.branch_pattern)
            )
        return True


@dataclass(frozen=True, slots=True)
class ToolPolicyDecision:
    decision_id: str
    occurred_at: datetime
    principal_id: str
    initiative_id: str
    tool: ToolKind
    operation: ToolOperation
    risk: ToolOperationRisk
    target: ToolTarget
    effect: ToolPolicyEffect
    reason: ToolPolicyReason
    base_decision_id: str

    def __post_init__(self) -> None:
        expected = {
            ToolPolicyEffect.ALLOW: ToolPolicyReason.ALLOWED_BY_POLICY,
            ToolPolicyEffect.REQUIRE_APPROVAL: ToolPolicyReason.APPROVAL_REQUIRED,
        }
        if self.effect in expected and self.reason is not expected[self.effect]:
            raise ValueError("tool policy effect and reason disagree")
        if self.effect is ToolPolicyEffect.DENY and self.reason in expected.values():
            raise ValueError("tool policy effect and reason disagree")


@dataclass(frozen=True, slots=True)
class ToolPolicyAuditEvent:
    decision_id: str
    occurred_at: datetime
    principal_id: str
    initiative_id: str
    tool: ToolKind
    operation: ToolOperation
    risk: ToolOperationRisk
    target: ToolTarget
    effect: ToolPolicyEffect
    reason: ToolPolicyReason
    base_decision_id: str

    @classmethod
    def from_decision(cls, decision: ToolPolicyDecision) -> ToolPolicyAuditEvent:
        return cls(
            decision_id=decision.decision_id,
            occurred_at=decision.occurred_at,
            principal_id=decision.principal_id,
            initiative_id=decision.initiative_id,
            tool=decision.tool,
            operation=decision.operation,
            risk=decision.risk,
            target=decision.target,
            effect=decision.effect,
            reason=decision.reason,
            base_decision_id=decision.base_decision_id,
        )
