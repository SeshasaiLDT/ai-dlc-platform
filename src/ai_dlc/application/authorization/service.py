"""The single application decision boundary for initiative authorization."""

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

from ai_dlc.domain.identity import (
    AdminPermission,
    MembershipDisabledError,
    MembershipNotFoundError,
    Role,
    ToolPermission,
)
from ai_dlc.domain.initiative import InitiativeProfile

from .decisions import (
    AdminAction,
    AuthorizationAuditEvent,
    AuthorizationDecision,
    AuthorizationReason,
    AuthorizationRequest,
    CapabilityAction,
    JiraProjectTarget,
    KnowledgeSourceTarget,
    PlatformAuthorizationRequest,
    RepositoryTarget,
    ToolAction,
)
from .errors import AuthorizationAuditError, AuthorizationDeniedError
from .models import ResolvedAuthorizationContext, RolePolicy, ScopeRestriction
from .ports import AuthorizationAuditSink, MembershipRepository, PlatformAdminRepository
from .resolution import resolve_authorization_context

_JIRA = frozenset({ToolPermission.JIRA_READ, ToolPermission.JIRA_WRITE})
_GIT = frozenset({ToolPermission.GIT_READ, ToolPermission.GIT_WRITE})
_KNOWLEDGE = frozenset({ToolPermission.KNOWLEDGE_READ})
_UNSCOPED = frozenset(
    {
        ToolPermission.SERVICENOW_READ,
        ToolPermission.SERVICENOW_WRITE,
        ToolPermission.ARTIFACT_READ,
        ToolPermission.ARTIFACT_WRITE,
    }
)


