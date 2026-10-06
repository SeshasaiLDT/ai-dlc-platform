"""A human decision applies to one exact trusted operation and current grants."""

from dataclasses import replace

import pytest
from conftest import MEMBERSHIPS, PEOPLE, POLICIES, A, B

from ai_dlc.adapters.approval import SQLiteApprovalRepository
from ai_dlc.application.approval import (
    ApprovalNotAuthorizedError,
    ApprovalStatus,
    InvalidApprovalSourceError,
    SelfApprovalNotAllowedError,
)
from ai_dlc.application.authorization import JiraProjectTarget
from ai_dlc.application.tool_policy import (
    GitOperation,
    GitTarget,
    JiraOperation,
    ToolPolicyEffect,
    ToolPolicyRequest,
)
from ai_dlc.domain.identity import Principal


def git_request(branch="feature/x", repository="travel-api", principal="developer-a", profile=A):
    return ToolPolicyRequest(
        PEOPLE[principal],
        profile.initiative.id,
        GitOperation.PUSH,
        GitTarget(repository, branch),
    )


def approved(world, *, request=None, revision=7, key="approval-action"):
    request = request or git_request()
    record = world.approval.request_approval(
        request, profile=A, request_key=key, initiative_revision=revision
    )
    return world.approval.approve(
        record.approval_id, PEOPLE["approver-a"], profile=A, expected_version=1
    )


def gate(world, record, request, *, profile=A, revision=7):
    return world.approval.approval_gate_satisfied(
        record.approval_id, request, profile=profile, initiative_revision=revision
    )


def test_pending_rejected_and_approved_are_distinct_execution_gates(world):
    request = git_request()
    pending = world.approval.request_approval(
        request, profile=A, request_key="pending", initiative_revision=7
    )
    assert not gate(world, pending, request)
    rejected = world.approval.reject(
        pending.approval_id, PEOPLE["approver-a"], profile=A, expected_version=1
    )
    assert rejected.status is ApprovalStatus.REJECTED
    assert not gate(world, rejected, request)
    accepted = approved(world, key="accepted")
    assert gate(world, accepted, request)
    assert len(world.approvals.history(accepted.approval_id)) == 2


@pytest.mark.parametrize(
    "replacement",
    [
        lambda: git_request(branch="main"),
        lambda: git_request(repository="travel-web"),
        lambda: ToolPolicyRequest(
            PEOPLE["developer-a"],
            A.initiative.id,
            GitOperation.COMMIT,
            GitTarget("travel-api", "feature/x"),
        ),
        lambda: ToolPolicyRequest(
            PEOPLE["developer-a"],
            A.initiative.id,
            JiraOperation.ADD_COMMENT,
            JiraProjectTarget("TRAVEL"),
        ),
        lambda: git_request(principal="multi", profile=B, repository="operations-runbooks"),
    ],
)
def test_approval_cannot_replay_for_target_tool_operation_or_initiative(world, replacement):
    record = approved(world)
    request = replacement()
    profile = B if request.initiative_id == B.initiative.id else A
    assert not gate(world, record, request, profile=profile)


def test_revision_metadata_cannot_bypass_scope_or_policy(world):
    record = approved(world)
    assert not gate(world, record, git_request(), revision=8)
    assert not gate(world, record, git_request(branch="main"), revision=7)
    assert gate(world, record, git_request(), revision=7)


def test_revoked_membership_or_changed_policy_invalidates_prior_approval(world_factory):
    original = world_factory()
    record = approved(original)
    revoked_members = tuple(
        replace(item, enabled=False) if item.principal_id == "developer-a" else item
        for item in MEMBERSHIPS
    )
    revoked = world_factory(memberships=revoked_members, approvals=original.approvals)
    assert not gate(revoked, record, git_request())
    changed = world_factory(
        policies=tuple(
            replace(policy, effect=ToolPolicyEffect.DENY)
            if policy.initiative_id == A.initiative.id and policy.operation is GitOperation.PUSH
            else policy
            for policy in POLICIES
        ),
        approvals=original.approvals,
    )
    assert not gate(changed, record, git_request())


