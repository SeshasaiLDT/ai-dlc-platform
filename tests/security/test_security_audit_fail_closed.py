"""Traceable allow/deny decisions and rollback when audit persistence fails."""

import sqlite3

import pytest
from conftest import PEOPLE, A

from ai_dlc.adapters.approval import SQLiteApprovalRepository
from ai_dlc.adapters.authorization import InMemoryAuthorizationAuditSink
from ai_dlc.adapters.tool_policy import InMemoryToolPolicyAuditSink
from ai_dlc.application.approval import ApprovalAuditError, ApprovalStatus
from ai_dlc.application.authorization import (
    AuthorizationAuditError,
    AuthorizationReason,
    AuthorizationRequest,
    JiraProjectTarget,
    ToolAction,
)
from ai_dlc.application.tool_policy import (
    GitOperation,
    GitTarget,
    JiraOperation,
    ToolPolicyAuditError,
    ToolPolicyEffect,
    ToolPolicyRequest,
)
from ai_dlc.domain.identity import ToolPermission


def test_full_decision_lineage_is_audit_safe(world):
    read = world.base.evaluate(
        AuthorizationRequest(
            PEOPLE["viewer-a"],
            A.initiative.id,
            ToolAction(ToolPermission.JIRA_READ),
            JiraProjectTarget("TRAVEL"),
        ),
        profile=A,
    )
    denied = world.base.evaluate(
        AuthorizationRequest(
            PEOPLE["viewer-a"],
            A.initiative.id,
            ToolAction(ToolPermission.JIRA_WRITE),
            JiraProjectTarget("TRAVEL"),
        ),
        profile=A,
    )
    assert read.allowed and denied.reason is AuthorizationReason.TOOL_PERMISSION_NOT_GRANTED
    for operation, target, effect in (
        (JiraOperation.READ_ISSUE, JiraProjectTarget("TRAVEL"), ToolPolicyEffect.ALLOW),
        (JiraOperation.DELETE_ISSUE, JiraProjectTarget("TRAVEL"), ToolPolicyEffect.DENY),
        (
            GitOperation.PUSH,
            GitTarget("travel-api", "feature/x"),
            ToolPolicyEffect.REQUIRE_APPROVAL,
        ),
    ):
        result = world.tool.evaluate(
            ToolPolicyRequest(PEOPLE["developer-a"], A.initiative.id, operation, target),
            profile=A,
        )
        assert result.effect is effect
        assert world.tool_audit.events[-1].base_decision_id == result.base_decision_id
    assert len(world.base_audit.events) == 5
    request = ToolPolicyRequest(
        PEOPLE["developer-a"],
        A.initiative.id,
        GitOperation.PUSH,
        GitTarget("travel-api", "feature/x"),
    )
    pending = world.approval.request_approval(request, profile=A, request_key="audit-pending")
    accepted = world.approval.approve(
        pending.approval_id, PEOPLE["approver-a"], profile=A, expected_version=1
    )
    assert accepted.status is ApprovalStatus.APPROVED
    events = world.approvals.audit_events(accepted.approval_id)
    assert [event.to_status for event in events] == [
        ApprovalStatus.PENDING,
        ApprovalStatus.APPROVED,
    ]
    assert events[-1].base_decision_id == pending.base_decision_id
    assert events[-1].tool_policy_decision_id == pending.tool_policy_decision_id
    rendered = repr((world.base_audit.events, world.tool_audit.events, events)).lower()
    assert all(
        secret not in rendered for secret in ("@example.test", "jwt", "credential", "endpoint")
    )


def test_rejection_audit_has_actor_and_lineage(world):
    request = ToolPolicyRequest(
        PEOPLE["developer-a"],
        A.initiative.id,
        GitOperation.PUSH,
        GitTarget("travel-api", "feature/x"),
    )
    pending = world.approval.request_approval(request, profile=A, request_key="audit-reject")
    rejected = world.approval.reject(
        pending.approval_id, PEOPLE["approver-a"], profile=A, expected_version=1
    )
    event = world.approvals.audit_events(rejected.approval_id)[-1]
    assert event.requester_id == PEOPLE["developer-a"].subject_id
    assert event.approver_id == PEOPLE["approver-a"].subject_id
    assert event.base_decision_id == pending.base_decision_id
    assert event.tool_policy_decision_id == pending.tool_policy_decision_id
    assert not rejected.approval_gate_satisfied


def test_authorization_audit_failure_blocks_protected_decision(world_factory):
    class FailingAudit(InMemoryAuthorizationAuditSink):
        def record(self, event):
            raise OSError("audit offline")

    world = world_factory(base_audit=FailingAudit())
    with pytest.raises(AuthorizationAuditError):
        world.base.require(
            AuthorizationRequest(
                PEOPLE["viewer-a"],
                A.initiative.id,
                ToolAction(ToolPermission.JIRA_READ),
                JiraProjectTarget("TRAVEL"),
            ),
            profile=A,
        )


def test_tool_policy_audit_failure_blocks_result(world_factory):
    class FailingAudit(InMemoryToolPolicyAuditSink):
        def record(self, event):
            raise OSError("audit offline")

    world = world_factory(tool_audit=FailingAudit())
    with pytest.raises(ToolPolicyAuditError):
        world.tool.require_allowed(
            ToolPolicyRequest(
                PEOPLE["developer-a"],
                A.initiative.id,
                JiraOperation.READ_ISSUE,
                JiraProjectTarget("TRAVEL"),
            ),
            profile=A,
        )


def test_approval_audit_failure_rolls_back_persistent_transition(world_factory, tmp_path):
    path = tmp_path / "audit-regression.db"
    world = world_factory(approvals=SQLiteApprovalRepository(path))
    request = ToolPolicyRequest(
        PEOPLE["developer-a"],
        A.initiative.id,
        GitOperation.PUSH,
        GitTarget("travel-api", "feature/x"),
    )
    pending = world.approval.request_approval(request, profile=A, request_key="audit-rollback")
    with sqlite3.connect(path) as db:
        db.execute(
            """CREATE TRIGGER deny_event BEFORE INSERT ON approval_audit
            BEGIN SELECT RAISE(FAIL, 'audit offline'); END"""
        )
    with pytest.raises(ApprovalAuditError):
        world.approval.approve(
            pending.approval_id, PEOPLE["approver-a"], profile=A, expected_version=1
        )
    assert world.approvals.get(pending.approval_id) == pending
    assert world.approvals.history(pending.approval_id) == (pending,)
    assert len(world.approvals.audit_events(pending.approval_id)) == 1
