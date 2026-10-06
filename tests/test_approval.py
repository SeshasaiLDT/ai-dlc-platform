"""Approval lifecycle, storage, and security boundary tests."""

import sqlite3
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ai_dlc.adapters.approval import (
    InMemoryApprovalRepository,
    InMemoryHumanIdentityVerifier,
    SQLiteApprovalRepository,
)
from ai_dlc.adapters.authorization import (
    InMemoryAuthorizationAuditSink,
    InMemoryMembershipRepository,
)
from ai_dlc.adapters.tool_policy import InMemoryToolPolicyAuditSink, InMemoryToolPolicyRepository
from ai_dlc.application.approval import (
    ApprovalAuditError,
    ApprovalConflictError,
    ApprovalInvalidTransitionError,
    ApprovalNotAuthorizedError,
    ApprovalPersistenceError,
    ApprovalService,
    ApprovalStatus,
    InvalidApprovalSourceError,
    SelfApprovalNotAllowedError,
)
from ai_dlc.application.authorization import (
    AuthorizationService,
    JiraProjectTarget,
    RoleGrant,
    RolePolicy,
)
from ai_dlc.application.tool_policy import (
    GitOperation,
    GitTarget,
    JiraOperation,
    ToolPolicy,
    ToolPolicyEffect,
    ToolPolicyRequest,
    ToolPolicyService,
)
from ai_dlc.domain.identity import (
    AdminPermission,
    InitiativeMembership,
    Principal,
    Role,
    ToolPermission,
)
from ai_dlc.domain.initiative import load_initiative_profile

EXAMPLES = Path(__file__).resolve().parents[1] / "configs" / "initiatives" / "examples"
PROFILE = load_initiative_profile(EXAMPLES / "travel-platform.yaml")
DOCUMENT = PROFILE.model_dump(mode="json")
DOCUMENT["policies"]["git_write"] = {"enabled": True, "human_approval_required": False}
DOCUMENT["policies"]["jira_write"] = {"enabled": True, "human_approval_required": False}
for repository in DOCUMENT["integrations"]["git"]["repositories"]:
    repository["access"] = "read_write"
PROFILE = type(PROFILE).model_validate(DOCUMENT)
NOW = datetime(2026, 10, 6, 18, tzinfo=UTC)
REQUESTER = Principal("requester", "enterprise", email="private@example.test")
APPROVER = Principal("approver", "enterprise")
OUTSIDER = Principal("outsider", "enterprise")


def git_request(branch="feature/test", principal=REQUESTER):
    return ToolPolicyRequest(
        principal, PROFILE.initiative.id, GitOperation.PUSH, GitTarget("travel-api", branch)
    )


def jira_request(principal=REQUESTER):
    return ToolPolicyRequest(
        principal,
        PROFILE.initiative.id,
        JiraOperation.ADD_COMMENT,
        JiraProjectTarget("TRAVEL"),
    )


def setup(
    repository=None,
    *,
    effect=ToolPolicyEffect.REQUIRE_APPROVAL,
    approver_enabled=True,
    approver_roles=(Role.INITIATIVE_ADMIN,),
    human_ids=frozenset({"approver", "requester"}),
):
    repository = repository or InMemoryApprovalRepository()
    members = InMemoryMembershipRepository(
        (
            InitiativeMembership("requester", PROFILE.initiative.id, (Role.DEVELOPER,)),
            InitiativeMembership(
                "approver", PROFILE.initiative.id, approver_roles, approver_enabled
            ),
        )
    )
    grants = RolePolicy(
        (
            RoleGrant(
                Role.DEVELOPER,
                tool_permissions=frozenset({ToolPermission.GIT_WRITE, ToolPermission.JIRA_WRITE}),
            ),
            RoleGrant(
                Role.INITIATIVE_ADMIN,
                admin_permissions=frozenset({AdminPermission.INITIATIVE_APPROVAL_MANAGE}),
            ),
        )
    )
    authorization = AuthorizationService(members, grants, InMemoryAuthorizationAuditSink())
    tool_policy = ToolPolicyService(
        authorization,
        InMemoryToolPolicyRepository(
            (
                ToolPolicy(PROFILE.initiative.id, GitOperation.PUSH, effect),
                ToolPolicy(PROFILE.initiative.id, JiraOperation.ADD_COMMENT, effect),
            )
        ),
        InMemoryToolPolicyAuditSink(),
    )
    service = ApprovalService(
        tool_policy,
        authorization,
        repository,
        InMemoryHumanIdentityVerifier(human_ids),
        clock=lambda: NOW,
    )
    return service, repository


