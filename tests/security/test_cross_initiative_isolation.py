"""Cross-initiative and multi-membership privilege isolation."""

import pytest
from conftest import PEOPLE, A, B

from ai_dlc.application.authorization import (
    AuthorizationReason,
    AuthorizationRequest,
    CapabilityAction,
    JiraProjectTarget,
    KnowledgeSourceTarget,
    RepositoryTarget,
    ScopeRestriction,
    ToolAction,
)
from ai_dlc.application.tool_policy import (
    GitOperation,
    GitTarget,
    JiraOperation,
    ToolPolicyEffect,
    ToolPolicyReason,
    ToolPolicyRequest,
)
from ai_dlc.domain.identity import Capability, ToolPermission


@pytest.mark.parametrize(
    ("action", "target"),
    [
        (CapabilityAction(Capability.INVESTIGATION), None),
        (ToolAction(ToolPermission.JIRA_READ), JiraProjectTarget("FIELD")),
        (ToolAction(ToolPermission.GIT_READ), RepositoryTarget("operations-runbooks")),
        (ToolAction(ToolPermission.KNOWLEDGE_READ), KnowledgeSourceTarget("runbooks")),
    ],
)
def test_unassigned_initiative_denied_for_every_base_resource(world, action, target):
    request = AuthorizationRequest(PEOPLE["viewer-a"], B.initiative.id, action, target)
    decision = world.base.evaluate(request, profile=B)
    assert not decision.allowed
    assert decision.reason is AuthorizationReason.MEMBERSHIP_NOT_FOUND
    assert world.base_audit.events[-1].reason is decision.reason


def test_changing_only_initiative_id_never_expands_access(world):
    valid = AuthorizationRequest(
        PEOPLE["viewer-a"],
        A.initiative.id,
        ToolAction(ToolPermission.GIT_READ),
        RepositoryTarget("travel-api"),
    )
    assert world.base.evaluate(valid, profile=A).allowed
    switched = AuthorizationRequest(valid.principal, B.initiative.id, valid.action, valid.target)
    assert (
        world.base.evaluate(switched, profile=B).reason is AuthorizationReason.MEMBERSHIP_NOT_FOUND
    )
    assert (
        world.base.evaluate(switched, profile=A).reason is AuthorizationReason.INITIATIVE_MISMATCH
    )


def test_unassigned_cannot_use_tool_policy_or_foreign_policy(world):
    request = ToolPolicyRequest(
        PEOPLE["viewer-a"],
        B.initiative.id,
        GitOperation.READ_REPOSITORY,
        GitTarget("operations-runbooks"),
    )
    decision = world.tool.evaluate(request, profile=B)
    assert decision.effect is ToolPolicyEffect.DENY
    assert decision.reason is ToolPolicyReason.BASE_AUTHORIZATION_REQUIRED
    a_request = ToolPolicyRequest(
        PEOPLE["developer-a"],
        A.initiative.id,
        JiraOperation.READ_ISSUE,
        JiraProjectTarget("TRAVEL"),
    )
    assert world.tool.evaluate(a_request, profile=A).effect is ToolPolicyEffect.ALLOW
    assert world.tool.evaluate(a_request, profile=B).effect is ToolPolicyEffect.DENY


def test_multi_initiative_roles_and_scopes_do_not_leak(world):
    principal = PEOPLE["multi"]
    a_write = ToolPolicyRequest(
        principal, A.initiative.id, GitOperation.PUSH, GitTarget("travel-api", "feature/x")
    )
    b_write = ToolPolicyRequest(
        principal,
        B.initiative.id,
        GitOperation.PUSH,
        GitTarget("operations-runbooks", "feature/x"),
    )
    b_read = ToolPolicyRequest(
        principal,
        B.initiative.id,
        GitOperation.READ_REPOSITORY,
        GitTarget("operations-runbooks"),
    )
    assert world.tool.evaluate(a_write, profile=A).effect is ToolPolicyEffect.REQUIRE_APPROVAL
    assert (
        world.tool.evaluate(b_write, profile=B).reason
        is ToolPolicyReason.BASE_AUTHORIZATION_REQUIRED
    )
    assert world.tool.evaluate(b_read, profile=B).effect is ToolPolicyEffect.ALLOW
    wrong_scope = AuthorizationRequest(
        principal,
        B.initiative.id,
        ToolAction(ToolPermission.KNOWLEDGE_READ),
        KnowledgeSourceTarget("architecture"),
    )
    assert (
        world.base.evaluate(wrong_scope, profile=B).reason
        is AuthorizationReason.TARGET_OUT_OF_SCOPE
    )


@pytest.mark.parametrize(
    ("permission", "inside", "outside", "restriction"),
    [
        (
            ToolPermission.JIRA_READ,
            JiraProjectTarget("TRAVEL"),
            JiraProjectTarget("JOURNEY"),
            ScopeRestriction(jira_projects=frozenset({"TRAVEL", "OUTSIDE"})),
        ),
        (
            ToolPermission.GIT_READ,
            RepositoryTarget("travel-api"),
            RepositoryTarget("travel-web"),
            ScopeRestriction(repository_ids=frozenset({"travel-api", "outside"})),
        ),
        (
            ToolPermission.KNOWLEDGE_READ,
            KnowledgeSourceTarget("architecture"),
            KnowledgeSourceTarget("codebase"),
            ScopeRestriction(knowledge_source_ids=frozenset({"architecture", "outside"})),
        ),
    ],
)
def test_scope_narrowing_intersects_profile_and_never_expands(
    world, permission, inside, outside, restriction
):
    principal = PEOPLE["viewer-a"]

    def check(target):
        return world.base.evaluate(
            AuthorizationRequest(principal, A.initiative.id, ToolAction(permission), target),
            profile=A,
            scope_restriction=restriction,
        )

    assert check(inside).allowed
    assert check(outside).reason is AuthorizationReason.TARGET_OUT_OF_SCOPE
