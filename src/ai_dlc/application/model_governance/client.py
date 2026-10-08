"""Governed execution loop: admit, invoke through ModelProvider, record usage, fall back.

This is the only place that calls ``ModelProvider``. It defers every policy decision to the
admission service and the fallback planner, never retries without bound, and never falls back
after authorization, policy, budget or quota failures or an uncertain outcome.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from pydantic import ValidationError

from ai_dlc.application.agent_harness import (
    AgentContext,
    ExecutionResult,
    ExecutionStatus,
    Invocation,
    ModelProvider,
    ModelSelectionRequest,
    OperationCategory,
    OperationSpec,
    ResilientExecutor,
    RetryPolicy,
    TelemetryProvider,
)
from ai_dlc.application.agent_harness.models import ContractModel
from ai_dlc.domain.initiative import InitiativeProfile

from .fallback import (
    FallbackPlanner,
    ModelFailure,
    ReviewerConstraints,
    classify_model_failure,
    fallback_permitted,
)
from .models import (
    AdmissionDecision,
    AdmissionRequest,
    AdmissionStatus,
    TokenUsage,
    UsageRecord,
)
from .service import ModelGovernanceService

_MAX_ITERATIONS = 10


class GovernedOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"  # budget or quota
    APPROVAL_REQUIRED = "approval_required"
    NO_ELIGIBLE_DEPLOYMENT = "no_eligible_deployment"
    AMBIGUOUS_OUTCOME = "ambiguous_outcome"  # reconciliation required


class AttemptRecord(ContractModel):
    deployment_id: str
    registry_revision: int | None = None
    outcome: str
    failure: ModelFailure | None = None
    reservation_id: str | None = None


class GovernedResult(ContractModel):
    outcome: GovernedOutcome
    result: ExecutionResult | None = None
    attempts: tuple[AttemptRecord, ...] = ()
    usage: tuple[UsageRecord, ...] = ()
    admission: AdmissionDecision | None = None


class GovernedModelClient:
    def __init__(
        self,
        provider: ModelProvider,
        governance: ModelGovernanceService,
        planner: FallbackPlanner,
        *,
        executor: ResilientExecutor | None = None,
        telemetry: TelemetryProvider | None = None,
        inner_attempts: int = 1,
    ) -> None:
        self._provider = provider
        self._governance = governance
        self._planner = planner
        # One provider attempt per candidate by default; fallback is the retry mechanism.
        self._executor = executor or ResilientExecutor(
            {OperationCategory.MODEL_INVOCATION: RetryPolicy(max_attempts=inner_attempts)}
        )
        self._telemetry = telemetry
        self._inner_attempts = inner_attempts

    async def invoke(
        self,
        selection: ModelSelectionRequest,
        invocation: Invocation,
        *,
        context: AgentContext,
        profile: InitiativeProfile,
        initiative_revision: int,
        estimated_input_tokens: int,
        max_output_tokens: int,
        invocation_ref: str,
        reviewer: ReviewerConstraints | None = None,
        request_cost_limit: Decimal | None = None,
        approval_id: str | None = None,
    ) -> GovernedResult:
        policy = profile.inference_controls.fallback
        currency = profile.inference_controls.budget.currency
        dispatch_limit = 1 + (policy.max_fallback_attempts if policy.enabled else 0)
        attempted: set[str] = set()
        attempts: list[AttemptRecord] = []
        usage: list[UsageRecord] = []
        dispatches = 0
        last_admission: AdmissionDecision | None = None
        last_failed: ExecutionResult | None = None
        for _ in range(_MAX_ITERATIONS):
            plan = self._planner.plan(
                selection, policy,
                estimated_input_tokens=estimated_input_tokens,
                max_output_tokens=max_output_tokens,
                currency=currency, exclude=frozenset(attempted), reviewer=reviewer,
            )  # fmt: skip
            if not plan.candidates:
                if dispatches:
                    self._emit(context, "model.fallback_rejected", "model.fallback.rejections")
                if (
                    last_failed is not None
                ):  # a dispatched attempt failed and nothing else qualifies
                    return self._done(
                        GovernedOutcome.FAILED, attempts, usage, last_admission, last_failed
                    )
                return self._done(
                    GovernedOutcome.NO_ELIGIBLE_DEPLOYMENT, attempts, usage, last_admission
                )
            candidate = plan.candidates[0]
            attempted.add(candidate.deployment_id)  # no deployment is ever tried twice
            admission = await self._governance.admit(
                AdmissionRequest(
                    selection=selection,
                    deployment_id=candidate.deployment_id,
                    evaluated_revision=candidate.revision,
                    estimated_input_tokens=estimated_input_tokens,
                    max_output_tokens=max_output_tokens,
                    invocation_ref=invocation_ref,
                    request_cost_limit=request_cost_limit,
                    approval_id=approval_id,
                    inner_attempts=self._inner_attempts,
                ),
                context=context, profile=profile, initiative_revision=initiative_revision,
            )  # fmt: skip
            last_admission = admission
            if admission.status in (
                AdmissionStatus.REGISTRY_CHANGED,
                AdmissionStatus.DEPLOYMENT_UNAVAILABLE,
            ):
                attempts.append(AttemptRecord(
                    deployment_id=candidate.deployment_id, outcome=admission.status.value,
                    registry_revision=candidate.revision))  # fmt: skip
                continue  # reselect; nothing was dispatched
            if not admission.admitted:
                outcome = {
                    AdmissionStatus.APPROVAL_REQUIRED: GovernedOutcome.APPROVAL_REQUIRED,
                    AdmissionStatus.BLOCKED_BUDGET: GovernedOutcome.BLOCKED,
                    AdmissionStatus.BLOCKED_QUOTA: GovernedOutcome.BLOCKED,
                }.get(admission.status, GovernedOutcome.FAILED)
                attempts.append(AttemptRecord(
                    deployment_id=candidate.deployment_id, outcome=admission.status.value,
                    registry_revision=candidate.revision))  # fmt: skip
                return self._done(outcome, attempts, usage, admission)  # never falls back

            dispatches += 1
            call = invocation.model_copy(
                update={
                    "metadata": {
                        **(invocation.metadata or {}),
                        "deployment_id": candidate.deployment_id,
                        "registry_revision": candidate.revision,
                    }
                }
            )
            result = await self._executor.run(
                lambda _, call=call: self._provider.invoke_model(
                    selection.role.value, call, context=context
                ),
                spec=OperationSpec(OperationCategory.MODEL_INVOCATION),
                context=context,
            )
            rid = admission.reservation_id
            if result.status is ExecutionStatus.SUCCEEDED:
                record = self._governance.record_usage(rid, _usage_of(result), context=context)
                usage.append(record)
                attempts.append(AttemptRecord(
                    deployment_id=candidate.deployment_id, outcome="succeeded",
                    registry_revision=candidate.revision, reservation_id=rid))  # fmt: skip
                return self._done(GovernedOutcome.SUCCEEDED, attempts, usage, admission, result)

            last_failed = result
            failure = classify_model_failure(result)
            attempts.append(AttemptRecord(
                deployment_id=candidate.deployment_id, outcome="failed", failure=failure,
                registry_revision=candidate.revision, reservation_id=rid))  # fmt: skip
            if failure in (ModelFailure.TIMEOUT, ModelFailure.CANCELLED):
                # Possibly executed: keep the hold, do not refund, retry or fall back.
                usage.append(self._governance.record_ambiguous(rid, context=context))
                return self._done(
                    GovernedOutcome.AMBIGUOUS_OUTCOME, attempts, usage, admission, result
                )
            reported = _usage_of(result)
            if reported is not None:
                usage.append(self._governance.record_usage(rid, reported, context=context))
            else:
                self._governance.release(rid, context=context)  # definitive failure, no usage
            if not fallback_permitted(failure, policy) or dispatches >= dispatch_limit:
                if policy.enabled and failure not in (ModelFailure.TIMEOUT,):
                    self._emit(context, "model.fallback_rejected", "model.fallback.rejections")
                return self._done(GovernedOutcome.FAILED, attempts, usage, admission, result)
            self._emit(context, "model.fallback_considered")
            self._emit(context, "model.fallback_selected", "model.fallback.count")
        return self._done(GovernedOutcome.FAILED, attempts, usage, last_admission)

    @staticmethod
    def _done(outcome, attempts, usage, admission, result=None) -> GovernedResult:
        return GovernedResult(
            outcome=outcome,
            result=result,
            attempts=tuple(attempts),
            usage=tuple(usage),
            admission=admission,
        )

    def _emit(self, context: AgentContext, event: str, *counters: str) -> None:
        if self._telemetry is None:
            return
        try:
            self._telemetry.record_event(event, context=context)
            for name in counters:
                self._telemetry.record_metric(name, 1.0, context=context)
        except Exception:
            return


def _usage_of(result: ExecutionResult) -> TokenUsage | None:
    """Provider-reported usage from trusted result metadata; invalid or absent means missing."""
    raw = (result.metadata or {}).get("usage")
    if not isinstance(raw, dict):
        return None
    try:
        return TokenUsage.model_validate(raw, strict=True)
    except ValidationError:
        return None