def pending(service, request=None, *, key="request-1", revision=7):
    return service.request_approval(
        request or git_request(),
        profile=PROFILE,
        request_key=key,
        initiative_revision=revision,
    )


@pytest.mark.parametrize("effect", [ToolPolicyEffect.ALLOW, ToolPolicyEffect.DENY])
def test_only_trusted_approval_required_decision_creates_record(effect):
    service, repository = setup(effect=effect)
    with pytest.raises(InvalidApprovalSourceError):
        pending(service)
    assert repository.list_pending(PROFILE.initiative.id) == ()


def test_request_is_exact_idempotent_and_audited():
    service, repository = setup()
    record = pending(service)
    assert record.status is ApprovalStatus.PENDING
    assert not record.approval_gate_satisfied
    assert record.principal_id == REQUESTER.subject_id
    assert record.initiative_id == PROFILE.initiative.id
    assert record.operation is GitOperation.PUSH
    assert record.target == GitTarget("travel-api", "feature/test")
    assert record.base_decision_id and record.tool_policy_decision_id
    assert record.initiative_revision == 7
    assert pending(service) == record
    assert len(repository.history(record.approval_id)) == 1
    assert repository.audit_events(record.approval_id)[0].to_status is ApprovalStatus.PENDING
    with pytest.raises(ApprovalConflictError):
        pending(service, git_request("main"))


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_authorized_human_transition_preserves_history_and_audit(decision):
    service, repository = setup()
    before = pending(service)
    after = getattr(service, decision)(
        before.approval_id, APPROVER, profile=PROFILE, expected_version=1
    )
    expected = ApprovalStatus.APPROVED if decision == "approve" else ApprovalStatus.REJECTED
    assert after.status is expected
    assert after.version == 2
    assert after.decided_by == APPROVER.subject_id
    assert after.approval_gate_satisfied is (decision == "approve")
    assert repository.history(after.approval_id) == (before, after)
    events = repository.audit_events(after.approval_id)
    assert [event.to_status for event in events] == [ApprovalStatus.PENDING, expected]
    assert events[-1].requester_id == REQUESTER.subject_id
    assert events[-1].approver_id == APPROVER.subject_id
    assert events[-1].base_decision_id == before.base_decision_id
    assert events[-1].tool_policy_decision_id == before.tool_policy_decision_id
    assert "private@example.test" not in repr(events)
    assert repository.list_pending(PROFILE.initiative.id) == ()
    assert repository.list_decided_by(APPROVER.subject_id) == (after,)
    assert (
        getattr(service, decision)(
            before.approval_id, APPROVER, profile=PROFILE, expected_version=1
        )
        == after
    )
    with pytest.raises(ApprovalInvalidTransitionError):
        getattr(service, "reject" if decision == "approve" else "approve")(
            before.approval_id, APPROVER, profile=PROFILE, expected_version=2
        )


def test_approver_requires_same_initiative_enabled_membership_explicit_grant_and_human():
    service, _ = setup(approver_enabled=False)
    record = pending(service)
    with pytest.raises(ApprovalNotAuthorizedError):
        service.approve(record.approval_id, APPROVER, profile=PROFILE, expected_version=1)
    with pytest.raises(ApprovalNotAuthorizedError):
        service.reject(record.approval_id, OUTSIDER, profile=PROFILE, expected_version=1)
    with pytest.raises(SelfApprovalNotAllowedError):
        service.approve(record.approval_id, REQUESTER, profile=PROFILE, expected_version=1)
    assert not service._repository.get(record.approval_id).approval_gate_satisfied


@pytest.mark.parametrize(
    "kwargs",
    [
        {"approver_roles": (Role.PLATFORM_ADMIN,)},
        {"human_ids": frozenset()},
    ],
)
def test_platform_admin_and_agent_identity_cannot_approve_without_explicit_authority(kwargs):
    service, repository = setup(**kwargs)
    record = pending(service)
    with pytest.raises(ApprovalNotAuthorizedError):
        service.approve(record.approval_id, APPROVER, profile=PROFILE, expected_version=1)
    assert repository.get(record.approval_id) == record


def test_version_conflict_and_exact_gate_binding():
    service, repository = setup()
    record = pending(service)
    with pytest.raises(ApprovalConflictError):
        service.approve(record.approval_id, APPROVER, profile=PROFILE, expected_version=99)
    assert not service.approval_gate_satisfied(
        record.approval_id, git_request(), profile=PROFILE, initiative_revision=7
    )
    service.approve(record.approval_id, APPROVER, profile=PROFILE, expected_version=1)
    assert service.approval_gate_satisfied(
        record.approval_id, git_request(), profile=PROFILE, initiative_revision=7
    )
    for request, revision in (
        (git_request("main"), 7),
        (jira_request(), 7),
        (git_request(), 8),
    ):
        assert not service.approval_gate_satisfied(
            record.approval_id, request, profile=PROFILE, initiative_revision=revision
        )
    assert repository.get(record.approval_id).status is ApprovalStatus.APPROVED