class AuthorizationService:
    def __init__(
        self,
        memberships: MembershipRepository,
        role_policy: RolePolicy,
        audit_sink: AuthorizationAuditSink,
        *,
        platform_admins: PlatformAdminRepository | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        decision_id_factory: Callable[[], str] = lambda: uuid4().hex,
    ) -> None:
        self._memberships = memberships
        self._role_policy = role_policy
        self._audit_sink = audit_sink
        self._platform_admins = platform_admins
        self._clock = clock
        self._decision_id_factory = decision_id_factory

    def evaluate(
        self,
        request: AuthorizationRequest | PlatformAuthorizationRequest,
        *,
        profile: InitiativeProfile | None = None,
        initiative_revision: int | None = None,
        scope_restriction: ScopeRestriction | None = None,
    ) -> AuthorizationDecision:
        """Evaluate trusted server inputs and record exactly one decision."""
        if not isinstance(request, (AuthorizationRequest, PlatformAuthorizationRequest)):
            raise TypeError("request must be an authorization request")
        if initiative_revision is not None and (
            type(initiative_revision) is not int or initiative_revision < 1
        ):
            raise ValueError("initiative_revision must be a positive integer")
        if scope_restriction is not None and not isinstance(scope_restriction, ScopeRestriction):
            raise TypeError("scope_restriction must be a ScopeRestriction")

        if isinstance(request, PlatformAuthorizationRequest):
            if (
                profile is not None
                or initiative_revision is not None
                or scope_restriction is not None
            ):
                raise ValueError("platform authorization has no initiative scope")
            reason = self._evaluate_platform_admin(request)
            initiative_id = None
            target = None
        else:
            if not isinstance(profile, InitiativeProfile):
                raise TypeError("profile must be an InitiativeProfile")
            initiative_id = request.initiative_id
            target = request.target
            if profile.initiative.id != request.initiative_id:
                reason = AuthorizationReason.INITIATIVE_MISMATCH
            else:
                try:
                    context = resolve_authorization_context(
                        request.principal,
                        request.initiative_id,
                        profile,
                        self._memberships,
                        self._role_policy,
                        scope_restriction=scope_restriction,
                    )
                except MembershipNotFoundError:
                    reason = AuthorizationReason.MEMBERSHIP_NOT_FOUND
                except MembershipDisabledError:
                    reason = AuthorizationReason.MEMBERSHIP_DISABLED
                else:
                    reason = self._evaluate_grant_and_target(request, context)

        occurred_at = self._clock()
        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            raise ValueError("authorization clock must be timezone-aware")
        decision_id = self._decision_id_factory()
        if not isinstance(decision_id, str) or not decision_id.strip():
            raise ValueError("decision ID must be nonblank")
        decision = AuthorizationDecision(
            decision_id=decision_id,
            occurred_at=occurred_at.astimezone(UTC),
            principal_id=request.principal.subject_id,
            initiative_id=initiative_id,
            action=request.action,
            target=target,
            allowed=reason is AuthorizationReason.ALLOWED,
            reason=reason,
            initiative_revision=initiative_revision,
        )
        try:
            self._audit_sink.record(AuthorizationAuditEvent.from_decision(decision))
        except Exception:
            # No decision reaches a protected caller when its audit record fails.
            raise AuthorizationAuditError from None
        return decision

    def require(
        self,
        request: AuthorizationRequest | PlatformAuthorizationRequest,
        *,
        profile: InitiativeProfile | None = None,
        initiative_revision: int | None = None,
        scope_restriction: ScopeRestriction | None = None,
    ) -> AuthorizationDecision:
        """Server-side guard: return an audited allow or raise a typed denial."""
        decision = self.evaluate(
            request,
            profile=profile,
            initiative_revision=initiative_revision,
            scope_restriction=scope_restriction,
        )
        if not decision.allowed:
            raise AuthorizationDeniedError(decision)
        return decision

    def _evaluate_platform_admin(
        self, request: PlatformAuthorizationRequest
    ) -> AuthorizationReason:
        if self._platform_admins is None:
            return AuthorizationReason.ADMIN_PERMISSION_NOT_GRANTED
        if self._platform_admins.is_platform_admin(request.principal.subject_id) is not True:
            return AuthorizationReason.ADMIN_PERMISSION_NOT_GRANTED
        grants = self._role_policy.for_roles((Role.PLATFORM_ADMIN,))
        return (
            AuthorizationReason.ALLOWED
            if any(AdminPermission.PLATFORM_MANAGE in grant.admin_permissions for grant in grants)
            else AuthorizationReason.ADMIN_PERMISSION_NOT_GRANTED
        )

    @staticmethod
    def _evaluate_grant_and_target(
        request: AuthorizationRequest, context: ResolvedAuthorizationContext
    ) -> AuthorizationReason:
        action = request.action
        target = request.target
        if isinstance(action, CapabilityAction):
            if target is not None:
                return AuthorizationReason.INVALID_REQUEST
            return (
                AuthorizationReason.ALLOWED
                if context.can_use(action.capability)
                else AuthorizationReason.CAPABILITY_NOT_GRANTED
            )
        if isinstance(action, AdminAction):
            if target is not None:
                return AuthorizationReason.INVALID_REQUEST
            if action.permission is AdminPermission.PLATFORM_MANAGE:
                return AuthorizationReason.INVALID_REQUEST
            return (
                AuthorizationReason.ALLOWED
                if context.can_administer(action.permission)
                else AuthorizationReason.ADMIN_PERMISSION_NOT_GRANTED
            )
        if not isinstance(action, ToolAction):
            return AuthorizationReason.INVALID_REQUEST

        permission = action.permission
        if permission in _JIRA:
            if target is not None and not isinstance(target, JiraProjectTarget):
                return AuthorizationReason.INVALID_REQUEST
        elif permission in _GIT:
            if target is not None and not isinstance(target, RepositoryTarget):
                return AuthorizationReason.INVALID_REQUEST
        elif permission in _KNOWLEDGE:
            if target is not None and not isinstance(target, KnowledgeSourceTarget):
                return AuthorizationReason.INVALID_REQUEST
        elif permission in _UNSCOPED:
            if target is not None:
                return AuthorizationReason.INVALID_REQUEST
        else:
            return AuthorizationReason.INVALID_REQUEST

        if not context.can_use_tool(permission):
            return AuthorizationReason.TOOL_PERMISSION_NOT_GRANTED
        if permission in _JIRA | _GIT | _KNOWLEDGE and target is None:
            return AuthorizationReason.TARGET_REQUIRED
        if isinstance(target, JiraProjectTarget):
            in_scope = target.project_id in context.allowed_scopes.jira_projects
        elif isinstance(target, RepositoryTarget):
            in_scope = target.repository_id in context.allowed_scopes.repository_ids
        elif isinstance(target, KnowledgeSourceTarget):
            in_scope = target.source_id in context.allowed_scopes.knowledge_source_ids
        else:
            in_scope = True
        return AuthorizationReason.ALLOWED if in_scope else AuthorizationReason.TARGET_OUT_OF_SCOPE
