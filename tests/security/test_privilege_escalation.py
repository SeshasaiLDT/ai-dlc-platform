"""Read/write, administration, target, and forged-grant regression cases."""

import pytest
from conftest import GRANTS, MEMBERSHIPS, PEOPLE, POLICIES, A, B, membership

from ai_dlc.application.authorization import (
    AdminAction,
    AuthorizationReason,
    AuthorizationRequest,
    CapabilityAction,
    JiraProjectTarget,
    KnowledgeSourceTarget,
    PlatformAuthorizationRequest,
    RepositoryTarget,
    RolePolicy,
    ToolAction,
)
from ai_dlc.application.tool_policy import (
    GitOperation,
    GitTarget,
    JiraOperation,
    ServiceNowOperation,
    ServiceNowTarget,
    ToolPolicy,
    ToolPolicyEffect,
    ToolPolicyReason,
    ToolPolicyRequest,
)
from ai_dlc.domain.identity import AdminPermission, Capability, Role, ToolPermission


@pytest.mark.parametrize(
    "operation",
    [
        JiraOperation.ADD_COMMENT,
        JiraOperation.UPDATE_ISSUE,
        JiraOperation.TRANSITION_ISSUE,
        JiraOperation.CREATE_ISSUE,
        JiraOperation.DELETE_ISSUE,
    ],
)
def test_jira_read_cannot_escalate_to_any_write(world_factory, operation):
    world = world_factory(
        policies=(*POLICIES, ToolPolicy(A.initiative.id, operation, ToolPolicyEffect.ALLOW))
    )
    decision = world.tool.evaluate(
        ToolPolicyRequest(
            PEOPLE["viewer-a"], A.initiative.id, operation, JiraProjectTarget("TRAVEL")
        ),
        profile=A,
    )
    assert decision.reason is ToolPolicyReason.BASE_AUTHORIZATION_REQUIRED
    assert world.base_audit.events[-1].reason is AuthorizationReason.TOOL_PERMISSION_NOT_GRANTED


@pytest.mark.parametrize(
    "operation",
    [
        GitOperation.CREATE_BRANCH,
        GitOperation.COMMIT,
        GitOperation.PUSH,
        GitOperation.CREATE_PR,
        GitOperation.UPDATE_PR,
        GitOperation.DELETE_BRANCH,
    ],
)
def test_git_read_cannot_escalate_to_any_write(world_factory, operation):
    world = world_factory(
        policies=(*POLICIES, ToolPolicy(A.initiative.id, operation, ToolPolicyEffect.ALLOW))
    )
    decision = world.tool.evaluate(
        ToolPolicyRequest(
            PEOPLE["viewer-a"],
            A.initiative.id,
            operation,
            GitTarget("travel-api", "feature/x"),
        ),
        profile=A,
    )
    assert decision.reason is ToolPolicyReason.BASE_AUTHORIZATION_REQUIRED


@pytest.mark.parametrize(
    "operation",
    [
        ServiceNowOperation.ADD_COMMENT,
        ServiceNowOperation.UPDATE_RECORD,
        ServiceNowOperation.CREATE_RECORD,
        ServiceNowOperation.DELETE_RECORD,
    ],
)
def test_servicenow_read_cannot_escalate_to_any_write(world_factory, operation):
    world = world_factory(
        policies=(*POLICIES, ToolPolicy(B.initiative.id, operation, ToolPolicyEffect.ALLOW))
    )
    decision = world.tool.evaluate(
        ToolPolicyRequest(
            PEOPLE["multi"], B.initiative.id, operation, ServiceNowTarget("incident")
        ),
        profile=B,
    )
    assert decision.reason is ToolPolicyReason.BASE_AUTHORIZATION_REQUIRED


