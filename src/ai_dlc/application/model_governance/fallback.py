"""Deterministic, requirement-preserving fallback planning. Pure: no model is invoked.

Candidates must satisfy the *same* effective requirements and role as the original request; a
fallback can never weaken data, provider, region, capability, cost, latency or reviewer
independence rules, and the role (so the reasoning tier) is never changed. No LLM chooses.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from ai_dlc.application.agent_harness import (
    ErrorClass,
    ErrorClassifier,
    ExecutionResult,
    ExecutionStatus,
    ModelRole,
    ModelSelectionRequest,
    RoleProfiles,
)
from ai_dlc.application.model_registry import ModelRegistryReader, RegisteredModel
from ai_dlc.application.review import ModelProvenance, ReviewRequirements, eligible_reviewers
from ai_dlc.domain.initiative.enums import FallbackFailure, FallbackOrdering
from ai_dlc.domain.initiative.models import FallbackPolicy

from .cost import estimate_cost

_UNLISTED_PRIORITY = 10_001


class ModelFailure(StrEnum):
    TRANSIENT = "transient"
    THROTTLED = "throttled"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    TIMEOUT = "timeout"  # outcome uncertain
    CANCELLED = "cancelled"
    PERMANENT_MODEL_ERROR = "permanent_model_error"
    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    INVALID_OUTPUT = "invalid_output"
    CONTEXT_CAPACITY = "context_capacity"
    POLICY_VIOLATION = "policy_violation"
    BUDGET_EXHAUSTED = "budget_exhausted"
    QUOTA_EXHAUSTED = "quota_exhausted"


# Never eligible for fallback, whatever the initiative configures.
NEVER_FALLBACK = frozenset(
    {
        ModelFailure.AUTHENTICATION,
        ModelFailure.AUTHORIZATION,
        ModelFailure.POLICY_VIOLATION,
        ModelFailure.BUDGET_EXHAUSTED,
        ModelFailure.QUOTA_EXHAUSTED,
        ModelFailure.TIMEOUT,
        ModelFailure.CANCELLED,
    }
)
_CONFIGURABLE = {item.value: ModelFailure(item.value) for item in FallbackFailure}
_THROTTLE = frozenset({"rate_limited", "throttled", "too_many_requests"})
_UNAVAILABLE = frozenset({"provider_unavailable", "remote_unavailable", "model_overloaded"})
_AUTHENTICATION = frozenset({"unauthenticated", "authentication_failed", "invalid_credentials"})
_POLICY = frozenset({"policy_violation", "content_policy_violation"})


def classify_model_failure(
    result: ExecutionResult, classifier: ErrorClassifier | None = None
) -> ModelFailure:
    """Map a failed result to a failure category using the harness error taxonomy."""
    if result.status is ExecutionStatus.SUCCEEDED:
        raise ValueError("a successful result is not a failure")
    code = result.error.code.lower() if result.error is not None else ""
    if code in _AUTHENTICATION:
        return ModelFailure.AUTHENTICATION
    if code in _POLICY:
        return ModelFailure.POLICY_VIOLATION
    if code in _THROTTLE:
        return ModelFailure.THROTTLED
    if code in _UNAVAILABLE:
        return ModelFailure.PROVIDER_UNAVAILABLE
    if code == "context_length_exceeded":
        return ModelFailure.CONTEXT_CAPACITY
    kind = (classifier or ErrorClassifier()).classify(result)
    return {
        ErrorClass.TRANSIENT: ModelFailure.TRANSIENT,
        ErrorClass.TIMEOUT: ModelFailure.TIMEOUT,
        ErrorClass.AMBIGUOUS: ModelFailure.TIMEOUT,
        ErrorClass.CANCELLATION: ModelFailure.CANCELLED,
        ErrorClass.AUTHORIZATION: ModelFailure.AUTHORIZATION,
        ErrorClass.APPROVAL_REQUIRED: ModelFailure.POLICY_VIOLATION,
        ErrorClass.VALIDATION: ModelFailure.INVALID_OUTPUT,
    }.get(kind, ModelFailure.PERMANENT_MODEL_ERROR)


def fallback_permitted(failure: ModelFailure, policy: FallbackPolicy) -> bool:
    if not policy.enabled or failure in NEVER_FALLBACK:
        return False
    return any(_CONFIGURABLE[item.value] is failure for item in policy.failure_categories)


@dataclass(frozen=True, slots=True)
class ReviewerConstraints:
    """Independence inputs from AIDLC-51; required when planning for the reviewer role."""

    generator: ModelProvenance | None
    requirements: ReviewRequirements


@dataclass(frozen=True, slots=True)
class CandidatePlan:
    """Ordered, eligible candidates plus explicit rejections. Evaluated, not yet dispatched."""

    candidates: tuple[RegisteredModel, ...]
    rejected: tuple[tuple[str, tuple[str, ...]], ...]


class FallbackPlanner:
    def __init__(self, registry: ModelRegistryReader, profiles: RoleProfiles) -> None:
        self._registry = registry
        self._profiles = profiles

    def plan(
        self,
        selection: ModelSelectionRequest,
        policy: FallbackPolicy,
        *,
        estimated_input_tokens: int,
        max_output_tokens: int,
        currency: str = "USD",
        exclude: frozenset[str] = frozenset(),
        reviewer: ReviewerConstraints | None = None,
    ) -> CandidatePlan:
        """Eligible candidates in governed order, excluding already-attempted deployments."""
        rejected: list[tuple[str, tuple[str, ...]]] = []
        if selection.role is ModelRole.INDEPENDENT_REVIEWER:
            if reviewer is None:
                raise ValueError("reviewer selection needs independence constraints")
            found = eligible_reviewers(
                self._registry, selection, self._profiles, reviewer.generator, reviewer.requirements
            )
            eligible = list(found.eligible)
            rejected.extend(found.rejected)
        else:
            eligible = list(self._registry.query_eligible(selection, self._profiles))
            listed = {m.deployment_id for m in eligible}
            for result in self._registry.evaluate(selection, self._profiles):
                if result.deployment_id not in listed:
                    rejected.append((result.deployment_id, tuple(r.value for r in result.reasons)))
        kept = []
        for model in eligible:
            if model.deployment_id in exclude:
                rejected.append((model.deployment_id, ("already_attempted",)))
            else:
                kept.append(model)
        kept.sort(
            key=lambda m: self._order_key(
                m, policy, estimated_input_tokens, max_output_tokens, currency
            )
        )
        return CandidatePlan(tuple(kept), tuple(sorted(rejected)))

    @staticmethod
    def _order_key(
        model: RegisteredModel,
        policy: FallbackPolicy,
        tokens_in: int,
        tokens_out: int,
        currency: str,
    ) -> tuple:
        priorities = {item.deployment_id: item.priority for item in policy.priorities}
        spec = model.spec
        parts: list[tuple] = []
        for key in policy.ordering:
            if key is FallbackOrdering.PRIORITY:
                parts.append((priorities.get(spec.deployment_id, _UNLISTED_PRIORITY),))
            elif key is FallbackOrdering.COST:
                cost = estimate_cost(tokens_in, tokens_out, spec.pricing, currency)
                # Unknown pricing sorts after every known price: it is never treated as free.
                parts.append((1, Decimal(0)) if cost is None else (0, cost))
            else:
                latency = spec.latency.typical_latency_ms
                # Unknown latency is never treated as optimal.
                parts.append((1, 0) if latency is None else (0, latency))
        parts.append((spec.deployment_id,))  # stable tie-breaker
        return tuple(parts)
