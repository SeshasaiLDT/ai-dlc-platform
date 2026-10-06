"""Behavioral contracts for provider-neutral identity and initiative authorization."""

from dataclasses import FrozenInstanceError, fields
from pathlib import Path

import pytest

from ai_dlc.adapters.authorization import InMemoryMembershipRepository
from ai_dlc.application.authorization import (
    LogicalScopes,
    ResolvedAuthorizationContext,
    RoleGrant,
    RolePolicy,
    ScopeRestriction,
    resolve_authorization_context,
)
from ai_dlc.domain.identity import (
    AdminPermission,
    Capability,
    InitiativeMembership,
    InvalidRoleError,
    MembershipDisabledError,
    MembershipNotFoundError,
    Principal,
    Role,
    ToolPermission,
)
from ai_dlc.domain.initiative import load_initiative_profile

EXAMPLES = Path(__file__).resolve().parents[1] / "configs" / "initiatives" / "examples"


@pytest.fixture
def principal() -> Principal:
    return Principal("subject-123", provider="enterprise", email="old@example.test")


@pytest.fixture
def policy() -> RolePolicy:
    return RolePolicy(
        (
            RoleGrant(
                Role.ANALYST,
                capabilities=frozenset({Capability.INVESTIGATION, Capability.CHANGE_IMPACT}),
                tool_permissions=frozenset({ToolPermission.JIRA_READ, ToolPermission.GIT_READ}),
            ),
            RoleGrant(
                Role.INITIATIVE_ADMIN,
                admin_permissions=frozenset(
                    {
                        AdminPermission.INITIATIVE_MEMBERSHIP_MANAGE,
                        AdminPermission.INITIATIVE_POLICY_MANAGE,
                    }
                ),
            ),
            RoleGrant(
                Role.PLATFORM_ADMIN,
                admin_permissions=frozenset({AdminPermission.PLATFORM_MANAGE}),
            ),
        )
    )


def resolve(
    principal: Principal,
    policy: RolePolicy,
    roles: tuple[Role, ...] = (Role.ANALYST,),
    restriction: ScopeRestriction | None = None,
) -> ResolvedAuthorizationContext:
    profile = load_initiative_profile(EXAMPLES / "travel-platform.yaml")
    repository = InMemoryMembershipRepository(
        (InitiativeMembership(principal.subject_id, profile.initiative.id, roles),)
    )
    return resolve_authorization_context(
        principal, profile.initiative.id, profile, repository, policy, scope_restriction=restriction
    )


def test_principal_identity_is_stable_and_immutable(principal: Principal) -> None:
    changed_contact = Principal(
        principal.subject_id, provider=principal.provider, display_name="New name", email="new@test"
    )
    assert changed_contact.subject_id == principal.subject_id
    with pytest.raises(FrozenInstanceError):
        principal.subject_id = "other"
    with pytest.raises(ValueError, match="subject_id"):
        Principal(" ", provider="enterprise")


def test_memberships_are_specific_to_principal_and_initiative(principal: Principal) -> None:
    first = InitiativeMembership(principal.subject_id, "initiative-a", (Role.VIEWER,))
    second = InitiativeMembership(principal.subject_id, "initiative-b", (Role.DEVELOPER,))
    disabled = InitiativeMembership(principal.subject_id, "initiative-c", enabled=False)
    repository = InMemoryMembershipRepository((second, disabled, first))
    assert repository.list_memberships(principal.subject_id) == (first, second, disabled)
    assert repository.get_membership(principal.subject_id, "initiative-b") == second
    with pytest.raises(MembershipNotFoundError):
        repository.get_membership("another-subject", "initiative-a")
    with pytest.raises(MembershipDisabledError):
        repository.get_membership(principal.subject_id, "initiative-c")
    with pytest.raises(ValueError, match="duplicate"):
        InMemoryMembershipRepository((first, first))
    with pytest.raises(FrozenInstanceError):
        first.enabled = False


def test_role_identifiers_are_generic_and_unknown_roles_fail() -> None:
    assert {role.value for role in Role} == {
        "viewer",
        "analyst",
        "developer",
        "reviewer",
        "initiative_admin",
        "platform_admin",
    }
    membership = InitiativeMembership("subject", "initiative", (Role.ANALYST, Role.ANALYST))
    assert membership.roles == (Role.ANALYST,)
    with pytest.raises(InvalidRoleError):
        InitiativeMembership("subject", "initiative", ("project-developer",))


def test_grants_are_explicit_and_independent(principal: Principal, policy: RolePolicy) -> None:
    context = resolve(principal, policy)
    assert context.can_use(Capability.INVESTIGATION)
    assert not context.can_use(Capability.IMPLEMENTATION)
    assert context.can_use_tool(ToolPermission.JIRA_READ)
    assert not context.can_use_tool(ToolPermission.JIRA_WRITE)
    assert context.can_use_tool(ToolPermission.GIT_READ)
    assert not context.can_use_tool(ToolPermission.GIT_WRITE)
    assert not context.can_use_tool(ToolPermission.KNOWLEDGE_READ)
    assert not context.can_administer(AdminPermission.PLATFORM_MANAGE)
    with pytest.raises(ValueError, match="unknown Capability"):
        context.can_use("investigation")
    with pytest.raises(ValueError, match="unknown ToolPermission"):
        context.can_use_tool("jira.read")


