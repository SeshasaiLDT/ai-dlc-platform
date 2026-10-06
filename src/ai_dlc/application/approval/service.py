"""Human approval orchestration after AIDLC-27 and AIDLC-28."""

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

from ai_dlc.application.authorization import (
    AdminAction,
    AuthorizationDeniedError,
    AuthorizationRequest,
    AuthorizationService,
    ScopeRestriction,
)
from ai_dlc.application.tool_policy import (
    ToolPolicyEffect,
    ToolPolicyRequest,
    ToolPolicyService,
)
from ai_dlc.domain.identity import AdminPermission, Principal
from ai_dlc.domain.initiative import InitiativeProfile

from .errors import (
    ApprovalConflictError,
    ApprovalInvalidTransitionError,
    ApprovalNotAuthorizedError,
    InvalidApprovalSourceError,
    SelfApprovalNotAllowedError,
)
from .models import ApprovalAuditEvent, ApprovalRecord, ApprovalStatus
from .ports import ApprovalRepository, HumanIdentityVerifier


class ApprovalService:
    def __init__(
        self,
        tool_policy: ToolPolicyService,
        authorization: AuthorizationService,
        repository: ApprovalRepository,
        human_verifier: HumanIdentityVerifier,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        id_factory: Callable[[], str] = lambda: uuid4().hex,
    ) -> None:
        self._tool_policy = tool_policy
        self._authorization = authorization
        self._repository = repository
        self._human_verifier = human_verifier
        self._clock = clock
        self._id_factory = id_factory

    def request_approval(
        self,
        request: ToolPolicyRequest,
        *,
        profile: InitiativeProfile,
        request_key: str,
        initiative_revision: int | None = None,
        scope_restriction: ScopeRestriction | None = None,
    ) -> ApprovalRecord:
        """Only this server-side call can obtain the trusted tool-policy decision."""
        if not isinstance(request, ToolPolicyRequest):
            raise TypeError("request must be a ToolPolicyRequest")
        if not isinstance(request_key, str) or not request_key.strip():
            raise ValueError("request key must be nonblank")
        if not isinstance(profile, InitiativeProfile):
            raise TypeError("profile must be an InitiativeProfile")
        if profile.initiative.id != request.initiative_id:
            raise InvalidApprovalSourceError
        previous = self._repository.get_by_request_key(request_key)
        if previous is not None:
            if not self._matches(previous, request, initiative_revision):
                raise ApprovalConflictError
            return previous
        decision = self._tool_policy.evaluate(
            request,
            profile=profile,
            initiative_revision=initiative_revision,
            scope_restriction=scope_restriction,
        )
        if decision.effect is not ToolPolicyEffect.REQUIRE_APPROVAL:
            raise InvalidApprovalSourceError
        if decision.principal_id != request.principal.subject_id or (
            decision.initiative_id != request.initiative_id
            or decision.operation != request.operation
            or decision.target != request.target
        ):
            raise InvalidApprovalSourceError
        record = ApprovalRecord.from_decision(
            decision, self._id_factory(), request_key, initiative_revision
        )
        event = ApprovalAuditEvent.for_transition(self._id_factory(), None, record)
        return self._repository.commit(record, event, expected_version=None)

    def approve(
        self,
        approval_id: str,
        approver: Principal,
        *,
        profile: InitiativeProfile,
        expected_version: int,
    ) -> ApprovalRecord:
        return self._decide(
            approval_id, approver, ApprovalStatus.APPROVED, profile, expected_version
        )

    def reject(
        self,
        approval_id: str,
        approver: Principal,
        *,
        profile: InitiativeProfile,
        expected_version: int,
    ) -> ApprovalRecord:
        return self._decide(
            approval_id, approver, ApprovalStatus.REJECTED, profile, expected_version
        )

    def _decide(
        self,
        approval_id: str,
        approver: Principal,
        status: ApprovalStatus,
        profile: InitiativeProfile,
        expected_version: int,
    ) -> ApprovalRecord:
        if not isinstance(approver, Principal):
            raise TypeError("approver must be an authenticated Principal")
        record = self._repository.get(approval_id)
        if record.status is not ApprovalStatus.PENDING:
            if record.status is status and record.decided_by == approver.subject_id:
                return record
            raise ApprovalInvalidTransitionError
        if record.version != expected_version:
            raise ApprovalConflictError
        if profile.initiative.id != record.initiative_id:
            raise ApprovalNotAuthorizedError
        if approver.subject_id == record.principal_id:
            raise SelfApprovalNotAllowedError
        if self._human_verifier.is_human(approver) is not True:
            raise ApprovalNotAuthorizedError
        try:
            self._authorization.require(
                AuthorizationRequest(
                    principal=approver,
                    initiative_id=record.initiative_id,
                    action=AdminAction(AdminPermission.INITIATIVE_APPROVAL_MANAGE),
                ),
                profile=profile,
                initiative_revision=record.initiative_revision,
            )
        except AuthorizationDeniedError:
            raise ApprovalNotAuthorizedError from None
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("approval clock must be timezone-aware")
        updated = record.decided(status, approver.subject_id, now.astimezone(UTC))
        event = ApprovalAuditEvent.for_transition(self._id_factory(), record, updated)
        return self._repository.commit(updated, event, expected_version=expected_version)

    def approval_gate_satisfied(
        self,
        approval_id: str,
        request: ToolPolicyRequest,
        *,
        profile: InitiativeProfile,
        initiative_revision: int | None = None,
        scope_restriction: ScopeRestriction | None = None,
    ) -> bool:
        """Re-evaluate current earlier gates; an old approval never creates a grant."""
        record = self._repository.get(approval_id)
        if not record.approval_gate_satisfied or not self._matches(
            record, request, initiative_revision
        ):
            return False
        decision = self._tool_policy.evaluate(
            request,
            profile=profile,
            initiative_revision=initiative_revision,
            scope_restriction=scope_restriction,
        )
        return decision.effect is ToolPolicyEffect.REQUIRE_APPROVAL

    @staticmethod
    def _matches(record: ApprovalRecord, request: ToolPolicyRequest, revision: int | None) -> bool:
        return (
            record.principal_id == request.principal.subject_id
            and record.initiative_id == request.initiative_id
            and record.operation == request.operation
            and record.target == request.target
            and record.initiative_revision == revision
        )