def test_approval_source_cannot_be_replayed_across_initiative(world):
    a_decision = world.tool.evaluate(git_request(), profile=A)
    with pytest.raises(TypeError):
        world.approval.request_approval(
            a_decision, profile=B, request_key="forged", initiative_revision=7
        )
    with pytest.raises(InvalidApprovalSourceError):
        world.approval.request_approval(
            git_request(), profile=B, request_key="wrong-profile", initiative_revision=7
        )
    b_request = git_request(principal="developer-a", profile=B, repository="operations-runbooks")
    with pytest.raises(InvalidApprovalSourceError):
        world.approval.request_approval(
            b_request, profile=B, request_key="unassigned", initiative_revision=7
        )


def test_approval_authority_is_initiative_scoped_and_not_platform_global(world):
    record = world.approval.request_approval(
        git_request(), profile=A, request_key="admin-boundary", initiative_revision=7
    )
    for name in ("platform-admin", "developer-b", "initiative-admin"):
        with pytest.raises(ApprovalNotAuthorizedError):
            world.approval.approve(record.approval_id, PEOPLE[name], profile=A, expected_version=1)
    assert world.approvals.get(record.approval_id).status is ApprovalStatus.PENDING


def test_self_approval_uses_subject_id_not_display_name_or_email(world):
    request = git_request(principal="self-approver")
    record = world.approval.request_approval(
        request, profile=A, request_key="self", initiative_revision=7
    )
    changed_details = Principal(
        "self-approver", "test-enterprise", "Another Name", "new@example.test"
    )
    with pytest.raises(SelfApprovalNotAllowedError):
        world.approval.approve(record.approval_id, changed_details, profile=A, expected_version=1)


def test_service_identity_cannot_approve_even_with_approval_grant(world):
    record = world.approval.request_approval(
        git_request(), profile=A, request_key="service-identity", initiative_revision=7
    )
    with pytest.raises(ApprovalNotAuthorizedError):
        world.approval.approve(
            record.approval_id, PEOPLE["service-agent"], profile=A, expected_version=1
        )


def test_disabled_approver_membership_and_foreign_approver_cannot_decide(world_factory):
    original = world_factory()
    pending = original.approval.request_approval(
        git_request(), profile=A, request_key="disabled-approver", initiative_revision=7
    )
    disabled_members = tuple(
        replace(item, enabled=False) if item.principal_id == "approver-a" else item
        for item in MEMBERSHIPS
    )
    resumed = world_factory(memberships=disabled_members, approvals=original.approvals)
    for principal in (PEOPLE["approver-a"], PEOPLE["developer-b"]):
        with pytest.raises(ApprovalNotAuthorizedError):
            resumed.approval.approve(pending.approval_id, principal, profile=A, expected_version=1)
    assert resumed.approvals.get(pending.approval_id).status is ApprovalStatus.PENDING


def test_pending_approval_survives_new_service_and_sqlite_connection(world_factory, tmp_path):
    path = tmp_path / "approvals.db"
    first = world_factory(approvals=SQLiteApprovalRepository(path))
    request = git_request()
    pending = first.approval.request_approval(
        request, profile=A, request_key="durable", initiative_revision=7
    )
    second = world_factory(approvals=SQLiteApprovalRepository(path))
    assert second.approvals.get(pending.approval_id) == pending
    assert second.approvals.list_pending(A.initiative.id) == (pending,)
    accepted = second.approval.approve(
        pending.approval_id, PEOPLE["approver-a"], profile=A, expected_version=1
    )
    third = world_factory(approvals=SQLiteApprovalRepository(path))
    assert gate(third, accepted, request)
    assert len(third.approvals.audit_events(pending.approval_id)) == 2
