"""Resolve grants and logical scope for one selected initiative."""

from ai_dlc.domain.identity import MembershipDisabledError, Principal
from ai_dlc.domain.initiative import InitiativeProfile

from .models import LogicalScopes, ResolvedAuthorizationContext, RolePolicy, ScopeRestriction
from .ports import MembershipRepository


def resolve_authorization_context(
    principal: Principal,
    initiative_id: str,
    profile: InitiativeProfile,
    memberships: MembershipRepository,
    role_policy: RolePolicy,
    *,
    scope_restriction: ScopeRestriction | None = None,
) -> ResolvedAuthorizationContext:
    """Build a snapshot; later runtime services enforce decisions at action boundaries."""
    if profile.initiative.id != initiative_id:
        raise ValueError("Initiative Profile does not match selected initiative")
    membership = memberships.get_membership(principal.subject_id, initiative_id)
    if not membership.enabled:
        raise MembershipDisabledError(principal.subject_id, initiative_id)
    grants = role_policy.for_roles(membership.roles)
    configured = LogicalScopes(
        jira_projects=frozenset(profile.integrations.jira.projects)
        if profile.integrations.jira.enabled
        else frozenset(),
        repository_ids=frozenset(repo.id for repo in profile.integrations.git.repositories)
        if profile.integrations.git.enabled
        else frozenset(),
        knowledge_source_ids=frozenset(
            source.id for source in profile.knowledge.sources if source.enabled
        ),
        servicenow_scopes=frozenset(profile.integrations.servicenow.scopes)
        if profile.integrations.servicenow.enabled
        else frozenset(),
        artifact_store_ids=frozenset(store.id for store in profile.artifacts.stores),
    )
    return ResolvedAuthorizationContext(
        principal=principal,
        initiative_id=initiative_id,
        membership=membership,
        capabilities=frozenset(cap for grant in grants for cap in grant.capabilities),
        tool_permissions=frozenset(perm for grant in grants for perm in grant.tool_permissions),
        admin_permissions=frozenset(perm for grant in grants for perm in grant.admin_permissions),
        configured_scopes=configured,
        allowed_scopes=configured.intersect(scope_restriction or ScopeRestriction()),
    )
