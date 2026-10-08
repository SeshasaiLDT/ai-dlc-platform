"""Construct server-only tool context from authenticated runtime state."""

from dataclasses import dataclass

from ai_dlc.application.authorization import (
    MembershipRepository,
    RolePolicy,
    ScopeRestriction,
    resolve_authorization_context,
)
from ai_dlc.application.git_remote.models import TrustedGitContext
from ai_dlc.application.initiatives import InitiativeRegistry
from ai_dlc.application.jira.models import TrustedJiraContext
from ai_dlc.application.resource_bindings import TrustedResolutionContext
from ai_dlc.application.servicenow.models import TrustedServiceNowContext
from ai_dlc.domain.identity import Principal


@dataclass(frozen=True, slots=True)
class AuthenticatedRuntimeSelection:
    """Passed by the authenticated harness/session store, never a tool payload."""

    principal: Principal
    initiative_id: str
    environment: str
    correlation_id: str
    workspace_id: str | None = None
    task_id: str | None = None
    approval_id: str | None = None
    scope_restriction: ScopeRestriction | None = None
    approved_commits: frozenset[tuple[str, str]] = frozenset()
    timeout_seconds: float = 10.0
    cancelled: bool = False


@dataclass(frozen=True, slots=True)
class TrustedGatewayContext:
    resolution: TrustedResolutionContext
    initiative_revision: int
    workspace_id: str | None
    task_id: str | None
    approval_id: str | None
    approved_commits: frozenset[tuple[str, str]]
    timeout_seconds: float
    cancelled: bool

    def for_jira(self) -> TrustedJiraContext:
        return TrustedJiraContext(
            self.resolution,
            self.initiative_revision,
            self.approval_id,
            self.workspace_id,
            self.task_id,
            self.timeout_seconds,
            self.cancelled,
        )

    def for_servicenow(self) -> TrustedServiceNowContext:
        return TrustedServiceNowContext(
            self.resolution,
            self.initiative_revision,
            self.approval_id,
            self.workspace_id,
            self.task_id,
            self.timeout_seconds,
            self.cancelled,
        )

    def for_git(self) -> TrustedGitContext:
        return TrustedGitContext(
            self.resolution,
            self.initiative_revision,
            self.approval_id,
            self.workspace_id,
            self.task_id,
            self.approved_commits,
            self.timeout_seconds,
            self.cancelled,
        )


class GatewayContextResolver:
    def __init__(
        self,
        initiatives: InitiativeRegistry,
        memberships: MembershipRepository,
        roles: RolePolicy,
    ) -> None:
        self._initiatives = initiatives
        self._memberships = memberships
        self._roles = roles

    def resolve(self, selection: AuthenticatedRuntimeSelection) -> TrustedGatewayContext:
        if not isinstance(selection, AuthenticatedRuntimeSelection) or not isinstance(
            selection.principal, Principal
        ):
            raise TypeError("authenticated runtime selection required")
        registered = self._initiatives.get(selection.initiative_id)
        if registered.status.value != "active":
            raise ValueError("selected initiative is not active")
        authorization = resolve_authorization_context(
            selection.principal,
            selection.initiative_id,
            registered.profile,
            self._memberships,
            self._roles,
            scope_restriction=selection.scope_restriction,
        )
        resolution = TrustedResolutionContext(
            selection.environment,
            registered.profile,
            authorization,
            selection.correlation_id,
        )
        return TrustedGatewayContext(
            resolution,
            registered.current_revision,
            selection.workspace_id,
            selection.task_id,
            selection.approval_id,
            selection.approved_commits,
            selection.timeout_seconds,
            selection.cancelled,
        )