@pytest.mark.parametrize(
    ("operation", "target", "profile", "principal"),
    [
        (GitOperation.DELETE_BRANCH, GitTarget("travel-api", "feature/x"), A, "developer-a"),
        (JiraOperation.DELETE_ISSUE, JiraProjectTarget("TRAVEL"), A, "developer-a"),
    ],
)
def test_generic_write_does_not_grant_unconfigured_destructive_operation(
    world, operation, target, profile, principal
):
    decision = world.tool.evaluate(
        ToolPolicyRequest(PEOPLE[principal], profile.initiative.id, operation, target),
        profile=profile,
    )
    assert decision.effect is ToolPolicyEffect.DENY
    assert decision.reason is ToolPolicyReason.NO_POLICY
    assert world.base_audit.events[-1].allowed


def test_destructive_approval_required_does_not_permit_immediate_execution(world):
    request = ToolPolicyRequest(
        PEOPLE["developer-b"],
        B.initiative.id,
        ServiceNowOperation.DELETE_RECORD,
        ServiceNowTarget("incident"),
    )
    assert world.tool.evaluate(request, profile=B).effect is ToolPolicyEffect.REQUIRE_APPROVAL
    from ai_dlc.application.tool_policy import ToolApprovalRequiredError

    with pytest.raises(ToolApprovalRequiredError):
        world.tool.require_allowed(request, profile=B)


@pytest.mark.parametrize(
    ("permission", "target", "expected"),
    [
        (
            ToolPermission.GIT_WRITE,
            RepositoryTarget("travel-api"),
            AuthorizationReason.TOOL_PERMISSION_NOT_GRANTED,
        ),
        (
            ToolPermission.JIRA_WRITE,
            JiraProjectTarget("TRAVEL"),
            AuthorizationReason.TOOL_PERMISSION_NOT_GRANTED,
        ),
        (ToolPermission.ARTIFACT_WRITE, None, AuthorizationReason.TOOL_PERMISSION_NOT_GRANTED),
    ],
)
def test_tool_grants_are_not_interchangeable(world, permission, target, expected):
    decision = world.base.evaluate(
        AuthorizationRequest(PEOPLE["viewer-a"], A.initiative.id, ToolAction(permission), target),
        profile=A,
    )
    assert decision.reason is expected


@pytest.mark.parametrize(
    ("permission", "target"),
    [
        (ToolPermission.GIT_READ, RepositoryTarget("travel-web")),
        (ToolPermission.JIRA_READ, JiraProjectTarget("JOURNEY")),
        (ToolPermission.KNOWLEDGE_READ, KnowledgeSourceTarget("codebase")),
    ],
)
def test_valid_permission_cannot_replay_another_logical_target(world, permission, target):
    restriction = {
        ToolPermission.GIT_READ: {"repository_ids": frozenset({"travel-api"})},
        ToolPermission.JIRA_READ: {"jira_projects": frozenset({"TRAVEL"})},
        ToolPermission.KNOWLEDGE_READ: {"knowledge_source_ids": frozenset({"architecture"})},
    }[permission]
    from ai_dlc.application.authorization import ScopeRestriction

    decision = world.base.evaluate(
        AuthorizationRequest(PEOPLE["viewer-a"], A.initiative.id, ToolAction(permission), target),
        profile=A,
        scope_restriction=ScopeRestriction(**restriction),
    )
    assert decision.reason is AuthorizationReason.TARGET_OUT_OF_SCOPE


def test_missing_role_grants_and_policy_deny_by_default(world_factory):
    world = world_factory(grants=RolePolicy(), policies=())
    principal = PEOPLE["developer-a"]
    capability = world.base.evaluate(
        AuthorizationRequest(
            principal, A.initiative.id, CapabilityAction(Capability.IMPLEMENTATION)
        ),
        profile=A,
    )
    assert capability.reason is AuthorizationReason.CAPABILITY_NOT_GRANTED
    tool = world.tool.evaluate(
        ToolPolicyRequest(
            principal,
            A.initiative.id,
            GitOperation.PUSH,
            GitTarget("travel-api", "feature/x"),
        ),
        profile=A,
    )
    assert tool.reason is ToolPolicyReason.BASE_AUTHORIZATION_REQUIRED
    with_grant = world_factory(policies=())
    assert (
        with_grant.tool.evaluate(
            ToolPolicyRequest(
                principal, A.initiative.id, GitOperation.PUSH, GitTarget("travel-api", "feature/x")
            ),
            profile=A,
        ).reason
        is ToolPolicyReason.NO_POLICY
    )


