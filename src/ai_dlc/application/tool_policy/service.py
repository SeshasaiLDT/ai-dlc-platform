"""Initiative tool policy evaluation after centralized base authorization."""

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

from ai_dlc.application.authorization import (
    AuthorizationRequest,
    AuthorizationService,
    JiraProjectTarget,
    RepositoryTarget,
    ScopeRestriction,
    ToolAction,
)
from ai_dlc.domain.initiative import InitiativeProfile
from ai_dlc.domain.initiative.enums import RepositoryAccess

from .errors import (
    ToolApprovalRequiredError,
    ToolPolicyAuditError,
    ToolPolicyConfigurationError,
    ToolPolicyDeniedError,
)
from .models import (
    GitTarget,
    ServiceNowTarget,
    ToolPolicy,
    ToolPolicyAuditEvent,
    ToolPolicyDecision,
    ToolPolicyEffect,
    ToolPolicyReason,
    ToolPolicyRequest,
)
from .operations import ToolKind, ToolOperationRisk, operation_risk, required_permission, tool_kind
from .ports import ToolPolicyAuditSink, ToolPolicyRepository


class ToolPolicyService:
    """Compose AIDLC-27 and exact-match policy; never accept caller-made grants."""

    def __init__(
        self,
        base_authorization: AuthorizationService,
        policies: ToolPolicyRepository,
        audit_sink: ToolPolicyAuditSink,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        decision_id_factory: Callable[[], str] = lambda: uuid4().hex,
    ) -> None:
        self._base_authorization = base_authorization
        self._policies = policies
        self._audit_sink = audit_sink
        self._clock = clock
        self._decision_id_factory = decision_id_factory

    def evaluate(
        self,
        request: ToolPolicyRequest,
        *,
        profile: InitiativeProfile,
        initiative_revision: int | None = None,
        scope_restriction: ScopeRestriction | None = None,
    ) -> ToolPolicyDecision:
        if not isinstance(request, ToolPolicyRequest):
            raise TypeError("request must be a ToolPolicyRequest")
        if not isinstance(profile, InitiativeProfile):
            raise TypeError("profile must be an InitiativeProfile")
        kind = tool_kind(request.operation)
        risk = operation_risk(request.operation)
        base_target = self._base_target(request)
        base = self._base_authorization.evaluate(
            AuthorizationRequest(
                principal=request.principal,
                initiative_id=request.initiative_id,
                action=ToolAction(required_permission(request.operation)),
                target=base_target,
            ),
            profile=profile,
            initiative_revision=initiative_revision,
            scope_restriction=scope_restriction,
        )
        if (
            not base.allowed
            or not isinstance(base.action, ToolAction)
            or base.action.permission is not required_permission(request.operation)
            or base.initiative_id != request.initiative_id
            or base.target != base_target
            or base.principal_id != request.principal.subject_id
            or base.initiative_revision != initiative_revision
        ):
            effect, reason = ToolPolicyEffect.DENY, ToolPolicyReason.BASE_AUTHORIZATION_REQUIRED
        elif kind is ToolKind.SERVICENOW and not self._servicenow_scope_allowed(request, profile):
            effect, reason = ToolPolicyEffect.DENY, ToolPolicyReason.TARGET_NOT_ALLOWED
        else:
            profile_reason = self._profile_denial(request, profile, kind, risk)
            if profile_reason is not None:
                effect, reason = ToolPolicyEffect.DENY, profile_reason
            else:
                effect, reason = self._evaluate_policy(request, kind, risk)
                if effect is ToolPolicyEffect.ALLOW and self._profile_requires_approval(
                    profile, kind, risk
                ):
                    effect, reason = (
                        ToolPolicyEffect.REQUIRE_APPROVAL,
                        ToolPolicyReason.APPROVAL_REQUIRED,
                    )

        occurred_at = self._clock()
        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            raise ValueError("tool policy clock must be timezone-aware")
        decision_id = self._decision_id_factory()
        if not isinstance(decision_id, str) or not decision_id.strip():
            raise ValueError("tool policy decision ID must be nonblank")
        decision = ToolPolicyDecision(
            decision_id=decision_id,
            occurred_at=occurred_at.astimezone(UTC),
            principal_id=request.principal.subject_id,
            initiative_id=request.initiative_id,
            tool=kind,
            operation=request.operation,
            risk=risk,
            target=request.target,
            effect=effect,
            reason=reason,
            base_decision_id=base.decision_id,
        )
        try:
            self._audit_sink.record(ToolPolicyAuditEvent.from_decision(decision))
        except Exception:
            raise ToolPolicyAuditError from None
        return decision

    def require_allowed(
        self,
        request: ToolPolicyRequest,
        *,
        profile: InitiativeProfile,
        initiative_revision: int | None = None,
        scope_restriction: ScopeRestriction | None = None,
    ) -> ToolPolicyDecision:
        """Only ALLOW permits immediate execution; approval is a separate gate."""
        decision = self.evaluate(
            request,
            profile=profile,
            initiative_revision=initiative_revision,
            scope_restriction=scope_restriction,
        )
        if decision.effect is ToolPolicyEffect.REQUIRE_APPROVAL:
            raise ToolApprovalRequiredError(decision)
        if decision.effect is ToolPolicyEffect.DENY:
            raise ToolPolicyDeniedError(decision)
        return decision

    @staticmethod
    def _base_target(request: ToolPolicyRequest) -> JiraProjectTarget | RepositoryTarget | None:
        target = request.target
        if isinstance(target, JiraProjectTarget):
            return target
        if isinstance(target, GitTarget):
            return RepositoryTarget(target.repository_id)
        return None

    @staticmethod
    def _servicenow_scope_allowed(request: ToolPolicyRequest, profile: InitiativeProfile) -> bool:
        target = request.target
        return (
            isinstance(target, ServiceNowTarget)
            and profile.integrations.servicenow.enabled
            and target.scope_id in profile.integrations.servicenow.scopes
        )

    @staticmethod
    def _profile_operation_policy(profile: InitiativeProfile, kind: ToolKind):
        if kind is ToolKind.JIRA:
            return profile.policies.jira_write
        if kind is ToolKind.GIT:
            return profile.policies.git_write
        return profile.policies.servicenow_write

    @classmethod
    def _profile_denial(
        cls,
        request: ToolPolicyRequest,
        profile: InitiativeProfile,
        kind: ToolKind,
        risk: ToolOperationRisk,
    ) -> ToolPolicyReason | None:
        if risk is ToolOperationRisk.READ:
            return None
        if not cls._profile_operation_policy(profile, kind).enabled:
            return ToolPolicyReason.INITIATIVE_POLICY_DISABLED
        if kind is ToolKind.GIT and isinstance(request.target, GitTarget):
            repository = next(
                (
                    item
                    for item in profile.integrations.git.repositories
                    if item.id == request.target.repository_id
                ),
                None,
            )
            if repository is None or repository.access is not RepositoryAccess.READ_WRITE:
                return ToolPolicyReason.TARGET_NOT_ALLOWED
        return None

    @classmethod
    def _profile_requires_approval(
        cls, profile: InitiativeProfile, kind: ToolKind, risk: ToolOperationRisk
    ) -> bool:
        return (
            risk is not ToolOperationRisk.READ
            and cls._profile_operation_policy(profile, kind).human_approval_required
        )

    def _evaluate_policy(
        self, request: ToolPolicyRequest, kind: ToolKind, risk: ToolOperationRisk
    ) -> tuple[ToolPolicyEffect, ToolPolicyReason]:
        try:
            policies = self._policies.policies_for(request.initiative_id, kind)
            if any(not isinstance(policy, ToolPolicy) for policy in policies):
                raise TypeError("invalid policy repository result")
        except Exception:
            raise ToolPolicyConfigurationError from None
        matching = tuple(policy for policy in policies if policy.matches(request))
        if not matching:
            has_operation = any(
                policy.initiative_id == request.initiative_id
                and type(policy.operation) is type(request.operation)
                and policy.operation == request.operation
                for policy in policies
            )
            reason = (
                ToolPolicyReason.TARGET_NOT_ALLOWED if has_operation else ToolPolicyReason.NO_POLICY
            )
            return ToolPolicyEffect.DENY, reason
        if len(matching) != 1:
            return ToolPolicyEffect.DENY, ToolPolicyReason.AMBIGUOUS_POLICY
        effect = matching[0].effect
        if effect is ToolPolicyEffect.ALLOW:
            return effect, ToolPolicyReason.ALLOWED_BY_POLICY
        if effect is ToolPolicyEffect.REQUIRE_APPROVAL:
            return effect, ToolPolicyReason.APPROVAL_REQUIRED
        if risk is ToolOperationRisk.DESTRUCTIVE:
            return effect, ToolPolicyReason.DESTRUCTIVE_OPERATION_DENIED
        return effect, ToolPolicyReason.OPERATION_DENIED
