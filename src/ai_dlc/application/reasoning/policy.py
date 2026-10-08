"""Deterministic reasoning-tier policy. Pure function of trusted inputs; invokes no model.

Selects the *logical* tier (``STANDARD_REASONING`` or ``DEEP_REASONING``). Choosing a concrete
deployment is the registry/router's job, and a deep tier is never downgraded automatically.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from ai_dlc.application.agent_harness import (
    AgentContext,
    ErrorClass,
    ErrorClassifier,
    ExecutionResult,
    ExecutionStatus,
    ModelRole,
    TelemetryProvider,
)
from ai_dlc.domain.initiative import InitiativeProfile
from ai_dlc.domain.initiative.models import ReasoningPolicy

from .models import (
    FAIL_CLOSED_FAILURES,
    DecisionKind,
    ReasoningDecision,
    ReasoningFailure,
    ReasoningSignals,
    RejectionReason,
    TriggerCode,
)

STANDARD = ModelRole.STANDARD_REASONING
DEEP = ModelRole.DEEP_REASONING
_THROTTLE_CODES = frozenset({"rate_limited", "throttled", "too_many_requests"})
_AUTHENTICATION_CODES = frozenset(
    {"unauthenticated", "authentication_failed", "invalid_credentials"}
)


def classify_failure(
    result: ExecutionResult, classifier: ErrorClassifier | None = None
) -> ReasoningFailure:
    """Map an execution result to a non-escalating failure kind using the harness taxonomy.

    ``INSUFFICIENT_REASONING`` is never returned here: only trusted execution logic that has
    evaluated answer quality may assign it. Model-claimed tier requests are not consulted.
    """
    if result.status is ExecutionStatus.SUCCEEDED:
        raise ValueError("a successful result is not a failure")
    code = result.error.code.lower() if result.error is not None else ""
    if code in _AUTHENTICATION_CODES:
        return ReasoningFailure.AUTHENTICATION
    if code in _THROTTLE_CODES:
        return ReasoningFailure.THROTTLED
    if code == "context_length_exceeded":
        return ReasoningFailure.CONTEXT_LIMITATION
    kind = (classifier or ErrorClassifier()).classify(result)
    return {
        ErrorClass.TRANSIENT: ReasoningFailure.TRANSIENT,
        ErrorClass.TIMEOUT: ReasoningFailure.TIMEOUT,
        ErrorClass.AUTHORIZATION: ReasoningFailure.AUTHORIZATION,
        ErrorClass.APPROVAL_REQUIRED: ReasoningFailure.POLICY_VALIDATION,
        ErrorClass.VALIDATION: ReasoningFailure.INVALID_OUTPUT,
        ErrorClass.CANCELLATION: ReasoningFailure.CANCELLED,
    }.get(kind, ReasoningFailure.UNKNOWN)


def evaluate_triggers(
    signals: ReasoningSignals, policy: ReasoningPolicy
) -> tuple[TriggerCode, ...]:
    """Ordered, deduplicated trigger codes; each rule is a plain threshold comparison."""
    found: set[TriggerCode] = set()
    if policy.always_deep:
        found.add(TriggerCode.EXPLICIT_GOVERNED_POLICY)
    if signals.requested_role is DEEP:
        found.add(TriggerCode.EXPLICIT_REQUEST)
    if (
        policy.deep_reasoning_level is not None
        and signals.required_reasoning_level >= policy.deep_reasoning_level
    ) or (signals.required_capabilities & set(policy.deep_capabilities)):
        found.add(TriggerCode.CAPABILITY_REQUIREMENT)
    threshold = policy.context_size_threshold_tokens
    if (
        threshold is not None
        and signals.estimated_context_tokens is not None
        and signals.estimated_context_tokens >= threshold
    ):
        found.add(TriggerCode.CONTEXT_SIZE_THRESHOLD)
    score = policy.complexity_score_threshold
    if (
        score is not None
        and signals.complexity_score is not None
        and signals.complexity_score >= score
    ):
        found.add(TriggerCode.TASK_COMPLEXITY_THRESHOLD)
    for value, limit in (
        (signals.affected_components, policy.affected_components_threshold),
        (signals.repositories, policy.repositories_threshold),
        (signals.dependency_relationships, policy.dependency_relationships_threshold),
    ):
        if limit is not None and value >= limit:
            found.add(TriggerCode.TASK_BREADTH_THRESHOLD)
    limit = policy.failed_attempt_threshold
    if limit is not None and signals.insufficient_reasoning_attempts >= limit:
        found.add(TriggerCode.FAILED_ATTEMPT_THRESHOLD)
    return tuple(code for code in TriggerCode if code in found)


class ReasoningTierPolicy:
    def __init__(
        self,
        telemetry: TelemetryProvider | None = None,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._telemetry = telemetry
        self._clock = clock

    def evaluate(
        self,
        signals: ReasoningSignals,
        *,
        context: AgentContext,
        profile: InitiativeProfile,
        initiative_revision: int,
    ) -> ReasoningDecision:
        """Decide the tier from trusted signals and the initiative's policy.

        The context and profile are read only. The escalation history in ``signals`` is supplied
        by the caller (durable task state is future work), so it must come from trusted storage.
        """
        if not isinstance(signals, ReasoningSignals) or not isinstance(context, AgentContext):
            raise TypeError("ReasoningSignals and trusted AgentContext required")
        if type(initiative_revision) is not int or initiative_revision < 1:
            raise ValueError("initiative_revision must be a positive integer")
        decision = self._decide(signals, context, profile, initiative_revision)
        self._emit(decision, signals, context)
        return decision

    def _decide(
        self,
        signals: ReasoningSignals,
        context: AgentContext,
        profile: InitiativeProfile,
        revision: int,
    ) -> ReasoningDecision:
        policy = profile.reasoning
        triggers: tuple[TriggerCode, ...] = ()
        prior = signals.prior_role

        def make(
            kind: DecisionKind,
            selected: ModelRole | None,
            rejection: RejectionReason | None = None,
            count: int | None = None,
        ) -> ReasoningDecision:
            return ReasoningDecision(
                kind=kind,
                selected_role=selected,
                previous_role=prior,
                requested_role=signals.requested_role,
                escalated=kind is DecisionKind.ESCALATED,
                trigger_codes=triggers,
                rejection_reason=rejection,
                escalation_count=signals.escalation_count if count is None else count,
                policy_revision=revision,
                initiative_id=context.authorization.initiative_id,
                request_id=context.request_id,
                correlation_id=context.correlation_id,
                trace_id=context.trace_id,
                task_id=context.task_id,
                decision_timestamp=self._now(),
            )

        if profile.initiative.id != context.authorization.initiative_id:
            return make(DecisionKind.BLOCKED, None, RejectionReason.POLICY_INITIATIVE_MISMATCH)
        if not signals.history_valid():
            return make(DecisionKind.BLOCKED, None, RejectionReason.INVALID_HISTORY)
        if signals.failures and signals.failures[-1] in FAIL_CLOSED_FAILURES:
            return make(DecisionKind.BLOCKED, None, RejectionReason.FAILURE_NOT_ESCALATABLE)

        triggers = evaluate_triggers(signals, policy)

        if prior is DEEP:
            if not policy.deep_reasoning_allowed:
                return make(DecisionKind.BLOCKED, None, RejectionReason.DEEP_NOT_ALLOWED)
            # Highest defined tier: nothing above it, and no automatic downgrade.
            return make(DecisionKind.RETAINED, DEEP)

        if prior is None:
            if not triggers:
                return make(DecisionKind.STANDARD_SELECTED, STANDARD)
            if not policy.deep_reasoning_allowed:
                return make(
                    DecisionKind.STANDARD_SELECTED, STANDARD, RejectionReason.DEEP_NOT_ALLOWED
                )
            return make(DecisionKind.DEEP_SELECTED, DEEP)

        # prior is STANDARD
        if not triggers:
            return make(DecisionKind.RETAINED, STANDARD)
        if not policy.deep_reasoning_allowed:
            return make(
                DecisionKind.ESCALATION_REJECTED, STANDARD, RejectionReason.DEEP_NOT_ALLOWED
            )
        if not policy.escalation_enabled:
            return make(
                DecisionKind.ESCALATION_REJECTED, STANDARD, RejectionReason.ESCALATION_DISABLED
            )
        if signals.escalation_count >= policy.max_escalations_per_task:
            return make(
                DecisionKind.ESCALATION_REJECTED, STANDARD, RejectionReason.ESCALATION_LIMIT_REACHED
            )
        return make(DecisionKind.ESCALATED, DEEP, count=signals.escalation_count + 1)

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise ValueError("clock must be timezone-aware")
        return value.astimezone(UTC)

    def _emit(
        self, decision: ReasoningDecision, signals: ReasoningSignals, context: AgentContext
    ) -> None:
        """Codes and counts only; failures are swallowed and never alter a decision."""
        if self._telemetry is None:
            return
        events: list[str] = []
        metrics: list[str] = []
        kind = decision.kind
        attempted = kind in (DecisionKind.ESCALATED, DecisionKind.ESCALATION_REJECTED)
        if attempted:
            events.append("reasoning.escalation_requested")
        if kind is DecisionKind.ESCALATED:
            events += ["reasoning.escalation_approved", "reasoning.deep_selected"]
            metrics += ["reasoning.escalations", "reasoning.decisions.deep"]
        elif kind is DecisionKind.ESCALATION_REJECTED:
            events.append("reasoning.escalation_rejected")
            metrics += ["reasoning.escalation_rejections", "reasoning.decisions.standard"]
            if decision.rejection_reason is RejectionReason.ESCALATION_LIMIT_REACHED:
                events.append("reasoning.escalation_limit_reached")
        elif kind is DecisionKind.BLOCKED:
            events.append("reasoning.blocked")
        elif decision.selected_role is DEEP:
            events.append("reasoning.deep_selected")
            metrics.append("reasoning.decisions.deep")
        else:
            events.append("reasoning.standard_selected")
            metrics.append("reasoning.decisions.standard")
        metrics += [f"reasoning.trigger.{code.value}" for code in decision.trigger_codes]
        try:
            for name in events:
                self._telemetry.record_event(name, context=context)
            for name in metrics:
                self._telemetry.record_metric(name, 1.0, context=context)
        except Exception:
            return