def test_records_are_immutable_and_queries_are_session_independent():
    repository = InMemoryApprovalRepository()
    first, _ = setup(repository)
    record = pending(first)
    second, _ = setup(repository)
    assert second._repository.get(record.approval_id) == record
    assert repository.list_pending(PROFILE.initiative.id) == (record,)
    assert repository.list_requested_by(REQUESTER.subject_id) == (record,)
    with pytest.raises(FrozenInstanceError):
        record.status = ApprovalStatus.APPROVED


def test_sqlite_survives_new_connection_and_keeps_atomic_history(tmp_path):
    path = tmp_path / "approvals.db"
    first, _ = setup(SQLiteApprovalRepository(path))
    record = pending(first)
    second, repository = setup(SQLiteApprovalRepository(path))
    assert repository.get(record.approval_id) == record
    assert repository.list_pending(PROFILE.initiative.id) == (record,)
    assert pending(second) == record
    after = second.reject(record.approval_id, APPROVER, profile=PROFILE, expected_version=1)
    third = SQLiteApprovalRepository(path)
    assert third.history(record.approval_id) == (record, after)
    assert len(third.audit_events(record.approval_id)) == 2
    assert third.list_requested_by(REQUESTER.subject_id) == (after,)


def test_repository_atomic_failure_never_returns_success():
    class FailingRepository(InMemoryApprovalRepository):
        def commit(self, record, event, *, expected_version):
            raise ApprovalPersistenceError

    service, repository = setup(FailingRepository())
    with pytest.raises(ApprovalPersistenceError):
        pending(service)
    assert repository.list_pending(PROFILE.initiative.id) == ()


def test_audit_failure_is_distinct_and_blocks_transition():
    class FailingAuditStore(InMemoryApprovalRepository):
        def commit(self, record, event, *, expected_version):
            if expected_version is not None:
                raise ApprovalAuditError
            return super().commit(record, event, expected_version=expected_version)

    service, repository = setup(FailingAuditStore())
    record = pending(service)
    with pytest.raises(ApprovalAuditError):
        service.approve(record.approval_id, APPROVER, profile=PROFILE, expected_version=1)
    assert repository.history(record.approval_id) == (record,)


def test_no_physical_fields_or_credentials_in_approval_record():
    service, _ = setup()
    record = pending(service)
    rendered = repr(record).lower()
    assert all(word not in rendered for word in ("credential", "endpoint", "bucket", "token"))


def test_sqlite_audit_failure_rolls_back_state(tmp_path):
    path = tmp_path / "audit-failure.db"
    service, repository = setup(SQLiteApprovalRepository(path))
    record = pending(service)
    with sqlite3.connect(path) as db:
        db.execute(
            """CREATE TRIGGER block_audit BEFORE INSERT ON approval_audit
            BEGIN SELECT RAISE(FAIL, 'audit unavailable'); END"""
        )
    with pytest.raises(ApprovalAuditError):
        service.approve(record.approval_id, APPROVER, profile=PROFILE, expected_version=1)
    assert repository.get(record.approval_id) == record
    assert repository.history(record.approval_id) == (record,)
    assert len(repository.audit_events(record.approval_id)) == 1


def test_approval_gate_rechecks_revoked_base_permission():
    repository = InMemoryApprovalRepository()
    service, _ = setup(repository)
    record = pending(service)
    service.approve(record.approval_id, APPROVER, profile=PROFILE, expected_version=1)
    revoked_base = AuthorizationService(
        InMemoryMembershipRepository(
            (InitiativeMembership("requester", PROFILE.initiative.id, (Role.VIEWER,)),)
        ),
        RolePolicy(),
        InMemoryAuthorizationAuditSink(),
    )
    revoked_tool = ToolPolicyService(
        revoked_base,
        InMemoryToolPolicyRepository(
            (
                ToolPolicy(
                    PROFILE.initiative.id, GitOperation.PUSH, ToolPolicyEffect.REQUIRE_APPROVAL
                ),
            )
        ),
        InMemoryToolPolicyAuditSink(),
    )
    resumed = ApprovalService(
        revoked_tool,
        revoked_base,
        repository,
        InMemoryHumanIdentityVerifier(),
    )
    assert not resumed.approval_gate_satisfied(
        record.approval_id, git_request(), profile=PROFILE, initiative_revision=7
    )
