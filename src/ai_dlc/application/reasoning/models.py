"""Signals and decision records for reasoning-tier selection (standard vs deep).

Tiers are the SDK's logical ``ModelRole`` values. Nothing here names a model, provider or
deployment, and a decision never proves that a model was invoked.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import JsonValue

from ai_dlc.application.agent_harness import ModelRole, ReasoningLevel
from ai_dlc.domain.identity import Capability

TIERS = (ModelRole.STANDARD_REASONING, ModelRole.DEEP_REASONING)
MAX_FAILURES = 50
MAX_ESCALATION_HISTORY = 10
MAX_TOKENS = 100_000_000
MAX_COUNT = 1_000_000


class ReasoningFailure(StrEnum):
    """Classification of a past attempt, assigned by trusted execution logic only."""

    TRANSIENT = "transient"
    THROTTLED = "throttled"
    TIMEOUT = "timeout"
    INVALID_OUTPUT = "invalid_output"
    CONTEXT_LIMITATION = "context_limitation"
    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    POLICY_VALIDATION = "policy_validation"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"
    INSUFFICIENT_REASONING = "insufficient_reasoning"  # the only kind that counts toward escalation


FAIL_CLOSED_FAILURES = frozenset(
    {
        ReasoningFailure.AUTHENTICATION,
        ReasoningFailure.AUTHORIZATION,
        ReasoningFailure.POLICY_VALIDATION,
    }
)


class TriggerCode(StrEnum):
    """Stable reason codes; the enum order is the deterministic reporting order."""

    EXPLICIT_GOVERNED_POLICY = "explicit_governed_policy"
    EXPLICIT_REQUEST = "explicit_request"
    CAPABILITY_REQUIREMENT = "capability_requirement"
    CONTEXT_SIZE_THRESHOLD = "context_size_threshold"
    TASK_COMPLEXITY_THRESHOLD = "task_complexity_threshold"
    TASK_BREADTH_THRESHOLD = "task_breadth_threshold"
    FAILED_ATTEMPT_THRESHOLD = "failed_attempt_threshold"


class DecisionKind(StrEnum):
    STANDARD_SELECTED = "standard_selected"  # initial selection
    DEEP_SELECTED = "deep_selected"  # initial selection; not an escalation
    ESCALATED = "escalated"  # standard -> deep after a standard tier was in use
    RETAINED = "retained"  # prior tier kept
    ESCALATION_REJECTED = "escalation_rejected"
    BLOCKED = "blocked"  # fail closed; no tier selected


class RejectionReason(StrEnum):
    ESCALATION_DISABLED = "escalation_disabled"
    DEEP_NOT_ALLOWED = "deep_not_allowed"
    ESCALATION_LIMIT_REACHED = "escalation_limit_reached"
    FAILURE_NOT_ESCALATABLE = "failure_not_escalatable"
    POLICY_INITIATIVE_MISMATCH = "policy_initiative_mismatch"
    INVALID_HISTORY = "invalid_history"


class ExecutionObservation(StrEnum):
    NOT_OBSERVED = "not_observed"  # policy decided; no invocation has been recorded
    INVOKED = "invoked"


def _count(value: object, name: str, limit: int = MAX_COUNT) -> None:
    if type(value) is not int or not 0 <= value <= limit:
        raise ValueError(f"{name} must be an integer between 0 and {limit}")


@dataclass(frozen=True, slots=True)
class ReasoningSignals:
    """Measurable task properties. Callers must derive them from trusted instrumentation.

    Never populate any field from user text, model output or model-claimed "needs more
    reasoning" statements. ``failures`` are classified by trusted execution logic.
    """

    estimated_context_tokens: int | None = None
    affected_components: int = 0
    repositories: int = 0
    dependency_relationships: int = 0
    complexity_score: float | None = None
    required_reasoning_level: ReasoningLevel = ReasoningLevel.NONE
    required_capabilities: frozenset[Capability] = frozenset()
    failures: tuple[ReasoningFailure, ...] = ()
    requested_role: ModelRole | None = None
    prior_role: ModelRole | None = None
    escalation_count: int = 0

    def __post_init__(self) -> None:
        if self.estimated_context_tokens is not None:
            _count(self.estimated_context_tokens, "estimated_context_tokens", MAX_TOKENS)
        for name in ("affected_components", "repositories", "dependency_relationships"):
            _count(getattr(self, name), name)
        if self.complexity_score is not None and (
            isinstance(self.complexity_score, bool)
            or not isinstance(self.complexity_score, (int, float))
            or not math.isfinite(self.complexity_score)
            or not 0 <= self.complexity_score <= 1
        ):
            raise ValueError("complexity_score must be finite and between 0 and 1")
        object.__setattr__(
            self, "required_reasoning_level", ReasoningLevel(self.required_reasoning_level)
        )
        caps = frozenset(self.required_capabilities)
        if any(not isinstance(item, Capability) for item in caps):
            raise ValueError("unknown required capability")
        object.__setattr__(self, "required_capabilities", caps)
        failures = tuple(ReasoningFailure(item) for item in self.failures)
        if len(failures) > MAX_FAILURES:
            raise ValueError("too many failure records")
        object.__setattr__(self, "failures", failures)
        for name in ("requested_role", "prior_role"):
            role = getattr(self, name)
            if role is not None:
                if ModelRole(role) not in TIERS:
                    raise ValueError(f"{name} must be a reasoning tier")
                object.__setattr__(self, name, ModelRole(role))
        _count(self.escalation_count, "escalation_count", MAX_ESCALATION_HISTORY)

    def history_valid(self) -> bool:
        """An escalation implies the task is now on the deep tier; counts cannot be invented."""
        if self.escalation_count == 0:
            return True
        return self.prior_role is ModelRole.DEEP_REASONING

    @property
    def insufficient_reasoning_attempts(self) -> int:
        return sum(1 for item in self.failures if item is ReasoningFailure.INSUFFICIENT_REASONING)


@dataclass(frozen=True, slots=True)
class ReasoningDecision:
    """Serializable, secret-free record of a tier decision (suitable for task-state storage)."""

    kind: DecisionKind
    selected_role: ModelRole | None
    previous_role: ModelRole | None
    requested_role: ModelRole | None
    escalated: bool
    trigger_codes: tuple[TriggerCode, ...]
    rejection_reason: RejectionReason | None
    escalation_count: int
    policy_revision: int
    initiative_id: str
    request_id: str
    correlation_id: str
    trace_id: str
    task_id: str | None
    decision_timestamp: datetime
    execution_status: ExecutionObservation = ExecutionObservation.NOT_OBSERVED
    invoked_role: ModelRole | None = None

    def __post_init__(self) -> None:
        if self.decision_timestamp.utcoffset() != timedelta(0):
            raise ValueError("decision_timestamp must be timezone-aware UTC")
        if (self.selected_role is None) != (self.kind is DecisionKind.BLOCKED):
            raise ValueError("a tier is selected unless the decision is blocked")
        if self.escalated != (self.kind is DecisionKind.ESCALATED):
            raise ValueError("escalated is true exactly for escalation decisions")
        if (self.execution_status is ExecutionObservation.INVOKED) != (
            self.invoked_role is not None
        ):
            raise ValueError("invoked_role is set exactly when execution is observed")

    def record_invocation(self, invoked_role: ModelRole) -> ReasoningDecision:
        """Trusted execution logic records which tier was actually invoked (may differ)."""
        if ModelRole(invoked_role) not in TIERS:
            raise ValueError("invoked_role must be a reasoning tier")
        return replace(
            self,
            execution_status=ExecutionObservation.INVOKED,
            invoked_role=ModelRole(invoked_role),
        )

    def to_dict(self) -> dict[str, JsonValue]:
        def role(value: ModelRole | None) -> str | None:
            return value.value if value is not None else None

        return {
            "kind": self.kind.value,
            "selected_role": role(self.selected_role),
            "previous_role": role(self.previous_role),
            "requested_role": role(self.requested_role),
            "escalated": self.escalated,
            "trigger_codes": [item.value for item in self.trigger_codes],
            "rejection_reason": self.rejection_reason.value if self.rejection_reason else None,
            "escalation_count": self.escalation_count,
            "policy_revision": self.policy_revision,
            "initiative_id": self.initiative_id,
            "request_id": self.request_id,
            "correlation_id": self.correlation_id,
            "trace_id": self.trace_id,
            "task_id": self.task_id,
            "decision_timestamp": self.decision_timestamp.isoformat(),
            "execution_status": self.execution_status.value,
            "invoked_role": role(self.invoked_role),
        }

    @classmethod
    def from_dict(cls, data: dict[str, JsonValue]) -> ReasoningDecision:
        def role(value: object) -> ModelRole | None:
            return ModelRole(value) if value is not None else None

        return cls(
            kind=DecisionKind(data["kind"]),
            selected_role=role(data["selected_role"]),
            previous_role=role(data["previous_role"]),
            requested_role=role(data["requested_role"]),
            escalated=bool(data["escalated"]),
            trigger_codes=tuple(TriggerCode(item) for item in data["trigger_codes"]),
            rejection_reason=RejectionReason(data["rejection_reason"])
            if data["rejection_reason"]
            else None,
            escalation_count=int(data["escalation_count"]),
            policy_revision=int(data["policy_revision"]),
            initiative_id=str(data["initiative_id"]),
            request_id=str(data["request_id"]),
            correlation_id=str(data["correlation_id"]),
            trace_id=str(data["trace_id"]),
            task_id=data["task_id"],
            decision_timestamp=datetime.fromisoformat(str(data["decision_timestamp"])),
            execution_status=ExecutionObservation(data["execution_status"]),
            invoked_role=role(data["invoked_role"]),
        )
