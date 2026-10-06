"""Policy grants and a single-initiative authorization snapshot."""

from __future__ import annotations

from dataclasses import dataclass, field

from ai_dlc.domain.identity import (
    AdminPermission,
    Capability,
    InitiativeMembership,
    Principal,
    Role,
    ToolPermission,
)


def _typed_set[T](values: frozenset[T], expected: type[T], field_name: str) -> frozenset[T]:
    try:
        result = frozenset(values)
    except TypeError as exc:
        raise TypeError(f"{field_name} must be iterable") from exc
    if any(not isinstance(value, expected) for value in result):
        raise ValueError(f"{field_name} contains an unknown {expected.__name__}")
    return result


@dataclass(frozen=True, slots=True)
class LogicalScopes:
    jira_projects: frozenset[str] = frozenset()
    repository_ids: frozenset[str] = frozenset()
    knowledge_source_ids: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        for name in ("jira_projects", "repository_ids", "knowledge_source_ids"):
            values = frozenset(getattr(self, name))
            if any(not isinstance(item, str) or not item.strip() for item in values):
                raise ValueError(f"{name} must contain nonblank logical identifiers")
            object.__setattr__(self, name, values)

    def intersect(self, restriction: ScopeRestriction) -> LogicalScopes:
        return LogicalScopes(
            jira_projects=self.jira_projects & restriction.jira_projects
            if restriction.jira_projects is not None
            else self.jira_projects,
            repository_ids=self.repository_ids & restriction.repository_ids
            if restriction.repository_ids is not None
            else self.repository_ids,
            knowledge_source_ids=self.knowledge_source_ids & restriction.knowledge_source_ids
            if restriction.knowledge_source_ids is not None
            else self.knowledge_source_ids,
        )


@dataclass(frozen=True, slots=True)
class ScopeRestriction:
    """None means no further restriction; an empty set denies that scope."""

    jira_projects: frozenset[str] | None = None
    repository_ids: frozenset[str] | None = None
    knowledge_source_ids: frozenset[str] | None = None

    def __post_init__(self) -> None:
        for name in ("jira_projects", "repository_ids", "knowledge_source_ids"):
            values = getattr(self, name)
            if values is not None:
                normalized = frozenset(values)
                if any(not isinstance(item, str) or not item.strip() for item in normalized):
                    raise ValueError(f"{name} must contain nonblank logical identifiers")
                object.__setattr__(self, name, normalized)


@dataclass(frozen=True, slots=True)
class RoleGrant:
    role: Role
    capabilities: frozenset[Capability] = frozenset()
    tool_permissions: frozenset[ToolPermission] = frozenset()
    admin_permissions: frozenset[AdminPermission] = frozenset()

    def __post_init__(self) -> None:
        if not isinstance(self.role, Role):
            raise ValueError("unknown role")
        for name, kind in (
            ("capabilities", Capability),
            ("tool_permissions", ToolPermission),
            ("admin_permissions", AdminPermission),
        ):
            object.__setattr__(self, name, _typed_set(getattr(self, name), kind, name))


@dataclass(frozen=True, slots=True)
class RolePolicy:
    """Injected role grants. Missing roles grant nothing (least privilege)."""

    grants: tuple[RoleGrant, ...] = ()

    def __post_init__(self) -> None:
        grants = tuple(self.grants)
        if any(not isinstance(grant, RoleGrant) for grant in grants):
            raise TypeError("grants must contain RoleGrant values")
        if len({grant.role for grant in grants}) != len(grants):
            raise ValueError("duplicate role grants")
        object.__setattr__(self, "grants", grants)

    def for_roles(self, roles: tuple[Role, ...]) -> tuple[RoleGrant, ...]:
        return tuple(grant for grant in self.grants if grant.role in roles)


@dataclass(frozen=True, slots=True)
class ResolvedAuthorizationContext:
    principal: Principal
    initiative_id: str
    membership: InitiativeMembership
    capabilities: frozenset[Capability] = frozenset()
    tool_permissions: frozenset[ToolPermission] = frozenset()
    admin_permissions: frozenset[AdminPermission] = frozenset()
    configured_scopes: LogicalScopes = field(default_factory=LogicalScopes)
    allowed_scopes: LogicalScopes = field(default_factory=LogicalScopes)

    def __post_init__(self) -> None:
        if not isinstance(self.principal, Principal) or not isinstance(
            self.membership, InitiativeMembership
        ):
            raise TypeError("principal and membership must be identity models")
        if self.membership.principal_id != self.principal.subject_id:
            raise ValueError("membership principal does not match context principal")
        if self.membership.initiative_id != self.initiative_id:
            raise ValueError("membership initiative does not match selected initiative")
        if not self.membership.enabled:
            raise ValueError("disabled membership cannot form an authorization context")
        for name, kind in (
            ("capabilities", Capability),
            ("tool_permissions", ToolPermission),
            ("admin_permissions", AdminPermission),
        ):
            object.__setattr__(self, name, _typed_set(getattr(self, name), kind, name))
        if not isinstance(self.configured_scopes, LogicalScopes) or not isinstance(
            self.allowed_scopes, LogicalScopes
        ):
            raise TypeError("scopes must be LogicalScopes")
        for name in ("jira_projects", "repository_ids", "knowledge_source_ids"):
            if not getattr(self.allowed_scopes, name) <= getattr(self.configured_scopes, name):
                raise ValueError(f"allowed {name} exceed Initiative Profile scope")

    def can_use(self, capability: Capability) -> bool:
        if not isinstance(capability, Capability):
            raise ValueError("unknown Capability")
        return capability in self.capabilities

    def can_use_tool(self, permission: ToolPermission) -> bool:
        if not isinstance(permission, ToolPermission):
            raise ValueError("unknown ToolPermission")
        return permission in self.tool_permissions

    def can_administer(self, permission: AdminPermission) -> bool:
        if not isinstance(permission, AdminPermission):
            raise ValueError("unknown AdminPermission")
        return permission in self.admin_permissions
