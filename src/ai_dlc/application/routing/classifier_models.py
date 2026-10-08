"""Classifier output schema and fallback result contracts. All model output is untrusted."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ai_dlc.domain.identity import Capability

from .models import RoutingDecision, RoutingRule

CLASSIFIER_SCHEMA_ID = "routing-classification-v1"
_CAPABILITY_VALUES = frozenset(item.value for item in Capability)
MAX_ALTERNATIVES = 5

ReasonCode = Literal[
    "clear_match", "partial_match", "multiple_matches", "insufficient_information", "out_of_scope"
]
_NO_CAPABILITY_REASONS = frozenset({"insufficient_information", "out_of_scope"})


class ClassifierOutput(BaseModel):
    """Exactly what the model may say. No identity, role, tool, endpoint or free-text fields.

    Capability names are plain strings checked against the platform ``Capability`` enum so the
    harness' strict JSON validation applies unchanged.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    capability: str | None
    confidence: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
    ambiguous: bool
    alternative_capabilities: list[str] = Field(default_factory=list, max_length=MAX_ALTERNATIVES)
    reason_code: ReasonCode

    @field_validator("capability")
    @classmethod
    def known_capability(cls, value: str | None) -> str | None:
        if value is not None and value not in _CAPABILITY_VALUES:
            raise ValueError("unknown capability")
        return value

    @field_validator("alternative_capabilities")
    @classmethod
    def known_unique_alternatives(cls, value: list[str]) -> list[str]:
        if any(item not in _CAPABILITY_VALUES for item in value):
            raise ValueError("unknown alternative capability")
        if len(set(value)) != len(value):
            raise ValueError("duplicate alternative capability")
        return value

    @model_validator(mode="after")
    def consistent(self) -> ClassifierOutput:
        if self.capability in self.alternative_capabilities:
            raise ValueError("selected capability repeated as alternative")
        if (self.capability is None) != (self.reason_code in _NO_CAPABILITY_REASONS):
            raise ValueError("reason code inconsistent with capability")
        if self.ambiguous and not self.alternative_capabilities and self.capability is not None:
            raise ValueError("ambiguous result needs alternatives")
        if not self.ambiguous and self.alternative_capabilities:
            raise ValueError("alternatives require ambiguous=true")
        return self


class ClassificationOutcome(StrEnum):
    CLASSIFIED = "classified"  # suggested, re-authorized and configuration-checked
    CLARIFICATION_REQUIRED = "clarification_required"
    DENIED = "denied"
    INVALID_OUTPUT = "invalid_output"
    MODEL_FAILED = "model_failed"
    CONFIGURATION_ERROR = "configuration_error"
    NOT_PERMITTED = "not_permitted"  # fallback gate closed; the model was not called


class ClassificationReason(StrEnum):
    ACCEPTED = "accepted"
    LOW_CONFIDENCE = "low_confidence"
    AMBIGUOUS = "ambiguous"
    NO_VALID_CAPABILITY = "no_valid_capability"
    NO_CONTENT = "no_content"
    CAPABILITY_NOT_AUTHORIZED = "capability_not_authorized"
    CAPABILITY_UNAVAILABLE = "capability_unavailable"
    NO_CLASSIFIABLE_CAPABILITIES = "no_classifiable_capabilities"
    INVALID_STRUCTURE = "invalid_structure"
    TOO_MANY_ALTERNATIVES = "too_many_alternatives"
    MODEL_INVOCATION_FAILED = "model_invocation_failed"
    CONTEXT_BUDGET_EXCEEDED = "context_budget_exceeded"
    FALLBACK_NOT_PERMITTED = "fallback_not_permitted"
    CLASSIFIER_DISABLED = "classifier_disabled"
    DECISION_CONTEXT_MISMATCH = "decision_context_mismatch"


@dataclass(frozen=True, slots=True)
class ClassificationResult:
    """Safe result: no prompt, model reasoning, credentials or authorization snapshot."""

    outcome: ClassificationOutcome
    reason: ClassificationReason
    initiative_id: str
    initiative_revision: int
    request_id: str
    correlation_id: str
    trace_id: str
    task_id: str | None = None
    model_invoked: bool = False
    confidence: float | None = None
    ambiguous: bool = False
    # Informational options for a clarification prompt; never executed or auto-selected.
    alternatives: tuple[Capability, ...] = ()
    # Present for CLASSIFIED/DENIED/CONFIGURATION_ERROR: the enforcement decision.
    routing: RoutingDecision | None = None

    def __post_init__(self) -> None:
        classified = self.outcome is ClassificationOutcome.CLASSIFIED
        if classified != (self.suggested_capability is not None):
            raise ValueError("a capability is present exactly when classified")
        if self.routing is not None and self.routing.rule is not RoutingRule.MODEL_SUGGESTED:
            raise ValueError("classifier routing decisions must be marked model_suggested")

    @property
    def suggested_capability(self) -> Capability | None:
        """Only set when authorized and available; origin is always ``MODEL_SUGGESTED``."""
        if self.routing is not None and self.routing.capability is not None:
            return self.routing.capability
        return None

    @property
    def origin(self) -> RoutingRule | None:
        return RoutingRule.MODEL_SUGGESTED if self.suggested_capability is not None else None
