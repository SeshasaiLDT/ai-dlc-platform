"""Budget, quota and usage governance for model calls. Decisions only: it never invokes a model.

Admission re-checks the registry (enablement, availability, eligibility, revision) immediately
before dispatch. This narrows but cannot close the race with a concurrent registry change; a
revision check is not a distributed execution lease.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from ai_dlc.application.agent_harness import (
    AgentContext,
    ApprovalIntent,
    ApprovalProvider,
    ModelRole,
    RoleProfiles,
    TelemetryProvider,
)
from ai_dlc.application.model_registry import (
    DeploymentNotFoundError,
    ModelRegistryReader,
    RegisteredModel,
)
from ai_dlc.domain.approval import ApprovalStatus
from ai_dlc.domain.initiative import InitiativeProfile
from ai_dlc.domain.initiative.enums import BudgetAction, BudgetPeriod
from ai_dlc.domain.initiative.models import QuotaLimits

from .cost import calculate_cost, estimate_cost
from .models import (
    AccountingStatus,
    AdmissionDecision,
    AdmissionLimits,
    AdmissionRequest,
    AdmissionStatus,
    BudgetState,
    LedgerStatus,
    Reservation,
    TokenUsage,
    UsageRecord,
    UsageSource,
)
from .ports import GovernanceLedger


def _hash(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:32]


def period_key(period: BudgetPeriod, now: datetime) -> str:
    return now.strftime("%Y-%m-%d" if period is BudgetPeriod.DAILY else "%Y-%m")


class ModelGovernanceService:
    def __init__(
        self,
        ledger: GovernanceLedger,
        registry: ModelRegistryReader,
        profiles: RoleProfiles,
        *,
        approvals: ApprovalProvider | None = None,
        telemetry: TelemetryProvider | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        reservation_ttl: timedelta = timedelta(minutes=10),
    ) -> None:
        self._ledger = ledger
        self._registry = registry
        self._profiles = profiles
        self._approvals = approvals
        self._telemetry = telemetry
        self._clock = clock
        self._ttl = reservation_ttl

    # -- admission -------------------------------------------------------------------------

    async def admit(
        self,
        request: AdmissionRequest,
        *,
        context: AgentContext,
        profile: InitiativeProfile,
        initiative_revision: int,
    ) -> AdmissionDecision:
        if not isinstance(request, AdmissionRequest) or not isinstance(context, AgentContext):
            raise TypeError("AdmissionRequest and trusted AgentContext required")
        decision = await self._admit(request, context, profile, initiative_revision)
        self._report_admission(decision, context)
        return decision

    def _decision(self, context: AgentContext, status, reason: str, **kwargs) -> AdmissionDecision:
        return AdmissionDecision(
            status=status,
            reason=reason,
            initiative_id=context.authorization.initiative_id,
            request_id=context.request_id,
            correlation_id=context.correlation_id,
            trace_id=context.trace_id,
            task_id=context.task_id,
            **kwargs,
        )

    async def _admit(
        self,
        req: AdmissionRequest,
        context: AgentContext,
        profile: InitiativeProfile,
        revision: int,
    ) -> AdmissionDecision:
        def decide(status, reason, **kw):
            return self._decision(context, status, reason, **kw)

        initiative = context.authorization.initiative_id
        if profile.initiative.id != initiative:
            return decide(AdmissionStatus.INITIATIVE_MISMATCH, "profile_initiative_mismatch")

        # 1. Revalidate the deployment against the registry right now.
        deployment = self._current_candidate(req)
        if isinstance(deployment, tuple):
            status, reason = deployment
            return decide(status, reason)
        governance = profile.inference_controls
        budget, quota = governance.budget, governance.quota
        now = self._now()
        role = req.selection.role
        spec = deployment.spec

        # 2. Estimate with trusted registry pricing; unknown stays unknown.
        estimate = estimate_cost(
            req.estimated_input_tokens,
            req.max_output_tokens,
            spec.pricing,
            budget.currency,
            attempts=req.inner_attempts,
        )
        role_cap = next(
            (item.amount for item in budget.role_limits if item.role.value == role.value), None
        )
        request_cap = _lowest(budget.request_limit, req.request_cost_limit)
        budget_active = any(
            cap is not None for cap in (budget.initiative_limit, role_cap, request_cap)
        )
        tokens = (req.estimated_input_tokens + req.max_output_tokens) * req.inner_attempts
        reservation = Reservation(
            reservation_id="res-" + _hash(req.invocation_ref, req.deployment_id),
            initiative_id=initiative,
            role=ModelRole(role.value),
            deployment_id=req.deployment_id,
            registry_revision=deployment.revision,
            invocation_ref=req.invocation_ref,
            estimated_cost=estimate,
            estimated_tokens=tokens,
            created_at=now,
            expires_at=now + self._ttl,
            budget_period_key=period_key(budget.period, now),
            quota_period_key=period_key(quota.period, now),
            budget_revision=budget.revision,
            currency=budget.currency,
            pricing=spec.pricing,
        )
        quota_role = next(
            (item.limits for item in quota.role_limits if item.role.value == role.value),
            QuotaLimits(),
        )

        def limits(enforce: bool) -> AdmissionLimits:
            return AdmissionLimits(
                currency=budget.currency,
                budget_revision=budget.revision,
                initiative_budget=budget.initiative_limit,
                role_budget=role_cap,
                enforce_budget=enforce and budget_active,
                warning_threshold=budget.warning_threshold,
                quota_initiative=quota.initiative,
                quota_role=quota_role,
                now=now,
            )

        # 3. Pre-checks the ledger cannot make: unknown cost and the per-request ceiling.
        breach: str | None = None
        state = BudgetState.AVAILABLE
        if budget_active and estimate is None:
            breach, state = "cost_unknown", BudgetState.UNKNOWN
        elif request_cap is not None and estimate is not None and estimate > request_cap:
            breach, state = "request_limit_exceeded", BudgetState.EXHAUSTED

        bypass = False
        if breach is not None:
            outcome = await self._on_breach(
                budget.on_exhausted, breach, req, role, reservation, context
            )
            if isinstance(outcome, AdmissionDecision):
                return outcome.model_copy(
                    update={"budget_state": state, "estimated_cost": estimate}
                )
            bypass = outcome

        admission = self._ledger.reserve(
            reservation.model_copy(update={"approval_id": req.approval_id if bypass else None}),
            limits(enforce=not bypass),
        )
        if admission.status is LedgerStatus.BUDGET_EXHAUSTED:
            outcome = await self._on_breach(
                budget.on_exhausted, f"{admission.exhausted_scope}_budget_exhausted",
                req, role, reservation, context,
            )  # fmt: skip
            if isinstance(outcome, AdmissionDecision):
                return outcome.model_copy(
                    update={
                        "budget_state": BudgetState.EXHAUSTED,
                        "estimated_cost": estimate,
                        "utilization": admission.utilization,
                    }
                )
            admission = self._ledger.reserve(
                reservation.model_copy(update={"approval_id": req.approval_id}), limits(False)
            )
        common = {
            "estimated_cost": estimate,
            "registry_revision": deployment.revision,
            "budget_revision": budget.revision,
            "utilization": admission.utilization,
        }
        if admission.status is LedgerStatus.QUOTA_EXHAUSTED:
            return decide(
                AdmissionStatus.BLOCKED_QUOTA, "quota_exhausted",
                budget_state=admission.budget_state, quota_dimension=admission.quota_dimension,
                **common,
            )  # fmt: skip
        if admission.status is LedgerStatus.STALE_BUDGET_REVISION:
            return decide(AdmissionStatus.STALE_BUDGET_REVISION, "stale_budget_revision", **common)
        if admission.status is LedgerStatus.APPROVAL_REUSED:
            return decide(AdmissionStatus.APPROVAL_INVALID, "approval_already_used", **common)
        status = (
            AdmissionStatus.DUPLICATE
            if admission.status is LedgerStatus.DUPLICATE
            else AdmissionStatus.ADMITTED
        )
        return decide(
            status, "admitted", budget_state=admission.budget_state,
            reservation_id=admission.reservation.reservation_id, **common,
        )  # fmt: skip

    def _current_candidate(self, req: AdmissionRequest) -> RegisteredModel | tuple:
        """The deployment as it is now, or (status, reason) when it must not be dispatched."""
        eligible = {
            m.deployment_id: m for m in self._registry.query_eligible(req.selection, self._profiles)
        }
        current = eligible.get(req.deployment_id)
        if current is None:
            try:
                self._registry.get(req.deployment_id)
            except DeploymentNotFoundError:
                return AdmissionStatus.DEPLOYMENT_UNAVAILABLE, "deployment_not_found"
            return AdmissionStatus.DEPLOYMENT_UNAVAILABLE, "deployment_not_currently_eligible"
        if current.revision != req.evaluated_revision:
            return AdmissionStatus.REGISTRY_CHANGED, "registry_revision_changed"
        return current

    async def _on_breach(
        self, action: BudgetAction, reason: str, req, role, reservation, context
    ) -> AdmissionDecision | bool:
        """Apply the configured policy: a decision (blocked/approval) or True to proceed."""
        if action is BudgetAction.ALLOW:
            self._emit(context, "model.budget_exhausted", "model.budget_breaches_allowed")
            return True
        if action is BudgetAction.REQUIRE_APPROVAL:
            if req.approval_id is not None and await self._approved(req.approval_id, context):
                return True
            if req.approval_id is not None:
                return self._decision(
                    context, AdmissionStatus.APPROVAL_INVALID, "approval_not_granted"
                )
            intent = ApprovalIntent(
                request_key="budget-"
                + _hash(reservation.reservation_id, reservation.budget_period_key),
                operation="model.budget_exception",
                logical_target={
                    "initiative_id": reservation.initiative_id,
                    "role": role.value,
                    "deployment_id": reservation.deployment_id,
                    "invocation_ref": reservation.invocation_ref,
                    "budget_period": reservation.budget_period_key,
                    "reason": reason,
                },
            )
            return self._decision(
                context, AdmissionStatus.APPROVAL_REQUIRED, reason, approval_intent=intent
            )
        return self._decision(context, AdmissionStatus.BLOCKED_BUDGET, reason)

    async def _approved(self, approval_id: str, context: AgentContext) -> bool:
        """Only the trusted approval boundary can say yes; model output or confidence cannot."""
        if self._approvals is None:
            return False
        try:
            reference = await self._approvals.get_approval_status(approval_id, context=context)
        except Exception:
            return False
        return reference.approval_id == approval_id and reference.status is ApprovalStatus.APPROVED

    # -- accounting ------------------------------------------------------------------------

    def record_usage(
        self, reservation_id: str, usage: TokenUsage | None, *, context: AgentContext
    ) -> UsageRecord:
        """Commit reported usage (or explicitly missing usage) for an admitted call. Idempotent."""
        reservation = self._require(reservation_id)
        if usage is None:
            source, cost = UsageSource.MISSING, None
        else:
            source = UsageSource.PROVIDER_REPORTED
            cost = (
                calculate_cost(usage, reservation.pricing, reservation.currency)
                if reservation.pricing is not None
                else None
            )
        status = AccountingStatus.COMMITTED if cost is not None else AccountingStatus.UNKNOWN_COST
        record = self._record(reservation, usage, source, cost, status, context)
        stored = self._ledger.commit(
            reservation_id, record, actual_cost=cost,
            actual_tokens=usage.total_tokens if usage else None,
        )  # fmt: skip
        self._emit(context, "model.usage_recorded", "model.invocations")
        if usage is not None:
            self._metric(context, "model.tokens.input", float(usage.input_tokens))
            self._metric(context, "model.tokens.output", float(usage.output_tokens))
        if cost is not None:
            self._emit(context, "model.cost_calculated")
            self._metric(context, "model.cost.recorded", float(cost))
        else:
            self._emit(context, None, "model.accounting.unknown")
        return stored

    def record_ambiguous(self, reservation_id: str, *, context: AgentContext) -> UsageRecord:
        """Timeout/cancellation: usage and cost are unknown, the estimate stays held."""
        reservation = self._require(reservation_id)
        record = self._record(
            reservation, None, UsageSource.MISSING, None, AccountingStatus.UNRECONCILED, context
        )
        stored = self._ledger.commit(reservation_id, record, actual_cost=None, actual_tokens=None)
        self._emit(context, "model.reconciliation_required", "model.accounting.unknown")
        return stored

    def release(self, reservation_id: str, *, context: AgentContext) -> None:
        """Release a hold for a call known not to have executed."""
        self._require(reservation_id)
        self._ledger.release(reservation_id)

    def reconcile(
        self,
        reservation_id: str,
        *,
        executed: bool,
        usage: TokenUsage | None = None,
        billed_cost: Decimal | None = None,
        context: AgentContext,
    ) -> UsageRecord:
        """Settle an unreconciled call from authoritative execution or billing information."""
        reservation = self._require(reservation_id)
        if not executed:
            record = self._record(
                reservation, None, UsageSource.MISSING, None, AccountingStatus.RELEASED, context
            )
            return self._ledger.reconcile(
                reservation_id, record, actual_cost=None, actual_tokens=None
            )
        cost = billed_cost
        if cost is None and usage is not None and reservation.pricing is not None:
            cost = calculate_cost(usage, reservation.pricing, reservation.currency)
        if cost is None or usage is None:
            raise ValueError("reconciling an executed call needs usage and a cost")
        record = self._record(
            reservation,
            usage,
            UsageSource.PROVIDER_REPORTED,
            cost,
            AccountingStatus.COMMITTED,
            context,
        )
        self._emit(context, "model.cost_calculated")
        return self._ledger.reconcile(
            reservation_id, record, actual_cost=cost, actual_tokens=usage.total_tokens
        )

    # -- helpers ---------------------------------------------------------------------------

    def _require(self, reservation_id: str) -> Reservation:
        reservation = self._ledger.get(reservation_id)
        if reservation is None:
            raise ValueError("unknown reservation")
        return reservation

    def _record(self, r: Reservation, usage, source, cost, status, context) -> UsageRecord:
        return UsageRecord(
            usage_id="use-" + _hash(r.reservation_id, status.value),
            initiative_id=r.initiative_id,
            request_id=context.request_id,
            correlation_id=context.correlation_id,
            task_id=context.task_id,
            role=r.role,
            deployment_id=r.deployment_id,
            registry_revision=r.registry_revision,
            invocation_ref=r.invocation_ref,
            usage=usage,
            source=source,
            pricing_effective_date=r.pricing.effective_date if r.pricing else None,
            estimated_cost=r.estimated_cost,
            actual_cost=cost,
            currency=r.currency,
            recorded_at=self._now(),
            status=status,
        )

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise ValueError("clock must be timezone-aware")
        return value.astimezone(UTC)

    def _report_admission(self, d: AdmissionDecision, context: AgentContext) -> None:
        self._emit(context, "model.budget_checked")
        if d.status is AdmissionStatus.BLOCKED_BUDGET or d.budget_state is BudgetState.EXHAUSTED:
            self._emit(context, "model.budget_exhausted", "model.budget.exhausted")
        if d.status is AdmissionStatus.BLOCKED_QUOTA:
            self._emit(context, "model.quota_exhausted", "model.quota.exhausted")
        if d.status in (AdmissionStatus.DEPLOYMENT_UNAVAILABLE, AdmissionStatus.REGISTRY_CHANGED):
            self._emit(context, "model.deployment_unavailable")
        if d.status is AdmissionStatus.ADMITTED:
            self._emit(context, "model.reservation_created", "model.reservations")
        if d.utilization is not None:
            self._metric(context, "model.budget.utilization", d.utilization)
        if d.estimated_cost is not None:
            self._metric(context, "model.cost.estimated", float(d.estimated_cost))

    def _emit(self, context: AgentContext, event: str | None, *counters: str) -> None:
        if self._telemetry is None:
            return
        try:
            if event is not None:
                self._telemetry.record_event(event, context=context)
            for name in counters:
                self._telemetry.record_metric(name, 1.0, context=context)
        except Exception:
            return

    def _metric(self, context: AgentContext, name: str, value: float) -> None:
        if self._telemetry is None:
            return
        try:
            self._telemetry.record_metric(name, value, context=context)
        except Exception:
            return


def _lowest(*values: Decimal | None) -> Decimal | None:
    present = [v for v in values if v is not None]
    return min(present) if present else None