def test_admin_grants_are_separate_from_execution(principal: Principal, policy: RolePolicy) -> None:
    initiative_admin = resolve(principal, policy, (Role.INITIATIVE_ADMIN,))
    assert initiative_admin.can_administer(AdminPermission.INITIATIVE_MEMBERSHIP_MANAGE)
    assert not initiative_admin.can_administer(AdminPermission.PLATFORM_MANAGE)
    assert initiative_admin.capabilities == frozenset()
    assert initiative_admin.tool_permissions == frozenset()
    platform_admin = resolve(principal, policy, (Role.PLATFORM_ADMIN,))
    assert platform_admin.can_administer(AdminPermission.PLATFORM_MANAGE)
    assert not platform_admin.can_administer(AdminPermission.INITIATIVE_POLICY_MANAGE)
    assert platform_admin.capabilities == frozenset()


def test_absent_policy_grants_default_to_deny(principal: Principal) -> None:
    context = resolve(principal, RolePolicy(), (Role.DEVELOPER,))
    assert (
        context.capabilities == context.tool_permissions == context.admin_permissions == frozenset()
    )
    with pytest.raises(ValueError, match="duplicate role"):
        RolePolicy((RoleGrant(Role.VIEWER), RoleGrant(Role.VIEWER)))
    with pytest.raises(ValueError, match="unknown ToolPermission"):
        RoleGrant(Role.VIEWER, tool_permissions=frozenset({"jira.read"}))


def test_context_is_single_initiative_consistent_and_immutable(
    principal: Principal, policy: RolePolicy
) -> None:
    context = resolve(principal, policy)
    assert context.initiative_id == context.membership.initiative_id == "travel-platform"
    with pytest.raises(FrozenInstanceError):
        context.initiative_id = "another"
    with pytest.raises(ValueError, match="principal"):
        ResolvedAuthorizationContext(
            principal, "travel-platform", InitiativeMembership("other", "travel-platform")
        )
    with pytest.raises(ValueError, match="initiative"):
        ResolvedAuthorizationContext(
            principal, "travel-platform", InitiativeMembership(principal.subject_id, "other")
        )
    with pytest.raises(ValueError, match="disabled"):
        ResolvedAuthorizationContext(
            principal,
            "travel-platform",
            InitiativeMembership(principal.subject_id, "travel-platform", enabled=False),
        )


def test_resolution_requires_enabled_membership_and_matching_profile(
    principal: Principal, policy: RolePolicy
) -> None:
    profile = load_initiative_profile(EXAMPLES / "travel-platform.yaml")
    missing = InMemoryMembershipRepository()
    with pytest.raises(MembershipNotFoundError):
        resolve_authorization_context(principal, profile.initiative.id, profile, missing, policy)
    disabled = InMemoryMembershipRepository(
        (InitiativeMembership(principal.subject_id, profile.initiative.id, enabled=False),)
    )
    with pytest.raises(MembershipDisabledError):
        resolve_authorization_context(principal, profile.initiative.id, profile, disabled, policy)
    with pytest.raises(ValueError, match="Profile"):
        resolve_authorization_context(principal, "other", profile, missing, policy)


def test_logical_scope_restrictions_only_narrow_profile(
    principal: Principal, policy: RolePolicy
) -> None:
    context = resolve(
        principal,
        policy,
        restriction=ScopeRestriction(
            jira_projects=frozenset({"TRAVEL", "UNCONFIGURED"}),
            repository_ids=frozenset(),
            knowledge_source_ids=frozenset({"missing"}),
        ),
    )
    assert context.configured_scopes.jira_projects == frozenset({"TRAVEL", "JOURNEY"})
    assert context.allowed_scopes.jira_projects == frozenset({"TRAVEL"})
    assert context.allowed_scopes.repository_ids == frozenset()
    assert context.allowed_scopes.knowledge_source_ids == frozenset()
    with pytest.raises(ValueError, match="exceed"):
        ResolvedAuthorizationContext(
            principal,
            context.initiative_id,
            context.membership,
            configured_scopes=LogicalScopes(jira_projects=frozenset({"TRAVEL"})),
            allowed_scopes=LogicalScopes(jira_projects=frozenset({"UNCONFIGURED"})),
        )


def test_role_policy_can_combine_grants_without_implicit_writes(principal: Principal) -> None:
    policy = RolePolicy(
        (
            RoleGrant(Role.ANALYST, capabilities=frozenset({Capability.CODE_ANALYSIS})),
            RoleGrant(Role.REVIEWER, tool_permissions=frozenset({ToolPermission.ARTIFACT_READ})),
        )
    )
    context = resolve(principal, policy, (Role.ANALYST, Role.REVIEWER))
    assert context.capabilities == frozenset({Capability.CODE_ANALYSIS})
    assert context.tool_permissions == frozenset({ToolPermission.ARTIFACT_READ})
    assert ToolPermission.ARTIFACT_WRITE not in context.tool_permissions


def test_disabled_profile_integrations_supply_no_logical_scope(
    principal: Principal, policy: RolePolicy
) -> None:
    profile = load_initiative_profile(EXAMPLES / "field-operations.yaml")
    repository = InMemoryMembershipRepository(
        (InitiativeMembership(principal.subject_id, profile.initiative.id, (Role.ANALYST,)),)
    )
    context = resolve_authorization_context(
        principal, profile.initiative.id, profile, repository, policy
    )
    assert context.configured_scopes.jira_projects == frozenset()
    assert context.allowed_scopes.jira_projects == frozenset()
    assert context.configured_scopes.repository_ids
    assert context.configured_scopes.knowledge_source_ids


def test_rbac_models_carry_only_logical_resource_identifiers() -> None:
    model_fields = {
        item.name
        for model in (Principal, InitiativeMembership, LogicalScopes, ResolvedAuthorizationContext)
        for item in fields(model)
    }
    assert not model_fields & {
        "pgvector_table",
        "database_endpoint",
        "rds_endpoint",
        "s3_bucket",
        "jira_credentials",
        "git_credentials",
    }