def test_disabled_membership_denies_capability_and_tool(world):
    principal = PEOPLE["disabled"]
    decision = world.base.evaluate(
        AuthorizationRequest(
            principal, A.initiative.id, CapabilityAction(Capability.IMPLEMENTATION)
        ),
        profile=A,
    )
    assert decision.reason is AuthorizationReason.MEMBERSHIP_DISABLED
    tool = world.tool.evaluate(
        ToolPolicyRequest(
            principal, A.initiative.id, GitOperation.PUSH, GitTarget("travel-api", "feature/x")
        ),
        profile=A,
    )
    assert tool.reason is ToolPolicyReason.BASE_AUTHORIZATION_REQUIRED


def test_platform_and_initiative_admin_boundaries(world):
    platform = PEOPLE["platform-admin"]
    assert world.base.evaluate(PlatformAuthorizationRequest(platform)).allowed
    assert (
        world.base.evaluate(
            AuthorizationRequest(
                platform,
                A.initiative.id,
                ToolAction(ToolPermission.GIT_WRITE),
                RepositoryTarget("travel-api"),
            ),
            profile=A,
        ).reason
        is AuthorizationReason.MEMBERSHIP_NOT_FOUND
    )
    initiative = PEOPLE["initiative-admin"]
    assert world.base.evaluate(
        AuthorizationRequest(
            initiative,
            A.initiative.id,
            AdminAction(AdminPermission.INITIATIVE_MEMBERSHIP_MANAGE),
        ),
        profile=A,
    ).allowed
    assert (
        world.base.evaluate(PlatformAuthorizationRequest(initiative)).reason
        is AuthorizationReason.ADMIN_PERMISSION_NOT_GRANTED
    )


def test_forged_request_grants_and_unknown_actions_are_rejected(world):
    with pytest.raises(ValueError):
        AuthorizationRequest(PEOPLE["viewer-a"], A.initiative.id, "git.write")
    with pytest.raises(ValueError):
        ToolPolicyRequest(
            PEOPLE["viewer-a"], A.initiative.id, "push", GitTarget("travel-api", "main")
        )
    with pytest.raises(TypeError):
        world.tool.evaluate({"permission": "git.write", "allowed": True}, profile=A)
    assert GRANTS.for_roles(()) == ()


def test_authorization_decision_cannot_be_substituted_across_tools_or_initiatives(world):
    jira = world.base.evaluate(
        AuthorizationRequest(
            PEOPLE["developer-a"],
            A.initiative.id,
            ToolAction(ToolPermission.JIRA_WRITE),
            JiraProjectTarget("TRAVEL"),
        ),
        profile=A,
    )
    git = world.base.evaluate(
        AuthorizationRequest(
            PEOPLE["developer-a"],
            A.initiative.id,
            ToolAction(ToolPermission.GIT_WRITE),
            RepositoryTarget("travel-api"),
        ),
        profile=A,
    )
    assert jira.allowed and git.allowed
    for decision in (jira, git):
        with pytest.raises(TypeError):
            world.tool.evaluate(decision, profile=B)
    with pytest.raises(TypeError):
        world.tool.evaluate(jira, profile=A)


def test_platform_admin_role_inside_membership_does_not_create_global_assignment(world_factory):
    world = world_factory(
        memberships=(*MEMBERSHIPS, membership("unassigned", A, Role.PLATFORM_ADMIN))
    )
    principal = PEOPLE["unassigned"]
    assert (
        world.base.evaluate(PlatformAuthorizationRequest(principal)).reason
        is AuthorizationReason.ADMIN_PERMISSION_NOT_GRANTED
    )
    assert (
        world.base.evaluate(
            AuthorizationRequest(
                principal,
                A.initiative.id,
                ToolAction(ToolPermission.GIT_WRITE),
                RepositoryTarget("travel-api"),
            ),
            profile=A,
        ).reason
        is AuthorizationReason.TOOL_PERMISSION_NOT_GRANTED
    )
