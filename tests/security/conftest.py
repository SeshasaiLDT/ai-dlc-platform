"""Synthetic, composed AIDLC-25 through AIDLC-29 security test environment."""

from dataclasses import dataclass
from pathlib import Path

import pytest

from ai_dlc.adapters.approval import InMemoryApprovalRepository, InMemoryHumanIdentityVerifier
from ai_dlc.adapters.authorization import (
    InMemoryAuthorizationAuditSink,
    InMemoryMembershipRepository,
    InMemoryPlatformAdminRepository,
)
from ai_dlc.adapters.tool_policy import InMemoryToolPolicyAuditSink, InMemoryToolPolicyRepository
from ai_dlc.application.approval import ApprovalService
from ai_dlc.application.authorization import AuthorizationService, RoleGrant, RolePolicy
from ai_dlc.application.tool_policy import (
    GitOperation,
    JiraOperation,
    ServiceNowOperation,
    ToolPolicy,
    ToolPolicyEffect,
    ToolPolicyService,
)
from ai_dlc.domain.identity import (
    AdminPermission,
    Capability,
    InitiativeMembership,
    Principal,
    Role,
    ToolPermission,
)
from ai_dlc.domain.initiative import InitiativeProfile, load_initiative_profile

EXAMPLES = Path(__file__).resolve().parents[2] / "configs" / "initiatives" / "examples"


def _profile(name: str) -> InitiativeProfile:
    original = load_initiative_profile(EXAMPLES / name)
    document = original.model_dump(mode="json")
    for policy in ("jira_write", "git_write", "servicenow_write"):
        document["policies"][policy] = {"enabled": True, "human_approval_required": False}
    for repository in document["integrations"]["git"]["repositories"]:
        repository["access"] = "read_write"
    return InitiativeProfile.model_validate(document)


A = _profile("travel-platform.yaml")
B = _profile("field-operations.yaml")
PEOPLE = {
    name: Principal(name, "test-enterprise", display_name=name, email=f"{name}@example.test")
    for name in (
        "viewer-a",
        "developer-a",
        "approver-a",
        "developer-b",
        "multi",
        "platform-admin",
        "initiative-admin",
        "unassigned",
        "service-agent",
        "self-approver",
        "disabled",
    )
}


def membership(name: str, profile: InitiativeProfile, *roles: Role, enabled=True):
    return InitiativeMembership(name, profile.initiative.id, roles, enabled)


MEMBERSHIPS = (
    membership("viewer-a", A, Role.VIEWER),
    membership("developer-a", A, Role.DEVELOPER),
    membership("approver-a", A, Role.REVIEWER),
    membership("developer-b", B, Role.DEVELOPER),
    membership("multi", A, Role.DEVELOPER),
    membership("multi", B, Role.VIEWER),
    membership("initiative-admin", A, Role.INITIATIVE_ADMIN),
    membership("service-agent", A, Role.REVIEWER),
    membership("self-approver", A, Role.DEVELOPER, Role.REVIEWER),
    membership("disabled", A, Role.DEVELOPER, enabled=False),
)
GRANTS = RolePolicy(
    (
        RoleGrant(
            Role.VIEWER,
            capabilities=frozenset({Capability.INVESTIGATION}),
            tool_permissions=frozenset(
                {
                    ToolPermission.JIRA_READ,
                    ToolPermission.GIT_READ,
                    ToolPermission.SERVICENOW_READ,
                    ToolPermission.KNOWLEDGE_READ,
                }
            ),
        ),
        RoleGrant(
            Role.DEVELOPER,
            capabilities=frozenset({Capability.IMPLEMENTATION}),
            tool_permissions=frozenset(
                {
                    ToolPermission.JIRA_READ,
                    ToolPermission.JIRA_WRITE,
                    ToolPermission.GIT_READ,
                    ToolPermission.GIT_WRITE,
                    ToolPermission.SERVICENOW_READ,
                    ToolPermission.SERVICENOW_WRITE,
                    ToolPermission.KNOWLEDGE_READ,
                }
            ),
        ),
        RoleGrant(
            Role.REVIEWER,
            admin_permissions=frozenset({AdminPermission.INITIATIVE_APPROVAL_MANAGE}),
        ),
        RoleGrant(
            Role.INITIATIVE_ADMIN,
            admin_permissions=frozenset({AdminPermission.INITIATIVE_MEMBERSHIP_MANAGE}),
        ),
        RoleGrant(
            Role.PLATFORM_ADMIN,
            admin_permissions=frozenset({AdminPermission.PLATFORM_MANAGE}),
        ),
    )
)
POLICIES = (
    ToolPolicy(A.initiative.id, GitOperation.READ_REPOSITORY, ToolPolicyEffect.ALLOW),
    ToolPolicy(A.initiative.id, GitOperation.PUSH, ToolPolicyEffect.REQUIRE_APPROVAL),
    ToolPolicy(A.initiative.id, JiraOperation.READ_ISSUE, ToolPolicyEffect.ALLOW),
    ToolPolicy(A.initiative.id, JiraOperation.ADD_COMMENT, ToolPolicyEffect.REQUIRE_APPROVAL),
    ToolPolicy(B.initiative.id, GitOperation.READ_REPOSITORY, ToolPolicyEffect.ALLOW),
    ToolPolicy(B.initiative.id, ServiceNowOperation.READ_RECORD, ToolPolicyEffect.ALLOW),
    ToolPolicy(
        B.initiative.id, ServiceNowOperation.UPDATE_RECORD, ToolPolicyEffect.REQUIRE_APPROVAL
    ),
    ToolPolicy(
        B.initiative.id, ServiceNowOperation.DELETE_RECORD, ToolPolicyEffect.REQUIRE_APPROVAL
    ),
)


@dataclass
class World:
    base: AuthorizationService
    tool: ToolPolicyService
    approval: ApprovalService
    approvals: InMemoryApprovalRepository
    base_audit: InMemoryAuthorizationAuditSink
    tool_audit: InMemoryToolPolicyAuditSink
    profiles: dict[str, InitiativeProfile]


@pytest.fixture
def world_factory():
    def build(
        *,
        memberships=MEMBERSHIPS,
        grants=GRANTS,
        policies=POLICIES,
        approvals=None,
        base_audit=None,
        tool_audit=None,
        human_ids=frozenset({"approver-a", "self-approver", "initiative-admin"}),
    ):
        approval_store = approvals if approvals is not None else InMemoryApprovalRepository()
        authorization_audit = base_audit or InMemoryAuthorizationAuditSink()
        policy_audit = tool_audit or InMemoryToolPolicyAuditSink()
        base = AuthorizationService(
            InMemoryMembershipRepository(memberships),
            grants,
            authorization_audit,
            platform_admins=InMemoryPlatformAdminRepository(frozenset({"platform-admin"})),
        )
        tool = ToolPolicyService(base, InMemoryToolPolicyRepository(policies), policy_audit)
        approval = ApprovalService(
            tool,
            base,
            approval_store,
            InMemoryHumanIdentityVerifier(human_ids),
        )
        return World(
            base,
            tool,
            approval,
            approval_store,
            authorization_audit,
            policy_audit,
            {A.initiative.id: A, B.initiative.id: B},
        )

    return build


@pytest.fixture
def world(world_factory):
    return world_factory()
